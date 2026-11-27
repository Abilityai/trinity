"""Fleet check (abilityai/trinity-enterprise#1176): which running agents are not
applying Trinity's managed hooks.

Trinity's guardrail hooks live in file-based managed settings in the agent
image. Claude Code skips that source when it holds an organization's
server-managed settings with any policy key (its cache is
`~/.claude/remote-settings.json`), so an agent can run with none of the hooks.
The agent server reads that cache and reports `guardrails_registration:
"skipped"` on `/health` (`agent_server/services/managed_hooks.py`); it also
refuses the turns whose rule lives in a hook while that holds. This module
READS and REPORTS: it files or ends ONE platform alert,
`managed_hooks_skipped:fleet`, on the system agent's queue, naming every
running agent that reads `skipped`.

It runs inside the fleet-image cycle (`fleet_image_service.FleetImageCheckService
.run_cycle`): same leader lease, same cadence, no loop of its own.

**"Could not check" is its own answer** (the #4483 rule): Docker unreadable
leaves the alert as it is. An agent whose `/health` gives no answer stays
named, and holds the alert, only if the alert's pending row names it; one
silent agent the alert never named must not keep a fixed fleet's alert open
forever.

**The pending row is the only memory (#4778).** Who is named is read from the
alert's own row (`context.agents`) on every pass, never kept in this process:
the pass runs in whichever worker holds the fleet cycle's lease (in every
worker when the lease fails open), and a restart or a lease move must not
change the answer. With no pending row nobody is named, so a silent agent holds
nothing. A row whose list cannot be read, or a read that fails, is "could not
tell": any silent agent then holds the alert. Stated limits: the list stores
the first 500 names (`_CONTEXT_MAX_AGENTS`), so an agent named beyond that
cannot hold the alert; a pass that only holds does not rewrite the row, so an
agent named there keeps holding while silent even if an earlier pass read it
clean; and after a person ends the alert, a silent agent is named again only
once it reads `skipped`.

The system agent is checked (no other alert covers it). Ephemeral agents are
left out: they come and go, so they would churn the named set; their turns are
still refused by the agent server.

Stdlib-only at module level: Docker, the database, HTTP and the alert seam are
imported inside the functions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

ALERT_KIND = "managed_hooks_skipped"
ALERT_KEY = "fleet"
# Equal to `db.agents.SYSTEM_AGENT_NAME`; a literal keeps this module import-free.
ALERT_AGENT_NAME = "trinity-system"

HEALTH_FIELD = "guardrails_registration"
SKIPPED = "skipped"
PROBE_TIMEOUT_SECONDS = 3.0
PROBE_CONCURRENCY = 8

_TEXT_MAX_AGENTS = 25
_CONTEXT_MAX_AGENTS = 500


def describe(agents: List[str]) -> Dict[str, str]:
    """The alert's title and text (wording approved on the issue)."""
    count = len(agents)
    shown = ", ".join(agents[:_TEXT_MAX_AGENTS])
    if count > _TEXT_MAX_AGENTS:
        shown += f" and {count - _TEXT_MAX_AGENTS} more"
    noun = "agent" if count == 1 else "agents"
    if count == 1:
        cause = (
            "Claude Code on this agent is using cached organization settings, so it skips "
            "Trinity's guardrails. If its Claude organization has managed settings, ask an Owner "
            "to add \"managedSourcesBehavior\": \"merge\", then have the agent's owner run one "
            "turn on it. If it has none, the file was written inside the agent: treat the agent "
            "as tampered with."
        )
    else:
        cause = (
            "Claude Code on these agents is using cached organization settings, so it skips "
            "Trinity's guardrails. If their Claude organization has managed settings, ask an "
            "Owner to add \"managedSourcesBehavior\": \"merge\", then have each agent's owner run "
            "one turn on it. If it has none, the file was written inside the agent: treat the "
            "agent as tampered with."
        )
    return {
        "title": f"Guardrails are not running on {count} {noun}",
        "question": f"Guardrails are not running on {count} {noun}: {shown}. {cause}",
    }


# ---------------------------------------------------------------------------
# Reads — thin seams over Docker, the database and the agent's /health
# ---------------------------------------------------------------------------

async def _container_states() -> Optional[Dict[str, str]]:
    from services.docker_utils import agent_container_states_async
    return await agent_container_states_async()


def _is_ephemeral(agent_name: str) -> bool:
    from database import db
    info = db.get_agent_ephemeral_info(agent_name)
    return bool(isinstance(info, dict) and info.get("is_ephemeral"))


