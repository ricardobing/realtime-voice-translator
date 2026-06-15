"""
Pre-flight checks run before the engine starts.

Validates API key connectivity, Voicemeeter availability, and resolves
all four required audio devices via fingerprint-or-search.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from devices import (
    ROLES, resolve_by_role, resolve_by_fingerprint,
    ResolvedDevice, list_all_devices, DeviceResolutionError,
)

log = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    ok: bool
    api_key_valid: bool
    voicemeeter_running: bool
    devices_resolved: dict  # role_name -> ResolvedDevice | None
    errors: List[str] = field(default_factory=list)


def check_api_key(api_key: str) -> tuple[bool, Optional[str]]:
    """Validate the Gemini API key with a lightweight call (2 s timeout)."""
    import socket
    from google import genai

    try:
        client = genai.Client(api_key=api_key)
        saved = socket.getdefaulttimeout()
        socket.setdefaulttimeout(2.0)
        list(client.models.list())
        return True, None
    except Exception as e:
        msg = str(e)
        if "API_KEY" in msg.upper() or "401" in msg:
            return False, "API key invalida o expirada"
        if "BILLING" in msg.upper() or "403" in msg:
            return False, "Billing no habilitado en Google Cloud"
        return False, f"Error validando API key: {msg[:120]}"
    finally:
        socket.setdefaulttimeout(None)


def check_voicemeeter_running() -> bool:
    """Return True if any Voicemeeter-related device is present."""
    for d in list_all_devices():
        if "VoiceMeeter" in d["name"] or "VAIO" in d["name"]:
            return True
    return False


def run_preflight(api_key: str, saved_fingerprints: dict) -> PreflightResult:
    """
    Run all pre-flight checks.

    *saved_fingerprints*: dict of role_name -> fingerprint from config.
    Resolves each role first by fingerprint, then by role search.
    """
    result = PreflightResult(
        ok=False, api_key_valid=False, voicemeeter_running=False,
        devices_resolved={},
    )

    # 1. API key
    ok_key, err = check_api_key(api_key)
    result.api_key_valid = ok_key
    if not ok_key:
        result.errors.append(f"API: {err}")
    else:
        log.info("Preflight: API key valid")

    # 2. Voicemeeter
    result.voicemeeter_running = check_voicemeeter_running()
    if result.voicemeeter_running:
        log.info("Preflight: Voicemeeter detected")
    else:
        result.errors.append("Voicemeeter Banana no esta corriendo")
        log.warning("Preflight: Voicemeeter NOT detected")

    # 3. Devices
    for role_name, role in ROLES.items():
        try:
            fp = saved_fingerprints.get(role_name)
            rd: Optional[ResolvedDevice] = None
            if fp:
                rd = resolve_by_fingerprint(fp)
                if rd is None:
                    log.info("Fingerprint for '%s' no longer valid — fallback to search", role_name)
            if rd is None:
                rd = resolve_by_role(role)
            result.devices_resolved[role_name] = rd
            log.info("Preflight: %-18s -> [%d] %s %s",
                     role_name, rd.index, rd.host_api, rd.name)
        except DeviceResolutionError as e:
            result.devices_resolved[role_name] = None
            msg = f"Device '{role_name}': {e}"
            result.errors.append(msg)
            log.error("Preflight: %s", msg)
        except Exception as e:
            result.devices_resolved[role_name] = None
            msg = f"Device '{role_name}': unexpected {e}"
            result.errors.append(msg)
            log.exception("Preflight: %s", msg)

    result.ok = (
        result.api_key_valid
        and result.voicemeeter_running
        and all(v is not None for v in result.devices_resolved.values())
    )
    return result
