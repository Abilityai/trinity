"""Flag + issuer resolution for the AAuth prototype (ent#623).

Both are runtime-resolved (``system_settings`` → env → default), the
``a2a_outbound_enabled`` shape, so an admin flip needs no restart. The issuer is
this instance's AAuth server identifier and therefore the domain of every agent
identity it asserts: ``https://<host>`` — https, host only, no port, no path, no
trailing slash, lowercase (protocol draft, Server Identifiers).

Flag ON without a valid issuer means AAuth is **inert**, never half-on: the
well-known documents 404, signing is refused and inbound AAuth is refused.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlsplit

FLAG_SETTING = "aauth_prototype_enabled"
FLAG_ENV = "AAUTH_PROTOTYPE_ENABLED"
ISSUER_SETTING = "aauth_issuer"
ISSUER_ENV = "AAUTH_ISSUER"

#: A DNS host: labels of letters/digits/hyphens, lowercase only (the issuer
#: rule), at least one dot. IP literals are not server identifiers here.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.(?![0-9]+$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def is_enabled() -> bool:
    """The prototype flag. Default OFF."""
    from services.settings_service import settings_service

    return settings_service._resolve_bool_flag(FLAG_SETTING, FLAG_ENV, default=False)


def normalize_issuer(value: Optional[str]) -> Optional[str]:
    """Return ``value`` if it is a valid AAuth server identifier, else None.

    Deliberately strict and non-repairing: an issuer that needs fixing is a
    configuration error, and silently lower-casing or stripping a port would
    make this instance assert an identity its operator did not type.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None:
        return None
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        return None
    host = parts.hostname or ""
    if value != f"https://{host}" or not _HOST_RE.match(host):
        return None
    return value


def issuer_host(issuer: str) -> str:
    return urlsplit(issuer).hostname or ""


def get_issuer() -> Optional[str]:
    """This instance's issuer, or None when unset/invalid."""
    from services.settings_service import settings_service

    try:
        stored = settings_service.get_setting(ISSUER_SETTING)
    except Exception:  # noqa: BLE001 — a settings read failure means "not configured"
        stored = None
    return normalize_issuer(stored if stored else os.getenv(ISSUER_ENV, ""))


def active_issuer() -> Optional[str]:
    """The issuer when AAuth is live (flag ON and a valid issuer), else None."""
    if not is_enabled():
        return None
    return get_issuer()


def agent_identity(agent_name: str, issuer: str) -> str:
    """``aauth:<agent-name>@<issuer-host>`` (protocol draft, Agent Identifiers)."""
    return f"aauth:{agent_name}@{issuer_host(issuer)}"
