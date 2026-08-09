"""
Canary watcher service (CANARY-001 / Issue #411).

Runs in the backend process. Every 5 minutes:

1. `collect_snapshot()` — read Redis, SQLite, agent registries.
2. `run_invariants(snapshot)` — apply S-01 / E-02 / L-03 (Phase 1 set).
3. Persist any violations to `canary_violations`.
4. Detect green→red transitions per invariant against the previously-stored
   latest violation; fire one Slack alert per transition via incoming
   webhook (`CANARY_SLACK_WEBHOOK_URL` env var; unset = silent sink), and
   re-attempt any alert the webhook rejected while its invariant is still
   red (#1897 — see `REDIS_KEY_ALERT_PENDING`).

Modeled on `services/cleanup_service.py` — single asyncio task, idempotent
start/stop, lock-guarded re-entrancy. Disabled by default; enable per
deployment with `CANARY_ENABLED=1`. Production deployment is staging/dev.

## One cycle per fleet, not one per worker (#1881)

The FastAPI lifespan starts this service in EVERY uvicorn worker, and prod runs
`uvicorn main:app --workers 2`. `self._lock` is an `asyncio.Lock` — it guards
re-entrancy inside ONE process and says nothing about cross-worker exclusion,
so before #1881 both workers ran a full cycle every 5 minutes. That is not a
tidiness problem: R-01 `docker exec`s into *every running agent container* each
cycle (twice per 5 min, per agent, on the fleet the harness is supposed to be
observing unobtrusively), every violation double-persists to `canary_violations`
(11,942 rows in 24h measured on eu2), and every shared cross-cycle marker —
`canary:last_cycle_at`, `canary:last_cycle_red`, E-02's `canary:e02:terminal_seen`,
H-01's `canary:h01:suspect_since` — has two independent writers.

The scheduled loop therefore holds a Redis leader lease (`canary:leader`),
mirroring `monitoring:leader` (#1464) and `opqueue:leader` (#1632): SET NX with
a TTL, own-lease-only refresh, atomic compare-and-delete release on shutdown.
Every worker still runs its loop and re-checks leadership each cycle, so a dead
leader's lease expires and a sibling takes over with no restart.

Unlike both precedents the lease is re-armed by a **heartbeat** on its own short
timer (`_heartbeat_loop`), not once at the top of each cycle. Those services'
cycles are bounded; a canary cycle is not (R-01's `docker exec` sweep carries no
timeout), so a cycle-driven refresh forced one TTL to answer both "how long may a
cycle run" and "how long before a dead leader is noticed" — two questions that
want opposite values, and it answered neither well. See the constants block.

`run_cycle()` itself is deliberately NOT gated — see its docstring.

Why a service and not a Trinity agent (Issue #411 design discussion):
the watcher does no LLM reasoning — it's a deterministic library invocation
on a 5-minute timer. Running it as a Trinity agent would add an LLM call
per cycle and a separate container for no benefit. Deterministic checks
belong in the backend; the agents are the *fleet* (load generators), which
are deployed via the canary-fleet manifest.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from canary import collect_snapshot, run_invariants
from canary.snapshot import ViolationReport
from database import db
from redis_breaker_util import ScriptCache, get_breaker_redis
from services.canary_alerts import AlertDelivery, CanaryAlerts


@dataclass
class CycleResult:
    """Outcome of one canary cycle.

    `violations` is the per-invariant list of `ViolationReport`s the
    deterministic library produced (now persisted to `canary_violations`).

    `transition_invariant_ids` is the subset the service classified as
    a fresh green→red flip this cycle — not a continuation of an
    already-known violation — **and actually notified on**. Since #1897
    it means *notified*, not merely *detected*: an alert the webhook
    rejected lands in `undelivered_invariant_ids` instead, and a retry
    that finally lands appears here on the cycle it succeeded rather than
    the cycle its flip was detected. The router exposes this directly to
    operators so the on-demand `/api/canary/run-cycle` response matches
    what the alert sink actually sent.

    `persisted_violation_ids` is index-aligned with `violations`: for
    each `ViolationReport` in `violations[inv_id][i]`, the row id
    returned by `insert_canary_violation` is at
    `persisted_violation_ids[inv_id][i]` — or `None` if the insert
    failed. Lets the router surface row ids without re-querying.

    `skipped` is True when the lock was already held (concurrent cycle
    in progress) and this call returned without running. The router
    maps this to HTTP 409 so an empty response can never be confused
    with a real green cycle.
    """

    violations: Dict[str, List[ViolationReport]] = field(default_factory=dict)
    persisted_violation_ids: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    transition_invariant_ids: List[str] = field(default_factory=list)
    # #1897: the invariants this cycle tried to alert on and could NOT
    # deliver — a rejected webhook, a raised emit, or a retry held off by
    # the per-interval floor. Disjoint from `transition_invariant_ids`,
    # which since #1897 means *notified*, not *detected*. Without this the
    # admin run-cycle response would report zero transitions during a
    # webhook outage — structurally identical to a green cycle, which is
    # exactly the ambiguity #1881 spent a 409 on. An entry here is
    # retried on a later cycle while its invariant stays red.
    undelivered_invariant_ids: List[str] = field(default_factory=list)
    # snapshot_time of the most recent prior violation per invariant this
    # cycle alerted on or still owes an alert for — used by the alert sink
    # to render "last red 2h ago" and by the run-cycle endpoint to surface
    # `previous_violation_at`. Populated for exactly the union of
    # `transition_invariant_ids` and `undelivered_invariant_ids`, which is
    # slightly wider than "emitted": an entry the retry floor held off is
    # in the union but made no POST this cycle. Harmless — the router
    # reads this map only for ids in `transition_invariant_ids` — but the
    # union, not emission, is the contract. On a #1897 retry the value is
    # the one captured when the transition was first detected, which is
    # the honest pre-episode "last red" rather than the failed episode
    # itself. `None` = first-ever violation for that invariant.
    previous_violation_at: Dict[str, Optional[str]] = field(default_factory=dict)
    snapshot_time: str = ""
    sources_unavailable: List[str] = field(default_factory=list)
    skipped: bool = False

logger = logging.getLogger(__name__)


# Five-minute cadence per the design doc. Deliberately the same as
# cleanup_service to share the operator's mental model (both are "every
# 5 min the backend reconciles state").
CANARY_INTERVAL_SECONDS = 300

# Redis key holding the snapshot_time of the most recent cycle that ran.
# Used by transition detection so a continuously-red invariant fires a
# notification once (on the first cycle that catches it) rather than every
# cycle thereafter — see `_run_cycle_inner` for the rule.
REDIS_KEY_LAST_CYCLE = "canary:last_cycle_at"

# Redis key holding the invariant ids that were RED in the previous cycle, as a
# JSON list. This is a fallback authority for transition detection, used only
# when the primary (`db.get_latest_canary_violation_per_invariant`) is
# unreadable — i.e. when the database is down.
#
# It exists because of H-01 (#1813). The pre-cycle DB read below used to be
# unguarded, so a database outage raised out of `_run_cycle_inner` before the
# snapshot was even collected: the canary went silent in exactly the scenario
# H-01 was built to announce. Making that read fail-open fixes the silence, but
# on its own it would swap one defect for another — an empty `previous_latest`
# makes every violation look like a fresh green→red flip, so a multi-hour DB
# outage would alert every 5 minutes, breaking the "a persistent condition
# chirps once" property. Redis is a separate failure domain from the DB, so it
# can still remember what was red while the DB cannot.
REDIS_KEY_LAST_CYCLE_RED = "canary:last_cycle_red"

# #1897: Redis HASH of transitions whose alert has NOT been delivered.
# Field = invariant id, value = a small JSON record (`run start`, last
# attempt, the `previous_violation_at` captured when the flip was
# detected, an attempt counter, the last webhook error).
#
# ONE hash rather than `canary:alert_pending:{id}`: one `HGETALL` per cycle
# instead of N `GET`s or a `SCAN` (discouraged in production Redis), and the
# whole pending set is enumerable in a single read.
#
# Per-invariant, and deliberately NOT the cycle-global
# `REDIS_KEY_LAST_CYCLE` cursor. Withholding that cursor on a failed post —
# the obvious fix, and the one the issue pre-emptively rejects — retries
# nothing: `previous_latest` is read BEFORE this cycle's inserts, so on the
# next cycle it already carries the row this cycle wrote, which post-dates
# the held-back cursor and reads as a continuation. What it *does* do is
# suppress an unrelated invariant's genuine red→green→red flip, i.e. it
# manufactures a fresh instance of the exact bug #1897 is about.
#
# NO TTL, and that is a decision rather than an omission. The structure is
# bounded by the invariant REGISTRY (at most one field per registered
# invariant, ≤16 today), and it is reaped by the success `HDEL` and the
# give-up `HDEL`. A TTL would instead mean that a fleet quiet for longer
# than the window — a long deploy, a rollback, `CANARY_ENABLED` off
# overnight, a Redis failover — silently discards every pending alert with
# no log line, while continuing-red suppression guarantees none of them is
# ever re-detected. That is silent permanent loss reintroduced inside the
# fix for silent permanent loss. A lingering field is inert: emission is
# gated on the invariant being red in the current cycle.
#
# Global (not `agent:`-keyed) because invariant ids are a fixed code
# registry that no user can recycle — the #1560 name-recycling hazard the
# `agent:` prefix exists for cannot apply. So, like `canary:leader` and
# `canary:e02:*`, it is legitimately absent from `CLEARED_KEYSPACES`:
# clearing it on an agent lifecycle event would drop a pending fleet-level
# alert.
REDIS_KEY_ALERT_PENDING = "canary:alert_pending"

# #1897: how long one contiguous failure run may keep retrying before we
# give up loudly. An AGE, not an attempt count — age is monotonic by
# construction, so it needs no inference about whether a "fresh" transition
# is genuine, and unlike a count it can never hand a brand-new episode a
# spent budget and drop its very first attempt.
#
# 30 minutes is chosen against the failure it has to survive, not picked
# round: a transient Slack 5xx clears within one cycle, a revoked webhook
# never clears, and a real Slack degradation routinely runs 30–60 minutes.
# The attempt-count form of this bound (3 × 5 min) tolerated 15.
#
# A module constant, NOT an env var and NOT a `system_settings` knob.
# #1644's `MAX_ROWS_PER_SWEEP` is the precedent: a mutable value read at
# action time that gates a safety behaviour is the #1638 class, and nobody
# outside this file can reason about the right number anyway.
#
# The budget is evaluated AFTER each attempt, not before it, so that the
# give-up ERROR can quote the elapsed and the `last_error` of the attempt
# that actually just failed rather than a stale one. The arithmetic
# consequence is that a run ends on the first attempt whose age EXCEEDS the
# window, not on the last one inside it: at the 5-minute default that is
# **8 POSTs spanning 2100s (35 min)**, not 6 over 30. Pinned by
# `test_retry_is_bounded_and_gives_up_loudly`, which derives its cycle count
# from this constant rather than hard-coding it.
MAX_ALERT_PENDING_AGE_SECONDS = 1800

# #1897: a gap longer than this many intervals between attempts ends the
# contiguous failure run, so the next failure starts a fresh window.
# Without the decay, brief separated flaps would consume the budget a later
# long outage needs; with it, they cannot.
#
# The dual of that, stated so nobody rediscovers it as a bug: an invariant
# that flaps red→green→red on a period LONGER than this never accumulates
# run age, so against a permanently dead webhook it never reaches a give-up.
# It also never retries — the invariant is green before the retry floor
# opens — so each red episode costs exactly ONE POST, which is the
# detection rate and is precisely the pre-#1897 behaviour. The delivery
# layer deliberately does not bound its own detector.
_ALERT_RUN_DECAY_INTERVALS = 3

# #1897: `post_webhook` returns Slack's response body VERBATIM, and a
# misrouted webhook URL answers with a whole HTML page. The give-up ERROR
# has to name a cause, so the string is persisted — truncated, because it
# is otherwise unbounded third-party text going into a Redis value.
_MAX_ALERT_ERROR_CHARS = 200

# #1881: single Redis key holding the current canary leader's worker id. One
# leader across all uvicorn workers — whoever holds it runs the scheduled cycle,
# the rest idle. Global (not `agent:`-keyed), so — like `canary:last_cycle_*` and
# `canary:e02:*` — it is legitimately absent from #1560's `CLEARED_KEYSPACES`.
REDIS_KEY_LEADER = "canary:leader"

# --- Lease timing: two questions, two constants (#1881) ----------------------
#
# The first cut of this lease refreshed the TTL once at the TOP of each cycle,
# which forced ONE number to answer two questions that want opposite values:
#
#   (a) "How long may a single cycle run before we conclude the leader is
#       wedged?" — wants to be LARGE. A canary cycle has no upper bound: its
#       cost is dominated by R-01's `container.exec_run` sweep across every
#       running agent container, which carries no timeout and scales with FLEET
#       SIZE, not with how often we look.
#   (b) "How long after a leader dies before somebody else cycles?" — wants to
#       be SMALL. Nobody is watching in that window, and an unwatched window is
#       the silent-green failure H-01 exists to announce.
#
# A single TTL sized for (a) — `max(interval * 3, 900)` — bought a ~1200s
# (TTL + interval) blind failover window to pay for it, and did not actually
# close (a) either: a cycle that overran 900s lapsed its lease mid-flight
# anyway, a sibling acquired, and two cycles ran concurrently — the exact state
# the lease exists to remove.
#
# `_heartbeat_loop` separates them. It re-arms the lease on its own short timer
# for as long as this worker holds it, independent of where the cycle is, so the
# TTL no longer has to cover a cycle at all — only a couple of missed
# heartbeats. Each constant below now answers exactly one of the two questions.

# (b) How stale the heartbeat may get before the lease lapses. 3× the refresh
# cadence is the precedents' rule (`monitoring:leader` #1464 and
# `opqueue:leader` #1632 both use 3× theirs): one or two missed refreshes must
# not move leadership, three consecutive misses should. Worst-case failover is
# now ~780s rather than ~1200s — see `_max_failover_seconds`, which owns that
# arithmetic.
_LEADER_HEARTBEAT_SECONDS = 60
_LEADER_TTL_SECONDS = _LEADER_HEARTBEAT_SECONDS * 3

# (a) How long the heartbeat will keep a running cycle's lease alive before it
# stops refreshing and lets a sibling take over. This is the old TTL floor,
# unchanged in value: the bound was always "how long may one cycle run", it was
# just spelled as a TTL.
#
# The cap is what stops the heartbeat being a regression rather than a fix.
# Without it a WEDGED leader would hold the lease forever and nobody would cycle
# at all — trading "two workers probing" for "no worker watching", which is the
# wrong direction for this subsystem specifically. Past the cap we deliberately
# choose the duplicate over the silence, and say so at ERROR: a wedged leader
# plus a fresh one is noisy and visible; a fleet with nobody watching is not.
# Same reasoning, and same direction, as the fail-open in
# `_try_acquire_leadership`.
_MAX_CYCLE_LEASE_SECONDS = 900

# Every mutation of the lease key is conditional on the stored value being OURS,
# evaluated atomically inside Redis, so no path can touch a sibling's lease.
#
# The GET-then-DELETE this replaces was not equivalent: if our lease expired
# between the GET and the DELETE and a sibling won `SET NX` in that window, we
# deleted the SUCCESSOR's lease. A third worker could then acquire while the
# successor still had `_is_leader = True`, and two cycles would run concurrently
# — precisely the duplication this whole change removes. `monitoring_service`
# (#1464) and `operator_queue_service` (#1632) share the non-atomic shape; we
# depart from them here for the same reason this service departs on the TTL, and
# it is the PR's own argument: a duplicated canary cycle is not inert.
#
# Refresh was NOT exposed the same way — an EXPIRE landing on a successor's key
# merely re-arms it to an identical TTL, destroying nothing — but it is Lua too
# so there is a single uniform predicate rather than two that can drift.
# `redis_breaker_util.lock_token_matches` (#1919) exists because exactly that
# drift happened once already.
_LEASE_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_LEASE_REFRESH_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_LEASE_SCRIPTS = ScriptCache(
    release=_LEASE_RELEASE_LUA,
    refresh=_LEASE_REFRESH_LUA,
)


def _parse_iso_to_unix(ts: Optional[str]) -> Optional[float]:
    """ISO-Z timestamp → unix epoch seconds, or None if unusable.

    Mirrors R-01's helper (`canary/invariants/r01_no_zombie_claude.py`).
    Total by contract: every caller here is inside a cycle that must not be
    broken by a timestamp, and `None` routes to the fail-open branch.
    """
    if not ts or not isinstance(ts, str):
        return None
    if ts.endswith("Z"):
        ts = ts[:-1]
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _coerce_int(value: Any) -> int:
    """Read an integer out of a Redis-round-tripped record, or 0.

    The pending record is parsed input: a poisoned or hand-edited
    `attempts` must not raise inside a cycle, and it gates nothing (the
    budget is an age), so 0 is a safe floor.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _truncate_error(error: Optional[str]) -> Optional[str]:
    """Bound a webhook error string before it is persisted.

    `slack_service.post_webhook` returns Slack's response body VERBATIM,
    and a misrouted webhook URL answers with an entire HTML page. The URL
    itself is never in this string — `post_webhook` returns only
    `type(e).__name__` for transport errors, precisely because the URL is
    the credential.
    """
    if error is None:
        return None
    text = error if isinstance(error, str) else str(error)
    return text[:_MAX_ALERT_ERROR_CHARS]


