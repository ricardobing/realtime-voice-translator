"""
VoiceTranslator GUI — tkinter overlay for the real-time voice translator.

Provides volume meters, transcription feed, and Start/Stop/Pause buttons.
The audio engine (main.py) runs in a background asyncio thread.

Usage:
    python ui.py
"""

import asyncio
import os
import sys
import threading
import time

from dotenv import load_dotenv
import logging

# Load env early so GEMINI_API_KEY is available
load_dotenv()

import tkinter as tk
from tkinter import font as tkfont

# Import engine components from main.py
from main import (
    State,
    StateMachine,
    run_engine,
    AudioDeviceManager,
    MIC_DEVICE_NAME,
    VBCABLE_DEVICE_NAME,
    LOOPBACK_DEVICE_NAME,
    HEADPHONES_DEVICE_NAME,
    setup_logging,
    log,
)

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

BG       = "#0f0f1a"
SURFACE  = "#1a1a2e"
TEXT     = "#e0e0e0"
ACCENT   = "#00ff88"
PAUSED   = "#ffcc00"
ERROR    = "#ff4444"
VOLBAR   = "#00ccff"
IDLE_LED = "#555555"
MUTED    = "#888888"

WINDOW_W = 400
WINDOW_H = 380
VOL_BAR_W = 200
VOL_BAR_H = 12

# ---------------------------------------------------------------------------
# Pulse animation helper
# ---------------------------------------------------------------------------

class PulseAnimator:
    """Drives a smooth LED pulse using tkinter .after()."""

    def __init__(self, widget: tk.Widget, canvas: tk.Canvas, led_id: int):
        self.widget = widget
        self.canvas = canvas
        self.led_id = led_id
        self._running = False
        self._phase = 0.0

    def start(self):
        if not self._running:
            self._running = True
            self._tick()

    def stop(self):
        self._running = False

    def _tick(self):
        if not self._running:
            return
        self._phase = (self._phase + 0.08) % (2 * 3.14159)
        # Pulse: green -> dim green -> green
        v = int(128 + 127 * (1 + __import__("math").sin(self._phase)) / 2)
        color = f"#{0:02x}{v:02x}{0:02x}"
        self.canvas.itemconfig(self.led_id, fill=color)
        self.widget.after(40, self._tick)

# ---------------------------------------------------------------------------
# Main UI class
# ---------------------------------------------------------------------------

