"""Flags + issuer resolution for AAuth (ent#623, graduated by ent#862).

Two switches (ent#862, decision N3):

* ``aauth_enabled`` — the graduated agent identity, Flows 1–4 (well-known
  documents, outbound signing, inbound verification, card / 401
  advertisement). :func:`active_issuer` is its single reader.
* ``aauth_prototype_enabled`` — the person-token and resource-calling
  prototypes (Flows 5–6) only. They require BOTH switches:
  :func:`prototype_active_issuer`, consulted once, at
  ``routers.aauth_person.require_live``.

Every value is runtime-resolved (``system_settings`` → env → default), the
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

FLAG_SETTING = "aauth_enabled"
FLAG_ENV = "AAUTH_ENABLED"
PROTOTYPE_FLAG_SETTING = "aauth_prototype_enabled"
PROTOTYPE_FLAG_ENV = "AAUTH_PROTOTYPE_ENABLED"
ISSUER_SETTING = "aauth_issuer"
ISSUER_ENV = "AAUTH_ISSUER"
#: ent#878: the person server this instance's agents bind through (one HTTPS
#: issuer URL, e.g. ``https://person.hello.coop``). Unset → person-token
#: requests fail closed with ``person_server_unconfigured``.
PERSON_SERVER_SETTING = "aauth_person_server"
PERSON_SERVER_ENV = "AAUTH_PERSON_SERVER"
#: ent#879: the AAuth resources agents may call — parsed by
#: ``services.aauth.resource_allowlist`` (which imports this name, not a twin).
RESOURCE_ALLOWLIST_SETTING = "aauth_resource_allowlist"
RESOURCE_ALLOWLIST_ENV = "AAUTH_RESOURCE_ALLOWLIST"

#: The keys only `PUT /api/settings/aauth` may write (ent#862); the generic
#: settings catch-all refuses them on PUT and DELETE. The person server and the
#: resource allow-list are here too (cso-diff-2026-10-13-ent862 F1): they decide
#: which host mediates owner binding and where a signed agent request may go —
#: the same grant class as the issuer, so the same human-only door.
SETTINGS_ROUTE_KEYS = frozenset({
    FLAG_SETTING, PROTOTYPE_FLAG_SETTING, ISSUER_SETTING,
    PERSON_SERVER_SETTING, RESOURCE_ALLOWLIST_SETTING,
})

#: A DNS host: labels of letters/digits/hyphens, lowercase only (the issuer
#: rule), at least one dot. IP literals are not server identifiers here.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.(?![0-9]+$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def is_enabled() -> bool:
    """The graduated AAuth switch (Flows 1–4). Default OFF.

    Fail-closed: a flag that cannot be read is OFF, the same direction
    `get_issuer` takes on an unreadable issuer. The card builder (ent#180, the
    one place every card is produced) and the bearer-less 401 both consult
    this on dev's paths, and both must keep answering dev's bytes when the
    AAuth configuration is unreadable rather than 5xx — AAuth is then inert,
    never half-on (#3644).
    """
    from services.settings_service import settings_service

    try:
        return bool(settings_service._resolve_bool_flag(FLAG_SETTING, FLAG_ENV, default=False))
    except Exception:  # noqa: BLE001 — an unreadable flag means "not enabled"
        return False


def is_prototype_enabled() -> bool:
    """The prototype switch, narrowed to Flows 5–6 by ent#862. Default OFF.

    On its own it turns nothing on: :func:`prototype_active_issuer` also needs
    :func:`is_enabled`. Fail-closed exactly like the graduated switch.
    """
    from services.settings_service import settings_service

    try:
        return bool(settings_service._resolve_bool_flag(
            PROTOTYPE_FLAG_SETTING, PROTOTYPE_FLAG_ENV, default=False))
    except Exception:  # noqa: BLE001 — an unreadable flag means "not enabled"
        return False


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


def get_person_server() -> Optional[str]:
    """The configured person server issuer, or None when unset/invalid (ent#878).

    Same strict server-identifier rule as the issuer: a PS that needs repairing
    is a configuration error, not something to lower-case on the operator's
    behalf. ``system_settings`` wins over the environment, like every other
    AAuth knob.
    """
    from services.settings_service import settings_service

    try:
        stored = settings_service.get_setting(PERSON_SERVER_SETTING)
    except Exception:  # noqa: BLE001 — a settings read failure means "not configured"
        stored = None
    return normalize_issuer(stored if stored else os.getenv(PERSON_SERVER_ENV, ""))


def active_issuer() -> Optional[str]:
    """The issuer when AAuth is live (graduated flag ON and a valid issuer), else None."""
    if not is_enabled():
        return None
    return get_issuer()


def prototype_active_issuer() -> Optional[str]:
    """The issuer when the Flow 5–6 prototypes are live: BOTH switches and an issuer.

    Read only by the routes' single gate (``aauth_person.require_live``). The
    services behind those routes keep reading :func:`active_issuer`, so a call
    that got past the gate compares one issuer throughout (ent#862 review).
    """
    if not is_prototype_enabled():
        return None
    return active_issuer()


def agent_identity(agent_name: str, issuer: str) -> str:
    """``aauth:<agent-name>@<issuer-host>`` (protocol draft, Agent Identifiers)."""
    return f"aauth:{agent_name}@{issuer_host(issuer)}"
