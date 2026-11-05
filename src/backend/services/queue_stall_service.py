"""Queue stall visibility (#4257): why a queued row is waiting, and an alert
when it has waited too long.

On a pull pilot the backend is deliberately NOT a consumer of the agent's
queue (#1766 — one owner, or the two race): only `pull_worker.py` claims. So
when an async `/task` on an idle pilot sits `queued` for hours, no backend
timer is late — a claim is being refused or is invisible to the worker, and
until now nothing on either side said a word about it. This module is the
backend half of making that audible:

* `classify_wait(...)` — a pure function naming WHY a row waits, in the
  vocabulary #3244 fixed (`agent_busy` means "the pool is full, nothing ran").
  A pilot row with idle workers is neither busy nor at capacity nor waiting on
  a backend drain that does not exist; it is waiting for the next poll.
* `assess_stalls(...)` — a pure function turning queue heads + a threshold
  into observations, so the decision is testable with no engine and no Redis.
* `run_once()` — the thin tick: read, assess, file or clear one
  `dispatch_queue_stalled` alert per agent. Called from the 60 s capacity
  maintenance sweep. Never raises, never blocks the drain.

**This is observability, not a second consumer.** It cannot make a refused
claim succeed; it bounds how long one can hide. The alert's context carries
the pool's own liveness marker (`agent:pull_poll:{name}`, canary B-08) and the
backend's count of claims IT refused (`agent:pull_claim_denied:{name}`), which
is what separates "the pool is dead" from "the pool is alive and being turned
away".

Stdlib-only at module level, like `pull_pilot.py`: `db`, Redis and the alert
seam are imported inside the functions, so the execution area's import graph
does not gain an edge for a monitor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

ALERT_KIND = "dispatch_queue_stalled"

# `wait_reason` vocabulary (#4257, honouring the #3244 contract). Five values,
# not the three the issue sketched: on a pilot, folding "waiting for the next
# worker poll" and "the conversation ahead is still running" into
# `backlog_drain` would name a backend drain that by design does not run.
WAIT_CAPACITY = "capacity"            # push agent, the slot meter is full
WAIT_BACKLOG_DRAIN = "backlog_drain"  # push agent, a slot is free, drain pending
WAIT_AGENT_BUSY = "agent_busy"        # pilot, leased running >= max_parallel (#3244)
WAIT_WORKER_CLAIM = "worker_claim"    # pilot, room to run, waiting for the next poll
WAIT_CONVERSATION_BUSY = "conversation_busy"  # pilot, the #2843 guard holds the head

WAIT_REASONS = (
    WAIT_CAPACITY,
    WAIT_BACKLOG_DRAIN,
    WAIT_AGENT_BUSY,
    WAIT_WORKER_CLAIM,
    WAIT_CONVERSATION_BUSY,
)

# The threshold's floor and ceiling. 5 minutes is below any sane poll interval
# multiple yet above a slow start; a day is the longest wait worth a row at all
# (`expire_stale_queued` fails the row at 24 h regardless).
SETTING_KEY = "queue_stall_alert_minutes"
DEFAULT_STALL_MINUTES = 30
MIN_STALL_MINUTES = 5
MAX_STALL_MINUTES = 1440


def classify_wait(
    *,
    is_pilot: bool,
    leased_running: int = 0,
    max_parallel: int = 1,
    head_conversation_running: bool = False,
    slot_free: bool = True,
) -> str:
    """Name why a queued row is waiting. Pure; one of `WAIT_REASONS`.

    The order is the order the conditions actually bind. On a pilot the #2843
    conversation guard is checked first because it holds the head even when
    there is room to run — a worker polls, sees the head it may not claim, and
    takes nothing. Then the pool: at or over `max_parallel` is the one state
    #3244 lets us call `agent_busy`. Otherwise the pilot has room and the only
    thing left is the next claim, which is `worker_claim` — honest about there
    being no backend dispatcher on this path.

    On a push agent the meter decides: a full one is `capacity`, a free slot
    with a row still queued is a drain that has not landed yet.
    """
    if is_pilot:
        if head_conversation_running:
            return WAIT_CONVERSATION_BUSY
        if int(leased_running) >= max(int(max_parallel), 1):
            return WAIT_AGENT_BUSY
        return WAIT_WORKER_CLAIM
    return WAIT_BACKLOG_DRAIN if slot_free else WAIT_CAPACITY


@dataclass(frozen=True)
class StallObservation:
    """One agent's queue head has waited past the threshold."""

    agent_name: str
    execution_id: str
    queued_at: str
    age_seconds: int
    wait_reason: str
    queued_count: int
    redelivery_count: int = 0
    last_worker_poll_age_seconds: Optional[int] = None
    claim_denied_count: int = 0
    leased_running: int = 0
    max_parallel: int = 1
    measured_at: str = ""
    # The episode marker: a person's ending snoozes re-reads of THIS episode
    # only (#3254 convention), and the episode is the admission of the head
    # that started it, so a later stall after a recovery files afresh.
    episode_started_at: str = ""

    def context(self) -> Dict[str, Any]:
        return {
            "oldest_execution_id": self.execution_id,
            "queued_at": self.queued_at,
            "age_seconds": self.age_seconds,
            "wait_reason": self.wait_reason,
            "queued_count": self.queued_count,
            "redelivery_count": self.redelivery_count,
            "last_worker_poll_age_seconds": self.last_worker_poll_age_seconds,
            "claim_denied_count": self.claim_denied_count,
            "leased_running": self.leased_running,
            "max_parallel": self.max_parallel,
            # P5: the age was read a moment before the alert was filed. A claim
            # in between files one row that clears on the next tick; saying
            # when it was measured is what makes that readable rather than
            # puzzling.
            "measured_at": self.measured_at,
            "episode_started_at": self.episode_started_at,
            # Cross-link to the pool's own liveness signal, so an operator
            # reading this row knows which half to look at next.
            "worker_liveness_signal": "canary B-08 (pull workers not alive)",
        }


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Tolerant UTC parse of the Z-suffixed strings `utc_now_iso` writes.

    Returns an aware UTC datetime, or None for anything unreadable — a row
    whose admission cannot be read has no age, and the monitor skips it rather
    than guessing one (Invariant #16: never compare these lexicographically
    against a `strftime` threshold).
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def assess_stalls(
    heads: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    threshold_seconds: int,
) -> List[StallObservation]:
    """Turn queue heads into the observations worth filing. Pure.

    Each head is the mapping `oldest_queued_head_per_agent` returns, enriched
    by the caller with `wait_reason`, `leased_running`, `max_parallel`,
    `last_worker_poll_age_seconds` and `claim_denied_count`. A head younger
    than the threshold, or one whose admission will not parse, yields nothing —
    which is also how the alert clears: the agent is simply absent from the
    result and `run_once` clears every agent it did not observe.
    """
    out: List[StallObservation] = []
    now_utc = now.astimezone(timezone.utc)
    measured_at = now_utc.isoformat().replace("+00:00", "Z")
    for head in heads:
        queued_at = head.get("queued_at")
        admitted = parse_iso(queued_at)
        if admitted is None:
            continue
        age = int((now_utc - admitted).total_seconds())
        if age < threshold_seconds:
            continue
        out.append(
            StallObservation(
                agent_name=head["agent_name"],
                execution_id=head["execution_id"],
                queued_at=str(queued_at),
                age_seconds=age,
                wait_reason=head.get("wait_reason") or WAIT_WORKER_CLAIM,
                queued_count=int(head.get("queued_count") or 1),
                redelivery_count=int(head.get("redelivery_count") or 0),
                last_worker_poll_age_seconds=head.get("last_worker_poll_age_seconds"),
                claim_denied_count=int(head.get("claim_denied_count") or 0),
                leased_running=int(head.get("leased_running") or 0),
                max_parallel=int(head.get("max_parallel") or 1),
                measured_at=measured_at,
                episode_started_at=str(queued_at),
            )
        )
    return out


