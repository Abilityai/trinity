"""The resource allow-list — the security sink between an LLM-supplied URL
and a signed server-side request (ent#879, taste T6).

``aauth_resource_allowlist`` (``system_settings`` → ``AAUTH_RESOURCE_ALLOWLIST``
→ nothing) is a JSON list, or a comma-separated string, of AAuth server
identifiers (``https://host`` — the strict ``config.normalize_issuer`` rule,
no port, no path). A call is allowed iff the resource the agent named equals a
listed identifier (origin EQUALITY, never a prefix: ``https://res.example``
does not admit ``https://res.example.evil``); the path travels separately and
is judged by the ladder.

Fail closed, every way: unset, empty, unreadable settings, malformed JSON, a
non-list, or ANY entry that is not a valid identifier → the list is EMPTY, not
partially honoured — an operator who typed one bad line should see every call
refused with ``resource_not_allowed`` and fix the line, not have the other
lines silently honoured with a typo'd origin among them.

Operator surface: the admin settings catch-all (``PUT /api/settings/
aauth_resource_allowlist``, ``require_admin`` → never agent-callable). A
Settings-UI row is deferred (DEBT_INBOX).
"""
from __future__ import annotations

import json
import os
from typing import Any, Tuple

from services.aauth import config

SETTING = "aauth_resource_allowlist"
ENV = "AAUTH_RESOURCE_ALLOWLIST"
MAX_RAW_CHARS = 16 * 1024
MAX_ENTRIES = 100


def parse_allowlist(raw: Any) -> Tuple[str, ...]:
    """The validated identifiers in ``raw``, or ``()`` if anything is wrong."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_RAW_CHARS:
        return ()
    text = raw.strip()
    if text.startswith("["):
        try:
            entries = json.loads(text)
        except ValueError:
            return ()
        if not isinstance(entries, list):
            return ()
    else:
        entries = [e.strip() for e in text.split(",")]
    if not entries or len(entries) > MAX_ENTRIES:
        return ()
    out = []
    for entry in entries:
        norm = config.normalize_issuer(entry) if isinstance(entry, str) else None
        if norm is None:
            return ()
        if norm not in out:
            out.append(norm)
    return tuple(out)


def allowed_resources() -> Tuple[str, ...]:
    """The live allow-list: ``system_settings`` wins over the environment."""
    from services.settings_service import settings_service

    try:
        stored = settings_service.get_setting(SETTING)
    except Exception:  # noqa: BLE001 — an unreadable list allows nothing
        return ()
    return parse_allowlist(stored if stored else os.getenv(ENV, ""))


def is_allowed(resource: Any) -> bool:
    """True iff ``resource`` is exactly a listed identifier."""
    norm = config.normalize_issuer(resource) if isinstance(resource, str) else None
    return bool(norm) and norm in allowed_resources()
