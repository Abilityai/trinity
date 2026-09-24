"""Readiness gate on a companion's proactive brief (trinity-enterprise#689).

The enforcement half of ent#663: "until it is flipped, the companion's proactive
brief schedule stays off". The scheduler asks this service — through
`GET /api/internal/agents/{name}/brief-readiness` — before it fires a cron seat
brief, and records a `skipped` execution with the reason when the answer is no.

Two properties are load-bearing:

* **The owner's stamp is the only authority.** `x-role.status` in template.yaml
  is written by the agent itself, so it is never read as a verdict. The template
  is read only to learn whether the agent declares a role at all — a companion.
* **Every ambiguity fails open** (#1638: never mute working behaviour on an
  ambiguous read). Stamp read failed, container not running, Docker unreadable,
  template unreadable within the bound → the brief fires, and the reason is logged.

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

#: The template read happens on the scheduler's dispatch path; the scheduler's
#: own call is bounded at 5 s, so this must finish well inside it.
TEMPLATE_READ_TIMEOUT_SECONDS = 3.0


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


def decide(agent_name: str, stamp: Optional[dict], companion: Optional[bool]) -> Verdict:
    """The rule, pure. `companion` is None when it could not be established."""
    if stamp and stamp.get("status"):
        if stamp["status"] == "ready":
            return Verdict(True, "stamped ready", "stamp")
        return Verdict(False, held_reason(agent_name), "stamp")
    if companion is None:
        return Verdict(True, "readiness could not be established — firing (fail open)", "fail_open")
    if not companion:
        return Verdict(True, "not a companion (no x-role)", "not_companion")
    return Verdict(False, held_reason(agent_name), "unstamped_companion")


async def _is_companion(agent_name: str) -> Optional[bool]:
    """Whether template.yaml declares `x-role` — True / False, or None when that
    cannot be read right now (not running, Docker unreadable, slow, unparsable)."""
    from services import docker_utils
    from services.agent_client import get_agent_client
    from utils.safe_yaml import load_template_yaml

    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#689] container state for %s unreadable (%s) — fail open", agent_name, e)
        return None
    if state != "running":
        return None
    try:
        res = await asyncio.wait_for(
            get_agent_client(agent_name).read_file("template.yaml", timeout=TEMPLATE_READ_TIMEOUT_SECONDS),
            TEMPLATE_READ_TIMEOUT_SECONDS + 0.5,
        )
    except Exception as e:  # noqa: BLE001 — TimeoutError and circuit-open included
        logger.warning("[ent#689] template.yaml for %s unreadable (%r) — fail open", agent_name, e)
        return None
    if not res or not res.get("success"):
        return None
    if res.get("not_found") or res.get("content") is None:
        return False  # no template at all: not a companion
    try:
        data = load_template_yaml(res["content"])
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#689] template.yaml for %s unparsable (%s) — fail open", agent_name, e)
        return None
    if not isinstance(data, dict):
        return None
    return isinstance(data.get("x-role"), dict)


async def brief_readiness(agent_name: str) -> Verdict:
    """The verdict the scheduler asks for. Never raises."""
    from database import db

    try:
        stamp = await asyncio.to_thread(db.get_agent_role_readiness, agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#689] readiness stamp for %s unreadable (%s) — fail open", agent_name, e)
        return Verdict(True, "readiness could not be established — firing (fail open)", "fail_open")
    companion = None if stamp else await _is_companion(agent_name)
    verdict = decide(agent_name, stamp, companion)
    if verdict.basis == "fail_open":
        logger.warning("[ent#689] %s: %s", agent_name, verdict.reason)
    return verdict
