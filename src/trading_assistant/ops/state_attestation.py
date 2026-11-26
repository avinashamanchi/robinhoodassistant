"""Domain-separated authenticated digests for bounded private observations."""
from __future__ import annotations

from datetime import datetime
import hashlib
import hmac
import json

from ..security.crypto import SensitiveFieldRef


def _digest(value):
    body = {k: v for k, v in value.items() if k != "attestation"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > 240_000:
        raise ValueError("observation exceeds bound")
    return hashlib.sha256(raw).hexdigest()


def seal_mapping(value, cipher, ref):
    result = {k: v for k, v in value.items() if k != "attestation"}
    result["attestation"] = cipher.encrypt(_digest(result), ref)
    return result


def verify_mapping(value, cipher, ref):
    try:
        return cipher is not None and hmac.compare_digest(cipher.decrypt(value["attestation"], ref), _digest(value))
    except Exception:
        return False


DOCTOR_REF = SensitiveFieldRef("operational_doctor", "health", "current_observation", 1)


def seal_doctor_health(health, cipher, *, previous_verified):
    publication = {"schema_version": 1, "checked_at": health["checked_at"], "checks": health["checks"],
                   "stale_observations": health.get("stale_observations", 0), "discarded_events": health.get("discarded_events", 0),
                   "legacy_history_unverified": health.get("legacy_history_unverified", False), "previous_verified": bool(previous_verified)}
    return {**health, "doctor_publication": seal_mapping(publication, cipher, DOCTOR_REF)}


def verify_doctor_health(health, cipher):
    from .incident_catalog import COMPONENTS
    try:
        pub = health["doctor_publication"]
        if not isinstance(pub, dict) or set(pub) != {"schema_version", "checked_at", "checks", "stale_observations", "discarded_events", "legacy_history_unverified", "previous_verified", "attestation"}:
            return False
        if type(pub["schema_version"]) is not int or pub["schema_version"] != 1 or type(health.get("schema_version")) is not int or health["schema_version"] != 1 or type(pub["previous_verified"]) is not bool:
            return False
        if type(pub["stale_observations"]) is not int or not 0 <= pub["stale_observations"] <= 1_000_000_000 or type(pub["discarded_events"]) is not int or pub["discarded_events"] < 0 or type(pub["legacy_history_unverified"]) is not bool:
            return False
        instant = datetime.fromisoformat(pub["checked_at"])
        checks = pub["checks"]
        if instant.utcoffset() is None or not isinstance(checks, list) or len(checks) > 32:
            return False
        if any(not isinstance(c, dict) or set(c) != {"component", "code", "status"} or c["component"] not in COMPONENTS or c["code"] not in COMPONENTS[c["component"]] or c["status"] not in {"pass", "warn", "fail", "unknown"} for c in checks):
            return False
        if any(health.get(k) != pub[k] for k in ("checked_at", "checks", "stale_observations", "discarded_events", "legacy_history_unverified")):
            return False
        return verify_mapping(pub, cipher, DOCTOR_REF)
    except Exception:
        return False
