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

A schedule's runs are rated by its creator, and nobody is chatting in them
(trinity#4281). For such a run the answer also carries why (`cause`, the
schedule's name, its creator, a `remedy` code), and the refusal names those
instead of a shared person, keyed on `cause`, never on `kind`. Switching the
runtime would not help that run, so the text does not suggest it. An answer
with no `cause` gets the text it always got.
"""
import logging
import os
import re
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

# A run refused because of its schedule (trinity#4281). The same facts as the
# hook's copy (`hooks/turn-tier-guard.py`), which a test pins. The name and the
# label are text a share holder chose, so they are bounded before use.
SCHEDULE_UNREADABLE = "schedule_unreadable"
CODE_SHAPE = re.compile(r"[a-z0-9_]{1,64}")
SCHEDULE_LABEL_MAX = 80
CREATOR_LABEL_MAX = 64
SCHEDULE_REMEDIES = {
    "take_over": "the agent's owner or an admin to take it over",
    "recreate": "the agent's owner or an admin to re-create it as their own and disable this one",
    "repair_agent_owner": "an admin to restore this agent's owner record",
}
SCHEDULE_REMEDY_GENERIC = "the agent's owner or an admin to take it over or re-create it as their own"
REFUSED_SCHEDULE = (
    "This run is {name}, not a chat. {runs_as}, and this agent runs on {runtime}, which cannot "
    "limit what such a turn may do, so it did not run. The schedule needs {remedy}."
)
REFUSED_SCHEDULE_UNREADABLE = (
    "This run is a scheduled task whose schedule can no longer be read (it was deleted or changed "
    "after the run was queued), not a chat, so it does not have the agent's owner's access. This "
    "agent runs on {runtime}, which cannot limit what such a turn may do, so it did not run."
)
COULD_NOT_CHECK = (
    "Trinity could not be reached to check whose turn this is, so it did not run on "
    "{runtime}. Try again in a moment."
)


def _agent_name() -> str:
    return agent_state.agent_name or os.getenv("AGENT_NAME") or os.getenv("TRINITY_AGENT_NAME") or ""


async def fetch_tier(agent: str, run: str) -> str:
    """The platform's answer for this turn: owner | share | unknown | unreachable."""
    return (await fetch_turn(agent, run))[0]


def bounded_label(value, limit: int) -> Optional[str]:
    """`value` with control, format and separator characters replaced, whitespace
    runs folded to one space, cut to `limit` characters; None when nothing is left."""
    if not isinstance(value, str):
        return None
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    return text[:limit].rstrip() or None


def schedule_detail(answer: dict) -> Optional[dict]:
    """Why a schedule-backed shared turn is not the owner's (trinity#4281):
    `{cause, schedule, creator, remedy}` from the platform's answer, or None when
    it names no cause. The labels are bounded, and an email loses its domain."""
    cause = answer.get("cause")
    if not isinstance(cause, str) or not CODE_SHAPE.fullmatch(cause):
        return None
    creator = answer.get("creator")
    if isinstance(creator, str) and "@" in creator:
        creator = creator.split("@", 1)[0]
    remedy = answer.get("remedy")
    return {"cause": cause,
            "schedule": bounded_label(answer.get("schedule"), SCHEDULE_LABEL_MAX),
            "creator": bounded_label(creator, CREATOR_LABEL_MAX),
            "remedy": remedy if isinstance(remedy, str) and remedy in SCHEDULE_REMEDIES else None}


def schedule_runs_as(cause: str, creator: Optional[str]) -> str:
    """Whose access a schedule's runs have, by the answer's cause."""
    named = f", {creator}" if creator else ""
    if cause == "creator_not_owner_tier":
        return f"It runs with the access of the person who created it{named}, who is not this agent's owner or an admin"
    if cause == "creator_missing":
        return "It runs with the access of the person who created it, an account that no longer exists"
    if cause == "agent_has_no_owner":
        return f"This agent has no owner on record, so it runs with the access of the person who created it{named}"
    return f"It runs with the access of the person who created it{named}, not with an owner's"


def schedule_refusal(runtime: str, detail: dict) -> str:
    """The refusal for a run refused because of its schedule."""
    if detail["cause"] == SCHEDULE_UNREADABLE:
        return REFUSED_SCHEDULE_UNREADABLE.format(runtime=runtime)
    name = f"the scheduled task '{detail['schedule']}'" if detail["schedule"] else "a scheduled task"
    return REFUSED_SCHEDULE.format(name=name, runs_as=schedule_runs_as(detail["cause"], detail["creator"]),
                                   runtime=runtime,
                                   remedy=SCHEDULE_REMEDIES.get(detail["remedy"], SCHEDULE_REMEDY_GENERIC))


async def fetch_turn(agent: str, run: str):
    """(tier, may_run, kind, schedule) for this turn; may_run is False unless the
    platform says True; kind is `person` or `visitor` for a share turn, else
    None; schedule is `schedule_detail` for a share turn, else None."""
    key = os.getenv("TRINITY_MCP_API_KEY")
    if not key or not agent:
        return "unreachable", False, None, None
    base = (os.getenv("TRINITY_BACKEND_URL") or DEFAULT_BACKEND_URL).rstrip("/")
    url = base + TURN_TIER_PATH.format(agent=quote(agent, safe=""), run=quote(run, safe=""))
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError:
        return "unreachable", False, None, None
    if response.status_code == 404:
        return "unknown", False, None, None
    if response.status_code != 200:
        return "unreachable", False, None, None
    try:
        answer = response.json()
        tier = answer.get("tier")
    except (ValueError, AttributeError):
        return "unreachable", False, None, None
    if tier not in ("owner", "share"):
        return "unreachable", False, None, None
    kind = answer.get("kind") if answer.get("kind") in ("person", "visitor") else None
    schedule = schedule_detail(answer) if tier == "share" else None
    return tier, answer.get("shell") is True, kind, schedule


async def require_owner_tier(runtime: str, execution_id: Optional[str]) -> None:
    """Raise unless this turn acts for the agent's owner (or there is no platform turn)."""
    if not execution_id:
        return
    tier, may_run, kind, schedule = await fetch_turn(_agent_name(), execution_id)
    if may_run:
        return
    cause = schedule["cause"] if schedule else None
    logger.info(f"event=turn_scope_refused runtime={runtime} execution_id={execution_id} tier={tier} "
                f"kind={kind} cause={cause}")
    if tier == "unreachable":
        raise HTTPException(status_code=503, detail=COULD_NOT_CHECK.format(runtime=runtime))
    if schedule:
        text = schedule_refusal(runtime, schedule)
    else:
        text = (REFUSED_VISITOR if kind == "visitor" else REFUSED).format(runtime=runtime)
    # A typed body: the pull sink reads `error_code` off it (#3012), and the
    # backend answers 403, so no caller retries a refusal that cannot change.
    raise HTTPException(status_code=403, detail={"message": text, "error_code": ERROR_CODE})
