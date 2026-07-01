"""
VoiceTranslator GUI — tkinter overlay for the real-time voice translator.

Provides volume meters, transcription feed, Start/Stop/Pause buttons,
per-direction LED indicators, device configuration by role, preflight
diagnostic modal, and test-tone audio verification.

The audio engine (main.py) runs in a background asyncio thread.
"""

import asyncio
import logging
import math
import os
import subprocess
import sys
import threading
import time

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv

load_dotenv()

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from tkinter import messagebox

from main import (
    State,
    StateMachine,
    run_engine,
    setup_logging,
    log,
)
from config import load_config, save_config
from devices import ROLES, list_all_devices, resolve_by_role, resolve_by_fingerprint, validate_samplerate
from windows_audio import get_windows_defaults, open_sound_settings_playback, open_sound_settings_recording

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
WINDOW_H = 320
WINDOW_H_DEVICES = 540
VOL_BAR_W = 200
VOL_BAR_H = 12

# ---------------------------------------------------------------------------
# Tooltip
# ---------------------------------------------------------------------------

class Tooltip:
    def __init__(self, widget: tk.Widget, text: str):
        self.widget = widget
        self.text = text
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)

    def _show(self, event=None):
        if self._window: return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self._window, text=self.text, bg="#2a2a3e", fg=TEXT,
                       font=("Consolas", 8), relief="flat", padx=8, pady=4,
                       wraplength=260, justify="left")
        lbl.pack()

    def _hide(self, event=None):
        if self._window: self._window.destroy(); self._window = None


# ---------------------------------------------------------------------------
# Pulse animation
# ---------------------------------------------------------------------------

class PulseAnimator:
    def __init__(self, widget: tk.Widget, canvas: tk.Canvas, led_id: int, speed: float = 0.08):
        self.widget = widget; self.canvas = canvas; self.led_id = led_id
        self._running = False; self._phase = 0.0; self._speed = speed

    def start(self):
        if not self._running: self._running = True; self._tick()

    def stop(self):
        self._running = False

    def _tick(self):
        if not self._running: return
        self._phase = (self._phase + self._speed) % (2 * math.pi)
        v = int(128 + 127 * (1 + math.sin(self._phase)) / 2)
        self.canvas.itemconfig(self.led_id, fill=f"#00{v:02x}00")
        self.widget.after(40, self._tick)


class FastPulseAnimator(PulseAnimator):
    def __init__(self, widget, canvas, led_id):
        super().__init__(widget, canvas, led_id, speed=0.15)
    def _tick(self):
        if not self._running: return
        self._phase = (self._phase + self._speed) % (2 * math.pi)
        v = int(200 + 55 * math.sin(self._phase))
        self.canvas.itemconfig(self.led_id, fill=f"#ff{v:02x}00")
        self.widget.after(30, self._tick)


# ---------------------------------------------------------------------------
# Main UI class
# ---------------------------------------------------------------------------

