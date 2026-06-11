# Real-Time Voice Translator for Video Calls

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows)](https://github.com/ricardobing/realtime-voice-translator)
[![Gemini](https://img.shields.io/badge/powered_by-Gemini_3.5_Live_Translate-4285F4?logo=google)](https://ai.google.dev)
[![Status](https://img.shields.io/badge/status-active-brightgreen)](https://github.com/ricardobing/realtime-voice-translator)

**Speak your native language during video calls. The other person hears their language. You hear theirs. No one installs anything extra.**

<!-- Add demo GIF here -->
<!-- `![Demo](docs/demo.gif)` -->
> *Speak Spanish → Your interviewer hears English. They speak English → You hear Spanish. Zero friction.*

---

## The problem it solves

You have a job interview in English, a remote meeting with an international team, or a client call across languages. Your spoken English isn't fluent enough to feel confident. You don't want to type, use a chatbot, or make the other person wait while you translate. You just want to talk naturally in your own language and have the conversation flow.

This app sits between your microphone and your video call app, translating your speech in real time and injecting the translated audio as a virtual microphone. In the other direction, it captures the system audio (what the other person is saying), translates it, and plays it back through your headphones. The other person hears you in their language and never knows you're using a translator.

---

## How it works

```
 YOUR VOICE                          THEIR VOICE
 (Spanish)                           (English)
     │                                    │
     ▼                                    ▼
┌──────────┐                      ┌──────────────┐
│ Physical │                      │  System      │
│   Mic    │                      │  Audio       │
└────┬─────┘                      │ (Voicemeeter)│
     │                            └──────┬───────┘
     │                                   │
     ▼                                   ▼
┌──────────┐                      ┌──────────────┐
│ Gemini   │   ◄──WebSocket──►    │  Gemini      │
│ 3.5 Live │                      │  3.5 Live    │
│ es → en  │                      │  en → es     │
└────┬─────┘                      └──────┬───────┘
     │                                   │
     ▼                                   ▼
┌──────────┐                      ┌──────────────┐
│ VB-Cable │                      │  Auriculares │
│ (virtual │                      │  (físicos)   │
│   mic)   │                      │              │
└────┬─────┘                      └──────┬───────┘
     │                                   │
     ▼                                   ▼
┌──────────┐                      ┌──────────────┐
│  Video   │                      │   YOU        │
│  Call    │                      │   HEAR       │
│  App     │                      │   Spanish    │
└──────────┘                      └──────────────┘
```

Two independent Gemini Live Translate WebSocket sessions run in parallel. Your microphone audio (16 kHz PCM) goes to session A, which returns translated English audio at 24 kHz injected into VB-Cable, a free virtual audio cable that video call apps see as a microphone. System audio from the video call is captured through Voicemeeter Banana's B1 bus into session B, which returns translated Spanish audio played through your headphones. Voicemeeter's routing ensures the translation output never feeds back into the system loopback, preventing infinite echo.

---

## Features

- **Bidirectional real-time translation** — sub-second latency end to end
- **70+ languages** — powered by Gemini 3.5 Live Translate
- **Works with any video call app** — Zoom, Google Meet, Microsoft Teams, WhatsApp Desktop, Discord
- **The other person installs nothing** — your translated voice arrives through a virtual microphone
- **Desktop GUI** — volume meters, transcriptions, one-click start/stop
- **CLI mode** — for headless use, scripting, or remote sessions
- **Global hotkeys** — `Ctrl+Shift+T` toggles translation, `Ctrl+Shift+P` pauses without disconnecting
- **Automatic reconnection** — exponential backoff if the WebSocket drops
- **Live transcriptions** — see what was said and what was translated, on screen
- **Volume indicators** — real-time RMS meters for both directions
- **Session statistics** — bytes sent/received, reconnection count, uptime
- **File logging** — debug-level logs to `logs/translator.log`

---

## Tech stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Audio I/O | sounddevice + numpy | Bundled PortAudio, no C build step, WASAPI support |
| Translation API | Google Gemini 3.5 Live Translate | WebSocket streaming, sub-second latency, natural TTS voice |
| UI | tkinter | Python stdlib, zero extra install |
| Concurrency | asyncio | Manages two WebSocket sessions + audio streams in parallel |
| Virtual microphone | VB-Cable | Free Windows driver, bridges playback → recording |
| Audio routing | Voicemeeter Banana | Free mixer, isolates system audio for loopback without feedback |
| Global hotkeys | pynput | Cross-thread safe, works while any app has focus |
| Configuration | python-dotenv | API key from `.env`, no config files in the repo |

---

## Prerequisites

### Software (install manually before `pip install`)

| Tool | Download | Why |
|------|----------|-----|
| **Python 3.10+** | [python.org](https://www.python.org/downloads/) | Runtime |
| **VB-Cable** | [vb-audio.com/Cable](https://vb-audio.com/Cable/) | Virtual audio device that bridges your translated voice into the video call |
| **Voicemeeter Banana** | [vb-audio.com/Voicemeeter/banana.htm](https://vb-audio.com/Voicemeeter/banana.htm) | Audio mixer that isolates system audio for translation without feedback loops |
| **Gemini API key** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier available; Live Translate has separate quota from text models |

VB-Cable and Voicemeeter both require a **reboot** after installation.

---

## Installation

```bash
git clone https://github.com/ricardobing/realtime-voice-translator.git
cd realtime-voice-translator
pip install -r src/requirements.txt
cp src/.env.example src/.env
```

Edit `src/.env` and add your Gemini API key:

```
GEMINI_API_KEY=your_actual_key_here
```

---

## Windows Audio Setup

This is the part that trips up most people. Follow each step in order.

### 8.1 VB-Cable

After installing VB-Cable and rebooting, right-click the speaker icon → **Sounds**. You should see:

- **Playback tab:** `CABLE Input` — this is where the translator writes translated audio
- **Recording tab:** `CABLE Output` — this is the virtual microphone your video call app reads

If either is missing, reinstall VB-Cable.

### 8.2 Voicemeeter Banana routing

Open **Voicemeeter Banana** (Start Menu). It must stay running in the background while you use the translator.

The routing matrix at the bottom of the window has columns A1, A2, A3, B1, B2, B3. You only need **A1** (headphones) and **B1** (our loopback capture bus).

Set these exactly:

| Channel strip | A1 | B1 |
|--------------|:--:|:--:|
| **VAIO** (Virtual Inputs section, top-left) | ON | ON |
| **Hardware Input 1** (your physical microphone) | ON | **OFF** |
| All other Hardware Inputs | OFF | **OFF** |

Why B1 off on the mic: if your mic leaks into B1, your translated output could feed back into the system loopback, creating an echo. The VAIO strip captures only the audio your video call app plays — not your microphone.

Click the **A1** dropdown in the top-right and select your hardware headphones or speakers.

### 8.3 Windows default devices

Right-click speaker icon → **Sounds**:

- **Playback tab:** Right-click `VoiceMeeter Input (VAIO)` → **Set as Default Device**
- **Recording tab:** Right-click `VoiceMeeter Out B1` → **Set as Default Device**

### 8.4 Video call app

In Zoom, Meet, Teams, or any other app:

| Setting | Value |
|---------|-------|
| **Speaker** | Default (which is VAIO, routed to your headphones) |
| **Microphone** | `CABLE Output (VB-Cable)` |

---

## Usage

### Verify your setup

```bash
# List every audio device on your system
python src/main.py --list-devices

# Run the automated routing diagnostic
python src/check_voicemeeter.py
```

`check_voicemeeter.py` plays a test tone through VAIO, captures from B1, and verifies the mic doesn't leak. Fix any failures it reports before continuing.

### Launch the GUI

```bash
python src/ui.py
```

A 400x380 dark-themed window opens in the bottom-right corner, always on top. Click **START** or press `Ctrl+Shift+T`. The LED pulses green while translating.

### Launch the CLI

```bash
python src/main.py                      # both directions (default)
python src/main.py --direction A        # only your voice: Spanish → English
python src/main.py --direction B        # only their voice: English → Spanish
python src/main.py --log-level DEBUG    # verbose console + file logging
```

The CLI prints transcriptions to the terminal with a status bar showing elapsed time and direction status.

### Hotkeys

Works globally, from any window:

| Hotkey | Action | Description |
|--------|--------|-------------|
| `Ctrl+Shift+T` | Start / Stop | Toggles the translation engine |
| `Ctrl+Shift+P` | Pause / Resume | Suspends audio capture without disconnecting the Gemini session |

---

## Architecture

### Dual WebSocket sessions

Each translation direction uses an independent Gemini Live Translate session connected over WebSocket. Session A targets `en` (English), session B targets `es` (Spanish). Both sessions run simultaneously inside the same `asyncio` event loop via `asyncio.TaskGroup`.

### Thread model

```
┌──────────────────┐     ┌──────────────────────────┐
│  Main thread     │     │  Background asyncio thread │
│  (tkinter GUI)   │     │  (audio + Gemini sessions) │
│                  │     │                            │
│  .mainloop()     │     │  run_engine() coroutine    │
│  _refresh()      │     │    ├─ Pipeline A           │
│  .after(0, ...)  │◄────│    │   ├─ AudioCapture     │
│                  │     │    │   ├─ GeminiSession    │
│  on_toggle() ────┼────►│    │   └─ AudioPlayer      │
│  on_pause()      │     │    └─ Pipeline B           │
│                  │     │        ├─ AudioCapture     │
│                  │     │        ├─ GeminiSession    │
│                  │     │        └─ AudioPlayer      │
└──────────────────┘     └──────────────────────────┘
```

- **GUI → engine:** StateMachine transitions via `call_soon_threadsafe`
- **Engine → GUI:** Volume and transcription callbacks via `root.after(0, lambda)`

### Anti-loop design

The feedback loop is prevented by Voicemeeter's bus isolation:

1. Direction A output goes to VB-Cable (virtual mic) — never played through speakers
2. Direction B output goes directly to the hardware headphones (A1) — never enters VAIO
3. The B1 loopback bus carries only audio from VAIO (system/app audio), not from A1
4. Therefore B1 never captures the translator's own output

### Reconnection

If a WebSocket session drops, `run_with_reconnect` applies exponential backoff: 2s → 4s → 8s → 16s → 30s (capped). A health-check task monitors that audio is received within 15 seconds; if not, it forces a reconnection.

### Class diagram

```
TranslatorUI
    │
    ├── StateMachine (thread-safe, threading.Lock)
    │
    └── run_engine()
            │
            ├── TranslationPipeline "A"
            │       ├── AudioCapture  (mic → asyncio.Queue)
            │       ├── GeminiSession (WebSocket, target=en)
            │       └── AudioPlayer   (asyncio.Queue → VB-Cable)
            │
            └── TranslationPipeline "B"
                    ├── AudioCapture  (B1 → asyncio.Queue)
                    ├── GeminiSession (WebSocket, target=es)
                    └── AudioPlayer   (asyncio.Queue → headphones)
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| VoiceMeeter devices missing from `--list-devices` | Voicemeeter Banana not running | Open Voicemeeter Banana, keep it open, re-run |
| VB-Cable not showing up | Driver not installed or needs reboot | Install from vb-audio.com/Cable and reboot |
| Direction A connects but no audio in VB-Cable | Wrong mic selected | Run `--list-devices`, find your mic name, pass `--mic-device "your name"` |
| Direction B doesn't translate (hear English) | B1 routing not configured | Run `check_voicemeeter.py`, enable B1 on VAIO in Voicemeeter |
| Echo / feedback loop | Microphone routed to B1 | In Voicemeeter: Hardware Input 1 must have B1 OFF |
| `429 RESOURCE_EXHAUSTED` | Free tier API quota exhausted | Wait for daily reset, or enable billing in Google AI Studio |
| Latency > 2 seconds | Network or server load | Try reducing `INPUT_CHUNK_SAMPLES` to 800 for 50ms chunks (may affect stability) |
| `ImportError: no module named 'google.genai'` | Missing dependency | `pip install -r src/requirements.txt` |

---

## Roadmap

- [ ] Language selector in GUI (currently hardcoded es↔en)
- [ ] macOS support (BlackHole virtual device instead of VB-Cable)
- [ ] Transcription-only mode (no audio output, lighter API usage)
- [ ] Persistent device profile (remember preferred mic/speaker between sessions)
- [ ] One-click installer for VB-Cable + Voicemeeter
- [ ] System tray minimize

---

## Contributing

PRs are welcome, especially for:

- **macOS / Linux support** — the audio routing layer is modular
- **New language pairs** — change the `target_language_code` in the config
- **Alternative speech APIs** — the session wrapper is API-agnostic

Open an issue before starting on a large feature.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [Google Gemini Live API](https://ai.google.dev/gemini-api/docs/live) and the [gemini-live-api-examples](https://github.com/google-gemini/gemini-live-api-examples) repository
- [VB-Audio](https://vb-audio.com/) for VB-Cable and Voicemeeter, the free tools that make Windows audio routing possible

<!-- keywords: gemini live api, real-time voice translation, python voice translator,
     video call translator, speech translation, bidirectional translator,
     zoom translator, google meet translator, vb-cable python, voicemeeter python -->