async def _get_health(agent_name: str):
    """One direct `GET /health`, capped as a whole, with NO circuit-breaker
    bookkeeping (the `skill_gate_map_service.hook_status` shape)."""
    from services.agent_auth import agent_httpx_client

    async def _read():
        async with agent_httpx_client(agent_name, timeout=PROBE_TIMEOUT_SECONDS) as client:
            return await client.get(f"http://agent-{agent_name}:8000/health")
    return await asyncio.wait_for(_read(), PROBE_TIMEOUT_SECONDS)


async def _read_registration(agent_name: str) -> Optional[str]:
    """The agent's `guardrails_registration`, or None when it gave no usable
    answer. A 200 without the field is an image from before the field: there is
    nothing it could report, so it reads as clean (G5; #4483 names old images).
    Never raises."""
    try:
        response = await _get_health(agent_name)
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:  # noqa: BLE001 — stopped, slow, unreachable or not JSON
        return None
    if not isinstance(data, dict):
        return None
    if HEALTH_FIELD not in data:
        return "ok"
    value = data[HEALTH_FIELD]
    return value if isinstance(value, str) else None


def _named_from(row: Any) -> Tuple[Set[str], bool]:
    """`(names, complete)` of the alert's pending row. No row: nobody is named,
    and that is the whole answer. A row without a list in `context.agents` is
    not readable: `complete` is False and the caller must not conclude that a
    silent agent was never named."""
    if row is None:
        return set(), True
    context = row.get("context") if isinstance(row, dict) else None
    agents = context.get("agents") if isinstance(context, dict) else None
    if not isinstance(agents, list):
        return set(), False
    return {a for a in agents if isinstance(a, str)}, True


def _pending_named() -> Tuple[Set[str], bool]:
    """The agents the pending alert names, read from its row (#4778). Blocking:
    call it off the event loop. Never raises: a failed read is "could not tell"."""
    try:
        from database import db
        from services import platform_alerts
        subject = platform_alerts.subject_for(ALERT_KIND, ALERT_KEY)
        return _named_from(db.find_pending_operator_queue_by_subject(ALERT_AGENT_NAME, subject))
    except Exception:  # noqa: BLE001 — "could not check" is its own answer
        logger.warning("[managed-hooks] could not read the pending alert", exc_info=True)
        return set(), False


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------

async def run_once() -> Dict[str, Any]:
    """One pass over the fleet. Files, updates or ends the one alert.

    Returns `{"status": "skipped" | "clear" | "could_not_check", ...}`.
    Never raises, and never changes a container."""
    try:
        return await _run_once()
    except Exception:  # noqa: BLE001 — a report must never break its loop
        logger.error("[managed-hooks] fleet check failed", exc_info=True)
        return {"status": "could_not_check", "reason": "error"}


async def _run_once() -> Dict[str, Any]:
    from services import platform_alerts

    states = await _container_states()
    if states is None:
        logger.warning("[managed-hooks] could not check: Docker is unreadable; "
                       "the alert is left as it is")
        return {"status": "could_not_check", "reason": "docker_unreadable"}

    names: List[str] = []
    for name in sorted(states):
        if states[name] == "running" and not await asyncio.to_thread(_is_ephemeral, name):
            names.append(name)

    gate = asyncio.Semaphore(PROBE_CONCURRENCY)

    async def read(name: str) -> Optional[str]:
        async with gate:
            return await _read_registration(name)

    values = await asyncio.gather(*(read(n) for n in names))
    skipped = [n for n, v in zip(names, values) if v == SKIPPED]
    unreadable = [n for n, v in zip(names, values) if v is None]
    # An agent the pending alert names that gives no answer now is still named:
    # it has not been proven clean. Read after the probes, so the decision uses
    # the row as it is now.
    was_named, complete = await asyncio.to_thread(_pending_named)
    held = [n for n in unreadable if n in was_named]
    named = sorted(set(skipped) | set(held))
    result: Dict[str, Any] = {"skipped": skipped, "unchecked": unreadable}

    if skipped:
        text = describe(named)
        outcome = platform_alerts.observe(
            ALERT_AGENT_NAME, ALERT_KIND, ALERT_KEY,
            priority="high",
            title=text["title"],
            question=text["question"],
            context={
                "agents": named[:_CONTEXT_MAX_AGENTS],
                "agents_skipped": len(skipped),
                "agents_unchecked": len(unreadable),
            },
        )
        logger.warning("[managed-hooks] %d agent(s) not applying the managed hooks: alert %s",
                       len(skipped), outcome)
        return {"status": "skipped", **result}

    # The row's list could not be read: any silent agent may be one it names.
    holding = held if complete else unreadable
    if holding:
        logger.warning("[managed-hooks] could not check %d agent(s); the alert is left as it is",
                       len(holding))
        return {"status": "could_not_check", "reason": "partial", **result}

    platform_alerts.clear(ALERT_AGENT_NAME, ALERT_KIND, ALERT_KEY)
    return {"status": "clear", **result}
