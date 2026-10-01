"""The per-agent budget on the objective fan-out — spelled once (ent#666, ent#676).

Reading an agent's objectives contacts its container: a directory listing plus
up to a hundred small file reads through the agent door
(`objective_join_service.read_objective_files`). Every door to that fan-out
draws on ONE bucket per agent, so the bound is on the container and not on
whichever route happened to be asked:

* `GET /api/agents/{name}/objectives` (and MCP `get_objectives` through it) —
  `enforce`: a 429 with `Retry-After`;
* the Workspace role card (`client_portal/router.py::portal_agent_role`) —
  `admit`: never raises. The card also carries the role, the readiness stamp
  and the owner's flip, and an agent polling its own objectives can empty this
  bucket; refusing the whole card would let the agent hide its owner's
  control. A refused card read leaves the objectives out and says so.

The key, the limit and the window live here so the two doors cannot drift into
two buckets that merely share a name. Which door calls which function stays in
the routers — that part is transport (Invariant #1).
"""
from __future__ import annotations

import os

from services import rate_limiter

# Its OWN, much lower ceiling than `/metrics` (ent#666). That route is
# store-only; this read drives a container, so the 240/min copied from a store
# read would let a loop pin an agent-server the platform also needs for chat.
# The role card loads once per open, so 60/min per agent clears normal traffic
# from both doors with room to spare.
OBJECTIVES_READ_RATE_LIMIT = int(os.getenv("OBJECTIVES_READ_RATE_LIMIT", "60"))
OBJECTIVES_READ_RATE_WINDOW = 60  # seconds


def key(agent_name: str) -> str:
    """The bucket. Call only with a name an access gate has already validated —
    an unvalidated path param here is a limiter-key amplification surface."""
    return f"agent_objectives_read:{agent_name}"


def enforce(agent_name: str, *, detail: str) -> None:
    """Spend one read or raise HTTP 429 with `Retry-After`."""
    rate_limiter.enforce(
        key(agent_name),
        OBJECTIVES_READ_RATE_LIMIT,
        OBJECTIVES_READ_RATE_WINDOW,
        detail=detail,
    )


def admit(agent_name: str) -> bool:
    """Spend one read if the budget allows it. A refused read costs nothing."""
    return rate_limiter.check(
        key(agent_name),
        OBJECTIVES_READ_RATE_LIMIT,
        OBJECTIVES_READ_RATE_WINDOW,
    ).allowed