class CanaryService:
    """Background watcher loop for the canary invariant harness."""

    def __init__(self, interval_seconds: int = CANARY_INTERVAL_SECONDS):
        self.interval = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        # #1881: unique per worker process so the cross-worker leader lease is
        # only ever refreshed/released by ITS OWN holder. pid is a readable
        # prefix for log triage; the uuid suffix disambiguates a recycled pid.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs
        # #1881: monotonic start of the in-flight cycle (None = no cycle
        # running). Read only by the heartbeat, to distinguish "slow, keep the
        # lease alive" from "wedged, let it go". Monotonic on purpose — a
        # wall-clock jump must not read as a wedge.
        self._cycle_started_at: Optional[float] = None
        self._wedge_reported = False  # one ERROR per wedge, not one per beat
        # Counters surface in /api/health-style monitoring; useful for
        # confirming the service is actually firing on deployed instances.
        #
        # #1897 splits detection from delivery. `cumulative_transitions`
        # takes the issue's literal semantics — it must not report a
        # delivery that did not happen — which silently costs it its old
        # meaning ("flips detected"), so that meaning gets its own counter
        # rather than being lost to satisfy an alerting AC. One counter
        # must not mean two things.
        #
        # These are in-process, per-worker, reset on restart and exposed by
        # no endpoint: an introspection aid, NOT the "a give-up is never
        # silent" surface. That is the distinct ERROR log.
        self.cumulative_cycles: int = 0
        self.cumulative_violations: int = 0
        self.cumulative_transitions: int = 0  # alerts actually delivered
        self.cumulative_transitions_detected: int = 0  # green→red flips seen
        self.cumulative_alerts_dropped: int = 0  # gave up after the age budget
        self.last_run_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background loop. No-op if already running or disabled."""
        if not self._is_enabled():
            logger.info("canary: disabled (set CANARY_ENABLED=1 to enable)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        # #1881: the lease is kept alive here, not by the cycle. Started with
        # the loop and torn down with it, so "the heartbeat is running" is
        # exactly "the service is running" and there is no state where a leader
        # holds a lease nothing re-arms.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"canary watcher started (interval={self.interval}s)")

    async def stop(self):
        """Stop the background loop cleanly.

        **Async since #1881 review round 2.** The release below must not land
        while a cycle is still unwinding inside the cancelled task: a sibling
        could acquire and start its cycle concurrently with our tail — a
        smaller version of the very overlap this lease removes. Awaiting the
        cancellation first makes the handoff strictly ordered. `stop()` has
        exactly one caller (`main.py`'s lifespan shutdown, which is async), so
        matching `monitoring_service.stop()`'s async precedent costs nothing
        and needs no "the overlap is bounded" caveat.
        """
        self._running = False
        await self._cancel(self._task)
        self._task = None
        await self._cancel(self._heartbeat_task)
        self._heartbeat_task = None
        # #1881: hand leadership off immediately on a graceful shutdown rather
        # than leaving the surviving worker idle for up to a full TTL. Matters
        # more here than for the precedents: the sibling idling means nobody is
        # watching, and an unwatched window is the silent-green failure H-01
        # exists to announce. Never raises.
        self._release_leadership()
        logger.info("canary watcher stopped")

    @staticmethod
    async def _cancel(task: Optional[asyncio.Task]) -> None:
        """Cancel a background task and wait for it to actually finish."""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("canary: background task raised during shutdown")

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv("CANARY_ENABLED", "0") == "1"

    # ------------------------------------------------------------------
    # Cross-worker leader lease (#1881)
    # ------------------------------------------------------------------

    def _leader_ttl(self) -> int:
        """Lease TTL — how stale the heartbeat may get before leadership moves.

        Independent of `self.interval`, and that is the point: the heartbeat,
        not the cycle, re-arms the lease, so this number no longer has to
        outlast a cycle. See the constants block for the two questions this
        used to have to answer at once.
        """
        return _LEADER_TTL_SECONDS

    def _max_failover_seconds(self) -> int:
        """Worst case from a dead leader's last cycle to a sibling's first.

        Three terms, all of which have to be paid:

        - up to `interval`, because the leader can die immediately BEFORE its
          next cycle, so its last observation is already a full interval old;
        - up to the TTL, because a heartbeat re-armed the lease at most
          `_LEADER_HEARTBEAT_SECONDS` ago and it then has to lapse;
        - up to `interval` again, because a sibling only re-checks leadership
          at the top of its own loop iteration.

        ≈780s at the defaults (300 + 180 + 300), against ~1200s when the TTL
        was cycle-driven. Two other places quote this window in prose —
        R-01's `_MAX_OBSERVATION_GAP_SECONDS` comment and
        `docs/memory/architecture.md` — so keep them in step with this method.

        Note what does NOT shrink: the two `interval` terms are inherent to a
        loop that ticks on a fixed period, so the failover window can never go
        below one interval however short the TTL gets.
        """
        return self.interval * 2 + self._leader_ttl()

    def _try_acquire_leadership(self) -> bool:
        """#1881 — cross-worker leader election for the scheduled cycle.

        Returns True iff this worker holds the lease for this cycle. Mirrors
        `monitoring:leader` (#1464) / `opqueue:leader` (#1632) exactly: SET NX to
        pick a single winner atomically, then refresh the TTL only when the
        stored id is our own, so we can never steal or clobber a sibling's lease.

        ## Fail-open, and what "open" has to mean here

        Redis unreachable ⇒ act as leader. Both precedents fail open too, but
        their tradeoff is not this one, so it is worth stating rather than
        inheriting:

        - `monitoring_service` fails open because a doubled `record_failure()`
          feed lands in a breaker that is itself fail-open — the duplicate is
          inert.
        - `operator_queue_service` fails open because duplicate creates dedupe on
          conflict and the DB depth cap still bounds the feed.
        - Here the duplicate is NOT inert: N workers means N× the `docker exec`
          sweep and N copies of every violation row.

        We fail open anyway, because the alternative is strictly worse. This is
        the one subsystem whose entire purpose is to notice that something has
        gone quiet — H-01 exists because a blind collector reports green, and a
        canary that stops running IS that same silent-green, one level up, with
        no invariant able to catch it (invariants only run inside the thing that
        isn't running). Fail-closed would mean a Redis blip silently stops the
        watcher on every worker at once, and nothing would say so. Duplicated
        probes are noisy and visible; silence is quiet and invisible. For a
        detector, noisy-and-visible is always the correct direction to fail.

        Note also that a Redis outage is *already* a degraded-canary state the
        harness is built to announce: `collect_snapshot` records the Redis
        collector in `sources_unavailable`, and H-01's marker read raises, which
        by design fires unconfirmed rather than skipping. Failing open keeps
        those paths reachable; failing closed would suppress them.

        ## What the lease does NOT buy

        It is best-effort, and the fail-open above re-opens the multi-writer
        window precisely when Redis is down. So the elapsed-wall-clock
        confirmation gates that were written for the pre-lease world —
        H-01's `CONFIRMATION_MIN_SECONDS` and R-01's `DWELL_SECONDS` — remain
        load-bearing and must NOT be relaxed to "seen in a second cycle" on the
        strength of this lease (learnings.md 2026-07-29). Two reasons: the
        window above, and the fact that both gates primarily ride out a
        real-time transient (a container teardown, a zombie awaiting `wait()`),
        which is a single-worker property the lease never addressed.

        ## Why the lease uses a different Redis client than the rest of this file

        Deliberate, and NOT to be unified. Cycle state (`_read_prev_cycle_at`
        and friends) goes through `self._redis()` — the slot-service client,
        which **raises** on failure and is caught per call site. The lease goes
        through `get_breaker_redis()`, which **returns None** and is the
        fail-open contract this method is built on: moving it onto `_redis()`
        would silently convert "Redis down ⇒ act as sole leader" into
        "Redis down ⇒ raise, get swallowed by `_loop`, cycle never runs" —
        i.e. exactly the silence argued against above, arrived at by
        refactoring. `get_breaker_redis()` also sets `decode_responses=True`,
        which is what makes `r.get(...) == self._worker_id` a valid str
        comparison; a client constructed without it returns bytes and every
        ownership check silently becomes False (no refresh, no release, both
        workers acquiring in turn — a failure with no error in it anywhere).
        """
        r = get_breaker_redis()
        if r is None:
            return True  # fail-open: no Redis → behave as the sole worker

        ttl = self._leader_ttl()
        try:
            # Acquire only if free (atomic). `nx` guarantees a single winner
            # across workers even under a simultaneous race.
            if r.set(REDIS_KEY_LEADER, self._worker_id, nx=True, ex=ttl):
                return True
            # Already held — refresh the TTL only if the lease is OURS.
            return self._refresh_lease(r, ttl)
        except Exception as e:
            logger.warning("canary leader lock check failed-open (%s)", e)
            return True

    def _refresh_lease(self, r: Any, ttl: int) -> bool:
        """Re-arm the lease iff it is still ours. Atomic; never acquires.

        Returns True when the TTL was extended, i.e. when we still hold it.

        Deliberately not an acquire: the heartbeat's job is to KEEP a lease
        already won, so leadership changes stay a single decision point at the
        top of a cycle (`_is_cycle_leader`) — which is also the only writer of
        `self._is_leader` and the only place a transition is logged. A
        heartbeat that could acquire would move leadership silently between
        cycles, and the transition-only logging the whole design leans on would
        stop telling the truth.
        """
        scripts = _LEASE_SCRIPTS.ensure(r)
        return bool(
            scripts["refresh"](
                keys=[REDIS_KEY_LEADER], args=[self._worker_id, ttl], client=r
            )
        )

    def _release_leadership(self) -> None:
        """Delete the lease iff we hold it (atomic, best-effort, never raises).

        Compare-and-delete in Lua rather than GET-then-DELETE — see
        `_LEASE_RELEASE_LUA` for the successor's-lease race that shape has.
        """
        try:
            r = get_breaker_redis()
            if r is None:
                return
            scripts = _LEASE_SCRIPTS.ensure(r)
            scripts["release"](keys=[REDIS_KEY_LEADER], args=[self._worker_id], client=r)
        except Exception:
            pass

    def _is_cycle_leader(self) -> bool:
        """Leadership for this cycle, logged only on the TRANSITION.

        A non-leader worker must not narrate its own idleness every 5 minutes —
        that turns a healthy two-worker deployment into a log line every cycle
        forever, which is how real canary output gets tuned out. Acquiring and
        yielding are both single events worth one INFO line each; steady state
        on either side is silent.
        """
        leader = self._try_acquire_leadership()
        if leader and not self._is_leader:
            logger.info("canary acquired leadership (worker %s)", self._worker_id)
        elif not leader and self._is_leader:
            logger.info("canary yielded leadership (worker %s)", self._worker_id)
        self._is_leader = leader
        return leader

    # ------------------------------------------------------------------
    # Lease heartbeat (#1881)
    # ------------------------------------------------------------------

    def _cycle_elapsed_seconds(self) -> Optional[float]:
        """Seconds the in-flight cycle has been running, or None if idle."""
        started = self._cycle_started_at
        return None if started is None else time.monotonic() - started

    def _heartbeat_once(self) -> None:
        """One heartbeat tick: keep the lease alive, unless we look wedged.

        Only the holder beats — a non-leader has nothing to re-arm, and
        `_refresh_lease` could not acquire for it even if it tried.

        The wedge check is the second half of the constants block's split. A
        cycle merely being SLOW must not cost us the lease (that was the
        pre-heartbeat defect: one wedged `container.exec_run` past the TTL
        handed leadership to a sibling mid-cycle and restored concurrent
        cycles). But a cycle that has blown `_MAX_CYCLE_LEASE_SECONDS` is no
        longer plausibly slow, and holding a lease we cannot act on would leave
        the fleet with nobody watching — the failure mode this subsystem exists
        to catch. So we stop refreshing and let it lapse: loud, visible
        duplication beats quiet blindness.
        """
        if not self._is_leader:
            return
        elapsed = self._cycle_elapsed_seconds()
        if elapsed is not None and elapsed > _MAX_CYCLE_LEASE_SECONDS:
            if not self._wedge_reported:
                self._wedge_reported = True
                logger.error(
                    "canary: cycle has run %.0fs (cap %ds) — leader %s looks "
                    "wedged; no longer refreshing `%s` so a sibling can take "
                    "over. Expect overlapping cycles until this one returns.",
                    elapsed,
                    _MAX_CYCLE_LEASE_SECONDS,
                    self._worker_id,
                    REDIS_KEY_LEADER,
                )
            return
        r = get_breaker_redis()
        if r is None:
            # Fail-open, consistent with `_try_acquire_leadership`: there is no
            # lease to re-arm, and with Redis down every worker acts as leader
            # anyway. Nothing to log — the acquire path already warns.
            return
        self._refresh_lease(r, self._leader_ttl())

    async def _heartbeat_loop(self):
        """Re-arm the leader lease on a short timer, independent of the cycle.

        This is the mechanism that lets `_LEADER_TTL_SECONDS` be small: because
        the lease is kept alive from here rather than from the top of a cycle,
        the TTL only has to survive a couple of missed beats instead of a
        worst-case cycle, and a crashed leader is therefore noticed in minutes
        rather than in ~20. Sleeps first so a cold start doesn't beat before
        the loop has ever evaluated leadership.
        """
        while self._running:
            try:
                await asyncio.sleep(_LEADER_HEARTBEAT_SECONDS)
            except asyncio.CancelledError:
                raise
            try:
                self._heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A dead heartbeat means the lease lapses and leadership churns
                # every cycle — degraded but not incorrect. Never let one bad
                # beat end the task.
                logger.exception("canary: leader heartbeat raised; continuing")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _loop(self):
        """Loop forever: cycle → sleep → cycle."""
        # Short delay so the backend is fully ready before the first cycle —
        # avoids a noisy "sources_unavailable" log line during cold start.
        await asyncio.sleep(30)
        while self._running:
            try:
                # #1881: only the lease-holding worker runs the scheduled cycle.
                # The gate is here rather than inside `run_cycle` on purpose —
                # see that method's docstring. A non-leader returns BEFORE
                # `collect_snapshot`, which is the point: the cost being removed
                # is R-01's per-agent `docker exec` sweep, not the bookkeeping
                # after it.
                if self._is_cycle_leader():
                    await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a cycle exception kill the loop. Log and retry
                # next interval. Mirrors cleanup_service.
                logger.exception("canary cycle raised; will retry next interval")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self,
        invariant_ids: Optional[Iterable[str]] = None,
    ) -> CycleResult:
        """Run one canary cycle. Public so it can be invoked from tests
        or by the operator via `POST /api/canary/run-cycle`.

        Returns a `CycleResult` carrying the per-invariant violation
        lists this cycle produced *and* the subset classified as
        green→red transitions. Both pieces of truth come from the same
        code path the background loop uses, so the on-demand endpoint
        cannot disagree with the alert sink (when wired).

        **Deliberately NOT leader-gated (#1881).** The lease belongs to the
        *scheduled* path (`_loop`), which is the one that runs unattended in
        every worker. `POST /api/canary/run-cycle` lands on whichever worker
        uvicorn happens to route it to, so gating here would make an explicit
        admin request return an empty result roughly half the time on
        `--workers 2` — structurally identical to a green cycle, which is the
        exact ambiguity the `skipped`/409 contract above exists to remove. An
        operator asking for a cycle is not a duplicate probe; it is the one
        cycle they asked for.

        **The ungated manual path DOES drive the alert sink**, so it cannot
        swallow a transition — traced, not assumed, because the lease makes
        this the one remaining multi-writer path. `run_cycle` →
        `_run_cycle_inner` → `CanaryAlerts.emit_transition` carries no
        leadership check anywhere along it, so a green→red that a manual cycle
        on a NON-leader worker is first to see still alerts, exactly once, from
        that worker. The leader's next cycle then reads the same shared
        `canary:last_cycle_at` / `canary:last_cycle_red` markers the manual
        cycle advanced, classifies the still-red invariant as a continuation,
        and correctly stays quiet. What a manual cycle on a non-leader *does*
        do is advance those markers and write E-02's `terminal_seen` and
        H-01's `suspect_since` — visible, and by design. What it cannot do is
        consume a transition silently.
        """
        if self._lock.locked():
            logger.debug("canary: cycle already in progress, skipping")
            return CycleResult(skipped=True)
        async with self._lock:
            # #1881: the heartbeat needs to know a cycle is in flight, so a
            # slow one cannot lose the lease, and how long it has been running,
            # so a wedged one eventually does. Cleared in `finally` — an
            # exception must not leave a permanent phantom wedge behind.
            self._cycle_started_at = time.monotonic()
            self._wedge_reported = False
            try:
                return await self._run_cycle_inner(invariant_ids)
            finally:
                self._cycle_started_at = None

    async def _run_cycle_inner(
        self,
        invariant_ids: Optional[Iterable[str]],
    ) -> CycleResult:
        # Capture pre-cycle state for green→red transition detection.
        # Two reads BEFORE the cycle runs:
        #   1. previous_latest — the most recent persisted violation per
        #      invariant id. Tells us "when was this invariant last red".
        #   2. prev_cycle_at — snapshot_time of the prior cycle (any cycle,
        #      regardless of outcome). Tells us "when did we last look".
        #
        # An invariant transition (green→red) fires only when (a) the
        # invariant has violations this cycle AND (b) the previous cycle
        # was green for it — i.e. the latest stored violation predates
        # the previous cycle's snapshot. This is the only rule that
        # silences continuously-red invariants without losing real
        # green→red flips, including red→green→red.
        #
        # The DB read is fail-open (#1813). It used to be unguarded, which made
        # a database outage abort the cycle before `collect_snapshot` ran — so
        # H-01, whose entire job is to announce that the harness cannot see the
        # fleet, never executed on the most total blindness there is. `{}` here
        # would by itself make every violation read as a fresh flip, so when the
        # primary is unreadable we fall back to a Redis-held record of the
        # previous cycle's red set (a separate failure domain from the DB) and
        # dedupe against that instead.
        previous_latest: Dict[str, dict] = {}
        previous_latest_available = True
        try:
            previous_latest = db.get_latest_canary_violation_per_invariant()
        except Exception:
            previous_latest_available = False
            logger.exception(
                "canary: latest-violation read failed; running the cycle anyway "
                "with Redis-backed transition detection"
            )
        prev_cycle_at = self._read_prev_cycle_at()
        prev_red = self._read_prev_cycle_red()

        # Heavy work — synchronous SQLite + Redis reads. Offload to a thread
        # so we don't block the asyncio loop while sqlite3 is blocking.
        snapshot = await asyncio.to_thread(collect_snapshot)
        results = await asyncio.to_thread(run_invariants, snapshot, invariant_ids)

        persisted_count = 0
        # Index-aligned with `results[inv_id]` — `None` slot means insert
        # failed. The router uses these ids directly instead of re-querying
        # by (invariant_id, snapshot_time).
        persisted_ids: Dict[str, List[Optional[int]]] = {}
        for inv_id, vlist in results.items():
            inv_ids: List[Optional[int]] = []
            for v in vlist:
                try:
                    row_id = db.insert_canary_violation(
                        invariant_id=v.invariant_id,
                        tier=v.tier,
                        severity=v.severity,
                        snapshot_time=snapshot.snapshot_time,
                        observed_state=v.observed_state,
                        signal_query=v.signal_query,
                    )
                    inv_ids.append(row_id)
                    persisted_count += 1
                except Exception:
                    inv_ids.append(None)
                    logger.exception(
                        "canary: failed to persist violation %s; continuing",
                        v.invariant_id,
                    )
            persisted_ids[inv_id] = inv_ids

        # Detect green→red transitions and emit one notification per — and
        # (#1897) re-attempt any transition whose alert was never delivered,
        # for as long as its invariant is still red.
        #
        # The pending read sits HERE, immediately above the loop, rather than
        # beside the two Redis reads at the top of the cycle. Those run before
        # `collect_snapshot`, whose R-01 sweep `docker exec`s into every
        # running agent container and carries no timeout, so reading there
        # would widen the window in which another worker's `HDEL` goes unseen
        # from the insert loop to the entire cycle.
        pending_alerts = self._read_pending_alerts()
        # The cycle's clock, read from `snapshot.snapshot_time` and never
        # `time.time()` — same rule as R-01's dwell (ent#337). It makes the
        # budget boundary testable with a fixed instant instead of a margin,
        # and it is the timestamp both workers already agree on. `None` (an
        # unparseable stamp, which `utc_now_iso()` cannot actually produce)
        # disables retry for the cycle rather than guessing at ages.
        now = _parse_iso_to_unix(snapshot.snapshot_time)
        transition_ids: List[str] = []
        undelivered_ids: List[str] = []
        detected_count = 0
        previous_violation_at: Dict[str, Optional[str]] = {}
        for inv_id, vlist in results.items():
            # The red gate — load-bearing twice over since #1897. A pending
            # entry can only act on a cycle where its invariant is red, so a
            # retry can never fire for something that has gone green; and the
            # payload below is rebuilt from THIS cycle's violations, so a
            # retried alert is never replayed stale content.
            if not vlist:
                continue
            if previous_latest_available:
                is_transition = self._is_green_to_red(
                    inv_id, previous_latest, prev_cycle_at
                )
            else:
                # DB unreadable — `previous_latest` is empty for every
                # invariant, so the primary rule would classify all of them as
                # transitions on every cycle. Fall back to the Redis red set:
                # red last cycle ⇒ continuation, exactly the property the
                # primary rule provides. `None` (Redis also unreadable, or a
                # first-ever run) keeps the historical verbose-on-failure
                # behavior — the canary's reason to exist is catching flips.
                is_transition = prev_red is None or inv_id not in prev_red
            pending = pending_alerts.get(inv_id)
            # Nothing fresh AND nothing owed — the continuing-red case, which
            # stays silent exactly as before.
            if not is_transition and pending is None:
                continue
            if is_transition:
                detected_count += 1
                # Capture the prior snapshot_time BEFORE emit so the alert
                # sink can render "last red Xm ago". `previous_latest` was
                # loaded pre-cycle (line ~214) and is None when this is the
                # first-ever violation for the invariant — pass that through
                # honestly rather than papering over with the current cycle.
                prev = previous_latest.get(inv_id) or {}
                previous_violation_at[inv_id] = prev.get("snapshot_time")
            else:
                # Retry-only emit. Prefer the value captured when the flip was
                # first detected: recomputing it now would render "last red 5
                # minutes ago" — the failed episode itself — instead of the
                # honest pre-episode last-red.
                previous_violation_at[inv_id] = pending.get("previous_violation_at")
                if not self._retry_is_due(pending, now):
                    # Held off by the per-interval floor. Still undelivered,
                    # and reported as such.
                    undelivered_ids.append(inv_id)
                    continue
            delivered = await self._emit_and_record(
                inv_id,
                vlist,
                snapshot.snapshot_time,
                previous_violation_at[inv_id],
                persisted_ids.get(inv_id, []),
                pending=pending,
                now=now,
            )
            (transition_ids if delivered else undelivered_ids).append(inv_id)

        # Update counters + last-run.
        self.cumulative_cycles += 1
        self.cumulative_violations += persisted_count
        self.cumulative_transitions += len(transition_ids)
        self.cumulative_transitions_detected += detected_count
        self.last_run_at = snapshot.snapshot_time
        # Persist this cycle's snapshot_time for the NEXT cycle's transition
        # check. UNCONDITIONAL, and it must stay that way (#1897).
        #
        # This line used to be annotated as a defense — "done AFTER
        # notifications so a crash mid-emit doesn't advance the cursor and
        # silence a real transition on retry". That defense never fired: the
        # emit loop's own `except Exception` swallows the raise well before it
        # could reach here. And withholding the cursor would not retry the
        # failed alert even if it did fire — the invariant's own freshly
        # inserted row already post-dates the held-back cursor, so it still
        # reads as a continuation, while an unrelated red→green→red flip gets
        # silently swallowed (the walk is in REDIS_KEY_ALERT_PENDING's
        # comment). The ordering here is now purely conventional; the retry
        # mechanism is the per-invariant pending store.
        self._write_prev_cycle_at(snapshot.snapshot_time)
        # Companion record for the DB-down fallback above. Written on EVERY
        # cycle, not just when the DB is unreadable: the fallback is only
        # useful if it was already being maintained when the outage began.
        self._write_prev_cycle_red([i for i, v in results.items() if v])

        if persisted_count or snapshot.sources_unavailable:
            # `detected` and `delivered` are reported separately since #1897:
            # collapsing them is what let a rejected POST read as a sent one.
            logger.info(
                "canary cycle: violations=%d transitions_detected=%d "
                "alerts_delivered=%d alerts_undelivered=%d unavailable=%s",
                persisted_count,
                detected_count,
                len(transition_ids),
                len(undelivered_ids),
                snapshot.sources_unavailable,
            )

        return CycleResult(
            violations=results,
            persisted_violation_ids=persisted_ids,
            transition_invariant_ids=transition_ids,
            undelivered_invariant_ids=undelivered_ids,
            previous_violation_at=previous_violation_at,
            snapshot_time=snapshot.snapshot_time,
            sources_unavailable=list(snapshot.sources_unavailable),
        )

    async def _emit_and_record(
        self,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
        previous_violation_at: Optional[str],
        persisted_ids: List[Optional[int]],
        *,
        pending: Optional[dict],
        now: Optional[float],
    ) -> bool:
        """Emit one alert and reconcile the pending store. True iff delivered.

        Extracted rather than inlined: `_run_cycle_inner` is already long,
        and this is the unit the retry tests actually want to drive.

        ## Ordering: ARM, then POST, then `HDEL` on success

        Not arm-on-failure, and the difference is not stylistic.
        `asyncio.CancelledError` is **not** a subclass of `Exception`, and
        `stop()` cancels a live cycle on lifespan shutdown — so a SIGTERM
        landing inside `post_webhook`'s 5s await propagates straight out of
        the cycle: the `except` below never runs, nothing gets armed, the
        cursor write never happens, and the next cycle classifies the still
        red invariant as a continuation. That is #1897 verbatim, on every
        deploy that coincides with a red cycle. Arming first makes the
        pending entry survive cancellation, crash, OOM and SIGKILL alike,
        and it subsumes the raise case too — under arm-on-failure, an
        `emit_transition` that raises on its FIRST call has no entry to
        arm and degrades to exactly the bug.

        Cost is one `HSET` + one `HDEL` per delivered transition, and only
        on installs that actually have a webhook configured.
        """
        record = self._pending_record(
            pending,
            snapshot_time=snapshot_time,
            now=now,
            previous_violation_at=previous_violation_at,
        )
        # Silent-sink shortcut: with no webhook configured the outcome is
        # SKIPPED, so arming would be pure churn on the default deployment
        # (`CANARY_SLACK_WEBHOOK_URL` unset). Correctness never depends on
        # the guess — if the emit fails anyway we arm below.
        armed = pending is not None or self._alert_sink_configured()
        if armed:
            self._write_pending_alert(invariant_id, record)

        outcome: Any = AlertDelivery.FAILED
        error: Optional[str] = None
        try:
            result = await CanaryAlerts.emit_transition(
                invariant_id,
                violations,
                snapshot_time,
                previous_violation_at,
                persisted_ids,
            )
        except Exception:
            # A raise — a payload-builder bug of the #1987 over-long-header
            # class, say — is a failed delivery, not a third silent state.
            # `outcome` is already FAILED, so it falls through to exactly the
            # same bookkeeping and everything below keys off `outcome` alone.
            logger.exception(
                "canary: failed to emit transition notification for %s",
                invariant_id,
            )
            error = "emit_transition raised"
        else:
            # Normalised with `getattr`, deliberately: a test double that
            # returns a bare enum — or `None`, as
            # `tests/unit/test_1813_h01_collector_blindness.py` does — then
            # degrades to the pre-#1897 reading. Keying off an explicit
            # FAILED rather than `is DELIVERED` is what makes the permissive
            # direction identical to today's behaviour, so no stub can be
            # reclassified into a failure and arm a phantom retry.
            outcome = getattr(result, "outcome", result)
            error = getattr(result, "error", None)

        if outcome is not AlertDelivery.FAILED:
            if armed:
                self._clear_pending_alert(invariant_id)
            return True

        record["last_error"] = _truncate_error(error)
        if not armed:
            # The shortcut guessed wrong (the emit raised with no webhook
            # configured). Arm now so the retry still exists.
            self._write_pending_alert(invariant_id, record)
            armed = True

        elapsed = self._pending_run_age(record, now)
        if elapsed is not None and elapsed > MAX_ALERT_PENDING_AGE_SECONDS:
            self._clear_pending_alert(invariant_id)
            self.cumulative_alerts_dropped += 1
            # The ONLY ERROR on this path — a per-attempt failure stays a
            # WARNING — so "we stopped trying" is never silent. It names the
            # cause because the give-up routinely runs several cycles later,
            # and often on a different worker, than the attempt that saw the
            # rejection. `last_error` is Slack's own response body;
            # `post_webhook` guarantees the webhook URL (which IS the
            # credential) is never in it, and the payload — agent names,
            # execution ids, G-04 credential pointers — is never logged here
            # at all.
            logger.error(
                "canary: GIVING UP on the %s alert after %ds and %d attempt(s); "
                "it was detected and never delivered. Last webhook error: %s",
                invariant_id,
                int(elapsed),
                record.get("attempts", 0),
                record.get("last_error"),
            )
            return False

        self._write_pending_alert(invariant_id, record)
        logger.warning(
            "canary: %s alert undelivered (attempt %d); will retry while it "
            "stays red. Last webhook error: %s",
            invariant_id,
            record.get("attempts", 0),
            record.get("last_error"),
        )
        return False

    def _pending_record(
        self,
        pending: Optional[dict],
        *,
        snapshot_time: str,
        now: Optional[float],
        previous_violation_at: Optional[str],
    ) -> dict:
        """Build the pending record for this attempt. Pure — writes nothing.

        `first_failed_at` is **first-write-wins within a run**: carried
        forward verbatim, never re-stamped while the run continues. It is
        the budget anchor, so this is load-bearing rather than cosmetic —
        re-stamping an anchor every cycle is behaviourally indistinguishable
        from a correct implementation right up until the thing it anchors
        stops working, which is precisely how R-01's dwell went permanently
        blind (learnings 2026-08-05).

        A gap of more than `_ALERT_RUN_DECAY_INTERVALS` intervals since the
        last attempt ends the run and starts a fresh window, so brief
        separated flaps cannot spend the budget a later long outage needs.
        """
        prior = pending or {}
        first_failed_at = prior.get("first_failed_at")
        attempts = _coerce_int(prior.get("attempts"))
        last_error = prior.get("last_error")
        last_attempt = _parse_iso_to_unix(prior.get("last_attempt_at"))
        run_start = _parse_iso_to_unix(first_failed_at)

        new_run = (
            run_start is None
            or now is None
            or last_attempt is None
            or (now - last_attempt) > self.interval * _ALERT_RUN_DECAY_INTERVALS
        )
        if new_run:
            first_failed_at = snapshot_time
            attempts = 0
            last_error = None

        return {
            "first_failed_at": first_failed_at,
            "last_attempt_at": snapshot_time,
            "previous_violation_at": previous_violation_at,
            # Diagnostic only — the budget is the age below. Logged at
            # give-up so the ERROR can say how hard we tried.
            "attempts": attempts + 1,
            "last_error": last_error,
        }

    @staticmethod
    def _pending_run_age(record: dict, now: Optional[float]) -> Optional[float]:
        """Seconds since this contiguous failure run started, or None.

        None (an unparseable clock at either end) means the budget cannot be
        evaluated, and the caller keeps the entry armed rather than dropping
        an alert on the strength of a timestamp it could not read.
        """
        run_start = _parse_iso_to_unix(record.get("first_failed_at"))
        if now is None or run_start is None:
            return None
        return now - run_start

    def _retry_is_due(self, pending: dict, now: Optional[float]) -> bool:
        """One retry per invariant per interval, on EVERY path.

        `run_cycle()` is deliberately not leader-gated, so an admin
        smoke-testing during a Slack blip — far and away the most likely
        reason anyone touches that endpoint — could otherwise spend the
        whole 30-minute window in about thirty seconds and force a give-up
        before the scheduled loop ever retried once. This floor is what
        makes "the cycle interval IS the retry spacing" true on the manual
        path too, and it collapses most of the two-worker duplicate window
        as a side effect.

        An unreadable clock returns False: no retry rather than an
        unspaced one.
        """
        if now is None:
            return False
        last_attempt = _parse_iso_to_unix(pending.get("last_attempt_at"))
        if last_attempt is None:
            return True
        return (now - last_attempt) >= self.interval

    # ------------------------------------------------------------------
    # Cycle-state side-table (Redis)
    # ------------------------------------------------------------------

    @staticmethod
    def _redis():
        """Redis client shared with the slot service. Lazy import so this
        module stays loadable in tests without a live Redis.

        Deliberately NOT the same client the leader lease uses: that one is
        `redis_breaker_util.get_breaker_redis()`, whose fail-open wrapper is
        calibrated for the breaker path. Everything in this section is
        cycle-state bookkeeping and handles its own errors, so the two
        coexist on purpose — do not "unify" them.
        """
        from services.slot_service import get_slot_service

        return get_slot_service().redis

    def _read_prev_cycle_at(self) -> Optional[str]:
        """Snapshot_time of the prior cycle, or None on first ever run.

        Falls back to None on any Redis error — that turns the next
        cycle's transition detection into "all violations are transitions"
        for that single cycle, which is verbose but never misses a real
        flip. We chose verbose-on-failure over silent-on-failure because
        the canary's whole reason to exist is catching transitions.
        """
        try:
            return self._redis().get(REDIS_KEY_LAST_CYCLE)
        except Exception:
            logger.exception("canary: failed to read previous-cycle marker")
            return None

    def _write_prev_cycle_at(self, snapshot_time: str) -> None:
        """Advance the previous-cycle cursor to this cycle's snapshot_time."""
        try:
            self._redis().set(REDIS_KEY_LAST_CYCLE, snapshot_time)
        except Exception:
            logger.exception("canary: failed to persist previous-cycle marker")

    def _read_prev_cycle_red(self) -> Optional[set]:
        """Invariant ids that were red last cycle, or None if unknown.

        `None` (never written, unparseable, or Redis down) is deliberately
        distinct from `set()` (last cycle was fully green): the DB-down
        fallback treats unknown as "notify" and a known-green as "this is a
        flip", and collapsing the two would either spam or silence.
        """
        try:
            raw = self._redis().get(REDIS_KEY_LAST_CYCLE_RED)
        except Exception:
            logger.exception("canary: failed to read previous-cycle red set")
            return None
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("canary: unparseable previous-cycle red set %r", raw)
            return None
        return set(parsed) if isinstance(parsed, list) else None

    def _write_prev_cycle_red(self, invariant_ids: List[str]) -> None:
        """Record which invariants were red, for the DB-down fallback.

        TTL'd rather than permanent: a stale set from an hours-old cycle is a
        worse authority than "unknown", which at least fails toward notifying.
        Sized well above the interval so a couple of missed cycles don't drop
        it, and it is rewritten every cycle anyway.
        """
        try:
            self._redis().set(
                REDIS_KEY_LAST_CYCLE_RED,
                json.dumps(sorted(invariant_ids)),
                ex=self.interval * 12,
            )
        except Exception:
            logger.exception("canary: failed to persist previous-cycle red set")

    @staticmethod
    def _alert_sink_configured() -> bool:
        """Whether an outbound webhook exists at all (#1897).

        Read directly rather than plumbed back from `CanaryAlerts`, which is
        a stateless composer and should stay one. This only decides whether
        to pay an `HSET`+`HDEL` on a transition that is going to be SKIPPED
        anyway — never whether an alert is retried.
        """
        return bool(os.getenv("CANARY_SLACK_WEBHOOK_URL", "").strip())

    def _read_pending_alerts(self) -> Dict[str, dict]:
        """Undelivered alerts by invariant id (#1897). Fail-open to `{}`.

        Parsed PER FIELD. The whole hash arrives in a single `HGETALL`, so a
        naive parse would let one corrupt value disable retry for every
        other invariant at once. And it IS parsed input — read back out of
        Redis, not trusted because we wrote it — so a value that is not a
        JSON object is treated as absent with a WARNING, mirroring
        `_read_prev_cycle_red`.

        Redis unreadable ⇒ no retries this cycle, which is exactly the
        pre-#1897 behaviour and never worse. A retry mechanism that could
        wedge the harness would be worse than the bug it fixes.
        """
        try:
            raw = self._redis().hgetall(REDIS_KEY_ALERT_PENDING) or {}
        except Exception:
            logger.exception("canary: failed to read the pending-alert store")
            return {}

        pending: Dict[str, dict] = {}
        for invariant_id, value in raw.items():
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                logger.warning(
                    "canary: unparseable pending alert for %s; treating as absent",
                    invariant_id,
                )
                continue
            if not isinstance(parsed, dict):
                logger.warning(
                    "canary: pending alert for %s is not an object; treating as absent",
                    invariant_id,
                )
                continue
            pending[invariant_id] = parsed
        return pending

    def _write_pending_alert(self, invariant_id: str, record: dict) -> None:
        """Arm/refresh one pending alert. Never raises (#1897).

        A write failure means the alert is lost exactly as it was before
        #1897 — degraded, never worse, and never a reason to break a cycle.
        """
        try:
            self._redis().hset(
                REDIS_KEY_ALERT_PENDING, invariant_id, json.dumps(record)
            )
        except Exception:
            logger.exception(
                "canary: failed to record the undelivered %s alert; it will not "
                "be retried",
                invariant_id,
            )

    def _clear_pending_alert(self, invariant_id: str) -> None:
        """Drop one pending alert — on delivery, or on give-up."""
        try:
            self._redis().hdel(REDIS_KEY_ALERT_PENDING, invariant_id)
        except Exception:
            logger.exception(
                "canary: failed to clear the pending %s alert", invariant_id
            )

    @staticmethod
    def _is_green_to_red(
        invariant_id: str,
        previous_latest: dict,
        prev_cycle_at: Optional[str],
    ) -> bool:
        """Decide whether this cycle's violation is a fresh transition.

        Green→red iff the latest persisted violation for this invariant
        predates the previous cycle's snapshot_time. Cases:

        - First-ever cycle (prev_cycle_at is None): every violation is a
          transition. Operators expect to be told once when the harness
          first starts seeing problems.
        - First-ever violation (previous_latest entry absent): transition.
        - Continuing-red (latest violation timestamp == prev_cycle_at):
          continuation, no notification — the previous cycle saw it too.
        - Red→green→red (latest violation predates prev_cycle_at):
          transition — there was at least one clean cycle in between.
        """
        prev = previous_latest.get(invariant_id)
        if prev is None:
            return True
        if prev_cycle_at is None:
            return True
        # `<` so a same-snapshot replay from an immediate manual rerun
        # is treated as a continuation rather than re-firing.
        return prev["snapshot_time"] < prev_cycle_at


# Module-level singleton, mirrors cleanup_service.
canary_service = CanaryService()
