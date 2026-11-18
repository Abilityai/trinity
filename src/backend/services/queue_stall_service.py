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
  `dispatch_queue_stalled` alert per agent, at the priority its wait deserves
  (#4636). Called from the 60 s capacity maintenance sweep in every worker;
  only the holder of a fail-open Redis lease runs it (#4635). Never raises,
  never blocks the drain.

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
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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

# #4636: a busy wait — every slot is running real work, or the head's own
# conversation is — is expected and only long, so it files at medium. A wait
# with room to run and nothing moving is the emergency the alert exists for.
# One set decides both the priority and the sentence below, and a reason added
# to the vocabulary without being named here files at high.
BUSY_WAITS = frozenset({WAIT_CAPACITY, WAIT_AGENT_BUSY, WAIT_CONVERSATION_BUSY})
WAIT_PRIORITY = {
    reason: ("medium" if reason in BUSY_WAITS else "high") for reason in WAIT_REASONS
}

# The operator's sentence for a busy wait (approved copy, #4636).
# `conversation_busy` has its own: slots can be free while the head waits for
# its own conversation's previous turn, and only that turn's end starts it.
_EVERY_SLOT_BUSY = (
    "Every slot on this agent is running work, so this wait is expected; "
    "the alert ends when a slot frees and the head starts."
)
BUSY_WAIT_TEXT = {
    WAIT_CAPACITY: _EVERY_SLOT_BUSY,
    WAIT_AGENT_BUSY: _EVERY_SLOT_BUSY,
    WAIT_CONVERSATION_BUSY: (
        "This conversation's previous turn is still running, so this wait is "
        "expected; the alert ends when that turn finishes and the head starts."
    ),
}

# The threshold's floor and ceiling. 5 minutes is below any sane poll interval
# multiple yet above a slow start; a day is the longest wait worth a row at all
# (`expire_stale_queued` fails the row at 24 h regardless).
SETTING_KEY = "queue_stall_alert_minutes"
DEFAULT_STALL_MINUTES = 30
MIN_STALL_MINUTES = 5
MAX_STALL_MINUTES = 1440

# #4635: one worker runs the monitor. Every uvicorn worker ticks the capacity
# sweep; the holder of this key is the one that reads the fleet and files.
# Three ticks long (each is a 60-75 s sleep plus its run): the holder renews it
# on every tick that could read the queue, a holder that dies or cannot read
# lets it lapse within one TTL, and a tick slow enough to outlive it only lets
# a second worker run the same idempotent tick.
LEASE_KEY = "queuestall:leader"
LEASE_TTL_SECONDS = 180
_lease_holder_id: Optional[str] = None


def priority_for(wait_reason: Optional[str]) -> str:
    """The priority a stall with this reason files at (#4636)."""
    return WAIT_PRIORITY.get(wait_reason, "high")


def classify_wait(
    *,
    is_pilot: bool,
    leased_running: int = 0,
    max_parallel: Optional[int] = 1,
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

    `max_parallel=None` is a cap that could not be read: it never makes a pool
    read as full (#4635), because a busy verdict now lowers the alert's
    priority and an unknown cap is not evidence of a full pool.
    """
    if is_pilot:
        if head_conversation_running:
            return WAIT_CONVERSATION_BUSY
        if max_parallel is not None and int(leased_running) >= max(int(max_parallel), 1):
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
    max_parallel: Optional[int] = 1  # None: the cap could not be read
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


def _measured_head(
    head: Mapping[str, Any], now: datetime, threshold_seconds: int
) -> Mapping[str, Any]:
    """The row the alert is about (#4635 review, D1).

    The pull claim skips a head whose conversation is still running (#2843)
    and takes the next row, so a held FIFO head says nothing about the rows
    behind it. When the oldest row a worker could claim has also waited past
    the threshold, the pool is not taking it — the #4257 incident — and the
    alert names that row, with the pool's reason. Otherwise the held head is
    the wait. The enrichment attaches the claimable row only to a held head.

    The episode stays the held head's admission either way: which of the two
    rows a tick names flips as the row behind crosses the threshold, and an
    episode marker that flipped with it would make a person's ending re-file
    on the next tick with nothing changed (the marker is a material key).
    """
    claimable = head.get("claimable")
    if not claimable:
        return head
    admitted = parse_iso(claimable.get("queued_at"))
    if admitted is None or (now - admitted).total_seconds() < threshold_seconds:
        return head
    return {**head, **claimable, "episode_started_at": head.get("queued_at")}


def assess_stalls(
    heads: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    threshold_seconds: int,
) -> List[StallObservation]:
    """Turn queue heads into the observations worth filing. Pure.

    Each head is the mapping `oldest_queued_head_per_agent` returns, enriched
    by the caller with `wait_reason`, `leased_running`, `max_parallel`,
    `last_worker_poll_age_seconds` and `claim_denied_count` (and, for a pilot
    whose head waits on its own conversation, the oldest `claimable` row). A
    head younger than the threshold, or one whose admission will not parse,
    yields nothing — which is also how the alert clears: the agent is simply
    absent from the result and `run_once` clears every agent it did not
    observe.
    """
    out: List[StallObservation] = []
    now_utc = now.astimezone(timezone.utc)
    measured_at = now_utc.isoformat().replace("+00:00", "Z")
    for head in heads:
        head = _measured_head(head, now_utc, threshold_seconds)
        queued_at = head.get("queued_at")
        admitted = parse_iso(queued_at)
        if admitted is None:
            continue
        age = int((now_utc - admitted).total_seconds())
        if age < threshold_seconds:
            continue
        max_parallel = head.get("max_parallel", 1)
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
                max_parallel=None if max_parallel is None else int(max_parallel or 1),
                measured_at=measured_at,
                episode_started_at=str(head.get("episode_started_at") or queued_at),
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


def _age_seconds(raw, now: datetime) -> Optional[int]:
    """Age in seconds of a unix-timestamp marker value, or None if unreadable."""
    try:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return max(int(now.timestamp() - float(raw)), 0)
    except Exception:  # noqa: BLE001
        return None


def _counter(raw) -> int:
    try:
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(float(raw))
    except Exception:  # noqa: BLE001
        return 0


def _liveness(
    redis, agent_names: Sequence[str], now: datetime
) -> Dict[str, Tuple[Optional[int], int]]:
    """`{agent: (last worker poll age, claims refused)}` in one MGET (#4635).

    Empty without Redis, so the heads carry no liveness fields and the
    observation reads them as unknown (None / 0), as before. An unreadable
    reply degrades every agent to unknown; the alert still files on age.
    """
    if redis is None or not agent_names:
        return {}
    from services.pull_pilot import PULL_CLAIM_DENIED_KEY_PREFIX, PULL_POLL_KEY_PREFIX

    names = list(agent_names)
    keys = [f"{PULL_POLL_KEY_PREFIX}{a}" for a in names]
    keys += [f"{PULL_CLAIM_DENIED_KEY_PREFIX}{a}" for a in names]
    try:
        values = redis.mget(keys)
    except Exception:  # noqa: BLE001 — the age fields degrade to unknown
        logger.debug("[#4257] could not read worker liveness", exc_info=True)
        values = None
    if not isinstance(values, (list, tuple)) or len(values) != len(keys):
        return {a: (None, 0) for a in names}
    n = len(names)
    return {
        a: (_age_seconds(values[i], now), _counter(values[n + i]))
        for i, a in enumerate(names)
    }


def _active_slots(agent_names: Sequence[str]) -> Dict[str, Optional[int]]:
    """Slots each push agent holds right now, in one round trip (#4635); an
    agent whose meter cannot be read is absent or None, and the caller keeps
    its prior reading. The same ZSET `CapacityManager.acquire` admits against."""
    if not agent_names:
        return {}
    try:
        from services.slot_service import get_slot_service

        return get_slot_service().active_slot_counts(list(agent_names))
    except Exception:  # noqa: BLE001 — the caller keeps its prior reading
        logger.debug("[#4257] could not read the slot meter", exc_info=True)
        return {}


def describe(observation: StallObservation) -> Dict[str, str]:
    """The operator-facing title and question for one observation."""
    minutes = observation.age_seconds // 60
    poll = observation.last_worker_poll_age_seconds
    poll_text = (
        "no claim attempt recorded" if poll is None
        else f"last claim attempt {poll}s ago"
    )
    cap = "?" if observation.max_parallel is None else observation.max_parallel
    busy = BUSY_WAIT_TEXT.get(observation.wait_reason)
    busy_text = f"{busy}\n\n" if busy else ""
    title = (
        f"Queue head on '{observation.agent_name}' has waited "
        f"{minutes} min ({observation.wait_reason})"
    )
    question = (
        f"Execution `{observation.execution_id}` has been queued on agent "
        f"'{observation.agent_name}' since {observation.queued_at} "
        f"({observation.age_seconds}s), with {observation.queued_count} row(s) "
        f"queued in total. Reason: **{observation.wait_reason}**. Pool: "
        f"{observation.leased_running}/{cap} leased, "
        f"{poll_text}, {observation.claim_denied_count} claim(s) refused by the "
        f"backend in the last day.\n\n"
        f"{busy_text}"
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
    60 s drain sweep this hangs off is never blocked by a monitor. `ran` is 0
    when another worker holds the lease and this one read nothing.
    """
    summary = {"heads": 0, "observed": 0, "cleared": 0, "errors": 0, "ran": 0}
    try:
        from database import db
        from services import platform_alerts
    except Exception:  # noqa: BLE001 — import failure must not break the sweep
        logger.debug("[#4257] queue stall monitor unavailable", exc_info=True)
        return summary

    if not _hold_lease():
        return summary
    summary["ran"] = 1

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    threshold_seconds = threshold_minutes() * 60

    try:
        heads = db.oldest_queued_head_per_agent()
    except Exception:  # noqa: BLE001
        logger.warning("[#4257] could not read queue heads", exc_info=True)
        summary["errors"] += 1
        return summary
    # Renewed only now: a holder that cannot read the queue lets the lease
    # lapse, so a worker that can takes over.
    _renew_lease()
    summary["heads"] = len(heads)

    pending = _pending_alerts(db)

    redis = None
    try:
        from services.slot_service import get_slot_service

        redis = get_slot_service().redis
    except Exception:  # noqa: BLE001 — the age fields degrade to None
        logger.debug("[#4257] no Redis for worker liveness", exc_info=True)

    try:
        enriched, failed = _enrich_all(heads, redis=redis, now=now_utc)
    except Exception:  # noqa: BLE001 — no head is assessed; drained agents still are
        logger.warning("[#4635] could not assess the queue heads", exc_info=True)
        enriched, failed = [], len(heads)
    summary["errors"] += failed

    observations = assess_stalls(
        enriched, now=now_utc, threshold_seconds=threshold_seconds
    )
    stalled = {o.agent_name for o in observations}

    for observation in observations:
        try:
            priority = priority_for(observation.wait_reason)
            row = (pending or {}).get(observation.agent_name)
            # #4636: a pending row keeps the text of its episode's first
            # reading (#3254), so a stall that began as a busy wait would stay
            # medium after it turned into a real one. A reading that outranks
            # the row rewrites its priority, title and text in place; a
            # same-or-lower one only counts.
            worse = row is not None and (
                platform_alerts.priority_rank(priority)
                > platform_alerts.priority_rank(row.get("priority"))
            )
            text = describe(observation)
            platform_alerts.observe(
                observation.agent_name,
                ALERT_KIND,
                observation.agent_name,
                title=text["title"],
                question=text["question"],
                priority=priority,
                context=observation.context(),
                keep_text=not worse,
            )
            logger.warning(
                "[#4257] queue head blocked agent=%s execution=%s reason=%s "
                "priority=%s age=%ss queued=%s leased=%s/%s last_poll=%ss "
                "denied=%s",
                observation.agent_name, observation.execution_id,
                observation.wait_reason, priority, observation.age_seconds,
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

    # Clear every agent that was assessed, has a head, and is NOT stalled — but
    # only where the snapshot shows a pending row: `clear` looks its row up per
    # agent, and a quiet fleet should cost no read per agent. With the rows
    # unreadable, ask for each one.
    for head in enriched:
        agent = head.get("agent_name")
        if agent in stalled or (pending is not None and agent not in pending):
            continue
        try:
            # Head moved: the default reason, `condition_cleared`.
            summary["cleared"] += platform_alerts.clear(agent, ALERT_KIND, agent)
        except Exception:  # noqa: BLE001
            summary["errors"] += 1

    # An agent whose queue emptied between ticks has no head to clear from, so
    # reconcile the pending rows against the queue itself. "Has queued rows"
    # comes from the head read, not from the agents assessed: one whose
    # assessment failed still has work waiting, and ending its row here filed
    # a fresh one on the next good tick.
    if pending is not None:
        queued = {h.get("agent_name") for h in heads}
        for agent in pending:
            if agent in stalled or agent in queued:
                continue
            try:
                # Queue drained: the default reason, `condition_cleared`.
                summary["cleared"] += platform_alerts.clear(agent, ALERT_KIND, agent)
            except Exception:  # noqa: BLE001
                summary["errors"] += 1
    return summary


def _pending_alerts(db) -> Optional[Dict[str, Mapping[str, Any]]]:
    """This kind's pending rows by agent, in one filtered read (#4635), or None
    when they cannot be read — then nothing is reconciled and no row rewritten
    this tick. One row per agent: the subject is the agent."""
    try:
        rows = db.list_pending_operator_queue_by_subject_prefix(f"{ALERT_KIND}:") or []
    except Exception:  # noqa: BLE001
        logger.warning(
            "[#4635] could not read the pending stall alerts; no reconcile "
            "and no rewrite this tick", exc_info=True,
        )
        return None
    return {row["agent_name"]: row for row in rows if row.get("agent_name")}


def _enrich_all(
    heads: Sequence[Mapping[str, Any]], *, redis, now: datetime
) -> Tuple[List[Dict[str, Any]], int]:
    """Add the wait reason and the pool/liveness fields to every head.

    One read per kind of fact for the whole tick, not one per agent (#4635):
    the effective caps, the live leases and running conversations of the
    pilots, the push agents' slot meters and every agent's liveness markers.
    Each read keeps the failure rule it had per agent: an unreadable cap is
    unknown (never a full pool), an unreadable meter keeps the drain reading,
    unreadable liveness is unknown, and a pilot whose leases or conversation
    guard cannot be read is not assessed this tick. Returns the heads that
    were assessed and the number that were not; one agent's failure never
    skips the next.
    """
    from database import db
    from services import settings_service
    from services.pull_pilot import is_pull_pilot_agent
    from utils.helpers import to_utc_iso

    assessable: List[Tuple[Dict[str, Any], bool]] = []
    failed = 0
    for head in heads:
        agent = head.get("agent_name")
        try:
            assessable.append((dict(head), bool(is_pull_pilot_agent(agent))))
        except Exception:  # noqa: BLE001 — one agent never skips the next
            logger.warning("[#4257] could not assess agent %s", agent, exc_info=True)
            failed += 1
    if not assessable:
        return [], failed

    agents = [h["agent_name"] for h, _ in assessable]
    pilots = [h["agent_name"] for h, is_pilot in assessable if is_pilot]
    keyed = [h["agent_name"] for h, is_pilot in assessable
             if is_pilot and h.get("conversation_key")]
    pushes = [h["agent_name"] for h, is_pilot in assessable if not is_pilot]

    try:
        caps = settings_service.get_effective_max_parallel_tasks_for(agents)
    except Exception:  # noqa: BLE001 — unknown, never full
        logger.warning("[#4635] could not read the agents' caps", exc_info=True)
        caps = {}
    leased: Optional[Dict[str, int]] = None
    running: Optional[Dict[str, List[str]]] = {}
    if pilots:
        try:
            # Live leases only: an expired one has no worker behind it until
            # the reaper requeues the row (canary B-02's rule).
            leased = db.count_active_leased_by_agent(pilots, unexpired_at=to_utc_iso(now))
        except Exception:  # noqa: BLE001
            logger.warning("[#4257] could not read the pilots' leases", exc_info=True)
        if keyed:
            try:
                running = db.running_conversation_keys_by_agent(keyed)
            except Exception:  # noqa: BLE001
                logger.warning("[#4257] could not read running conversations",
                               exc_info=True)
                running = None
    meter = _active_slots(pushes)
    liveness = _liveness(redis, agents, now)

    enriched: List[Dict[str, Any]] = []
    held: List[Dict[str, Any]] = []
    for head, is_pilot in assessable:
        agent = head["agent_name"]
        try:
            max_parallel = caps.get(agent)  # None: unknown
            leased_running = 0
            head_conversation_running = False
            if is_pilot:
                if leased is None:
                    raise RuntimeError("the pilots' leases could not be read")
                leased_running = int(leased.get(agent, 0))
                key = head.get("conversation_key")
                if key:
                    if running is None:
                        raise RuntimeError("the running conversations could not be read")
                    head_conversation_running = key in set(running.get(agent, ()))
            # The push answer comes from the meter, never from an assumption. A
            # push agent with `max_parallel=1` and a legitimately long turn
            # (timeouts run to hours) keeps a row queued past the threshold
            # with the meter full: that is `capacity`, an expected wait, and
            # calling it `backlog_drain` tells the operator the backend owes a
            # dispatch it does not owe. A row still queued a full tick after
            # admission refused it *with a slot now free* is the real drain
            # miss — the backend's own job there, unlike on a pilot where it
            # has none by design (#1766). An unreadable meter or cap keeps the
            # drain reading: neither is evidence of a full pool, and with Redis
            # down the dispatch path is down with it. The pilot branch never
            # asks — the slot meter is disjoint from pilot rows by
            # construction, so it would be a wrong answer rather than a slow
            # one.
            slot_free = True
            if not is_pilot and max_parallel is not None:
                active = meter.get(agent)
                if active is not None:
                    slot_free = active < max(int(max_parallel), 1)
            head["is_pilot"] = is_pilot
            head["leased_running"] = leased_running
            head["max_parallel"] = max_parallel
            head["wait_reason"] = classify_wait(
                is_pilot=is_pilot,
                leased_running=leased_running,
                max_parallel=max_parallel,
                head_conversation_running=head_conversation_running,
                slot_free=slot_free,
            )
            if redis is not None:
                poll_age, denied = liveness.get(agent, (None, 0))
                head["last_worker_poll_age_seconds"] = poll_age
                head["claim_denied_count"] = denied
            enriched.append(head)
            if head_conversation_running and int(head.get("queued_count") or 1) > 1:
                held.append(head)
        except Exception:  # noqa: BLE001 — one agent never skips the next
            logger.warning("[#4257] could not assess agent %s", agent, exc_info=True)
            failed += 1

    if held:
        try:
            claimable = {
                row["agent_name"]: row
                for row in db.oldest_queued_head_per_agent(
                    [h["agent_name"] for h in held], claimable=True
                )
            }
        except Exception:  # noqa: BLE001 — the held head is still the wait
            logger.warning("[#4635] could not read the claimable rows", exc_info=True)
            failed += 1
            claimable = {}
        for head in held:
            row = claimable.get(head["agent_name"])
            if row is None:
                continue
            head["claimable"] = {
                "execution_id": row["execution_id"],
                "queued_at": row["queued_at"],
                "conversation_key": row["conversation_key"],
                "triggered_by": row["triggered_by"],
                "redelivery_count": row["redelivery_count"],
                "wait_reason": classify_wait(
                    is_pilot=True,
                    leased_running=head["leased_running"],
                    max_parallel=head["max_parallel"],
                ),
            }
    return enriched, failed


def _lease_holder() -> str:
    """This process's lease value. Made on first use and kept, so a holder
    recognises its own lease; the pid tells uvicorn's workers apart (it spawns
    them) and makes a forked child mint its own."""
    global _lease_holder_id
    if _lease_holder_id is None or not _lease_holder_id.startswith(f"{os.getpid()}:"):
        _lease_holder_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    return _lease_holder_id


def _hold_lease() -> bool:
    """True when this worker runs the tick: it holds `LEASE_KEY`, took a free
    one, or Redis could not be asked (#4635).

    **Fail open**: with Redis unreadable every worker runs the monitor, and
    the one-pending-row-per-subject index (`uq_operator_queue_pending_subject`)
    keeps that to one row per agent — a monitor that goes dark with Redis is
    worse than a duplicated reading. Holding is checked without extending the
    lease; `_renew_lease` extends it once the tick has read the queue.
    """
    try:
        from redis_breaker_util import get_breaker_redis, lock_token_matches

        r = get_breaker_redis()
        if r is None:
            return True
        holder = _lease_holder()
        if r.set(LEASE_KEY, holder, nx=True, ex=LEASE_TTL_SECONDS):
            return True
        return lock_token_matches(r.get(LEASE_KEY), holder)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[#4635] queue-stall lease check failed open (%s)",
                       type(exc).__name__)
        return True


def _renew_lease() -> None:
    """Extend our own lease to a full TTL. Compare-and-expire in one Lua script
    (#4028): a holder whose lease lapsed never extends its successor's, and
    this never acquires. Never raises."""
    try:
        from redis_breaker_util import LEASE_REFRESH_LUA, get_breaker_redis

        r = get_breaker_redis()
        if r is not None:
            r.register_script(LEASE_REFRESH_LUA)(
                keys=[LEASE_KEY], args=[_lease_holder(), LEASE_TTL_SECONDS], client=r
            )
    except Exception:  # noqa: BLE001 — the TTL decides, as for a dead holder
        logger.debug("[#4635] could not renew the queue-stall lease", exc_info=True)


def release_lease() -> None:
    """Give the lease up at shutdown, so the next worker need not wait out the
    TTL before the monitor runs again. Compare-and-delete in one Lua script: a
    successor's lease is never touched. Never raises."""
    try:
        from redis_breaker_util import LEASE_RELEASE_LUA, get_breaker_redis

        r = get_breaker_redis()
        if r is not None:
            r.register_script(LEASE_RELEASE_LUA)(
                keys=[LEASE_KEY], args=[_lease_holder()], client=r
            )
    except Exception:  # noqa: BLE001 — the TTL ends it
        pass
