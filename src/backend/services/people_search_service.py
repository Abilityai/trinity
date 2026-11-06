"""One people search behind every people picker (trinity-enterprise#1153).

A picker — the room's `@`, the Workspace chat's `@`, the Access tab's add field,
and the form fields that follow — says only WHO MAY APPEAR, as a scope the db
layer builds (`db.people_scope_*`). Everything else is applied here, once:

* the match (a prefix of the email, the username or a word of the name) and the
  exclusions (suspended, no email, the person asking) — in `db.search_people`;
* a query of at least `min_query` characters, or nothing is asked and nothing is
  spent; a query cut to `MAX_QUERY`;
* at most `LIMIT` rows;
* a per-person rate limit, budgeted per kind of picker.

So a picker is never a directory: an empty query lists no one, and an owner
probing prefixes is slowed down. A route that returns people without reaching
`search` fails `tests/unit/test_ent1153_people_search_seam.py`.

Rows carry `email`, `username`, `label` and `role`; each caller decides which of
them its reader may see (role is admin-only on the Access tab, ent#450).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from database import db
from services import rate_limiter

LIMIT = 8
MAX_QUERY = 64
WINDOW_SECONDS = 60


class PeopleSearchLimited(Exception):
    """Too many lookups by one person in one kind of picker."""

    def __init__(self, retry_after: int):
        super().__init__("Too many lookups")
        self.retry_after = retry_after


def person_label(user: Optional[Dict]) -> str:
    """A person's display name: `name`, else the username (often the email)."""
    if not user:
        return ""
    return " ".join(str(user.get("name") or "").split()) or str(user.get("username") or "")


def search(scope, query: str, *, requester_email: Optional[str], rate_key: str, rate_limit: int,
           min_query: int = 1, limit: int = LIMIT) -> List[Dict]:
    """People in `scope` matching `query`, for the person `rate_key`.

    Raises `PeopleSearchLimited` past `rate_limit` lookups a minute; the route
    turns it into a named 429."""
    q = (query or "").strip()[:MAX_QUERY]
    if len(q) < max(1, min_query) or scope.empty:
        return []
    result = rate_limiter.check(f"people-search:{scope.kind}:{rate_key}", rate_limit, WINDOW_SECONDS)
    if not result.allowed:
        raise PeopleSearchLimited(result.retry_after)
    rows = db.search_people(scope, q, exclude_email=requester_email, limit=limit)
    return [
        {
            "email": (r.get("email") or "").strip().lower(),
            "username": r.get("username"),
            "label": person_label(r),
            "role": r.get("role"),
        }
        for r in rows
    ]


RATE_LIMITED_DETAIL = {"code": "rate_limited", "message": "Too many lookups. Try again shortly."}
