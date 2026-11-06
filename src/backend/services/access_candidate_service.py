"""Who the Access tab's add field can suggest (trinity-enterprise#450).

The people offered are by definition not on the agent yet, so unlike the tagging
picker this can't be limited to people the agent already reaches. An owner could
find accounts on the instance by trying prefixes. These bounds slow that down and
don't prevent it: a minimum query, a row cap, a per-person rate limit, and no role
for anyone but an admin.
"""
from typing import List

from database import db
from models import AccessCandidate
from services import rate_limiter

MIN_QUERY = 2
MAX_QUERY = 64
LIMIT = 8
RATE_LIMIT = 60
WINDOW_SECONDS = 60


class CandidateLookupLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__("Too many lookups")
        self.retry_after = retry_after


def suggest(agent_name: str, query: str, current_user) -> List[AccessCandidate]:
    q = (query or "").strip()[:MAX_QUERY]
    if len(q) < MIN_QUERY:
        return []
    result = rate_limiter.check(f"access-candidates:{current_user.username}", RATE_LIMIT, WINDOW_SECONDS)
    if not result.allowed:
        raise CandidateLookupLimited(result.retry_after)
    is_admin = current_user.role == "admin"
    rows = db.list_share_candidates(agent_name, q, exclude_email=current_user.email, limit=LIMIT)
    return [
        AccessCandidate(
            email=row["email"].strip().lower(),
            label=row.get("name") or row["username"],
            role=row["role"] if is_admin else None,
        )
        for row in rows
    ]
