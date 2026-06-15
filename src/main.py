"""
Real-Time Bidirectional Voice Translator for Video Calls (Windows).

Direction A (es -> en): Mic -> Gemini Live Translate -> VB-Cable (virtual mic)
Direction B (en -> es): System loopback -> Gemini Live Translate -> Headphones

Dependencies: google-genai, sounddevice, numpy, python-dotenv, pynput
Hardware setup: VB-Cable + Voicemeeter Banana (see docs/00-technical-analysis.md)

Usage:
    python main.py              # both directions (bidirectional)
    python main.py --direction A   # only es->en
    python main.py --direction B   # only en->es
    python main.py --list-devices  # show audio devices and exit
    python main.py --log-level DEBUG  # more verbose logging

Hotkeys (global, work from any window):
    Ctrl+Shift+T   — start / stop translation
    Ctrl+Shift+P   — pause / resume (WebSocket stays open)
"""

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from enum import Enum
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pynput import keyboard

from devices import ROLES, resolve_by_role, validate_samplerate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "gemini-3.5-live-translate-preview"
INPUT_SAMPLE_RATE = 16000          # Hz — what Gemini expects as input
OUTPUT_SAMPLE_RATE = 24000         # Hz — what Gemini returns as output
CHANNELS = 1
DTYPE = "int16"
INPUT_CHUNK_SAMPLES = 1600         # ~100ms at 16kHz
INPUT_CHUNK_BYTES = INPUT_CHUNK_SAMPLES * 2

HEALTH_CHECK_INTERVAL = 5.0         # seconds between health checks
HEALTH_CHECK_TIMEOUT = 15.0         # seconds without audio triggers reconnect
RECONNECT_MAX_DELAY = 30.0          # max backoff delay in seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(level: int = logging.INFO) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logger = logging.getLogger()
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_dir / "translator.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(ch)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class State(Enum):
    IDLE    = "IDLE"
    RUNNING = "RUNNING"
    PAUSED  = "PAUSED"
    STOPPED = "STOPPED"

