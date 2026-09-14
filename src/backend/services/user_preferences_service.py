"""Per-user UI preferences — policy layer (trinity-enterprise#413, OSS-core).

Sits between ``routers/users.py`` and ``db/user_preferences.py`` (Invariant
#1). Owns the three things the DB layer deliberately does not: which keys
exist, what a value may be, and how big it may get. Every refusal is a
``PreferenceError`` the router maps 1:1 onto an HTTP status, so a caller
always learns *which* rule it broke.

Adding a per-user UI preference is one line in ``PREFERENCE_KEYS`` — never a
new table (the record is generic by design) and never a widening of the
value shape beyond "a JSON object": the client owns the schema of each value
and heals what it reads (``utils/gridLayout.js::normalizeLayout``), so a
malformed value is self-harm only.
"""

from __future__ import annotations

import json
from typing import Dict, Optional

from database import db

# The allowlist. A key outside it is a 404 — not enumerable beyond these, and
# a typo cannot create a row. Order is documentation, not semantics.
PREFERENCE_KEYS = frozenset({
    "grid_layout",   # Dashboard Grid tile positions: {agent|widget:* → {c, r}}
    "grid_widgets",  # Tiles ▾ override map: {widgetId → bool}
    "grid_org",      # Org overlay toggles: {zones: bool, lines: bool}
    # ent#403 — the Workspace composer's model choice, {agent_name → model-id}.
    # A SERVER record and not browser storage, deliberately: the Workspace's own
    # identity term (`clientPortal.clientEmail`) starts null and is filled from a
    # network response, so a browser key built on it reads `anon` on every reload
    # and writes under the email a moment later (`composables/useColumnResize.js`
    # — caught live). Here the scope is per (user, agent) BY CONSTRUCTION: the
    # server knows who is asking, so "never leaks between agents or between
    # clients" is a property of the storage rather than of a key.
    "workspace_model",
})

# A DoS bound, not a feature limit: a 500-agent layout serializes to ~15 KB.
MAX_VALUE_BYTES = 256 * 1024


class PreferenceError(Exception):
    """A refusal the router turns into an HTTP status, 1:1 (Invariant #1)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class PreferenceConflict(PreferenceError):
    """The write's stated base did not match — carries the live record."""

    def __init__(self, current: Optional[Dict]):
        super().__init__(409, "Preference changed since it was read")
        self.current = current


def validate_key(key: str) -> str:
    if key not in PREFERENCE_KEYS:
        raise PreferenceError(404, f"Unknown preference key '{key}'")
    return key


def serialize_value(value) -> str:
    """The canonical stored form, refused when it is not an object or too big.

    Measured on the UTF-8 bytes of the compact ``json.dumps`` the DB will
    hold, not on the request body — a pretty-printed body and a compact one
    are the same preference and must meet the same cap.
    """
    if not isinstance(value, dict):
        raise PreferenceError(422, "Preference value must be a JSON object")
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
        raise PreferenceError(
            413, f"Preference value exceeds {MAX_VALUE_BYTES} bytes"
        )
    return encoded


def _decode(record: Dict) -> Dict:
    """A stored row as the API record. An undecodable value reads as ``{}``
    with its real ``updated_at`` — the client heals an empty object into a
    default, and a row that 500s a whole dashboard is worse than one
    preference reading empty."""
    try:
        value = json.loads(record["value_json"])
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {"key": record["key"], "value": value, "updated_at": record["updated_at"]}


def get_all(user_id: int) -> Dict[str, Dict]:
    """Every stored preference of one user, keyed by preference key."""
    return {k: _decode(r) for k, r in db.get_user_preferences(user_id).items()}


def put(user_id: int, key: str, value, base_updated_at: Optional[str]) -> Dict:
    """Conditionally store one preference.

    ``base_updated_at`` — ``None``: insert only; a string: replace only if the
    row still carries it. A failed condition is a 409 whose detail carries
    the LIVE record, so the caller can adopt it or retry against its
    ``updated_at`` without a second round trip.
    """
    validate_key(key)
    encoded = serialize_value(value)
    stored = db.set_user_preference(user_id, key, encoded, base_updated_at=base_updated_at)
    if stored is None:
        live = db.get_user_preference(user_id, key)
        raise PreferenceConflict(_decode(live) if live else None)
    return _decode(stored)


def delete(user_id: int, key: str) -> bool:
    validate_key(key)
    return db.delete_user_preference(user_id, key)

