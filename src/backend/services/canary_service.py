"""
Canary watcher service (CANARY-001 / Issue #411).

Runs in the backend process. Every 5 minutes:

1. `collect_snapshot()` — read Redis, SQLite, agent registries.
2. `run_invariants(snapshot)` — apply S-01 / E-02 / L-03 (Phase 1 set).
3. Persist any violations to `canary_violations`.
4. Detect green→red transitions per invariant against the previously-stored
   latest violation; fire one Slack alert per transition via incoming
   webhook (`CANARY_SLACK_WEBHOOK_URL` env var; unset = silent sink).

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
a TTL, own-lease-only refresh, best-effort release on shutdown. Every worker
still runs its loop and re-checks leadership each cycle, so a dead leader's
lease expires and a sibling takes over with no restart.

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
import uuid
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from canary import collect_snapshot, run_invariants
from canary.snapshot import ViolationReport
from database import db
from redis_breaker_util import get_breaker_redis
from services.canary_alerts import CanaryAlerts


@dataclass
class CycleResult:
    """Outcome of one canary cycle.

    `violations` is the per-invariant list of `ViolationReport`s the
    deterministic library produced (now persisted to `canary_violations`).

    `transition_invariant_ids` is the subset the service classified as
    a fresh green→red flip this cycle, not a continuation of an
    already-known violation. The router exposes this directly to
    operators so the on-demand `/api/canary/run-cycle` response matches
    what the alert sink (when wired) will see.

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
    # snapshot_time of the most recent prior violation per transitioning
    # invariant — used by the alert sink to render "last red 2h ago" and
    # by the run-cycle endpoint to surface `previous_violation_at`. Only
    # populated for invariants in `transition_invariant_ids`. Value is
    # `None` if this is the first-ever violation for that invariant.
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

# #1881: single Redis key holding the current canary leader's worker id. One
# leader across all uvicorn workers — whoever holds it runs the scheduled cycle,
# the rest idle. Global (not `agent:`-keyed), so — like `canary:last_cycle_*` and
# `canary:e02:*` — it is legitimately absent from #1560's `CLEARED_KEYSPACES`.
REDIS_KEY_LEADER = "canary:leader"

# Floor under the lease TTL, in seconds. The TTL is refreshed once at the TOP of
# each cycle, so it must outlast one worst-case cycle PLUS the inter-cycle sleep
# or the lease lapses mid-cycle, a sibling grabs it, and leadership flaps — which
# restores exactly the concurrent double-probing the lease exists to remove.
#
# `interval * 3` (monitoring #1464's shape) is the right scaling for a leader
# whose work scales with its interval. A canary cycle's cost does NOT: it is
# dominated by R-01's `container.exec_run` sweep across every running agent
# container, which is unbounded by any timeout and scales with FLEET SIZE, not
# with how often we look. So a shortened interval must not shorten the TTL below
# what one sweep can take. The floor is set at the value `interval * 3` already
# yields at the default 300s interval, so the default is unchanged and this only
# binds if someone constructs the service with a shorter interval.
_LEADER_TTL_FLOOR_SECONDS = 900


