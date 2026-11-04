"""Workspace Build: the company profile, kept once per instance (core-agent AC-14).

Each person who can build has their own guide. What is about the company (its name,
website, research report and context) is kept here once, so a colleague's guide starts
from it and the next person isn't asked the company questions again. The Build page
writes it with the person's own session; ``routers/workspace_build.py`` lets only people
who can build read or write it, never an agent key.

Stored as one JSON document in the system setting ``workspace_build_company`` through
the canonical settings sink. The rules of the document are pure (standard library
only), so they can be tested without the backend.
"""

import json
from datetime import datetime, timezone
from typing import Optional

SETTING_KEY = "workspace_build_company"

_BOUNDS = {"name": 500, "website": 500, "research_md": 200_000, "context_md": 100_000}


def normalize_company(raw) -> dict:
    """The four texts the profile holds, trimmed and cut at their bounds."""
    raw = raw if isinstance(raw, dict) else {}
    return {
        field: raw[field].strip()[:bound] if isinstance(raw.get(field), str) else ""
        for field, bound in _BOUNDS.items()
    }


def stored_document(raw, updated_by: str, updated_at: str) -> dict:
    """What is written: the profile, and who kept it when."""
    return {**normalize_company(raw), "updated_by": str(updated_by or ""), "updated_at": str(updated_at or "")}


def parse_stored(value) -> Optional[dict]:
    """The stored profile, or None when there is none worth using."""
    if not isinstance(value, str) or not value:
        return None
    try:
        doc = json.loads(value)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    company = stored_document(doc, doc.get("updated_by"), doc.get("updated_at"))
    return company if company["name"] else None


def merged_with(old, raw) -> dict:
    """The new profile, keeping what was kept before wherever the new text is empty,
    so one person's guide never blanks what a colleague's had written."""
    new = normalize_company(raw)
    kept = normalize_company(old)
    return {field: new[field] or kept[field] for field in _BOUNDS}


def load_company() -> Optional[dict]:
    from database import db  # lazy, so the rules above import without the backend

    return parse_stored(db.get_setting_value(SETTING_KEY))


def save_company(raw, username: str) -> dict:
    from database import db

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    doc = stored_document(merged_with(parse_stored(db.get_setting_value(SETTING_KEY)), raw), username, now)
    db.set_setting(SETTING_KEY, json.dumps(doc))
    return doc