class TranslatorUI:
    def __init__(self):
        # ---------- asyncio engine thread ----------
        self.loop = asyncio.new_event_loop()
        self._engine_task: asyncio.Task | None = None
        self._engine_started = False
        self._async_thread = threading.Thread(
            target=self.loop.run_forever, daemon=True
        )

        # ---------- state machine (created on the asyncio thread later) ----------
        self.state_machine: StateMachine | None = None
        self._state = State.IDLE

        # ---------- device indices ----------
        self.mic_idx: int | None = None
        self.vbcable_idx: int | None = None
        self.loopback_idx: int | None = None
        self.headphones_idx: int | None = None

        # ---------- volume / transcription tracking ----------
        self._vol_A: int = -60
        self._vol_B: int = -60
        self._transcripts: dict[str, dict[str, str]] = {
            "A": {"in": "", "out": ""},
            "B": {"in": "", "out": ""},
        }
        self._direction_start: float = 0.0

        # ---------- build the window ----------
        self._discover_devices()
        self._build_ui()
        self._start_engine_thread()

    # ------------------------------------------------------------------
    # Device discovery (runs before tkinter to catch errors early)
    # ------------------------------------------------------------------

    def _discover_devices(self):
        try:
            self.mic_idx = AudioDeviceManager.find_input(MIC_DEVICE_NAME)
            self.vbcable_idx = AudioDeviceManager.find_output(VBCABLE_DEVICE_NAME)
            log.info("Direction A devices found: Mic[%d], VB-Cable[%d]",
                     self.mic_idx, self.vbcable_idx)
        except SystemExit:
            log.warning("Direction A devices not fully available")
            self.mic_idx = None
            self.vbcable_idx = None

        try:
            self.loopback_idx = AudioDeviceManager.find_input(LOOPBACK_DEVICE_NAME)
            self.headphones_idx = AudioDeviceManager.find_output(HEADPHONES_DEVICE_NAME)
            log.info("Direction B devices found: B1[%d], Phones[%d]",
                     self.loopback_idx, self.headphones_idx)
        except SystemExit:
            log.warning("Direction B devices not fully available")
            self.loopback_idx = None
            self.headphones_idx = None

    # ------------------------------------------------------------------
    # Thread-safe callbacks (called from asyncio thread)
    # ------------------------------------------------------------------

    def _cb_volume_A(self, db: float):
        self.root.after(0, lambda: self._update_volume("A", db))

    def _cb_volume_B(self, db: float):
        self.root.after(0, lambda: self._update_volume("B", db))

    def _cb_transcription_A(self, role: str, text: str):
        self.root.after(0, lambda: self._update_transcription("A", role, text))

    def _cb_transcription_B(self, role: str, text: str):
        self.root.after(0, lambda: self._update_transcription("B", role, text))

    def _update_volume(self, direction: str, db: float):
        if direction == "A":
            self._vol_A = int(db)
        else:
            self._vol_B = int(db)
        self._redraw_volume("A")
        self._redraw_volume("B")

    def _update_transcription(self, direction: str, role: str, text: str):
        self._transcripts[direction][role] = text[:80]
        self._redraw_transcripts()

    # ------------------------------------------------------------------
    # Start / stop engine
    # ------------------------------------------------------------------

    def _start_engine_thread(self):
        self._async_thread.start()
        # Create state machine on the asyncio thread
        future = asyncio.run_coroutine_threadsafe(self._init_state_machine(), self.loop)
        future.result(timeout=5)

    async def _init_state_machine(self):
        self.state_machine = StateMachine(self.loop)

    def _schedule_engine(self):
        """Launch run_engine on the background asyncio loop."""
        if self.state_machine is None:
            return
        api_key = os.getenv("GEMINI_API_KEY")

        async def _run():
            await run_engine(
                sm=self.state_machine,
                api_key=api_key,
                mic_idx=self.mic_idx,
                vbcable_idx=self.vbcable_idx,
                loopback_idx=self.loopback_idx,
                headphones_idx=self.headphones_idx,
                show_status_bar=False,
                callbacks={
                    "on_t_A": self._cb_transcription_A,
                    "on_v_A": self._cb_volume_A,
                    "on_t_B": self._cb_transcription_B,
                    "on_v_B": self._cb_volume_B,
                },
            )

        self._engine_task = asyncio.run_coroutine_threadsafe(_run(), self.loop)

    # ------------------------------------------------------------------
    # Button actions (called from tkinter thread)
    # ------------------------------------------------------------------

    def _on_toggle(self):
        if self._state in (State.IDLE, State.STOPPED):
            if not self._engine_started:
                self._engine_started = True
                self._schedule_engine()
            self.state_machine.set_state(State.RUNNING)
        elif self._state == State.RUNNING:
            self.state_machine.set_state(State.STOPPED)
        elif self._state == State.PAUSED:
            self.state_machine.set_state(State.STOPPED)

    def _on_pause(self):
        if self._state == State.RUNNING:
            self.state_machine.set_state(State.PAUSED)
        elif self._state == State.PAUSED:
            self.state_machine.set_state(State.RUNNING)

    # ------------------------------------------------------------------
    # UI refresh loop (runs on tkinter thread every 100ms)
    # ------------------------------------------------------------------

    def _refresh(self):
        if self.state_machine is not None:
            self._state = self.state_machine.state

        # LED
        led_colors = {
            State.IDLE: IDLE_LED,
            State.RUNNING: ACCENT,
            State.PAUSED: PAUSED,
            State.STOPPED: ERROR,
        }
        color = led_colors.get(self._state, IDLE_LED)

        if self._state == State.RUNNING:
            if not self._pulse._running:
                self._pulse.start()
            self.canvas.itemconfig(self._led_id, fill="")  # pulse handles it
        else:
            self._pulse.stop()
            self.canvas.itemconfig(self._led_id, fill=color)

        # Buttons
        if self._state == State.RUNNING:
            self._btn_toggle.config(text="\u23f9 STOP", fg=ERROR, state=tk.NORMAL)
            self._btn_pause.config(text="\u23f8 PAUSE", fg=PAUSED, state=tk.NORMAL)
        elif self._state == State.PAUSED:
            self._btn_toggle.config(text="\u23f9 STOP", fg=ERROR, state=tk.NORMAL)
            self._btn_pause.config(text="\u25b6 RESUME", fg=ACCENT, state=tk.NORMAL)
        elif self._state in (State.IDLE, State.STOPPED):
            self._btn_toggle.config(text="\u25b6 START", fg=ACCENT, state=tk.NORMAL)
            self._btn_pause.config(text="\u23f8 PAUSE", fg=MUTED, state=tk.DISABLED)
            # Reset volume and transcriptions on stop
            self._vol_A = -60
            self._vol_B = -60
            self._transcripts = {
                "A": {"in": "", "out": ""},
                "B": {"in": "", "out": ""},
            }
            self._direction_start = 0.0

        # Timer
        if self._state == State.RUNNING and self._direction_start == 0.0:
            self._direction_start = time.time()
        if self._state == State.RUNNING:
            self._update_timer()
        elif self._state in (State.IDLE, State.STOPPED):
            self._timer_label.config(text="00:00:00")

        # Volume bars
        self._redraw_volume("A")
        self._redraw_volume("B")

        # Transcripts
        self._redraw_transcripts()

        # Schedule next refresh
        self.root.after(100, self._refresh)

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _update_timer(self):
        if self._direction_start:
            elapsed = int(time.time() - self._direction_start)
            hh, rem = divmod(elapsed, 3600)
            mm, ss = divmod(rem, 60)
            self._timer_label.config(text=f"{hh:02d}:{mm:02d}:{ss:02d}")
        self.root.after(1000, self._update_timer)

    # ------------------------------------------------------------------
    # Volume bar rendering
    # ------------------------------------------------------------------

    def _redraw_volume(self, direction: str):
        canvas = self._vol_canvas_A if direction == "A" else self._vol_canvas_B
        label = self._vol_label_A if direction == "A" else self._vol_label_B
        db = self._vol_A if direction == "A" else self._vol_B

        # Map dB (-60 .. 0) to bar width (0 .. VOL_BAR_W)
        clamped = max(-60, min(0, db))
        fraction = (clamped + 60) / 60.0
        w = int(fraction * VOL_BAR_W)

        canvas.delete("bar")
        if w > 0:
            # Gradient: clipped at w
            canvas.create_rectangle(0, 0, w, VOL_BAR_H, fill=VOLBAR, outline="", tags="bar")
        label.config(text=f"{db} dB" if db > -60 else "-∞ dB")

    # ------------------------------------------------------------------
    # Transcription rendering
    # ------------------------------------------------------------------

    def _redraw_transcripts(self):
        for d in ("A", "B"):
            t_in = self._transcripts[d]["in"]
            t_out = self._transcripts[d]["out"]
            # Truncate to ~55 chars to fit window
            if len(t_in) > 55:
                t_in = t_in[:52] + "..."
            if len(t_out) > 55:
                t_out = t_out[:52] + "..."
            label_in = self._label_in_A if d == "A" else self._label_in_B
            label_out = self._label_out_A if d == "A" else self._label_out_B
            label_in.config(text=t_in if t_in else "\u2014")
            label_out.config(text=t_out if t_out else "\u2014")

    # ------------------------------------------------------------------
    # Window builder
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("VoiceTranslator")
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)

        # Position: bottom-right corner
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = sw - WINDOW_W - 20
        y = sh - WINDOW_H - 60
        self.root.geometry(f"+{x}+{y}")

        # Prevent tkinter from stealing focus on start (Windows)
        try:
            self.root.attributes("-alpha", 0.99)
            self.root.after(100, lambda: self.root.attributes("-alpha", 1.0))
        except Exception:
            pass

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Fonts
        title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        small_font = tkfont.Font(family="Segoe UI", size=8)

        # ================ HEADER ================
        header = tk.Frame(self.root, bg=BG, height=36)
        header.pack(fill=tk.X, padx=8, pady=(6, 0))
        header.pack_propagate(False)

        # LED canvas
        self.canvas = tk.Canvas(header, width=16, height=16, bg=BG, highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, padx=(2, 6))
        self._led_id = self.canvas.create_oval(2, 2, 14, 14, fill=IDLE_LED, outline="")
        self._pulse = PulseAnimator(self.root, self.canvas, self._led_id)

        # State label
        self._state_label = tk.Label(header, text="IDLE", fg=TEXT, bg=BG, font=title_font, width=8, anchor="w")
        self._state_label.pack(side=tk.LEFT)

        # Timer
        self._timer_label = tk.Label(header, text="00:00:00", fg=TEXT, bg=BG, font=title_font)
        self._timer_label.pack(side=tk.LEFT, expand=True)

        # Close button
        btn_close = tk.Label(header, text="\u2715", fg=MUTED, bg=BG, font=title_font, cursor="hand2")
        btn_close.pack(side=tk.RIGHT, padx=(0, 2))
        btn_close.bind("<Button-1>", lambda e: self._on_close())

        # ================ BUTTONS ================
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill=tk.X, padx=8, pady=6)

        self._btn_toggle = tk.Label(
            btn_frame, text="\u25b6 START", fg=ACCENT, bg=SURFACE,
            font=title_font, padx=20, pady=4, cursor="hand2",
        )
        self._btn_toggle.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_toggle.bind("<Button-1>", lambda e: self._on_toggle())

        self._btn_pause = tk.Label(
            btn_frame, text="\u23f8 PAUSE", fg=MUTED, bg=SURFACE,
            font=title_font, padx=20, pady=4, cursor="hand2",
        )
        self._btn_pause.pack(side=tk.LEFT)
        self._btn_pause.bind("<Button-1>", lambda e: self._on_pause())

        # ================ DIRECTION A ================
        self._build_direction("A", "Mic  (es -> en)", True)

        # ================ DIRECTION B ================
        self._build_direction("B", "Sys  (en -> es)", False)

    def _build_direction(self, tag: str, label_text: str, is_a: bool):
        """Create a section (volume bar + transcriptions) for one direction."""
        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill=tk.X, padx=8, pady=2)

        # Label
        lbl = tk.Label(
            frame, text=label_text, fg=TEXT, bg=BG,
            font=tkfont.Font(family="Segoe UI", size=8, weight="bold"),
            anchor="w",
        )
        lbl.pack(fill=tk.X)

        # Volume row: canvas + dB label
        vol_row = tk.Frame(frame, bg=BG)
        vol_row.pack(fill=tk.X, pady=(1, 1))

        cv = tk.Canvas(vol_row, width=VOL_BAR_W, height=VOL_BAR_H, bg=SURFACE, highlightthickness=0)
        cv.pack(side=tk.LEFT, padx=(0, 6))
        # Background outline
        cv.create_rectangle(0, 0, VOL_BAR_W, VOL_BAR_H, outline="#333", width=1)

        db_label = tk.Label(vol_row, text="-60 dB", fg=TEXT, bg=BG,
                            font=tkfont.Font(family="Consolas", size=8), width=7, anchor="w")
        db_label.pack(side=tk.LEFT)

        # Transcription IN
        in_label = tk.Label(
            frame, text="\u2014", fg=MUTED, bg=BG,
            font=tkfont.Font(family="Segoe UI", size=8), anchor="w",
        )
        in_label.pack(fill=tk.X)

        # Transcription OUT
        out_label = tk.Label(
            frame, text="\u2014", fg=ACCENT, bg=BG,
            font=tkfont.Font(family="Segoe UI", size=8), anchor="w",
        )
        out_label.pack(fill=tk.X)

        # Store references
        if is_a:
            self._vol_canvas_A = cv
            self._vol_label_A = db_label
            self._label_in_A = in_label
            self._label_out_A = out_label
        else:
            self._vol_canvas_B = cv
            self._vol_label_B = db_label
            self._label_in_B = in_label
            self._label_out_B = out_label

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self):
        if self.state_machine is not None:
            self.state_machine.set_state(State.STOPPED)
        self._pulse.stop()
        self.root.after(200, self._destroy)

    def _destroy(self):
        self.root.destroy()
        # Stop the asyncio loop
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._async_thread.join(timeout=3)
        sys.exit(0)

    def run(self):
        setup_logging(logging.INFO)
        self._refresh()
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = TranslatorUI()
    app.run()