def threshold_minutes() -> int:
    """The configured threshold in minutes (Settings-surfaced, #506 precedent).

    Thin wrapper so this module stays stdlib-only at import; the clamp and the
    fallback live with the setting itself in `settings_service`.
    """
    try:
        from services import settings_service

        return settings_service.get_queue_stall_alert_minutes()
    except Exception:  # noqa: BLE001 — a config read never breaks the sweep
        return DEFAULT_STALL_MINUTES


def _redis_age_seconds(redis, key: str, now: datetime) -> Optional[int]:
    """Age in seconds of a unix-timestamp marker, or None if unreadable."""
    try:
        raw = redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return max(int(now.timestamp() - float(raw)), 0)
    except Exception:  # noqa: BLE001
        return None


def _redis_counter(redis, key: str) -> int:
    try:
        raw = redis.get(key)
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(float(raw))
    except Exception:  # noqa: BLE001
        return 0


def describe(observation: StallObservation) -> Dict[str, str]:
    """The operator-facing title and question for one observation."""
    minutes = observation.age_seconds // 60
    poll = observation.last_worker_poll_age_seconds
    poll_text = (
        "no claim attempt recorded" if poll is None
        else f"last claim attempt {poll}s ago"
    )
    title = (
        f"Queue head on '{observation.agent_name}' has waited "
        f"{minutes} min ({observation.wait_reason})"
    )
    question = (
        f"Execution `{observation.execution_id}` has been queued on agent "
        f"'{observation.agent_name}' since {observation.queued_at} "
        f"({observation.age_seconds}s), with {observation.queued_count} row(s) "
        f"queued in total. Reason: **{observation.wait_reason}**. Pool: "
        f"{observation.leased_running}/{observation.max_parallel} leased, "
        f"{poll_text}, {observation.claim_denied_count} claim(s) refused by the "
        f"backend in the last day.\n\n"
        f"On a pull pilot the backend is not a consumer of this queue by design "
        f"(#1766) — only the agent's own workers claim. If the reason is "
        f"`worker_claim` with no recent claim attempt, the worker pool is the "
        f"place to look (canary B-08); with recent attempts AND refusals, the "
        f"backend is turning them away (check that `PULL_MODE_PILOT_AGENTS` and "
        f"the container's `TRINITY_PULL_MODE` agree). This row ends itself as "
        f"soon as the head is claimed."
    )
    return {"title": title, "question": question}