class StateMachine:
    """Thread-safe state machine.  pynput calls from a background thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._loop = loop
        self._transition_time = time.time()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def set_state(self, new_state: State) -> None:
        """
        Called from the pynput thread (non-async).
        Validates transitions and notifies the asyncio event loop.
        """
        with self._lock:
            old = self._state
            valid = {
                State.IDLE:    {State.RUNNING},
                State.RUNNING: {State.PAUSED, State.STOPPED},
                State.PAUSED:  {State.RUNNING, State.STOPPED},
                State.STOPPED: {State.RUNNING},
            }
            if new_state not in valid.get(old, set()):
                log.debug("StateMachine: invalid transition %s -> %s (ignored)",
                          old.value, new_state.value)
                return
            self._state = new_state
            self._transition_time = time.time()

        log.info("State: %s -> %s", old.value, new_state.value)
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait_for_change(self) -> State:
        """Await this from the asyncio loop to block until a state change."""
        await self._event.wait()
        self._event.clear()
        return self.state

    def elapsed(self) -> float:
        return time.time() - self._transition_time

# ---------------------------------------------------------------------------
# Hotkey listener
# ---------------------------------------------------------------------------

def start_hotkey_listener(sm: StateMachine) -> keyboard.Listener:
    """
    Launch a global hotkey listener in a daemon thread.
    Ctrl+Shift+T  — toggle RUNNING / STOPPED
    Ctrl+Shift+P  — toggle PAUSE
    """
    current_keys: set = set()

    def _ctrl() -> bool:
        return keyboard.Key.ctrl_l in current_keys or keyboard.Key.ctrl_r in current_keys

    def _shift() -> bool:
        return keyboard.Key.shift in current_keys or keyboard.Key.shift_r in current_keys

    def on_press(key):
        current_keys.add(key)
        if not _ctrl() or not _shift():
            return
        try:
            if hasattr(key, 'char'):
                if key.char == 't':
                    cur = sm.state
                    if cur == State.IDLE or cur == State.STOPPED:
                        sm.set_state(State.RUNNING)
                    elif cur == State.RUNNING:
                        sm.set_state(State.STOPPED)
                    elif cur == State.PAUSED:
                        sm.set_state(State.STOPPED)
                elif key.char == 'p':
                    cur = sm.state
                    if cur == State.RUNNING:
                        sm.set_state(State.PAUSED)
                    elif cur == State.PAUSED:
                        sm.set_state(State.RUNNING)
        except Exception:
            pass

    def on_release(key):
        current_keys.discard(key)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    log.info("Hotkeys active: Ctrl+Shift+T = start/stop, Ctrl+Shift+P = pause/resume")
    return listener

# ---------------------------------------------------------------------------
# Audio device utilities (sounddevice-based)
# ---------------------------------------------------------------------------

class AudioDeviceManager:
    """Thin wrapper — delegates to src/devices.py for device listing."""

    @staticmethod
    def list_all() -> None:
        from devices import list_all_devices
        ds = list_all_devices()
        print("\n=== AVAILABLE AUDIO DEVICES ===\n")
        print(f"{'Idx':>3s}  {'Host API':<20s} {'Name':<55s} {'In':>4s} {'Out':>4s} {'Rate':>8s}")
        print("-" * 100)
        for d in ds:
            print(f"{d['index']:3d}  {d['host_api']:<20s} {d['name']:<55s} {d['max_input_channels']:4d} {d['max_output_channels']:4d} {d['default_samplerate']:8.0f}")
        print()

# ---------------------------------------------------------------------------
# Device recovery helper
# ---------------------------------------------------------------------------

def _recover_device(role_name: str, kind: str, desired_sr: int,
                    channels: int, blocksize: int | None) -> tuple:
    """Re-resolve a device role and validate sample rate after disconnect."""
    from devices import ROLES as DEV_ROLES, resolve_by_fingerprint, resolve_by_role, validate_samplerate, ResolvedDevice
    from config import load_fingerprints, save_fingerprint
    fps = load_fingerprints()
    rd = None
    if fps.get(role_name):
        rd = resolve_by_fingerprint(fps[role_name])
    if rd is None:
        rd = resolve_by_role(DEV_ROLES[role_name])
        save_fingerprint(role_name, rd.fingerprint)
    actual_sr = validate_samplerate(rd.index, desired_sr, channels, kind)
    return rd, actual_sr


# ---------------------------------------------------------------------------
# Audio capture — microphone or loopback
# ---------------------------------------------------------------------------

class AudioCapture:
    """
    Captures audio from an input device and pushes raw PCM bytes onto an
    asyncio.Queue.  Respects *pause_event* so capture can be suspended
    without closing the hardware stream.
    """

    def __init__(
        self,
        device_index: int,
        sample_rate: int = INPUT_SAMPLE_RATE,
        chunk_samples: int = INPUT_CHUNK_SAMPLES,
        name: str = "",
        pause_event: asyncio.Event | None = None,
        on_volume: "callable | None" = None,
        role_name: str = "",
        on_status: "callable | None" = None,
    ):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.chunk_samples = chunk_samples
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._stream: sd.InputStream | None = None
        self._running = False
        self._pause_event = pause_event or asyncio.Event()
        self._pause_event.set()  # running by default
        self._on_volume = on_volume  # (db: float) -> None
        self._role_name = role_name
        self._on_status = on_status  # (status: str) -> None
        self.MAX_LOCAL_RETRIES = 3

    async def start(self) -> None:
        # Validate sample rate; input at wrong rate breaks Gemini
        actual_sr = validate_samplerate(self.device_index, self.sample_rate,
                                        CHANNELS, "input")
        if actual_sr != self.sample_rate:
            raise RuntimeError(
                f"[Capture:{self.name}] Device {self.device_index} does not support "
                f"{self.sample_rate} Hz input (gemini requires exactly {INPUT_SAMPLE_RATE} Hz)"
            )
        self._stream = sd.InputStream(
            device=self.device_index,
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=self.chunk_samples,
        )
        self._stream.start()
        self._running = True
        log.info("[Capture:%s] Started on device %d @ %d Hz",
                 self.name, self.device_index, self.sample_rate)

    async def run(self) -> None:
        """Continuously read chunks into the queue.  Call after start()."""
        reconnect_attempts = 0
        backoff = 1.0
        try:
            while self._running:
                await self._pause_event.wait()  # block while PAUSED
                try:
                    data_np, overflowed = await asyncio.to_thread(
                        self._stream.read, self.chunk_samples
                    )
                    reconnect_attempts = 0
                    backoff = 1.0
                except sd.PortAudioError as e:
                    log.warning("[Capture:%s] Input device error: %s", self.name, e)
                    try: self._stream.stop(); self._stream.close()
                    except Exception: pass
                    if reconnect_attempts >= self.MAX_LOCAL_RETRIES:
                        log.error("[Capture:%s] Irrecuperable tras %d intentos", self.name, reconnect_attempts)
                        if self._on_status: self._on_status("FAILED")
                        raise
                    if self._on_status: self._on_status("RECONNECTING")
                    await asyncio.sleep(backoff)
                    try:
                        rd, actual_sr = _recover_device(self._role_name, "input",
                                                        self.sample_rate, CHANNELS, self.chunk_samples)
                        self._stream = sd.InputStream(
                            device=rd.index, samplerate=actual_sr, channels=CHANNELS,
                            dtype=DTYPE, blocksize=self.chunk_samples)
                        self._stream.start()
                        self.device_index = rd.index; self.sample_rate = actual_sr
                        log.info("[Capture:%s] Recovered -> [%d] %s @ %d Hz", self.name, rd.index, rd.name, actual_sr)
                        reconnect_attempts += 1; backoff *= 2
                        if self._on_status: self._on_status("RUNNING")
                        continue
                    except Exception as reopen_err:
                        log.error("[Capture:%s] Reopen failed: %s", self.name, reopen_err)
                        reconnect_attempts += 1; backoff *= 2
                        continue
                if overflowed:
                    log.warning("[Capture:%s] Input overflow", self.name)

                # Volume callback
                if self._on_volume:
                    try:
                        rms = float(np.sqrt(np.mean(data_np.astype(np.float64) ** 2)))
                        db = int(20.0 * np.log10((rms / 32768.0) + 1e-9))
                        self._on_volume(db)
                    except Exception:
                        pass

                data_bytes = data_np.tobytes()
                if self.queue.full():
                    try:
                        self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                await self.queue.put(data_bytes)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("[Capture:%s] Fatal error", self.name)
        finally:
            self._running = False

    def pause(self) -> None:
        self._pause_event.clear()
        log.debug("[Capture:%s] Paused", self.name)

    def resume(self) -> None:
        self._pause_event.set()
        log.debug("[Capture:%s] Resumed", self.name)

    def stop(self) -> None:
        self._running = False
        self._pause_event.set()  # unblock if waiting
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

# ---------------------------------------------------------------------------
# Audio playback — VB-Cable or headphones
# ---------------------------------------------------------------------------

class AudioPlayer:
    """
    Reads raw PCM bytes from an asyncio.Queue and plays them to the
    specified output device using sounddevice OutputStream.
    """

    def __init__(
        self,
        device_index: int,
        sample_rate: int = OUTPUT_SAMPLE_RATE,
        name: str = "",
        resample_fn: "callable[[bytes], bytes] | None" = None,
        role_name: str = "",
        on_status: "callable | None" = None,
    ):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._stream: sd.OutputStream | None = None
        self._running = False
        self._resample_fn = resample_fn
        self._role_name = role_name
        self._on_status = on_status
        self.MAX_LOCAL_RETRIES = 3

    async def start(self) -> None:
        actual_sr = validate_samplerate(self.device_index, self.sample_rate,
                                        CHANNELS, "output")
        if actual_sr != self.sample_rate:
            log.warning("[Player:%s] Using fallback rate %d Hz (requested %d Hz)",
                        self.name, actual_sr, self.sample_rate)
            self.sample_rate = actual_sr
        self._stream = sd.OutputStream(
            device=self.device_index,
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
        )
        self._stream.start()
        self._running = True
        log.info("[Player:%s] Started on device %d @ %d Hz",
                 self.name, self.device_index, self.sample_rate)

    async def run(self) -> None:
        """Continuously drain the queue and play audio."""
        reconnect_attempts = 0
        backoff = 1.0
        try:
            while self._running:
                try:
                    data_bytes = await self.queue.get()
                    if self._resample_fn:
                        data_bytes = self._resample_fn(data_bytes)
                    data_np = np.frombuffer(data_bytes, dtype=np.int16).copy()
                    underflowed = await asyncio.to_thread(self._stream.write, data_np)
                    reconnect_attempts = 0
                    backoff = 1.0
                except sd.PortAudioError as e:
                    log.warning("[Player:%s] Output device error: %s", self.name, e)
                    try: self._stream.stop(); self._stream.close()
                    except Exception: pass
                    if reconnect_attempts >= self.MAX_LOCAL_RETRIES:
                        log.error("[Player:%s] Irrecuperable tras %d intentos", self.name, reconnect_attempts)
                        if self._on_status: self._on_status("FAILED")
                        raise
                    if self._on_status: self._on_status("RECONNECTING")
                    await asyncio.sleep(backoff)
                    try:
                        rd, actual_sr = _recover_device(self._role_name, "output",
                                                        self.sample_rate, CHANNELS, None)
                        self._stream = sd.OutputStream(
                            device=rd.index, samplerate=actual_sr, channels=CHANNELS, dtype=DTYPE)
                        self._stream.start()
                        self.device_index = rd.index; self.sample_rate = actual_sr
                        log.info("[Player:%s] Recovered -> [%d] %s @ %d Hz", self.name, rd.index, rd.name, actual_sr)
                        reconnect_attempts += 1; backoff *= 2
                        if self._on_status: self._on_status("RUNNING")
                    except Exception as reopen_err:
                        log.error("[Player:%s] Reopen failed: %s", self.name, reopen_err)
                        reconnect_attempts += 1; backoff *= 2
                if underflowed:
                    log.debug("[Player:%s] Output underflow", self.name)
                self.queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("[Player:%s] Fatal error", self.name)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


# ---------------------------------------------------------------------------
# Audio resampling helpers
# ---------------------------------------------------------------------------

def resample_24k_to_48k(data_bytes: bytes) -> bytes:
    """Linear-interpolation upsampling from 24000 → 48000 Hz."""
    samples = np.frombuffer(data_bytes, dtype=np.int16).astype(np.float64)
    n = len(samples)
    resampled = np.interp(
        np.arange(0, n - 1, 0.5),
        np.arange(n),
        samples[:n]
    ).astype(np.int16)
    return resampled.tobytes()


# ---------------------------------------------------------------------------
# Gemini Live Translate session
# ---------------------------------------------------------------------------

class GeminiSession:
    """
    Wraps one Gemini Live Translate WebSocket session.

    One session = one translation direction (e.g. es->en).
    Bidirectional translation requires two independent instances.

    Includes health-check monitoring, pause support, and per-session
    byte counters for statistics.
    """

    def __init__(
        self,
        api_key: str,
        target_language: str,
        label: str = "",
        pause_event: asyncio.Event | None = None,
        on_transcription: "callable | None" = None,
        on_direction_status: "callable | None" = None,
    ):
        self.api_key = api_key
        self.target_language = target_language
        self.label = label or target_language
        self.client = genai.Client(api_key=api_key)
        self._on_transcription = on_transcription  # (role, text) -> None
        self._on_direction_status = on_direction_status  # (status: str) -> None
        self._max_retries = 10

        # Pause support
        self._pause_event = pause_event or asyncio.Event()
        self._pause_event.set()

        # Health monitoring
        self._last_audio_received: float = 0.0
        self._last_audio_sent: float = 0.0

        # Stats counters
        self.bytes_sent: int = 0
        self.bytes_received: int = 0
        self.reconnect_count: int = 0
        self.session_start: float | None = None

    def _build_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            translation_config=types.TranslationConfig(
                target_language_code=self.target_language,
                echo_target_language=True,
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    def _print_transcript(self, direction: str, lang: str, text: str) -> None:
        """Print a transcript line without corrupting the status bar."""
        sys.stdout.write(f"\r\033[K[{self.label}] {direction} [{lang}]: {text}\n")
        sys.stdout.flush()

    async def run(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """
        Connect to Gemini Live, then run send + receive + health-check
        loops concurrently.  Blocks until session ends or a fatal error.
        """
        config = self._build_config()
        self.session_start = time.time()
        self._last_audio_received = time.time()
        self._last_audio_sent = time.time()
        log.info("[%s] Connecting to Gemini Live (target=%s, model=%s)...",
                 self.label, self.target_language, MODEL)
        if self._on_direction_status:
            try: self._on_direction_status("CONNECTING")
            except Exception: pass

        try:
            async with self.client.aio.live.connect(model=MODEL, config=config) as session:
                log.info("[%s] Connected to Gemini Live", self.label)
                if self._on_direction_status:
                    try: self._on_direction_status("RUNNING")
                    except Exception: pass

                async def sender():
                    try:
                        while True:
                            await self._pause_event.wait()
                            chunk = await input_queue.get()
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=chunk,
                                    mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                                )
                            )
                            self.bytes_sent += len(chunk)
                            self._last_audio_sent = time.time()
                            input_queue.task_done()
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception("[%s] Sender error", self.label)
                        raise

                async def receiver():
                    try:
                        while True:
                            async for response in session.receive():
                                sc = response.server_content
                                if sc is None:
                                    continue

                                if sc.model_turn:
                                    for part in sc.model_turn.parts:
                                        if part.inline_data and isinstance(part.inline_data.data, bytes):
                                            self.bytes_received += len(part.inline_data.data)
                                            self._last_audio_received = time.time()
                                            if output_queue.full():
                                                try:
                                                    output_queue.get_nowait()
                                                except asyncio.QueueEmpty:
                                                    pass
                                            await output_queue.put(part.inline_data.data)

                                if sc.interrupted:
                                    log.debug("[%s] Turn interrupted, flushing output", self.label)
                                    while not output_queue.empty():
                                        try:
                                            output_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            break

                                if sc.input_transcription and sc.input_transcription.text:
                                    lang = sc.input_transcription.language_code or "?"
                                    text = sc.input_transcription.text
                                    self._print_transcript("SOURCE", lang, text)
                                    if self._on_transcription:
                                        try:
                                            self._on_transcription("in", text)
                                        except Exception:
                                            pass
                                if sc.output_transcription and sc.output_transcription.text:
                                    lang = sc.output_transcription.language_code or "?"
                                    text = sc.output_transcription.text
                                    self._print_transcript("TARGET", lang, text)
                                    if self._on_transcription:
                                        try:
                                            self._on_transcription("out", text)
                                        except Exception:
                                            pass

                            log.debug("[%s] Receive iterator ended; re-entering", self.label)

                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        log.exception("[%s] Receiver error", self.label)
                        raise

                async def health_check():
                    try:
                        while True:
                            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                            elapsed = time.time() - self._last_audio_received
                            if elapsed > HEALTH_CHECK_TIMEOUT:
                                log.warning("[%s] No audio received for %.0fs — forcing reconnect",
                                            self.label, elapsed)
                                raise ConnectionError(
                                    f"Health check: no audio for {elapsed:.0f}s"
                                )
                    except asyncio.CancelledError:
                        pass

                async with asyncio.TaskGroup() as tg:
                    tg.create_task(sender())
                    tg.create_task(receiver())
                    tg.create_task(health_check())

        except asyncio.CancelledError:
            log.info("[%s] Session cancelled", self.label)
            raise
        except Exception:
            log.exception("[%s] Session error", self.label)
            raise

    async def run_with_reconnect(
        self,
        input_queue: asyncio.Queue,
        output_queue: asyncio.Queue,
    ) -> None:
        """Run session with exponential-backoff reconnection on failure."""
        while True:
            try:
                await self.run(input_queue, output_queue)
                break  # clean exit (cancelled externally)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.reconnect_count += 1
                if self._on_direction_status:
                    try:
                        self._on_direction_status(
                            "FAILED" if self.reconnect_count >= self._max_retries
                            else "RECONNECTING"
                        )
                    except Exception: pass
                if self.reconnect_count >= self._max_retries:
                    log.error("[%s] Max retries (%d) reached — giving up",
                              self.label, self._max_retries)
                    raise
                delay = min(2.0 ** self.reconnect_count, RECONNECT_MAX_DELAY)
                log.warning("[%s] Reconnecting in %.1fs (attempt %d)...",
                            self.label, delay, self.reconnect_count)
                await asyncio.sleep(delay)

    def pause(self) -> None:
        self._pause_event.clear()
        log.debug("[%s] Paused", self.label)

    def resume(self) -> None:
        self._pause_event.set()
        self._last_audio_received = time.time()  # reset health timer
        log.debug("[%s] Resumed", self.label)

    def stats_summary(self) -> str:
        active_time = time.time() - self.session_start if self.session_start else 0
        mm, ss = divmod(int(active_time), 60)
        hh, mm = divmod(mm, 60)
        return (
            f"  [{self.label}]\n"
            f"    Active:          {hh:02d}:{mm:02d}:{ss:02d}\n"
            f"    Reconnections:   {self.reconnect_count}\n"
            f"    Audio sent:      ~{self.bytes_sent / 1e6:.1f} MB\n"
            f"    Audio received:  ~{self.bytes_received / 1e6:.1f} MB"
        )

# ---------------------------------------------------------------------------
# Translation pipeline — one full direction
# ---------------------------------------------------------------------------

class TranslationPipeline:
    """
    Orchestrates one direction: Capture -> Gemini -> Play.

    Exposes pause/resume that cascades to the capture and session.
    """

    def __init__(
        self,
        capture: AudioCapture,
        session: GeminiSession,
        player: AudioPlayer,
        label: str,
        pause_event: asyncio.Event,
        on_transcription: "callable | None" = None,
        on_volume: "callable | None" = None,
    ):
        self.capture = capture
        self.session = session
        self.player = player
        self.label = label
        self.pause_event = pause_event
        self.on_transcription = on_transcription
        self.on_volume = on_volume

    async def run(self) -> None:
        """Run the full pipeline concurrently. Blocks until cancelled or error."""
        await self.capture.start()
        await self.player.start()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.capture.run(), name=f"{self.label}-capture")
            tg.create_task(self.player.run(), name=f"{self.label}-playback")
            tg.create_task(
                self.session.run_with_reconnect(self.capture.queue, self.player.queue),
                name=f"{self.label}-session",
            )

    def pause(self) -> None:
        self.pause_event.clear()
        self.capture.pause()
        self.session.pause()

    def resume(self) -> None:
        self.pause_event.set()
        self.capture.resume()
        self.session.resume()

    def stop(self) -> None:
        self.pause_event.set()  # unblock
        self.capture.stop()
        self.player.stop()

# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status(state: State, elapsed: float, directions: str) -> None:
    """Print a one-line status bar (overwrites previous line with \\r)."""
    mm, ss = divmod(int(elapsed), 60)
    hh, mm = divmod(mm, 60)
    line = (
        f"\r\033[K[{state.value}] {hh:02d}:{mm:02d}:{ss:02d} | "
        f"Pipelines: [{directions}] | "
        f"Ctrl+Shift+T=toggle  Ctrl+Shift+P=pause"
    )
    sys.stdout.write(line)
    sys.stdout.flush()

# ---------------------------------------------------------------------------
# Pipeline engine — reusable by CLI and UI
# ---------------------------------------------------------------------------

async def run_engine(
    sm: StateMachine,
    api_key: str,
    callbacks: dict[str, "callable | None"] | None = None,
    mic_idx: int | None = None,
    vbcable_idx: int | None = None,
    loopback_idx: int | None = None,
    headphones_idx: int | None = None,
    show_status_bar: bool = True,
) -> None:
    """
    Run the full translation engine driven by *sm* (StateMachine).

    *callbacks* dict keys:
        on_t_A, on_v_A   — transcription / volume for direction A
        on_t_B, on_v_B   — transcription / volume for direction B

    Device indices are optional; if not provided, they are re-discovered
    from config each time the pipeline starts (so UI config changes work).
    Pass them explicitly in CLI mode for predictable behaviour.
    """
    cb = callbacks or {}

    # ---------- preflight + device resolution ----------
    from preflight import run_preflight
    from config import load_fingerprints, save_fingerprint

    fps = load_fingerprints()
    pf = run_preflight(api_key, fps)
    if not pf.ok:
        for err in pf.errors:
            log.error("Preflight: %s", err)
        raise RuntimeError(f"Preflight failed: {pf.errors}")

    resolved = pf.devices_resolved
    for role_name, rd in resolved.items():
        if rd is not None:
            save_fingerprint(role_name, rd.fingerprint)

    def _get_resolved(name: str) -> ResolvedDevice | None:
        from devices import ResolvedDevice
        return resolved.get(name)

    # ---------- helper: build a pipeline ----------
    def _build(
        tag: str,
        capture_idx: int,
        target_lang: str,
        player_idx: int,
        cap_name: str,
        plr_name: str,
        cap_role: str,
        plr_role: str,
        on_t: "callable | None" = None,
        on_v: "callable | None" = None,
        player_rate: int = OUTPUT_SAMPLE_RATE,
        player_resample: "callable[[bytes], bytes] | None" = None,
    ) -> TranslationPipeline:
        # Map role → direction for status callbacks
        dir_map = {"mic": "A", "virtual_mic_out": "A", "loopback_in": "B", "headphones_out": "B"}
        on_ds = cb.get("on_ds")
        cap_dir = dir_map.get(cap_role, "?")
        plr_dir = dir_map.get(plr_role, "?")

        def _cap_status(st): 
            if on_ds: on_ds(cap_dir, st)
        def _plr_status(st):
            if on_ds: on_ds(plr_dir, st)

        pause_evt = asyncio.Event()
        pause_evt.set()
        cap = AudioCapture(capture_idx, INPUT_SAMPLE_RATE, INPUT_CHUNK_SAMPLES,
                           name=cap_name, pause_event=pause_evt, on_volume=on_v,
                           role_name=cap_role, on_status=_cap_status)
        ses = GeminiSession(api_key=api_key, target_language=target_lang,
                            label=tag, pause_event=pause_evt, on_transcription=on_t,
                            on_direction_status=cb.get("on_ds"))
        plr = AudioPlayer(player_idx, player_rate, name=plr_name,
                           resample_fn=player_resample,
                           role_name=plr_role, on_status=_plr_status)
        return TranslationPipeline(cap, ses, plr, tag, pause_evt, on_t, on_v)

    # ---------- hotkeys ----------
    listener = start_hotkey_listener(sm)

    pipelines: dict[str, TranslationPipeline] = {}
    tasks: dict[str, asyncio.Task] = {}
    direction_start: float = 0.0
    status_task: asyncio.Task | None = None

    # ---------- status bar (CLI only) ----------
    async def cli_status_loop():
        while True:
            elapsed = time.time() - direction_start if direction_start else 0
            active = "" if sm.state in (State.IDLE, State.STOPPED) else "".join(pipelines.keys())
            print_status(sm.state, elapsed, active)
            await asyncio.sleep(0.5)

    if show_status_bar:
        status_task = asyncio.create_task(cli_status_loop())

    # ---------- pipeline lifecycle ----------
    async def start_pipelines():
        nonlocal direction_start
        mic_rd = _get_resolved("mic")
        vm_rd  = _get_resolved("virtual_mic_out")
        lb_rd  = _get_resolved("loopback_in")
        hp_rd  = _get_resolved("headphones_out")

        # Validate sample rates
        mic_sr = INPUT_SAMPLE_RATE
        if mic_rd:
            mic_sr = validate_samplerate(mic_rd.index, INPUT_SAMPLE_RATE, CHANNELS, "input")
        hp_sr = 48000  # headphone target (we resample 24k->48k)
        if hp_rd:
            hp_sr = validate_samplerate(hp_rd.index, 48000, CHANNELS, "output")

        if mic_rd is not None and vm_rd is not None:
            p = _build("A(es->en)", mic_rd.index, "en", vm_rd.index, "Mic", "VB-Cable",
                       cap_role="mic", plr_role="virtual_mic_out",
                       on_t=cb.get("on_t_A"), on_v=cb.get("on_v_A"))
            pipelines["A"] = p
            log.info("Direction A ready: %s[%d] -> Gemini(en) -> %s[%d] @%d Hz",
                     mic_rd.host_api, mic_rd.index, vm_rd.host_api, vm_rd.index, mic_sr)
        if lb_rd is not None and hp_rd is not None:
            need_resample = hp_sr != OUTPUT_SAMPLE_RATE
            p = _build("B(en->es)", lb_rd.index, "es", hp_rd.index, "Loopback", "Phones",
                       cap_role="loopback_in", plr_role="headphones_out",
                       on_t=cb.get("on_t_B"), on_v=cb.get("on_v_B"),
                       player_rate=hp_sr,
                       player_resample=resample_24k_to_48k if need_resample else None)
            pipelines["B"] = p
            log.info("Direction B ready: %s[%d] -> Gemini(es) -> %s[%d] @%d Hz%s",
                     lb_rd.host_api, lb_rd.index,
                     hp_rd.host_api, hp_rd.index, hp_sr,
                     " (resampled)" if need_resample else "")

        if not pipelines:
            log.error("No pipelines could be created.")
            sm.set_state(State.STOPPED)
            return

        direction_start = time.time()
        log.info("Launching %d pipeline(s)...", len(pipelines))
        for key, p in pipelines.items():
            tasks[key] = asyncio.create_task(p.run(), name=f"pipeline-{key}")

        # Heartbeat watchdog — pings the UI every 2s
        async def _heartbeat():
            hb = cb.get("on_heartbeat")
            while True:
                await asyncio.sleep(2.0)
                if hb:
                    try: hb()
                    except Exception: pass
        tasks["heartbeat"] = asyncio.create_task(_heartbeat(), name="heartbeat")

    async def stop_pipelines():
        log.info("Stopping pipelines...")
        for p in pipelines.values():
            p.stop()
        for key, t in list(tasks.items()):
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        tasks.clear()

        if show_status_bar:
            print()
            print("=== Session Statistics ===")
            total = 0.0
            if direction_start:
                total = time.time() - direction_start
                mm, ss = divmod(int(total), 60)
                hh, mm = divmod(mm, 60)
                print(f"  Total active:     {hh:02d}:{mm:02d}:{ss:02d}")
            for p in pipelines.values():
                print(p.session.stats_summary())
            print("==========================\n")

        pipelines.clear()

    # ---------- main event loop ----------
    try:
        if show_status_bar:
            print("\nReady. Press Ctrl+Shift+T to start.\n")

        prev_state = sm.state

        while True:
            new_state = await sm.wait_for_change()

            if new_state == State.RUNNING:
                if prev_state == State.PAUSED:
                    for p in pipelines.values():
                        p.resume()
                    log.info("Resumed — translation continuing")
                else:
                    if pipelines:
                        await stop_pipelines()
                    await start_pipelines()
                    if not pipelines:
                        sm.set_state(State.STOPPED)
                        prev_state = State.IDLE
                        continue

            elif new_state == State.PAUSED:
                for p in pipelines.values():
                    p.pause()
                log.info("Paused — WebSocket stays connected")

            elif new_state == State.STOPPED:
                if pipelines:
                    await stop_pipelines()
                sm.set_state(State.IDLE)
                if show_status_bar:
                    print("\nReady. Press Ctrl+Shift+T to start.\n")

            prev_state = new_state

    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        if status_task:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
        if pipelines:
            await stop_pipelines()

# ---------------------------------------------------------------------------
# Main application (CLI entry point)
# ---------------------------------------------------------------------------

async def async_main(args) -> None:
    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set. Create a .env file with your API key.")
        sys.exit(1)

    setup_logging(args.log_level)
    log.info("=== Voice Translator starting ===")

    if args.list_devices:
        AudioDeviceManager.list_all()
        return

    loop = asyncio.get_running_loop()
    sm = StateMachine(loop)

    await run_engine(
        sm=sm, api_key=api_key,
        show_status_bar=True,
    )

    print("\nGoodbye.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Real-Time Bidirectional Voice Translator for Video Calls (Windows)"
    )
    parser.add_argument(
        "--direction", choices=["A", "B", "both"], default="both",
        help="A=es->en, B=en->es, both=bidirectional (default: both)",
    )
    parser.add_argument("--mic-device", help="Name substring of the physical microphone")
    parser.add_argument("--vbcable-device", help="Name substring of the VB-Cable playback device")
    parser.add_argument("--loopback-device", help="Name substring of VoiceMeeter B1 loopback")
    parser.add_argument("--headphones-device", help="Name substring of headphones/speakers")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging verbosity (default: INFO). File log is always DEBUG.",
    )
    args = parser.parse_args()
    # Convert string to logging level
    args.log_level = getattr(logging, args.log_level)

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception:
        log.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Windows setup instructions
# ---------------------------------------------------------------------------
#
# 1. INSTALL VB-CABLE
#    Download from: https://vb-audio.com/Cable/
#    Run VBCABLE_Driver_Pack43.zip installer.  Reboot.
#    Verify: Sound Settings > Recording tab shows "CABLE Output"
#            Sound Settings > Playback tab shows "CABLE Input"
#
# 2. INSTALL VOICEMEETER BANANA
#    Download from: https://vb-audio.com/Voicemeeter/banana.htm
#    Install and reboot.  Open Voicemeeter Banana and keep it running.
#    Verify: Sound Settings shows "VoiceMeeter Input (VAIO)" and B1 outputs.
#
# 3. CONFIGURE WINDOWS DEFAULT DEVICES
#    Right-click speaker icon > Sounds
#    Playback tab:  set "VoiceMeeter Input (VAIO)" as Default Device
#    Recording tab: set "VoiceMeeter Output (B1)"  as Default Device
#    Recording tab: set "CABLE Output" as Default Communications Device
#
# 4. CONFIGURE VOICEMEETER BANANA ROUTING
#    In Voicemeeter Banana:
#      - Do NOT route your physical mic to B1 (B1 should only carry system audio)
#      - Route VAIO (Virtual Input) -> A1 (headphones)  AND  B1 (loopback capture)
#      - Route your physical mic -> A1 (headphones, low volume for monitoring)
#    This prevents feedback loops (see docs/00-technical-analysis.md).
#
# 5. INSTALL PYTHON DEPENDENCIES
#    pip install -r requirements.txt
#
# 6. SET API KEY
#    Create src/.env with:  GEMINI_API_KEY=your_actual_key_here
#
# 7. VERIFY DEVICES
#    python main.py --list-devices
#
# 8. TEST DIRECTION A (es -> en)
#    python main.py --direction A
#    Speak in Spanish -> check that VB-Cable receives English:
#      Sound Settings > Recording > CABLE Output > Properties > Listen > Listen to this device
#
# 9. TEST BIDIRECTIONAL
#    python main.py
#    Play an English video on YouTube -> you should hear Spanish translation in headphones
#    Speak in Spanish -> videocall app (using CABLE Output as mic) sends English
#
# 10. VIDEO CALL SETUP
#     In Zoom/Meet/Teams audio settings:
#       Speaker:  Default (VoiceMeeter VAIO -> headphones)
#       Mic:      CABLE Output (VB-Cable)
