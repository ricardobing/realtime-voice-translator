"""
Simple persistent config for the Voice Translator.

Stores device indices, rates, and preferences in config.json.
No fingerprint resolution — devices are identified by index + name + host API.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "devices": {
        "mic":        {"index": 11, "name": "Microfono (USB PnP Sound Device", "hostapi": "MME", "rate": 44100},
        "vbcable":    {"index": 14, "name": "CABLE Input (VB-Audio Virtual C",  "hostapi": "MME", "rate": 44100},
        "loopback":   {"index": 1,  "name": "Voicemeeter Out B1",               "hostapi": "MME", "rate": 44100},
        "headphones": {"index": 13, "name": "Altavoces (USB PnP Sound Device",  "hostapi": "MME", "rate": 48000},
    },
    "smart_muting_enabled": False,
    "vad_aggressiveness": 2,
    "source_language": "es",
    "target_language_a": "en",
    "target_language_b": "es",
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("config.json corrupt — using defaults: %s", e)
            saved = {}
        merged = {**DEFAULT_CONFIG, **saved}
        merged["devices"] = deep_merge_device(DEFAULT_CONFIG["devices"], saved.get("devices", {}))
        return merged
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_device_index(config: dict, role: str) -> int:
    return config["devices"][role]["index"]


def get_device_rate(config: dict, role: str) -> int:
    return config["devices"][role]["rate"]


def deep_merge_device(base: dict, override: dict) -> dict:
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged
