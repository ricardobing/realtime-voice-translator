"""
Persistent device configuration for the Voice Translator.

Stores device name substrings and resolved-device fingerprints in
config.json.  CLI and UI both read from this file.

Fingerprints allow the engine to re-find the exact same device that
was used in a previous session, even if new devices appear.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "device_fingerprints": {},
}

OBSOLETE_KEYS = {"headphones_device_index", "mic_device_name",
                 "vbcable_device_name", "loopback_device_name",
                 "headphones_device_name"}


def load_config() -> dict:
    """Return the current config dict. Creates config.json from defaults if missing."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = _merge(data)
            if merged != data:
                save_config(merged)
            return merged
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt config.json — recreating from defaults")
    return save_config(dict(DEFAULTS))


def save_config(data: dict) -> dict:
    """Write config to disk. Only stores keys present in DEFAULTS."""
    clean = {k: data.get(k, DEFAULTS[k]) for k in DEFAULTS}
    # Strip any obsolete keys that leaked through
    for k in OBSOLETE_KEYS:
        clean.pop(k, None)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    return clean


def load_fingerprints() -> dict:
    """Return {role_name: fingerprint} or {} if not yet saved."""
    cfg = load_config()
    fps = cfg.get("device_fingerprints", {})
    return fps if isinstance(fps, dict) else {}


def save_fingerprint(role_name: str, fingerprint: str) -> None:
    """Persist a single fingerprint for *role_name*."""
    cfg = load_config()
    fps = cfg.get("device_fingerprints", {})
    if not isinstance(fps, dict):
        fps = {}
    fps[role_name] = fingerprint
    cfg["device_fingerprints"] = fps
    save_config(cfg)
    log.debug("Saved fingerprint '%s' = %s", role_name, fingerprint[:60])


def _merge(data: dict) -> dict:
    """Fill missing keys from defaults, strip obsolete keys."""
    merged = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in data:
            merged[k] = data[k]
    changed = False
    for k in list(merged.keys()):
        if k in OBSOLETE_KEYS:
            del merged[k]
            changed = True
    return merged