def run_once(now: Optional[datetime] = None) -> Dict[str, int]:
    """One monitor tick. Never raises; returns a small count summary.

    Reads every agent's queue head, classifies the wait, and files or clears
    one `dispatch_queue_stalled` alert per agent. Per-agent errors are
    swallowed and logged so one bad agent cannot skip the rest — and so the
    60 s drain sweep this hangs off is never blocked by a monitor.
    """
    summary = {"heads": 0, "observed": 0, "cleared": 0, "errors": 0}
    try:
        from database import db
        from services import platform_alerts
    except Exception:  # noqa: BLE001 — import failure must not break the sweep
        logger.debug("[#4257] queue stall monitor unavailable", exc_info=True)
        return summary

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    threshold_seconds = threshold_minutes() * 60

    try:
        heads = db.oldest_queued_head_per_agent()
    except Exception:  # noqa: BLE001
        logger.warning("[#4257] could not read queue heads", exc_info=True)
        summary["errors"] += 1
        return summary
    summary["heads"] = len(heads)

    redis = None
    try:
        from services.slot_service import get_slot_service

        redis = get_slot_service().redis
    except Exception:  # noqa: BLE001 — the age fields degrade to None
        logger.debug("[#4257] no Redis for worker liveness", exc_info=True)

    enriched: List[Dict[str, Any]] = []
    for head in heads:
        agent = head.get("agent_name")
        try:
            enriched.append(_enrich(dict(head), redis=redis, now=now_utc))
        except Exception:  # noqa: BLE001 — one agent never skips the next
            logger.warning("[#4257] could not assess agent %s", agent, exc_info=True)
            summary["errors"] += 1

    observations = assess_stalls(
        enriched, now=now_utc, threshold_seconds=threshold_seconds
    )
    stalled = {o.agent_name for o in observations}

    for observation in observations:
        try:
            text = describe(observation)
            platform_alerts.observe(
                observation.agent_name,
                ALERT_KIND,
                observation.agent_name,
                title=text["title"],
                question=text["question"],
                priority="high",
                context=observation.context(),
                # The reading is a measurement; the pending row keeps the text
                # of the episode's first reading (#3254).
                keep_text=True,
            )
            logger.warning(
                "[#4257] queue head blocked agent=%s execution=%s reason=%s "
                "age=%ss queued=%s leased=%s/%s last_poll=%ss denied=%s",
                observation.agent_name, observation.execution_id,
                observation.wait_reason, observation.age_seconds,
                observation.queued_count, observation.leased_running,
                observation.max_parallel,
                observation.last_worker_poll_age_seconds,
                observation.claim_denied_count,
            )
            summary["observed"] += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "[#4257] could not file a stall alert for %s",
                observation.agent_name, exc_info=True,
            )
            summary["errors"] += 1

    # Clear every agent that has a head but is NOT stalled. An agent with no
    # queued rows at all is covered too: its head is absent from `heads`, so
    # the reconcile below ends its row.
    for head in enriched:
        agent = head.get("agent_name")
        if agent in stalled:
            continue
        try:
            summary["cleared"] += platform_alerts.clear(
                agent, ALERT_KIND, agent, reason="queue head moved"
            )
        except Exception:  # noqa: BLE001
            summary["errors"] += 1
    try:
        # An agent whose queue emptied between ticks has no head to clear
        # from, so reconcile against the live stalled set as well.
        for agent in _agents_with_pending_alerts(stalled, enriched):
            summary["cleared"] += platform_alerts.clear(
                agent, ALERT_KIND, agent, reason="queue drained"
            )
    except Exception:  # noqa: BLE001
        logger.debug("[#4257] stall alert reconcile skipped", exc_info=True)
    return summary


