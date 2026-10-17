"""The admin settings surface for AAuth (abilityai/trinity-enterprise#862).

Reads and writes the switches and the issuer behind `GET/PUT
/api/settings/aauth`. The router holds the gates and the audit row; this module
holds the rules (Invariant #1):

* every value reports WHERE it came from (`settings` row, `env`, or the
  `default`), because the row wins in both directions and an operator looking
  at "on" needs to know whether turning it off here will stick;
* `live` is the honest answer — switched on without a valid issuer is OFF in
  effect, and the panel says so instead of showing a green toggle;
* the issuer is never repaired: a value that needs fixing is refused by name
  (`invalid_issuer`), the same strict rule the resolver applies;
* writes touch only the fields the request carried.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from services.aauth import config, resource_allowlist

class AAuthSettingsError(ValueError):
    """A refused update, with a stable reason code for the response."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def _db():
    from database import db

    return db


def _flag(setting: str, env: str) -> Dict[str, Any]:
    """The value through the ONE resolver every gate uses, plus which tier answered.

    The value is never re-derived here: a second truthy set would let the panel
    show "on" for a row the gate reads as off.
    """
    from services.settings_service import settings_service

    value = bool(settings_service._resolve_bool_flag(setting, env, default=False))
    if _db().get_setting_value(setting, None) is not None:
        source = "settings"
    elif os.getenv(env, "").strip():
        source = "env"
    else:
        source = "default"
    return {"value": value, "source": source}


def _text(setting: str, env: str) -> Dict[str, Any]:
    """A string knob with its source (row → env → unset), never re-validated here."""
    stored = _db().get_setting_value(setting, None)
    if stored:
        return {"value": stored, "source": "settings"}
    value = os.getenv(env, "").strip()
    if value:
        return {"value": value, "source": "env"}
    return {"value": "", "source": "default"}


def _readiness(issuer_valid: bool) -> Dict[str, Any]:
    """What else a call to another instance needs, beyond this switch.

    Read-only facts, each of which an operator otherwise discovers one refusal
    at a time: outbound calling has its own kill switch, and an agent can only
    call a peer through an endpoint registered with the `aauth` scheme.
    """
    from services import a2a_outbound, a2a_outbound_service

    try:
        endpoints = a2a_outbound.list_oss_endpoints()
    except Exception:  # noqa: BLE001 — a readiness hint never fails the read
        endpoints = []
    return {
        "issuer_valid": issuer_valid,
        "outbound_enabled": bool(a2a_outbound_service.is_outbound_enabled()),
        "endpoints": len(endpoints),
        "aauth_endpoints": sum(1 for e in endpoints if e.get("auth_scheme") == "aauth"),
    }


def get_state() -> Dict[str, Any]:
    enabled = _flag(config.FLAG_SETTING, config.FLAG_ENV)
    prototype = _flag(config.PROTOTYPE_FLAG_SETTING, config.PROTOTYPE_FLAG_ENV)
    issuer = _text(config.ISSUER_SETTING, config.ISSUER_ENV)
    valid = config.normalize_issuer(issuer["value"]) if issuer["value"] else None
    host = config.issuer_host(valid) if valid else ""
    person_server = _text(config.PERSON_SERVER_SETTING, config.PERSON_SERVER_ENV)
    allowlist = _text(config.RESOURCE_ALLOWLIST_SETTING, config.RESOURCE_ALLOWLIST_ENV)
    return {
        "enabled": enabled["value"],
        "enabled_source": enabled["source"],
        "issuer": issuer["value"],
        "issuer_source": issuer["source"],
        "issuer_valid": valid is not None,
        "live": bool(enabled["value"] and valid),
        "identity_example": f"aauth:<agent-name>@{host}" if host else "",
        "prototype_enabled": prototype["value"],
        "person_server": person_server["value"],
        "person_server_source": person_server["source"],
        "resource_allowlist": allowlist["value"],
        "resource_allowlist_source": allowlist["source"],
        "resource_allowlist_count": len(resource_allowlist.parse_allowlist(allowlist["value"])),
        "readiness": _readiness(valid is not None),
    }


def apply_update(*, enabled: Optional[bool] = None, issuer: Optional[str] = None,
                 prototype_enabled: Optional[bool] = None,
                 person_server: Optional[str] = None,
                 resource_allowlist_raw: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Write the fields that were sent; return ``{field: {from, to}}`` for the audit row.

    Validates everything before writing anything, so a refused value never
    leaves a half-applied update behind.
    """
    sent = (enabled, issuer, prototype_enabled, person_server, resource_allowlist_raw)
    if all(v is None for v in sent):
        raise AAuthSettingsError(
            "nothing_to_change",
            "Send at least one of `enabled`, `issuer`, `prototype_enabled`, "
            "`person_server`, `resource_allowlist`.")

    if issuer is not None:
        issuer = issuer.strip()
        if issuer and config.normalize_issuer(issuer) is None:
            raise AAuthSettingsError(
                "invalid_issuer",
                "The issuer must be https://<host> — lowercase, no port, no path, "
                "not an IP address (e.g. https://trinity.example.com).",
            )
    if person_server is not None:
        person_server = person_server.strip()
        if person_server and config.normalize_issuer(person_server) is None:
            raise AAuthSettingsError(
                "invalid_person_server",
                "The person server must be https://<host> — lowercase, no port, no "
                "path (e.g. https://person.example.com).",
            )
    if resource_allowlist_raw is not None:
        resource_allowlist_raw = resource_allowlist_raw.strip()
        # The sink reads a list it cannot parse as NOTHING; storing one would
        # look configured while allowing no resource at all.
        if resource_allowlist_raw and not resource_allowlist.parse_allowlist(resource_allowlist_raw):
            raise AAuthSettingsError(
                "invalid_resource_allowlist",
                "Every entry must be an https://<host> origin — lowercase, no port, "
                "no path — as a comma-separated list or a JSON array, at most "
                f"{resource_allowlist.MAX_ENTRIES} entries (e.g. https://senzing.example.com).",
            )

    db = _db()
    changes: Dict[str, Dict[str, Any]] = {}

    def _set_flag(field: str, setting: str, value: Optional[bool]) -> None:
        if value is None:
            return
        before = db.get_setting_value(setting, None)
        db.set_setting(setting, "true" if value else "false")
        changes[field] = {"from": before, "to": "true" if value else "false"}

    _set_flag("enabled", config.FLAG_SETTING, enabled)
    _set_flag("prototype_enabled", config.PROTOTYPE_FLAG_SETTING, prototype_enabled)

    def _set_text(field: str, setting: str, value: Optional[str]) -> None:
        if value is None:
            return
        before = db.get_setting_value(setting, None)
        if value:
            db.set_setting(setting, value)
        else:
            db.delete_setting(setting)
        changes[field] = {"from": before, "to": value or None}

    _set_text("issuer", config.ISSUER_SETTING, issuer)
    _set_text("person_server", config.PERSON_SERVER_SETTING, person_server)
    _set_text("resource_allowlist", config.RESOURCE_ALLOWLIST_SETTING, resource_allowlist_raw)

    return changes
