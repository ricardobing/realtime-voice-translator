"""
VoiceMeeter Routing Diagnostic & Functional Test.

Checks that the audio routing between Windows, Voicemeeter Banana, and
VB-Cable is correctly configured for the bidirectional translator.

Usage:
    python check_voicemeeter.py
"""

import sys
import time
import sounddevice as sd
import numpy as np

# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------

def _find_devices(name_hint: str, want_input: int = -1) -> list[tuple[int, dict]]:
    """Return list of (index, info) for devices whose name contains *name_hint*."""
    results = []
    for i, d in enumerate(sd.query_devices()):
        if name_hint.lower() not in d["name"].lower():
            continue
        if want_input == 0 and d["max_input_channels"] == 0:
            continue
        if want_input == 1 and d["max_output_channels"] == 0:
            continue
        results.append((i, d))
    return results

def find_input(name_hint: str) -> int | None:
    m = _find_devices(name_hint, want_input=0)
    return m[0][0] if m else None

def find_output(name_hint: str) -> int | None:
    m = _find_devices(name_hint, want_input=1)
    return m[0][0] if m else None

def device_name(idx: int) -> str:
    try:
        return sd.query_devices()[idx]["name"]
    except Exception:
        return "?"

# ---------------------------------------------------------------------------
# Default device detection
# ---------------------------------------------------------------------------

def get_defaults() -> tuple[int | None, int | None]:
    """Return (default_input_idx, default_output_idx) as reported by PortAudio."""
    inp, out = sd.default.device
    return (inp if inp is not None and inp >= 0 else None,
            out if out is not None and out >= 0 else None)

# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  VoiceMeeter Routing Check")
    print("=" * 60)
    print()

    all_ok = True
    issues: list[str] = []

    # ---------- 1. Discover devices ----------
    vaio_idx = find_output("Voicemeeter Input")
    b1_idx   = find_input("Voicemeeter Out B1")
    cable_i  = find_output("CABLE Input")
    cable_o  = find_input("CABLE Output")

    print("--- Device Discovery ---")
    print(f"  VoiceMeeter Input (VAIO)  output : {'[{}] {}'.format(vaio_idx, device_name(vaio_idx)) if vaio_idx is not None else 'MISSING'}")
    print(f"  VoiceMeeter Out B1        input  : {'[{}] {}'.format(b1_idx, device_name(b1_idx)) if b1_idx is not None else 'MISSING'}")
    print(f"  CABLE Input               output : {'[{}] {}'.format(cable_i, device_name(cable_i)) if cable_i is not None else 'MISSING'}")
    print(f"  CABLE Output              input  : {'[{}] {}'.format(cable_o, device_name(cable_o)) if cable_o is not None else 'MISSING'}")
    print()

    if vaio_idx is None:
        issues.append("VoiceMeeter Input (VAIO) not found. Is Voicemeeter Banana running?")
        all_ok = False
    if b1_idx is None:
        issues.append("VoiceMeeter Out B1 not found. Enable B1 bus in Voicemeeter Banana.")
        all_ok = False
    if cable_i is None:
        issues.append("CABLE Input not found. VB-Cable driver may not be installed.")
        all_ok = False
    if cable_o is None:
        issues.append("CABLE Output not found. VB-Cable driver may not be installed.")
        all_ok = False

    # ---------- 2. Check Windows default devices ----------
    def_in, def_out = get_defaults()
    print("--- Windows Default Devices (PortAudio view) ---")
    print(f"  Default Playback : [{def_out}] {device_name(def_out) if def_out is not None else '?'}")
    print(f"  Default Recording: [{def_in}]  {device_name(def_in) if def_in is not None else '?'}")
    print()

    ideal_playback_ok = def_out is not None and def_out == vaio_idx
    ideal_recording_ok = def_in is not None and def_in == b1_idx

    if not ideal_playback_ok:
        all_ok = False
        if vaio_idx is not None:
            issues.append(
                f"Default Playback should be 'VoiceMeeter Input (VAIO)'. "
                f"Current: [{def_out}] {device_name(def_out)}"
            )
    else:
        print("  Default Playback  = VoiceMeeter Input (VAIO)  OK")

    if not ideal_recording_ok:
        all_ok = False
        if b1_idx is not None:
            issues.append(
                f"Default Recording should be 'VoiceMeeter Out B1'. "
                f"Current: [{def_in}] {device_name(def_in)}"
            )
    else:
        print("  Default Recording = VoiceMeeter Out B1       OK")

    print()

    # ---------- 3. Functional tests ----------
    if vaio_idx is not None and b1_idx is not None:
        print("--- Functional Routing Tests ---")
        DURATION = 2       # seconds
        RATE = 16000

        # -- Test 1: send tone to VAIO, capture from B1 --
        print()
        print("Test 1: System audio reaches B1...")
        # Generate a 440 Hz tone
        t = np.linspace(0, DURATION, RATE * DURATION, endpoint=False)
        tone = np.int16(16384 * np.sin(2 * np.pi * 440 * t))

        # Play tone into VAIO and record from B1 simultaneously
        # Use playrec with different input/output devices
        rec = sd.playrec(tone, samplerate=RATE, channels=1, dtype="int16",
                         device=(b1_idx, vaio_idx),  # (input, output)
                         blocking=True)
        rms = float(np.sqrt(np.mean(rec.astype(np.float64) ** 2)))

        if rms > 200:
            print(f"  PASS  — RMS = {rms:.0f}  (system audio flows to B1)")
        else:
            all_ok = False
            print(f"  FAIL  — RMS = {rms:.0f}  (too low — B1 is not receiving VAIO audio)")
            issues.append(
                "In Voicemeeter Banana: make sure the VAIO channel strip has B1 button "
                "enabled (lit yellow/orange)."
            )

        # -- Test 2: verify microphone does NOT leak to B1 --
        print()
        print("Test 2: Microphone does NOT leak to B1...")
        print("  SPEAK into your microphone for 3 seconds NOW...")
        time.sleep(0.5)

        mic_leak = sd.rec(RATE * 3, samplerate=RATE, channels=1,
                          dtype="int16", device=b1_idx, blocking=True)
        rms_leak = float(np.sqrt(np.mean(mic_leak.astype(np.float64) ** 2)))

        if rms_leak < 500:
            print(f"  PASS  — Mic RMS in B1 = {rms_leak:.0f}  (anti-loop OK)")
        else:
            all_ok = False
            print(f"  WARN  — Mic RMS in B1 = {rms_leak:.0f}  (mic is leaking into B1!)")
            issues.append(
                "In Voicemeeter Banana: make sure Hardware Input 1 (your physical microphone) "
                "does NOT have the B1 button enabled. Only VAIO should route to B1."
            )

        # -- Test 3: VB-Cable loopback (CABLE Input -> CABLE Output) --
        print()
        print("Test 3: CABLE Input -> CABLE Output loopback...")
        if cable_i is not None and cable_o is not None:
            cable_tone = np.int16(16384 * np.sin(2 * np.pi * 600 * t))
            cable_rec = sd.playrec(cable_tone, samplerate=RATE, channels=1,
                                   dtype="int16",
                                   device=(cable_o, cable_i),  # (input, output)
                                   blocking=True)
            cable_rms = float(np.sqrt(np.mean(cable_rec.astype(np.float64) ** 2)))
            if cable_rms > 200:
                print(f"  PASS  — RMS = {cable_rms:.0f}  (VB-Cable loopback OK)")
            else:
                all_ok = False
                print(f"  FAIL  — RMS = {cable_rms:.0f}  (VB-Cable not routing)")
                issues.append(
                    "VB-Cable internal bridge is not working. "
                    "Reinstall VB-Cable driver and reboot."
                )
        else:
            print("  SKIP  — VB-Cable not fully detected")

    # ---------- 4. Summary ----------
    print()
    print("=" * 60)
    if all_ok and not issues:
        print("  ROUTING: CORRECTO — bidirectional translator ready")
        print("=" * 60)
    else:
        print("  ROUTING ISSUES FOUND — see below")
        print("=" * 60)
        print()
        for i, issue in enumerate(issues, 1):
            print(f"  [{i}] {issue}")

        print()
        print("--- Fix Instructions ---")
        print("""
1. Open Voicemeeter Banana (it must stay running in the background).

2. Set Windows default devices:
   - Playback:  "VoiceMeeter Input (VAIO)"
   - Recording: "VoiceMeeter Output (B1)"

   Right-click speaker icon > Sounds:
     Playback tab  -> select VoiceMeeter Input -> Set Default
     Recording tab -> select VoiceMeeter Out B1 -> Set Default

3. In Voicemeeter Banana routing matrix:
   - VAIO (Virtual Inputs section):     A1=ON   B1=ON
   - Hardware Input 1 (physical mic):   A1=ON   B1=OFF
   - ALL other Hardware Inputs:         B1=OFF

   A1 = your headphones/speakers
   B1 = the virtual bus our app captures from

4. Keep Voicemeeter Banana OPEN while using the translator.
""")
    print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