def _enrich(head: Dict[str, Any], *, redis, now: datetime) -> Dict[str, Any]:
    """Add the wait reason and the pool/liveness fields to one head."""
    from database import db
    from services import settings_service
    from services.pull_pilot import (
        PULL_CLAIM_DENIED_KEY_PREFIX,
        PULL_POLL_KEY_PREFIX,
        is_pull_pilot_agent,
    )

    agent = head["agent_name"]
    is_pilot = is_pull_pilot_agent(agent)
    try:
        max_parallel = int(settings_service.get_effective_max_parallel_tasks(agent))
    except Exception:  # noqa: BLE001
        max_parallel = 1
    leased_running = 0
    head_conversation_running = False
    if is_pilot:
        leased_running = int(db.count_active_leased_by_agent([agent]).get(agent, 0))
        key = head.get("conversation_key")
        if key:
            head_conversation_running = key in set(
                db.agents_with_running_conversation(agent)
            )
    head["is_pilot"] = is_pilot
    head["leased_running"] = leased_running
    head["max_parallel"] = max_parallel
    head["wait_reason"] = classify_wait(
        is_pilot=is_pilot,
        leased_running=leased_running,
        max_parallel=max_parallel,
        head_conversation_running=head_conversation_running,
        # A push agent with a queued row either has a full meter or a drain
        # that has not landed; the physical leased count is the pilot term, so
        # fall back to the queue's own existence meaning "not yet drained".
        slot_free=leased_running < max_parallel,
    )
    if redis is not None:
        head["last_worker_poll_age_seconds"] = _redis_age_seconds(
            redis, f"{PULL_POLL_KEY_PREFIX}{agent}", now
        )
        head["claim_denied_count"] = _redis_counter(
            redis, f"{PULL_CLAIM_DENIED_KEY_PREFIX}{agent}"
        )
    return head


def _agents_with_pending_alerts(
    stalled: set, enriched: Sequence[Mapping[str, Any]]
) -> List[str]:
    """Agents carrying a pending stall row that no longer have a stalled head."""
    from database import db

    with_heads = {h.get("agent_name") for h in enriched}
    agents: List[str] = []
    for row in db.list_operator_queue_items(status="pending", limit=1000) or []:
        subject = row.get("subject") or ""
        if not subject.startswith(f"{ALERT_KIND}:"):
            continue
        agent = row.get("agent_name")
        if agent and agent not in stalled and agent not in with_heads:
            agents.append(agent)
    return agents
