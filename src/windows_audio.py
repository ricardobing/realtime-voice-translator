"""
Windows audio defaults reader using pycaw (Windows Core Audio wrapper).

Reads the current Default Playback and Default Recording device names
from Windows, and provides shortcuts to open the classic Sound control
panel at the correct tabs.
"""

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class WindowsDefaults:
    playback_name: Optional[str]
    recording_name: Optional[str]
    playback_is_vaio: bool
    recording_is_b1: bool


def get_windows_defaults() -> WindowsDefaults:
    """
    Read Windows Default Playback and Default Recording devices.
    Does NOT require admin.  Returns all-None/False on error (doesn't crash).
    """
    try:
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        mic = AudioUtilities.GetMicrophone()

        pb_name = getattr(speakers, "FriendlyName", None) if speakers else None
        rec_name = getattr(mic, "FriendlyName", None) if mic else None

        pb_ok = bool(pb_name and ("VAIO" in pb_name or "VoiceMeeter Input" in pb_name))
        rec_ok = bool(rec_name and "VoiceMeeter Out B1" in rec_name)

        return WindowsDefaults(pb_name, rec_name, pb_ok, rec_ok)
    except Exception as e:
        log.warning("Could not read Windows defaults: %s", e)
        return WindowsDefaults(None, None, False, False)


def open_sound_settings_playback() -> None:
    """Open the classic Sound control panel at the Playback tab."""
    subprocess.Popen(["control.exe", "mmsys.cpl,,0"], shell=False)


def open_sound_settings_recording() -> None:
    """Open the classic Sound control panel at the Recording tab."""
    subprocess.Popen(["control.exe", "mmsys.cpl,,1"], shell=False)