class CanaryService:
    """Background watcher loop for the canary invariant harness."""

    def __init__(self, interval_seconds: int = CANARY_INTERVAL_SECONDS):
        self.interval = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        # #1881: unique per worker process so the cross-worker leader lease is
        # only ever refreshed/released by ITS OWN holder. pid is a readable
        # prefix for log triage; the uuid suffix disambiguates a recycled pid.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs
        # Counters surface in /api/health-style monitoring; useful for
        # confirming the service is actually firing on deployed instances.
        self.cumulative_cycles: int = 0
        self.cumulative_violations: int = 0
        self.cumulative_transitions: int = 0
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
        logger.info(f"canary watcher started (interval={self.interval}s)")

    def stop(self):
        """Stop the background loop cleanly."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # #1881: hand leadership off immediately on a graceful shutdown rather
        # than leaving the surviving worker idle for up to a full TTL. Matters
        # more here than for the precedents: the sibling idling means nobody is
        # watching, and an unwatched window is the silent-green failure H-01
        # exists to announce. Never raises.
        self._release_leadership()
        logger.info("canary watcher stopped")

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv("CANARY_ENABLED", "0") == "1"

    # ------------------------------------------------------------------
    # Cross-worker leader lease (#1881)
    # ------------------------------------------------------------------

    def _leader_ttl(self) -> int:
        """Lease TTL — long enough that one cycle plus one sleep cannot expire
        it, short enough that a dead holder's lease lapses and a sibling takes
        over. See `_LEADER_TTL_FLOOR_SECONDS` for why the floor exists."""
        return max(self.interval * 3, _LEADER_TTL_FLOOR_SECONDS)

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
            if r.get(REDIS_KEY_LEADER) == self._worker_id:
                r.expire(REDIS_KEY_LEADER, ttl)
                return True
            return False
        except Exception as e:
            logger.warning("canary leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        """Delete the lease iff we hold it (best-effort, never raises)."""
        try:
            r = get_breaker_redis()
            if r is not None and r.get(REDIS_KEY_LEADER) == self._worker_id:
                r.delete(REDIS_KEY_LEADER)
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
        """
        if self._lock.locked():
            logger.debug("canary: cycle already in progress, skipping")
            return CycleResult(skipped=True)
        async with self._lock:
            return await self._run_cycle_inner(invariant_ids)

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

        # Detect green→red transitions and emit one notification per.
        transition_ids: List[str] = []
        previous_violation_at: Dict[str, Optional[str]] = {}
        for inv_id, vlist in results.items():
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
            if not is_transition:
                continue
            # Capture the prior snapshot_time BEFORE emit so the alert
            # sink can render "last red Xm ago". `previous_latest` was
            # loaded pre-cycle (line ~214) and is None when this is the
            # first-ever violation for the invariant — pass that through
            # honestly rather than papering over with the current cycle.
            prev = previous_latest.get(inv_id) or {}
            previous_violation_at[inv_id] = prev.get("snapshot_time")
            try:
                await CanaryAlerts.emit_transition(
                    inv_id,
                    vlist,
                    snapshot.snapshot_time,
                    previous_violation_at[inv_id],
                    persisted_ids.get(inv_id, []),
                )
                transition_ids.append(inv_id)
            except Exception:
                logger.exception(
                    "canary: failed to emit transition notification for %s",
                    inv_id,
                )

        # Update counters + last-run.
        self.cumulative_cycles += 1
        self.cumulative_violations += persisted_count
        self.cumulative_transitions += len(transition_ids)
        self.last_run_at = snapshot.snapshot_time
        # Persist this cycle's snapshot_time for the NEXT cycle's transition
        # check. Done AFTER notifications so a crash mid-emit doesn't
        # advance the cursor and silence a real transition on retry.
        self._write_prev_cycle_at(snapshot.snapshot_time)
        # Companion record for the DB-down fallback above. Written on EVERY
        # cycle, not just when the DB is unreadable: the fallback is only
        # useful if it was already being maintained when the outage began.
        self._write_prev_cycle_red([i for i, v in results.items() if v])

        if persisted_count or snapshot.sources_unavailable:
            logger.info(
                "canary cycle: violations=%d transitions=%d unavailable=%s",
                persisted_count,
                len(transition_ids),
                snapshot.sources_unavailable,
            )

        return CycleResult(
            violations=results,
            persisted_violation_ids=persisted_ids,
            transition_invariant_ids=transition_ids,
            previous_violation_at=previous_violation_at,
            snapshot_time=snapshot.snapshot_time,
            sources_unavailable=list(snapshot.sources_unavailable),
        )


    # ------------------------------------------------------------------
    # Cycle-state side-table (Redis)
    # ------------------------------------------------------------------

    @staticmethod
    def _redis():
        """Redis client shared with the slot service. Lazy import so this
        module stays loadable in tests without a live Redis."""
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
