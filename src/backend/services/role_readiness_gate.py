"""Readiness gate on a companion's proactive brief (trinity-enterprise#689).

The enforcement half of ent#663: "until it is flipped, the companion's proactive
brief schedule stays off". The scheduler asks this service — through
`GET /api/internal/agents/{name}/brief-readiness` — before it fires a cron seat
brief, and records a `skipped` execution with the reason when the answer is no.

Two properties are load-bearing:

* **The owner's stamp is the only authority.** `x-role.status` in template.yaml
  is written by the agent itself, so it is never read as a verdict. Once an owner
  has stamped `calibrating`, nothing the agent writes releases the brief.
* **Every ambiguity fails open** (#1638: never mute working behaviour on an
  ambiguous read). Stamp read failed, no seat lookup, a lookup error → the brief
  fires, and the reason is logged.

**Who is a companion (trinity-enterprise#813, ruling 2026-10-06).** Asked only
for an UNSTAMPED agent: an agent whose primary is assigned and holds a seat —
`assignment_provider.resolve_seat` answers `serves`. The template is no longer
read for it (`x-role` stopped being truth), so an agent can no longer take itself
out of scope by editing its own files: the seat is admin-written. An agent that
holds a seat ITSELF is an autonomous player, not a companion; no primary, no
provider or a lookup error all read as "not a companion", so the brief fires as
it did before.

Grandfathering is not here: a one-time data seed (`role_readiness_rollout_seed`,
both migration tracks) stamped every agent with a live seat brief at deploy.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: `agent_role_readiness.changed_by` of the one-time rollout seed. Not an email:
#: the role card reads it and says "carried over", never "by <owner>".
ROLLOUT_CHANGED_BY = "rollout:ent#689"


class AgentNotFound(Exception):
    """No ownership row for the agent — the route answers 404 (the scheduler fails open)."""


@dataclass(frozen=True)
class Verdict:
    fire: bool
    reason: str
    #: `stamp` | `not_companion` | `unstamped_companion` | `fail_open`
    basis: str


def held_reason(agent_name: str) -> str:
    return (
        f"Held: {agent_name} is a calibrating companion — its proactive brief runs "
        "once its owner marks it ready (Workspace › Info › Role)."
    )


def decide(agent_name: str, stamp: Optional[dict], companion: Optional[bool],
           why: str = "") -> Verdict:
    """The rule, pure. `companion` is None when it could not be established;
    `why` is the seat reason behind a "not a companion" (#813)."""
    if stamp and stamp.get("status"):
        if stamp["status"] == "ready":
            return Verdict(True, "stamped ready", "stamp")
        return Verdict(False, held_reason(agent_name), "stamp")
    if companion is None:
        return Verdict(True, "readiness could not be established — firing (fail open)", "fail_open")
    if not companion:
        return Verdict(True, f"not a companion ({why or 'no seat'})", "not_companion")
    return Verdict(False, held_reason(agent_name), "unstamped_companion")


def is_seat_delivery_schedule(enabled, deliver_to_workspace_email) -> bool:
    """A live schedule that delivers into someone's Workspace — the seat brief the
    gate holds. Shared by the role card and the agents list (PR #3038)."""
    return bool(enabled) and bool((deliver_to_workspace_email or "").strip())


def brief_is_held(stamp_status: Optional[str], autonomy_enabled, has_seat_delivery_schedule) -> bool:
    """Whether "its scheduled brief is paused until it is marked ready" is TRUE.

    Not `ready`, a seat-delivery schedule to hold, and autonomy on — with
    autonomy off every schedule is stopped before readiness is asked, so the
    sentence would promise a flip that starts nothing. The role card and the
    agents list both say it from this one predicate, so they cannot disagree.
    """
    if stamp_status == "ready":
        return False
    return bool(autonomy_enabled) and bool(has_seat_delivery_schedule)


def briefs_held_for_list(agents: list, readiness_by_name: dict) -> set:
    """The names on `GET /api/agents` whose brief is held — one batched read.

    Only a `calibrating` STAMP can be held here: the list never reads a template,
    so an unstamped agent (companion or not) carries no badge and no claim.
    Autonomy comes from the rows themselves. Fail-soft like the role card: an
    unreadable schedule list claims no pause.
    """
    from database import db

    candidates = [
        a.get("name") for a in agents
        if a.get("name")
        and (readiness_by_name.get(a.get("name")) or {}).get("status") == "calibrating"
        and a.get("autonomy_enabled")
    ]
    if not candidates:
        return set()
    try:
        rows = db.get_workspace_delivery_schedules_for_agents(candidates)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#527] schedule read for the agents list failed: %s", e)
        return set()
    seated = {
        r["agent_name"] for r in rows
        if is_seat_delivery_schedule(r.get("enabled"), r.get("deliver_to_workspace_email"))
    }
    return {
        n for n in candidates
        if brief_is_held(readiness_by_name[n]["status"], True, n in seated)
    }


def companion_from_seat(agent_name: str) -> tuple:
    """(is it a companion, why) from the seat on record (trinity-enterprise#813).

    A companion is an agent whose primary is assigned and holds a seat. Every
    other answer — the agent holds a seat itself, no primary with a seat, no
    seat lookup, a lookup error — is "not a companion", stated with its reason.
    Never raises (`resolve_seat` never does).
    """
    from services.assignment_provider import resolve_seat

    seat = resolve_seat(agent_name)
    if seat["reason"] != "provider":
        return False, f"seat lookup unavailable: {seat['reason']}"
    if seat["case"] == "serves":
        return True, f"its primary holds the seat {seat['role_id']}"
    if seat["case"] == "holds":
        return False, f"it holds the seat {seat['role_id']} itself"
    return False, "no primary with a seat"


async def brief_readiness(agent_name: str) -> Verdict:
    """The verdict the scheduler asks for. Raises only `AgentNotFound`."""
    from database import db

    if not await asyncio.to_thread(db.get_agent_owner, agent_name):
        raise AgentNotFound(agent_name)
    try:
        stamp = await asyncio.to_thread(db.get_agent_role_readiness, agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#689] readiness stamp for %s unreadable (%s) — fail open", agent_name, e)
        return Verdict(True, "readiness could not be established — firing (fail open)", "fail_open")
    companion, why = (None, "") if stamp else companion_from_seat(agent_name)
    verdict = decide(agent_name, stamp, companion, why)
    if verdict.basis == "fail_open":
        logger.warning("[ent#689] %s: %s", agent_name, verdict.reason)
    elif not stamp:
        # #813: the companion decision, with the reason it took.
        logger.info("[ent#813] %s: %s → %s", agent_name, why,
                    "held" if not verdict.fire else "fires")
    return verdict
