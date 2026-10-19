"""A runtime that cannot scope a shared person's turn does not run one
(trinity-enterprise#926, ruling 2026-10-19).

On Claude Code the turn-tier hook (`/opt/trinity/hooks/turn-tier-guard.py`)
holds a turn for a person the agent is merely shared with to the paths the
Files API allows them, and gives it no shell. Gemini CLI and Codex run no Claude
Code hooks, and their own sandboxes need user namespaces the container does not
grant (see `codex_runtime._resolve_sandbox_mode`). So on those runtimes such a
turn is refused before the CLI starts, with a reason that names what to do. A
channel or public-link visitor's turn runs while the owner leaves the
`visitor_shell` guardrail on (the default); the platform's `shell` answer says.

This is the one place every turn passes on its way to a non-Claude runtime: the
backend's push to /api/task, the sync /api/chat, and a turn the pull worker
claimed (#3127). The backend decides the tier from the execution row, through
the same `turn-tier` route the hook asks, using the agent's own key.

A call with no execution id is not a platform turn (a terminal, a manual run)
and is not asked about, as in the hook.
"""
import logging
import os
from typing import Optional
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from ..state import agent_state

logger = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "http://backend:8000"
TURN_TIER_PATH = "/api/agents/{agent}/executions/{run}/turn-tier"
HTTP_TIMEOUT_SECONDS = 5.0

REFUSED = (
    "This turn is for a person the agent is shared with, and this agent runs on {runtime}, "
    "which cannot limit what such a turn may do. Only the agent's owner can run it here; "
    "the owner can switch the agent to Claude Code to let shared people use it."
)
REFUSED_VISITOR = (
    "This conversation comes from a channel or a public link, and the agent's owner has "
    "not allowed those to run on {runtime}. The owner can allow it in the agent's "
    "Guardrails settings."
)
# The class the backend and the pull sink read off the refusal (ent#926).
ERROR_CODE = "turn_refused"
COULD_NOT_CHECK = (
    "Trinity could not be reached to check whose turn this is, so it did not run on "
    "{runtime}. Try again in a moment."
)


def _agent_name() -> str:
    return agent_state.agent_name or os.getenv("AGENT_NAME") or os.getenv("TRINITY_AGENT_NAME") or ""


async def fetch_tier(agent: str, run: str) -> str:
    """The platform's answer for this turn: owner | share | unknown | unreachable."""
    return (await fetch_turn(agent, run))[0]


async def fetch_turn(agent: str, run: str):
    """(tier, may_run, kind) for this turn; may_run is False unless the platform
    says True; kind is `person` or `visitor` for a share turn, else None."""
    key = os.getenv("TRINITY_MCP_API_KEY")
    if not key or not agent:
        return "unreachable", False, None
    base = (os.getenv("TRINITY_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
    url = base + TURN_TIER_PATH.format(agent=quote(agent, safe=""), run=quote(run, safe=""))
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return "unreachable", False, None
    if response.status_code == 404:
        return "unknown", False, None
    if response.status_code != 200:
        return "unreachable", False, None
    try:
        answer = response.json()
        tier = answer.get("tier")
    except (ValueError, AttributeError):
        return "unreachable", False, None
    if tier not in ("owner", "share"):
        return "unreachable", False, None
    kind = answer.get("kind") if answer.get("kind") in ("person", "visitor") else None
    return tier, answer.get("shell") is True, kind


async def require_owner_tier(runtime: str, execution_id: Optional[str]) -> None:
    """Raise unless this turn acts for the agent's owner (or there is no platform turn)."""
    if not execution_id:
        return
    tier, may_run, kind = await fetch_turn(_agent_name(), execution_id)
    if may_run:
        return
    logger.info(f"event=turn_scope_refused runtime={runtime} execution_id={execution_id} tier={tier} kind={kind}")
    if tier == "unreachable":
        raise HTTPException(status_code=503, detail=COULD_NOT_CHECK.format(runtime=runtime))
    text = (REFUSED_VISITOR if kind == "visitor" else REFUSED).format(runtime=runtime)
    # A typed body: the pull sink reads `error_code` off it (#3012), and the
    # backend answers 403, so no caller retries a refusal that cannot change.
    raise HTTPException(status_code=403, detail={"message": text, "error_code": ERROR_CODE})
