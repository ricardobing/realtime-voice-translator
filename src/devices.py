"""
Device resolution layer for the Voice Translator.

Replaces fragile substring-based device matching with a scoring system
and role-based resolution.  Each of the four required devices is defined
by a DeviceRole that specifies search patterns, I/O kind, preferred host
API, and exclusion rules.

Also provides sample-rate validation that gracefully falls back to a
device's native rate when the requested rate is unsupported.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, List

import sounddevice as sd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedDevice:
    """A concrete audio device that was found for a role."""
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    @property
    def fingerprint(self) -> str:
        return (
            f"{self.host_api}||{self.name}||"
            f"{self.max_input_channels}||{self.max_output_channels}||"
            f"{self.default_samplerate}"
        )


@dataclass
class DeviceRole:
    """Describes a required device and how to find it."""
    name: str                                # "mic" | "virtual_mic_out" | "loopback_in" | "headphones_out"
    kind: str                                # "input" | "output"
    search_patterns: List[str]               # ordered by preference
    preferred_host_api: Optional[str] = None  # e.g. "Windows WASAPI" or "MME"
    excluded_patterns: List[str] = field(default_factory=list)


class DeviceResolutionError(Exception):
    """Raised when a device role cannot be uniquely resolved."""
    pass


# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------

ROLES = {
    "mic": DeviceRole(
        name="mic",
        kind="input",
        search_patterns=["Micr\u00f3fono (USB PnP Sound Device",
                         "USB PnP Sound Device", "Microphone"],
        preferred_host_api="MME",
        excluded_patterns=["CABLE", "VoiceMeeter", "VAIO"],
    ),
    "virtual_mic_out": DeviceRole(
        name="virtual_mic_out",
        kind="output",
        search_patterns=["CABLE Input"],
        preferred_host_api="MME",
    ),
    "loopback_in": DeviceRole(
        name="loopback_in",
        kind="input",
        search_patterns=["Voicemeeter Out B1"],
        preferred_host_api="MME",
    ),
    "virtual_mic_out": DeviceRole(
        name="virtual_mic_out",
        kind="output",
        search_patterns=["CABLE Input"],
        preferred_host_api="MME",
    ),
    "loopback_in": DeviceRole(
        name="loopback_in",
        kind="input",
        search_patterns=["Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)",
                         "Voicemeeter Out B1 (VB-Audio Vo",
                         "Voicemeeter Out B1"],
        preferred_host_api="MME",
    ),
    "headphones_out": DeviceRole(
        name="headphones_out",
        kind="output",
        search_patterns=["USB PnP Sound Device",
                         "Speakers", "Altavoces"],
        preferred_host_api="Windows WASAPI",
        excluded_patterns=["CABLE", "VoiceMeeter", "VAIO"],
    ),
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def list_all_devices() -> List[dict]:
    """Return every device with its host API name resolved to a string."""
    hostapis = sd.query_hostapis()
    devices = sd.query_devices()
    out = []
    for i, d in enumerate(devices):
        out.append({**d, "index": i, "host_api": hostapis[d["hostapi"]]["name"]})
    return out


def resolve_by_fingerprint(fingerprint: str) -> Optional[ResolvedDevice]:
    """Find the device whose fingerprint matches exactly, or None."""
    for d in list_all_devices():
        rd = _to_resolved(d)
        if rd.fingerprint == fingerprint:
            log.debug("Fingerprint match: [%d] %s", rd.index, rd.name)
            return rd
    log.debug("Fingerprint not found: %s", fingerprint[:80])
    return None


def resolve_by_role(role: DeviceRole) -> ResolvedDevice:
    """
    Resolve a role to the best-matching device using a scoring system.

    1. Filter by I/O kind, exclude patterns, and WDM-KS host API.
    2. Score each candidate by how well a search pattern matches its name:
       exact match = 3, starts_with = 2, substring = 1, no_match = 0.
    3. Pick the highest score.  Tiebreak with *preferred_host_api*.
    4. If the winner is ambiguous, raise DeviceResolutionError.
    5. If no candidates, raise DeviceResolutionError with diagnostics.
    """
    all_devs = list_all_devices()
    candidates: list[tuple[ResolvedDevice, int, int, str]] = []  # (device, score, pattern_count, matched_pattern)

    for d in all_devs:
        # ---- I/O filter ----
        if role.kind == "input" and d["max_input_channels"] == 0:
            continue
        if role.kind == "output" and d["max_output_channels"] == 0:
            continue

        # ---- exclude WDM-KS (too low-level) ----
        if "WDM-KS" in d["host_api"]:
            continue

        # ---- exclude patterns ----
        name_lower = d["name"].lower()
        if any(ep.lower() in name_lower for ep in role.excluded_patterns):
            continue

        # ---- score ----
        best_score = 0
        pattern_count = 0
        matched_pat = ""
        for pat in role.search_patterns:
            pat_low = pat.lower()
            if name_lower == pat_low:
                best_score = max(best_score, 3)
                pattern_count += 1
                if best_score == 3:
                    matched_pat = pat
                break
            elif name_lower.startswith(pat_low):
                best_score = max(best_score, 2)
                pattern_count += 1
                if best_score == 2:
                    matched_pat = pat
            elif pat_low in name_lower:
                best_score = max(best_score, 1)
                pattern_count += 1
                if best_score == 1 and matched_pat == "":
                    matched_pat = pat

        if best_score > 0:
            rd = _to_resolved(d)
            candidates.append((rd, best_score, pattern_count, matched_pat))

    if not candidates:
        all_names = "\n".join(
            f"  [{d['index']}] {d['host_api']:20s} {d['name']}"
            for d in all_devs if d["max_input_channels"] + d["max_output_channels"] > 0
        )
        raise DeviceResolutionError(
            f"No devices found for role '{role.name}'.\n"
            f"Search patterns: {role.search_patterns}\n"
            f"All devices seen:\n{all_names}"
        )

    # WINNER = max score, tiebreak by pattern_count, then host API
    max_score = max(c[1] for c in candidates)
    top = [c for c in candidates if c[1] == max_score]

    # Secondary tiebreak: prefer more matching patterns
    if len(top) > 1:
        max_pcount = max(c[2] for c in top)
        top = [c for c in top if c[2] == max_pcount]

    # Tiebreak by preferred host API
    if len(top) > 1 and role.preferred_host_api:
        pref = role.preferred_host_api
        pref_matches = [c for c in top if c[0].host_api == pref]
        if pref_matches:
            top = pref_matches

    if len(top) > 1:
        summary = "\n".join(
            f"  [{c[0].index}] {c[0].host_api} {c[0].name} (score={c[1]}, patterns={c[2]})"
            for c in top
        )
        raise DeviceResolutionError(
            f"Ambiguous match for role '{role.name}': {len(top)} candidates\n{summary}"
        )

    winner = top[0][0]
    log.debug("Resolved '%s' -> [%d] %s %s (score=%d, patterns=%d, pattern='%s')",
              role.name, winner.index, winner.host_api, winner.name,
              top[0][1], top[0][2], top[0][3])
    return winner


def validate_samplerate(
    device_index: int, samplerate: int, channels: int, kind: str,
) -> int:
    """
    Check whether *device_index* supports *samplerate*.  Return the
    validated rate, falling back to the device's native rate if needed.
    """
    try:
        if kind == "input":
            sd.check_input_settings(
                device=device_index, samplerate=samplerate,
                channels=channels, dtype="int16",
            )
        else:
            sd.check_output_settings(
                device=device_index, samplerate=samplerate,
                channels=channels, dtype="int16",
            )
        return samplerate
    except sd.PortAudioError as e:
        devices = sd.query_devices()
        fallback = int(devices[device_index]["default_samplerate"])
        log.warning(
            "Device %d (%s) doesn't support %d Hz for %s (%s). "
            "Fallback to %d Hz with resampling.",
            device_index, devices[device_index]["name"],
            samplerate, kind, e, fallback,
        )
        return fallback


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _to_resolved(d: dict) -> ResolvedDevice:
    return ResolvedDevice(
        index=d["index"],
        name=d["name"],
        host_api=d["host_api"],
        max_input_channels=d["max_input_channels"],
        max_output_channels=d["max_output_channels"],
        default_samplerate=d["default_samplerate"],
    )