class TranslatorUI:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._engine_task: asyncio.Task | None = None
        self._engine_started = False
        self._async_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.state_machine: StateMachine | None = None
        self._state = State.IDLE

        self._vol_A: int = -60
        self._vol_B: int = -60
        self._transcripts = {"A": {"in": "", "out": ""}, "B": {"in": "", "out": ""}}
        self._direction_start: float = 0.0

        self._device_options: dict[str, list[tuple[str, str]]] = {}  # role -> [(label, fingerprint), ...]
        self._device_combos: dict[str, ttk.Combobox] = {}
        self._device_status_labels: dict[str, tk.Label] = {}
        self._direction_status: dict[str, str] = {"A": "IDLE", "B": "IDLE"}
        self._last_heartbeat: float | None = None
        self._health_check_id: str | None = None

        self._build_ui()
        self._start_engine_thread()

    # ------------------------------------------------------------------
    # Thread-safe callbacks
    # ------------------------------------------------------------------

    def _cb_volume_A(self, db: float):
        self.root.after(0, lambda: self._update_volume("A", db))

    def _cb_volume_B(self, db: float):
        self.root.after(0, lambda: self._update_volume("B", db))

    def _cb_transcription_A(self, role: str, text: str):
        self.root.after(0, lambda: self._update_transcription("A", role, text))

    def _cb_transcription_B(self, role: str, text: str):
        self.root.after(0, lambda: self._update_transcription("B", role, text))

    def _on_heartbeat(self):
        self._last_heartbeat = time.monotonic()

    def _on_direction_status(self, direction: str, status: str):
        self.root.after(0, lambda: self._update_direction_led(direction, status))

    def _update_volume(self, direction: str, db: float):
        if direction == "A": self._vol_A = int(db)
        else: self._vol_B = int(db)
        self._redraw_volume("A"); self._redraw_volume("B")

    def _update_transcription(self, direction: str, role: str, text: str):
        self._transcripts[direction][role] = text[:80]
        self._redraw_transcripts()

    def _update_direction_led(self, direction: str, status: str):
        self._direction_status[direction] = status
        s = {"IDLE": IDLE_LED, "CONNECTING": "#fbbf24", "RUNNING": "#10b981",
             "RECONNECTING": "#f97316", "FAILED": ERROR}
        c = s.get(status, IDLE_LED)
        if direction == "A":
            pid = self._pulse_a; lid = self._led_a
        else:
            pid = self._pulse_b; lid = self._led_b
        if status in ("CONNECTING", "RECONNECTING"):
            pid.start()
        else:
            pid.stop()
            self._led_canvas_a.itemconfig(lid, fill=c) if direction == "A" else self._led_canvas_b.itemconfig(lid, fill=c)

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    def _start_engine_thread(self):
        self._async_thread.start()
        future = asyncio.run_coroutine_threadsafe(self._init_state_machine(), self.loop)
        future.result(timeout=5)

    async def _init_state_machine(self):
        self.state_machine = StateMachine(self.loop)

    def _schedule_engine(self):
        if self.state_machine is None: return
        api_key = os.getenv("GEMINI_API_KEY")

        async def _run():
            await run_engine(
                sm=self.state_machine, api_key=api_key,
                show_status_bar=False,
                callbacks={
                    "on_t_A": self._cb_transcription_A,
                    "on_v_A": self._cb_volume_A,
                    "on_t_B": self._cb_transcription_B,
                    "on_v_B": self._cb_volume_B,
                    "on_ds": self._on_direction_status,
                    "on_heartbeat": self._on_heartbeat,
                },
            )

        self._engine_task = asyncio.run_coroutine_threadsafe(_run(), self.loop)
        self._engine_task.add_done_callback(self._on_engine_done)

    def _on_engine_done(self, future):
        try:
            future.result()
        except Exception as e:
            self.root.after(0, lambda: self._show_engine_error(str(e)))

    def _show_engine_error(self, msg: str):
        if self.state_machine: self.state_machine.set_state(State.IDLE)
        log.error("Engine error: %s", msg)
        self._direction_status = {"A": "FAILED", "B": "FAILED"}
        for d in ("A", "B"):
            pid = self._pulse_a if d == "A" else self._pulse_b
            lid = self._led_a if d == "A" else self._led_b
            pid.stop()
            (self._led_canvas_a if d == "A" else self._led_canvas_b).itemconfig(lid, fill=ERROR)
        short_msg = msg[:300] if len(msg) > 300 else msg
        messagebox.showerror("Error del Engine", short_msg)
        self._engine_started = False

    def _check_engine_health(self):
        if self._state != State.RUNNING:
            self._health_check_id = self.root.after(3000, self._check_engine_health)
            return
        if self._last_heartbeat is None:
            self._health_check_id = self.root.after(3000, self._check_engine_health)
            return
        if time.monotonic() - self._last_heartbeat > 8.0:
            self._show_engine_error("El engine no responde hace mas de 8s. Verifica los logs.")
            return
        self._health_check_id = self.root.after(3000, self._check_engine_health)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def _on_toggle(self):
        if self._state in (State.IDLE, State.STOPPED):
            if not self._engine_started:
                if not self._show_preflight_modal():
                    return
                self._engine_started = True
                self._schedule_engine()
            self.state_machine.set_state(State.RUNNING)
            self._last_heartbeat = time.monotonic()
            if self._health_check_id:
                self.root.after_cancel(self._health_check_id)
            self._health_check_id = self.root.after(3000, self._check_engine_health)
        elif self._state == State.RUNNING:
            self.state_machine.set_state(State.STOPPED)
            if self._health_check_id:
                self.root.after_cancel(self._health_check_id)
                self._health_check_id = None
        elif self._state == State.PAUSED:
            self.state_machine.set_state(State.STOPPED)
            if self._health_check_id:
                self.root.after_cancel(self._health_check_id)
                self._health_check_id = None

    def _on_pause(self):
        if self._state == State.RUNNING: self.state_machine.set_state(State.PAUSED)
        elif self._state == State.PAUSED: self.state_machine.set_state(State.RUNNING)

    def _build_smart_muting_toggle(self):
        frame = tk.Frame(self.root, bg=SURFACE)
        frame.pack(fill=tk.X, padx=10, pady=(0, 2))

        cfg = load_config()
        self.smart_muting_var = tk.BooleanVar(value=cfg.get("smart_muting_enabled", True))
        toggle = tk.Checkbutton(
            frame, text="\u26a1 Smart Muting",
            variable=self.smart_muting_var,
            command=self._on_smart_muting_toggle,
            bg=SURFACE, fg=TEXT, selectcolor=SURFACE,
            activebackground=SURFACE, activeforeground=ACCENT,
            font=tkfont.Font(family="Consolas", size=9), cursor="hand2",
        )
        toggle.pack(side=tk.LEFT)
        Tooltip(toggle,
            "Pausa tu voz mientras el entrevistador habla.\n"
            "Reduce costo de API ~50%. Desactivar si hay problemas.")

        self.muting_status_label = tk.Label(
            frame, text="", bg=SURFACE, fg=MUTED,
            font=tkfont.Font(family="Consolas", size=8),
        )
        self.muting_status_label.pack(side=tk.RIGHT, padx=(0, 8))

    def _on_smart_muting_toggle(self):
        enabled = self.smart_muting_var.get()
        cfg = load_config()
        cfg["smart_muting_enabled"] = enabled
        save_config(cfg)
        log.info("Smart Muting: %s", "ON" if enabled else "OFF")

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    def _refresh(self):
        if self.state_machine is not None:
            self._state = self.state_machine.state

        if self._state == State.RUNNING:
            self._state_label.config(text="RUNNING")
        else:
            self._state_label.config(text=self._state.value)

        if self._state == State.RUNNING:
            self._btn_toggle.config(text="\u23f9 STOP", fg=ERROR, state=tk.NORMAL)
            self._btn_pause.config(text="\u23f8 PAUSE", fg=PAUSED, state=tk.NORMAL)
        elif self._state == State.PAUSED:
            self._btn_toggle.config(text="\u23f9 STOP", fg=ERROR, state=tk.NORMAL)
            self._btn_pause.config(text="\u25b6 RESUME", fg=ACCENT, state=tk.NORMAL)
        elif self._state in (State.IDLE, State.STOPPED):
            self._btn_toggle.config(text="\u25b6 START", fg=ACCENT, state=tk.NORMAL)
            self._btn_pause.config(text="\u23f8 PAUSE", fg=MUTED, state=tk.DISABLED)
            self._vol_A = -60; self._vol_B = -60
            self._transcripts = {"A": {"in": "", "out": ""}, "B": {"in": "", "out": ""}}
            self._direction_start = 0.0

        if self._state == State.RUNNING and self._direction_start == 0.0:
            self._direction_start = time.time()
        if self._state == State.RUNNING: self._update_timer()
        elif self._state in (State.IDLE, State.STOPPED): self._timer_label.config(text="00:00:00")

        self._redraw_volume("A"); self._redraw_volume("B")
        self._redraw_transcripts()
        self.root.after(100, self._refresh)

    def _update_timer(self):
        if self._direction_start:
            e = int(time.time() - self._direction_start)
            hh, r = divmod(e, 3600); mm, ss = divmod(r, 60)
            self._timer_label.config(text=f"{hh:02d}:{mm:02d}:{ss:02d}")
        self.root.after(1000, self._update_timer)

    def _redraw_volume(self, direction: str):
        canvas = self._vol_canvas_A if direction == "A" else self._vol_canvas_B
        label = self._vol_label_A if direction == "A" else self._vol_label_B
        db = self._vol_A if direction == "A" else self._vol_B
        clamped = max(-60, min(0, db))
        w = int(((clamped + 60) / 60.0) * VOL_BAR_W)
        canvas.delete("bar")
        if w > 0: canvas.create_rectangle(0, 0, w, VOL_BAR_H, fill=VOLBAR, outline="", tags="bar")
        label.config(text=f"{db} dB" if db > -60 else "-\u221e dB")

    def _redraw_transcripts(self):
        for d in ("A", "B"):
            ti, to = self._transcripts[d]["in"], self._transcripts[d]["out"]
            if len(ti) > 55: ti = ti[:52] + "..."
            if len(to) > 55: to = to[:52] + "..."
            li = self._label_in_A if d == "A" else self._label_in_B
            lo = self._label_out_A if d == "A" else self._label_out_B
            li.config(text=ti if ti else "\u2014")
            lo.config(text=to if to else "\u2014")

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
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"+{sw - WINDOW_W - 20}+{sh - WINDOW_H - 60}")
        try:
            self.root.attributes("-alpha", 0.99)
            self.root.after(100, lambda: self.root.attributes("-alpha", 1.0))
        except Exception: pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        title_font = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        led_font = tkfont.Font(family="Segoe UI", size=7)

        # ==== HEADER ====
        header = tk.Frame(self.root, bg=BG, height=36)
        header.pack(fill=tk.X, padx=8, pady=(6, 0))
        header.pack_propagate(False)

        # Direction A LED
        self._led_canvas_a = tk.Canvas(header, width=14, height=14, bg=BG, highlightthickness=0)
        self._led_canvas_a.pack(side=tk.LEFT, padx=(2, 1))
        self._led_a = self._led_canvas_a.create_oval(1, 1, 13, 13, fill=IDLE_LED, outline="")
        self._pulse_a = FastPulseAnimator(self.root, self._led_canvas_a, self._led_a)
        led_a_lbl = tk.Label(header, text="A", fg=MUTED, bg=BG, font=led_font)
        led_a_lbl.pack(side=tk.LEFT, padx=(0, 6))
        Tooltip(self._led_canvas_a, "Direcci\u00f3n A: traduce tu voz (espa\u00f1ol \u2192 ingl\u00e9s).")
        Tooltip(led_a_lbl, "Direcci\u00f3n A: traduce tu voz (espa\u00f1ol \u2192 ingl\u00e9s).")

        # State label
        self._state_label = tk.Label(header, text="IDLE", fg=TEXT, bg=BG, font=title_font, width=7, anchor="w")
        self._state_label.pack(side=tk.LEFT)

        # Timer
        self._timer_label = tk.Label(header, text="00:00:00", fg=TEXT, bg=BG, font=title_font)
        self._timer_label.pack(side=tk.LEFT, expand=True)
        Tooltip(self._timer_label, "Tiempo total de sesi\u00f3n activa.")

        # Direction B LED
        led_b_lbl = tk.Label(header, text="B", fg=MUTED, bg=BG, font=led_font)
        led_b_lbl.pack(side=tk.RIGHT, padx=(0, 1))
        self._led_canvas_b = tk.Canvas(header, width=14, height=14, bg=BG, highlightthickness=0)
        self._led_canvas_b.pack(side=tk.RIGHT, padx=(1, 2))
        self._led_b = self._led_canvas_b.create_oval(1, 1, 13, 13, fill=IDLE_LED, outline="")
        self._pulse_b = FastPulseAnimator(self.root, self._led_canvas_b, self._led_b)
        Tooltip(self._led_canvas_b, "Direcci\u00f3n B: traduce al interlocutor (ingl\u00e9s \u2192 espa\u00f1ol).")
        Tooltip(led_b_lbl, "Direcci\u00f3n B: traduce al interlocutor (ingl\u00e9s \u2192 espa\u00f1ol).")

        # Close
        btn_close = tk.Label(header, text="\u2715", fg=MUTED, bg=BG, font=title_font, cursor="hand2")
        btn_close.pack(side=tk.RIGHT, padx=(4, 0))
        btn_close.bind("<Button-1>", lambda e: self._on_close())

        # ==== BUTTONS ====
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill=tk.X, padx=8, pady=6)
        self._btn_toggle = tk.Label(btn_frame, text="\u25b6 START", fg=ACCENT, bg=SURFACE,
                                    font=title_font, padx=20, pady=4, cursor="hand2")
        self._btn_toggle.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_toggle.bind("<Button-1>", lambda e: self._on_toggle())
        Tooltip(self._btn_toggle, "Detiene todo y muestra estad\u00edsticas de la sesi\u00f3n.")
        self._btn_pause = tk.Label(btn_frame, text="\u23f8 PAUSE", fg=MUTED, bg=SURFACE,
                                   font=title_font, padx=20, pady=4, cursor="hand2")
        self._btn_pause.pack(side=tk.LEFT)
        self._btn_pause.bind("<Button-1>", lambda e: self._on_pause())
        Tooltip(self._btn_pause, "Pausa la traducci\u00f3n sin cortar la conexi\u00f3n con Gemini.")

        # Smart Muting toggle
        self._build_smart_muting_toggle()

        # ==== DIRECTIONS ====
        self._build_direction("A", "Mic  (es \u2192 en)", True)
        self._build_direction("B", "Sys  (en \u2192 es)", False)

        # ==== DEVICE PANEL ====
        self._build_device_panel()

    def _build_direction(self, tag: str, label_text: str, is_a: bool):
        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill=tk.X, padx=8, pady=2)
        lbl = tk.Label(frame, text=label_text, fg=TEXT, bg=BG,
                       font=tkfont.Font(family="Segoe UI", size=8, weight="bold"), anchor="w")
        lbl.pack(fill=tk.X)
        vol_row = tk.Frame(frame, bg=BG)
        vol_row.pack(fill=tk.X, pady=(1, 1))
        cv = tk.Canvas(vol_row, width=VOL_BAR_W, height=VOL_BAR_H, bg=SURFACE, highlightthickness=0)
        cv.pack(side=tk.LEFT, padx=(0, 6))
        cv.create_rectangle(0, 0, VOL_BAR_W, VOL_BAR_H, outline="#333", width=1)
        vol_tip = "Nivel de tu voz." if is_a else "Nivel del audio del entrevistador."
        Tooltip(cv, vol_tip)
        db_label = tk.Label(vol_row, text="-60 dB", fg=TEXT, bg=BG,
                            font=tkfont.Font(family="Consolas", size=8), width=7, anchor="w")
        db_label.pack(side=tk.LEFT)
        il = tk.Label(frame, text="\u2014", fg=MUTED, bg=BG,
                      font=tkfont.Font(family="Segoe UI", size=8), anchor="w")
        il.pack(fill=tk.X)
        it = "Lo que dijiste en espa\u00f1ol." if is_a else "Lo que dijo el entrevistador en ingl\u00e9s."
        Tooltip(il, it)
        ol = tk.Label(frame, text="\u2014", fg=ACCENT, bg=BG,
                      font=tkfont.Font(family="Segoe UI", size=8), anchor="w")
        ol.pack(fill=tk.X)
        ot = "C\u00f3mo lo escucha el entrevistador." if is_a else "C\u00f3mo lo escuch\u00e1s vos."
        Tooltip(ol, ot)
        if is_a: self._vol_canvas_A=cv; self._vol_label_A=db_label; self._label_in_A=il; self._label_out_A=ol
        else: self._vol_canvas_B=cv; self._vol_label_B=db_label; self._label_in_B=il; self._label_out_B=ol

    # ==================================================================
    # Device settings panel (role-based)
    # ==================================================================

    def _build_device_panel(self):
        self.devices_expanded = False
        self.help_expanded = False
        tiny_font = tkfont.Font(family="Consolas", size=8)

        header = tk.Frame(self.root, bg=SURFACE, cursor="hand2")
        header.pack(fill=tk.X, padx=10, pady=(6, 0))
        self.devices_toggle_label = tk.Label(header, text="\u2699  Dispositivos  \u25bc",
                                             bg=SURFACE, fg=MUTED, font=tiny_font, cursor="hand2")
        self.devices_toggle_label.pack(side=tk.LEFT, pady=4)
        header.bind("<Button-1>", self._toggle_devices_panel)
        self.devices_toggle_label.bind("<Button-1>", self._toggle_devices_panel)
        self.devices_frame = tk.Frame(self.root, bg=SURFACE)

        # Populate by ROLES
        role_labels = {
            "mic": ("\U0001f3a4 Micr\u00f3fono (tu voz)", "input"),
            "virtual_mic_out": ("\U0001f4de Salida virtual videollamada", "output"),
            "loopback_in": ("\U0001f249 Captura audio sistema", "input"),
            "headphones_out": ("\U0001f3a7 Auriculares (audio traducido)", "output"),
        }
        tips = {
            "mic": "Habl\u00e1s por ac\u00e1 en espa\u00f1ol.",
            "virtual_mic_out": "El audio en ingl\u00e9s que escucha el entrevistador.",
            "loopback_in": "Lo que dice el entrevistador entra por ac\u00e1.",
            "headphones_out": "Ac\u00e1 escuch\u00e1s al entrevistador traducido.",
        }

        for role_name in ROLES:
            label_text, kind = role_labels.get(role_name, (role_name, "output"))
            row = tk.Frame(self.devices_frame, bg=SURFACE)
            row.pack(fill=tk.X, padx=8, pady=1)
            lbl = tk.Label(row, text=label_text, bg=SURFACE, fg=TEXT, font=tiny_font, width=28, anchor="w")
            lbl.pack(side=tk.LEFT)
            Tooltip(lbl, tips.get(role_name, ""))
            var = tk.StringVar()
            combo = ttk.Combobox(row, textvariable=var, font=tiny_font, width=28, state="readonly")
            combo.pack(side=tk.LEFT)
            Tooltip(combo, tips.get(role_name, ""))
            sl = tk.Label(row, text="", bg=SURFACE, fg=ACCENT, font=tiny_font, width=3)
            sl.pack(side=tk.LEFT, padx=(4, 0))
            self._device_combos[role_name] = combo
            self._device_status_labels[role_name] = sl
            self.root.after(20, lambda rn=role_name, c=combo, k=kind, s=sl: self._init_combo_role(rn, c, k, s))

        btn_frame = tk.Frame(self.devices_frame, bg=SURFACE)
        btn_frame.pack(fill=tk.X, padx=8, pady=(4, 6))
        apply_btn = tk.Label(btn_frame, text="Apply & Restart", fg=ACCENT, bg="#2a2a3e",
                             font=tiny_font, padx=14, pady=3, cursor="hand2")
        apply_btn.pack(side=tk.RIGHT)
        apply_btn.bind("<Button-1>", lambda e: self._apply_devices())
        Tooltip(apply_btn, "Guarda los dispositivos y reinicia la traducci\u00f3n.")

        # Quick Sound Settings shortcuts
        ss_row = tk.Frame(self.devices_frame, bg=SURFACE)
        ss_row.pack(fill=tk.X, padx=8, pady=(0, 2))
        tiny = tkfont.Font(family="Segoe UI", size=7)
        tk.Label(ss_row, text="\u2699", fg=MUTED, bg=SURFACE, font=tiny).pack(side=tk.LEFT)
        pb_btn = tk.Label(ss_row, text="Sound Reproducci\u00f3n", fg=MUTED, bg=SURFACE,
                          font=tiny, cursor="hand2")
        pb_btn.pack(side=tk.LEFT, padx=(2, 6))
        pb_btn.bind("<Button-1>", lambda e: open_sound_settings_playback())
        rec_btn = tk.Label(ss_row, text="Sound Grabaci\u00f3n", fg=MUTED, bg=SURFACE,
                           font=tiny, cursor="hand2")
        rec_btn.pack(side=tk.LEFT, padx=2)
        rec_btn.bind("<Button-1>", lambda e: open_sound_settings_recording())
        self._build_help_panel()

    def _init_combo_role(self, role_name, combo, kind, status_lbl):
        """Populate combo from list_all_devices, filter by role kind, prefer host API."""
        role = ROLES.get(role_name)
        all_ds = list_all_devices()
        filtered = []
        for d in all_ds:
            if kind == "input" and d["max_input_channels"] == 0: continue
            if kind == "output" and d["max_output_channels"] == 0: continue
            if "WDM-KS" in d["host_api"]: continue
            if "Sound Mapper" in d["name"] or "Controlador primario" in d["name"]: continue
            if role and role.excluded_patterns:
                nl = d["name"].lower()
                if any(ep.lower() in nl for ep in role.excluded_patterns): continue
            filtered.append(d)

        # Sort: preferred host API first
        if role and role.preferred_host_api:
            pref = role.preferred_host_api
            filtered.sort(key=lambda d: (0 if d["host_api"] == pref else 1, d["index"]))

        labels = []
        for d in filtered:
            short_api = d["host_api"].replace("Windows ", "")[:6]
            lbl = "[{:3d}] {:6s} \u2014 {}".format(d["index"], short_api, d["name"])
            fp = "{}||{}||{}||{}||{}".format(
                d["host_api"], d["name"],
                d["max_input_channels"], d["max_output_channels"],
                d["default_samplerate"])
            labels.append((lbl, fp))
        self._device_options[role_name] = labels

        # Preselect from config
        cfg = load_config()
        saved_dev = cfg.get("devices", {}).get(role_name, {})
        saved_idx = saved_dev.get("index", -1)
        selected = False
        if saved_idx >= 0:
            for lbl, fp in labels:
                if str(saved_idx) in lbl.split("]")[0]:
                    combo.set(lbl)
                    selected = True
                    break
        if not selected:
            try:
                rd = resolve_by_role(role)
                for lbl, fp in labels:
                    if fp == rd.fingerprint:
                        combo.set(lbl); break
            except Exception: pass

        combo["values"] = [lbl for lbl, _ in labels]

        def _on_select(event=None):
            val = combo.get()
            for lbl, fp in labels:
                if lbl == val:
                    ok = resolve_by_fingerprint(fp) is not None
                    status_lbl.config(text="\u2713" if ok else "\u26a0", fg=ACCENT if ok else PAUSED)
                    break
            else:
                status_lbl.config(text="\u26a0", fg=PAUSED)

        combo.bind("<<ComboboxSelected>>", _on_select)
        _on_select()

    def _apply_devices(self):
        cfg = load_config()
        for role_name, combo in self._device_combos.items():
            val = combo.get()
            for lbl in self._device_options.get(role_name, []):
                label_str = lbl[0] if isinstance(lbl, tuple) else lbl
                if label_str == val:
                    # Parse "[ 13] MME    — Altavoces..." -> index + name + hostapi
                    parts = label_str.split("\u2014", 1)
                    header = parts[0].strip()  # "[ 13] MME   "
                    name = parts[1].strip() if len(parts) > 1 else ""
                    idx_str = header.split("]")[0].replace("[", "").strip()
                    hostapi = header.split("]")[1].strip()
                    idx = int(idx_str)
                    # Get rate from sounddevice
                    import sounddevice as sd
                    rate = int(sd.query_devices()[idx]["default_samplerate"])
                    cfg["devices"][role_name] = {"index": idx, "name": name,
                                                  "hostapi": hostapi, "rate": rate}
                    break
        save_config(cfg)
        if self._state in (State.RUNNING, State.PAUSED):
            self.state_machine.set_state(State.STOPPED)
            self.root.after(800, lambda: self.state_machine.set_state(State.RUNNING))
        self._show_toast("Dispositivos actualizados \u2713")

    def _build_help_panel(self):
        tiny_font = tkfont.Font(family="Consolas", size=8)
        mini_font = tkfont.Font(family="Consolas", size=7)
        header = tk.Frame(self.root, bg=BG, cursor="hand2")
        header.pack(fill=tk.X, padx=10, pady=(2, 0))
        self.help_toggle_label = tk.Label(header, text="\u2753  C\u00f3mo funciona  \u25bc",
                                          bg=BG, fg=MUTED, font=tiny_font, cursor="hand2")
        self.help_toggle_label.pack(side=tk.LEFT, pady=2)
        header.bind("<Button-1>", self._toggle_help_panel)
        self.help_toggle_label.bind("<Button-1>", self._toggle_help_panel)
        self.help_frame = tk.Frame(self.root, bg=BG)
        help_text = ("\U0001f5e3 Vos habl\u00e1s espa\u00f1ol \u2192 el entrevistador escucha ingl\u00e9s\n"
                     "\U0001f3a7 El entrevistador habla ingl\u00e9s \u2192 vos escuch\u00e1s espa\u00f1ol\n\n"
                     "En la videollamada configur\u00e1:\n"
                     "  \u2022 Micr\u00f3fono: CABLE Output (VB-Cable)\n"
                     "  \u2022 Parlante: Predeterminado\n\n"
                     "Hotkeys:\n  \u2022 Ctrl+Shift+T \u2192 Start / Stop\n"
                     "  \u2022 Ctrl+Shift+P \u2192 Pause / Resume")
        tk.Label(self.help_frame, text=help_text, bg=BG, fg=MUTED, font=mini_font,
                 justify="left", padx=10, pady=6).pack(fill=tk.X)

    def _toggle_devices_panel(self, event=None):
        if self.devices_expanded:
            self.devices_frame.pack_forget()
            self.devices_toggle_label.config(text="\u2699  Dispositivos  \u25bc")
            base = WINDOW_H + (80 if self.help_expanded else 0)
            self.root.geometry("{}x{}".format(WINDOW_W, base))
        else:
            delta = WINDOW_H_DEVICES - WINDOW_H
            self.devices_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
            self.devices_toggle_label.config(text="\u2699  Dispositivos  \u25b2")
            x = self.root.winfo_x(); y = self.root.winfo_y() - delta
            if y < 0: y = 0
            self.root.geometry("{}x{}+{}+{}".format(WINDOW_W, self.root.winfo_height() + delta, x, y))
        self.devices_expanded = not self.devices_expanded

    def _toggle_help_panel(self, event=None):
        if self.help_expanded:
            self.help_frame.pack_forget()
            self.help_toggle_label.config(text="\u2753  C\u00f3mo funciona  \u25bc")
            delta = -80
        else:
            self.help_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
            self.help_toggle_label.config(text="\u2753  C\u00f3mo funciona  \u25b2")
            delta = 80
        self.help_expanded = not self.help_expanded
        x = self.root.winfo_x(); y = self.root.winfo_y() - delta
        if y < 0: y = 0
        self.root.geometry("{}x{}+{}+{}".format(WINDOW_W, self.root.winfo_height() + delta, x, y))

    def _show_toast(self, message: str):
        toast = tk.Label(self.root, text=message, bg=ACCENT, fg="#000000",
                         font=tkfont.Font(family="Segoe UI", size=9, weight="bold"), padx=10, pady=4)
        toast.place(relx=0.5, rely=0.93, anchor="center")
        self.root.after(2000, toast.destroy)

    # ==================================================================
    # Preflight modal
    # ==================================================================

    def _show_preflight_modal(self) -> bool:
        from preflight import check_api_key, check_voicemeeter_running
        from devices import ROLES as DEV_ROLES, resolve_by_role as dev_resolve

        api_key = os.getenv("GEMINI_API_KEY", "")
        modal = tk.Toplevel(self.root)
        modal.title("Verificaci\u00f3n previa")
        modal.configure(bg=SURFACE)
        modal.resizable(False, False)
        modal.grab_set()
        w, h = 420, 300
        sw, sh = modal.winfo_screenwidth(), modal.winfo_screenheight()
        modal.geometry("{}x{}+{}+{}".format(w, h, (sw - w)//2, (sh - h)//2))
        modal.protocol("WM_DELETE_WINDOW", lambda: setattr(modal, "_result", False) or modal.destroy())
        modal._result = False

        f = tkfont.Font(family="Consolas", size=9)
        check_vars = {"api": False, "vm": False, "devices": False, "audio": False,
                      "playback": True, "recording": True}  # warnings, not blockers
        check_labels = {}

        tk.Label(modal, text="Verificaci\u00f3n previa", bg=SURFACE, fg=TEXT,
                 font=tkfont.Font(family="Segoe UI", size=12, weight="bold")).pack(pady=(10, 6))

        for key, label in [("api", "API Key Gemini"), ("vm", "Voicemeeter Banana"),
                            ("devices", "Dispositivos de audio"), ("audio", "Test de audio"),
                            ("playback", "Default Playback (VAIO)"), ("recording", "Default Recording (B1)")]:
            row = tk.Frame(modal, bg=SURFACE)
            row.pack(fill=tk.X, padx=20, pady=2)
            icon = tk.Label(row, text="\u23f3", bg=SURFACE, fg=MUTED, font=f)
            icon.pack(side=tk.LEFT, padx=(0, 8))
            tk.Label(row, text=label, bg=SURFACE, fg=TEXT, font=f, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
            check_labels[key] = icon

        continue_btn = tk.Label(modal, text="Continuar", bg=ACCENT, fg="#000",
                                font=tkfont.Font(family="Segoe UI", size=10, weight="bold"),
                                padx=20, pady=6, cursor="hand2", state=tk.DISABLED)
        continue_btn.pack(side=tk.BOTTOM, pady=(8, 14))
        continue_btn.bind("<Button-1>", lambda e: setattr(modal, "_result", True) or modal.destroy())

        def _set_check(key, ok, msg=""):
            c = ACCENT if ok else ERROR
            check_labels[key].config(text="\u2713" if ok else "\u2717", fg=c)
            if msg:
                check_labels[key].config(text="\u2717" if not ok else "\u2713")
                Tooltip(check_labels[key], msg)
            check_vars[key] = ok
            if all(check_vars.values()):
                continue_btn.config(state=tk.NORMAL, fg="#000", bg=ACCENT)

        def _run_checks():
            # 1. API key
            ok, err = check_api_key(api_key)
            modal.after(0, lambda: _set_check("api", ok, err or ""))

            # 2. Voicemeeter
            vm_ok = check_voicemeeter_running()
            modal.after(0, lambda: _set_check("vm", vm_ok,
                  "Voicemeeter no detectado. Abrilo y reintent\u00e1." if not vm_ok else ""))

            # 3. Devices
            try:
                cfg = load_config()
                for rn in DEV_ROLES:
                    idx = cfg.get("devices", {}).get(rn, {}).get("index", -1)
                    if idx < 0:
                        raise Exception("Device '{}' not configured".format(rn))
                modal.after(0, lambda: _set_check("devices", True))
            except Exception as e:
                modal.after(0, lambda: _set_check("devices", False, str(e)[:200]))

            # 3b. Windows audio defaults (warning, non-blocking)
            try:
                wd = get_windows_defaults()
                if wd.playback_is_vaio:
                    modal.after(0, lambda: _set_check("playback", True))
                else:
                    pb_info = wd.playback_name or "No detectado"
                    modal.after(0, lambda: _set_check("playback", False,
                        "Es: {}. Deberia ser VoiceMeeter Input (VAIO).".format(pb_info)))
                if wd.recording_is_b1:
                    modal.after(0, lambda: _set_check("recording", True))
                else:
                    rec_info = wd.recording_name or "No detectado"
                    modal.after(0, lambda: _set_check("recording", False,
                        "Es: {}. Deberia ser VoiceMeeter Out B1.".format(rec_info)))
            except Exception:
                modal.after(0, lambda: _set_check("playback", False, "No se pudo verificar (pycaw no disponible)"))
                modal.after(0, lambda: _set_check("recording", False, "No se pudo verificar (pycaw no disponible)"))

            # 4. Audio test
            def _audio_test():
                try:
                    rd = dev_resolve(DEV_ROLES["headphones_out"])
                    sr = validate_samplerate(rd.index, 48000, 1, "output")
                    # 3 ascending beeps: 700, 1000, 1400 Hz, ~1.2s total
                    freqs = [700, 1000, 1400]
                    beep_dur = 0.18; gap_dur = 0.12
                    pieces = []
                    for f in freqs:
                        t_arr = np.linspace(0, beep_dur, int(sr * beep_dur), False)
                        tone = (0.35 * np.sin(2 * np.pi * f * t_arr)).astype(np.float32)
                        fade_n = int(sr * 0.015)
                        tone[:fade_n] *= np.linspace(0, 1, fade_n)
                        tone[-fade_n:] *= np.linspace(1, 0, fade_n)
                        pieces.append(tone)
                        pieces.append(np.zeros(int(sr * gap_dur)))
                    full = np.concatenate(pieces[:-1])
                    sd.play((full * 32767).astype(np.int16), samplerate=sr, device=rd.index, blocking=True)
                    modal.after(0, lambda: _ask_audio_result(rd.index, sr))
                except Exception as e:
                    _err = str(e)[:200]
                    modal.after(0, lambda err=_err: _set_check("audio", False, err))

            threading.Thread(target=_audio_test, daemon=True).start()

        def _ask_audio_result(idx, sr):
            ask = tk.Toplevel(modal)
            ask.title("Test de audio")
            ask.configure(bg=SURFACE)
            ask.resizable(False, False)
            ask.grab_set()
            ask.geometry("+{}+{}".format(modal.winfo_rootx()+20, modal.winfo_rooty()+50))
            tk.Label(ask, text="\u00bfEscuchaste el beep?", bg=SURFACE, fg=TEXT,
                     font=tkfont.Font(family="Segoe UI", size=10)).pack(pady=(10, 6))
            def _yes():
                ask.destroy(); _set_check("audio", True)
            def _no():
                ask.destroy()
                _set_check("audio", False,
                    "No escuchaste los beeps. Esto casi siempre es por una de dos cosas:\n"
                    "1) Auriculares en otra salida (Volume Mixer de Windows)\n"
                    "2) Auriculares fisicamente desconectados o en volumen 0")
                # Offer to open mixer
                def _open_mixer():
                    try:
                        subprocess.Popen(["sndvol.exe"])
                        modal.after(3000, lambda: _retry_audio_test(idx, sr))
                    except Exception:
                        pass
                retry_btn = tk.Label(ask, text="Abrir Mezclador de volumen", fg=ACCENT,
                                     bg=SURFACE, font=tkfont.Font(family="Segoe UI", size=9),
                                     cursor="hand2", padx=10, pady=4)
                retry_btn.pack(pady=4)
                retry_btn.bind("<Button-1>", lambda e: _open_mixer())
            tk.Label(ask, text="\u25b6 Reproducir beep", fg=ACCENT, bg=SURFACE,
                     font=tkfont.Font(family="Segoe UI", size=10), cursor="hand2",
                     padx=12, pady=4).pack(side=tk.LEFT, padx=(20, 10), pady=10)
            tk.Label(ask, text="S\u00ed", fg=ACCENT, bg=SURFACE, font=tkfont.Font(family="Segoe UI", size=10),
                     cursor="hand2", padx=12, pady=4).pack(side=tk.LEFT, padx=5, pady=10)
            tk.Label(ask, text="No", fg=ERROR, bg=SURFACE, font=tkfont.Font(family="Segoe UI", size=10),
                     cursor="hand2", padx=12, pady=4).pack(side=tk.LEFT, padx=5, pady=10)
            for lbl in ask.winfo_children():
                if isinstance(lbl, tk.Label) and lbl.cget("text") == "\u25b6 Reproducir beep":
                    lbl.bind("<Button-1>", lambda e: sd.play(
                        (np.sin(2*np.pi*700*np.linspace(0,0.4,int(sr*0.4),False))*0.5*32767).astype(np.int16),
                        samplerate=sr, device=idx, blocking=True))
                elif isinstance(lbl, tk.Label) and lbl.cget("text") == "S\u00ed":
                    lbl.bind("<Button-1>", lambda e: _yes())
                elif isinstance(lbl, tk.Label) and lbl.cget("text") == "No":
                    lbl.bind("<Button-1>", lambda e: _no())

        def _retry_audio_test(idx, sr):
            t = np.linspace(0, 0.4, int(sr * 0.4), False)
            tone = (0.25 * (np.sin(2*np.pi*700*t) + 0.5*np.sin(2*np.pi*1100*t))).astype(np.float32)
            fade_n = int(sr * 0.02)
            tone[:fade_n] *= np.linspace(0, 1, fade_n)
            tone[-fade_n:] *= np.linspace(1, 0, fade_n)
            sd.play((tone * 32767).astype(np.int16), samplerate=sr, device=idx, blocking=True)
            modal.after(0, lambda: _ask_audio_result(idx, sr))

        run_btn = tk.Label(modal, text="Ejecutar checks", fg=ACCENT, bg=SURFACE,
                           font=tkfont.Font(family="Segoe UI", size=10), cursor="hand2",
                           padx=14, pady=4)
        run_btn.pack(side=tk.BOTTOM, pady=(6, 0))
        run_btn.bind("<Button-1>", lambda e: threading.Thread(target=_run_checks, daemon=True).start())

        # Sound settings shortcuts
        ss_row = tk.Frame(modal, bg=SURFACE)
        ss_row.pack(side=tk.BOTTOM, pady=(2, 0))
        tk.Label(ss_row, text="Sound Settings:", bg=SURFACE, fg=MUTED,
                 font=tkfont.Font(family="Segoe UI", size=8)).pack(side=tk.LEFT, padx=(10, 4))
        tk.Label(ss_row, text="Reprod", fg=ACCENT, bg=SURFACE, cursor="hand2",
                 font=tkfont.Font(family="Segoe UI", size=8)).pack(side=tk.LEFT, padx=2)
        tk.Label(ss_row, text="|", bg=SURFACE, fg=MUTED,
                 font=tkfont.Font(family="Segoe UI", size=8)).pack(side=tk.LEFT)
        tk.Label(ss_row, text="Grabac", fg=ACCENT, bg=SURFACE, cursor="hand2",
                 font=tkfont.Font(family="Segoe UI", size=8)).pack(side=tk.LEFT, padx=2)
        # Bind actions
        for child in ss_row.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") == "Reprod":
                child.bind("<Button-1>", lambda e: open_sound_settings_playback())
            elif isinstance(child, tk.Label) and child.cget("text") == "Grabac":
                child.bind("<Button-1>", lambda e: open_sound_settings_recording())
        cancel_btn = tk.Label(modal, text="Cancelar", fg=MUTED, bg=SURFACE,
                              font=tkfont.Font(family="Segoe UI", size=10), cursor="hand2",
                              padx=14, pady=4)
        cancel_btn.pack(side=tk.BOTTOM, pady=(0, 4))
        cancel_btn.bind("<Button-1>", lambda e: setattr(modal, "_result", False) or modal.destroy())

        self.root.wait_window(modal)
        return getattr(modal, "_result", False)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self):
        if self.state_machine is not None:
            self.state_machine.set_state(State.STOPPED)
        self._pulse_a.stop(); self._pulse_b.stop()
        self.root.after(200, self._destroy)

    def _destroy(self):
        self.root.destroy()
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
