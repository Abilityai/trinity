"""Who the Access tab's add field can suggest (trinity-enterprise#450).

The people offered are by definition not on the agent yet, so unlike the tagging
picker this can't be limited to people the agent already reaches. An owner could
find accounts on the instance by trying prefixes. These bounds slow that down and
don't prevent it: a minimum query, a row cap, a per-person rate limit, and no role
for anyone but an admin.

The match, the exclusions and the bounds are the one people search's
(`people_search_service`, trinity-enterprise#1153); this module supplies only the
scope (not the owner, not already on the roster) and its own limits.
"""
from typing import List

from database import db
from models import AccessCandidate
from services import people_search_service

MIN_QUERY = 2
MAX_QUERY = 64
LIMIT = 8
RATE_LIMIT = 60             # lookups a minute, per person


# The seam's own exception; the router turns it into the named 429.
CandidateLookupLimited = people_search_service.PeopleSearchLimited


def suggest(agent_name: str, query: str, current_user) -> List[AccessCandidate]:
    is_admin = current_user.role == "admin"
    rows = people_search_service.search(
        db.people_scope_share_candidates(agent_name), (query or "").strip()[:MAX_QUERY],
        requester_email=current_user.email, rate_key=current_user.username,
        rate_limit=RATE_LIMIT, min_query=MIN_QUERY, limit=LIMIT)
    return [
        AccessCandidate(email=row["email"], label=row["label"], role=row["role"] if is_admin else None)
        for row in rows
    ]
