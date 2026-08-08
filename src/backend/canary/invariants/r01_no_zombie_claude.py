"""
R-01 — No PERSISTING zombie Claude processes (CANARY-001 / #411 Phase 3).

For every running Trinity agent container, no zombie `claude` process may
remain unreaped for longer than the dwell window below.

A nonzero zombie count means a `claude` child exited but was not reaped by
its parent. PR #407 fixed the case where the agent-server stopped calling
`wait()` on its Claude subprocesses; R-01 is the regression guard for that
bug class.

## Why a dwell gate exists (ent#337)

R-01 used to fire **critical** on any single positive sample. But a zombie
between child-exit and the parent's `wait()` is a normal transient state in
any process tree, so the check was firing on the sampling window rather than
on the leak it was built to catch. On eu2 it produced 6 critical pages in
~13h, every one `zombie_count: 1` and every one already self-resolved by the
next cycle — on containers with 13h uptime that were never restarted, so
nothing external reaped them: the ordinary parent `wait()` did, within one
5-minute cycle.

The harm model in this invariant's own docstring is explicitly **cumulative**:
zombies hold a PID and eventually exhaust the container's process table. A
single zombie that clears within one cycle exhausts nothing. What is worth
paging on is a count that PERSISTS or GROWS — and a genuine #407-class leak is
monotonic, because the whole point of that bug was that `wait()` stopped being
called at all.

## Why the dwell is measured on PIDs, not on the count

A count cannot support a dwell. Three consecutive samples that each catch a
DIFFERENT short-lived zombie are indistinguishable from one zombie stuck for
the whole window, and on a busy agent that is routine — precisely the agents
that filed this bug. "Cleared when the count returns to 0" does not save it:
that requires a zero to be OBSERVED between them, and under sustained load a
zero is exactly what you do not observe.

So the collector reports the zombie PID **set**, and the dwell is the
intersection across the window: R-01 fires only when a SPECIFIC pid was present
at first-seen and is still present now. A pid present at T and T+dwell has
demonstrably dwelled — a measurement, not an inference. It also makes "the
count is growing" a real signal (new pids appearing while old ones persist)
rather than an artifact of sampling.

Two things could break that assumption, and they need different answers:

- **PID reuse inside a RUNNING container.** Needs the container's pid space to
  wrap (default `pid_max` is ~4.2M) within 10 minutes AND to land on this exact
  number AND for the new holder to also be a zombie `claude`. Accepted as a
  residual — the alternative (start time from `/proc`) buys nothing here, per
  the next paragraph.
- **A container RESTART.** Not a residual — a real re-arm of the false positive,
  and the `pid_max` argument does not cover it. A restart outside the backend
  (`docker restart`, a restart policy after an OOM kill or a crash) gives a
  fresh PID namespace that hands out LOW pids immediately, and a zombie `claude`
  in a freshly restarted agent is exactly the low-pid case; `clear_agent_breakers`
  only covers backend-mediated lifecycle events, so the marker would survive.
  The snapshot therefore carries the container's `State.StartedAt`
  (`snapshot.zombie_container_started_at`), stored in a reserved
  `__started_at` field on the marker HASH: when it moves, the whole marker is
  dropped and every pid restarts its dwell. It is free — docker-py's
  `containers.list()` already full-inspects each container — and it is the
  *generation* of the namespace, which is what pid identity is actually scoped
  to. When the value is not observable the marker is left ALONE rather than
  restarted: an unreadable field is not evidence of a restart, and treating it
  as one would blind the invariant every cycle.

The same reasoning rejects reading `ps -eo etimes` to age the zombie in a
single sample, which looks cleaner and sidesteps all cross-cycle state:
`etimes` is elapsed time since the process **started**, not since it became a
zombie, and Linux exposes no zombie-transition timestamp. A `claude` process
that ran 45 minutes and zombified 200ms ago reads `etimes=2700` and would fire
immediately — the exact transient this gate exists to stop flagging.

## Why elapsed wall-clock, not a cycle count

"Seen in N consecutive cycles" is wrong under multiple uvicorn workers: when
this gate was written each ran its own cycle loop with no cross-worker lease, so
two workers sampling ~1s apart would self-confirm a 2-cycle rule instantly. The
eu2 data shows exactly that — two R-01 rows 0.8s apart for the same condition.
Elapsed wall-clock is immune: extra observers cannot make time pass. This
follows H-01's precedent, which chose elapsed time over cycle counts for the
same reason.

trinity#1881 part 2 has since given the service a `canary:leader` lease, and
that changes nothing here. The lease is best-effort and fails open to leader
when Redis is unreachable, so concurrent loops over this shared marker are still
reachable — and more to the point, what this dwell measures is how long a
specific pid has SURVIVED, which is a wall-clock quantity whether one worker is
sampling it or four. Do not convert it back to a count.

The clock is read from `snapshot.snapshot_time`, never `time.time()`. That is
what makes the boundary testable with a fixed instant rather than a margin
(learnings 2026-08-03 / #1909: a test that builds a boundary fixture from its
own clock races the implementation's clock). The two instants are now adjacent:
since #1813 the Docker collector runs FIRST in `collect_snapshot`, immediately
after `snapshot_time` is stamped, so the effective dwell is the nominal one.
(It previously ran last, biasing the dwell slightly LONG — the safe direction —
so nothing depended on the gap; do not reintroduce a dependency on it. The
ordering is owned by H-01, which needs Docker evidence on the arm where the
roster read early-returns.)

## Marker state

Redis HASH per agent, `agent:canary_zombie:{name}`, field = pid:

    {pid: "<first_seen_unix>:<first_count>:<last_seen_unix>"}

plus one reserved non-numeric field carrying the container generation:

    {"__started_at": "<container State.StartedAt>"}

Reserved-field collision is structurally impossible: every other field is
`str(pid)`, i.e. digits only.

Deliberately under the `agent:` prefix rather than `canary:`: it is the first
**name-keyed** canary key, so it inherits the #1560 recycled-name hazard
(purge `foo` mid-dwell, a new `foo` appears, the fresh container inherits
`first_seen` and pages on its first transient). The `agent:` prefix makes
`tests/unit/test_1560_agent_redis_key_parity.py` require it to be registered in
`services/agent_runtime_state.py`, and it is cleared on every lifecycle event
there. E-02's `canary:e02:*` keys are legitimately unregistered because they
are global, not per-agent.

Three independent bounds keep it from going stale:

- **TTL**, refreshed on every positive observation. Bounds memory and cleans up
  after a deleted / renamed / discarded-ghost agent that the lifecycle hooks
  miss.
- **Continuity check** via `last_seen`. A docker-exec gap (container stopped,
  exec failed, backend down for an hour) leaves the marker untouched; without
  this, `now − first_seen` would exceed the dwell on the next successful sample
  and fire immediately with no evidence of continuity at all. A gap longer than
  `_MAX_OBSERVATION_GAP_SECONDS` restarts the dwell.
- **Generation check** via `__started_at`. The continuity check above cannot see
  a container restart: a restart inside one cycle leaves no observation gap at
  all, so `last_seen` stays fresh while the pid space underneath it is brand
  new. See the container-restart bullet above.

## Severity

Tier A, critical — for a PERSISTING or GROWING count. A zombie leak in an agent
container is a slow-fuse breakage: the agent keeps running fine for minutes,
then suddenly can't fork anything. A transient single zombie deliberately
produces no violation row at all.

## Source caveats

- Docker client unavailable (test/embedded mode): the snapshot records
  `docker: client unavailable` in `sources_unavailable` and produces no
  `zombie_counts` entries. Every agent reads as "no data, skip" — silently
  green, no false fires.
- Per-container exec failure: recorded as `docker.exec[name]: <reason>`. That
  agent is skipped and its marker is left intact (see the continuity check
  above); the rest of the cycle continues normally.
- Redis unavailable: the check cannot prove persistence, so it fires nothing
  and records the reason. Fail-open toward NOT firing is deliberate — a canary
  that pages on its own unreadable state is worse than one that stays quiet.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from ..snapshot import Snapshot, ViolationReport


logger = logging.getLogger(__name__)


INVARIANT_ID = "R-01"
TIER = "A"
SEVERITY = "critical"

# Per-agent marker HASH. `agent:`-prefixed on purpose — see the module
# docstring (#1560 parity + lifecycle clearing).
REDIS_KEY_PREFIX = "agent:canary_zombie:"

# How long a specific zombie pid must persist before it is a violation.
# 2x CANARY_INTERVAL_SECONDS (300) so a condition must survive at least one
# full cycle boundary. Not imported from canary_service: that module imports
# this package, so importing back would be a cycle.
DWELL_SECONDS = 600

# A gap longer than this between two observations of the same agent means we
# cannot claim continuity across it, so the dwell restarts. Sized at 2x the
# canary interval, i.e. one missed cycle is tolerated (clock jitter, a slow
# collection) but a real outage is not.
#
# The #1881 leader lease adds one more producer of a long gap: if the holding
# worker dies, nobody cycles until its lease expires and a sibling's next loop
# iteration picks it up. `canary_service._max_failover_seconds` owns that
# arithmetic — ~780s at the defaults (one interval of staleness before the
# crash + the 180s lease TTL + one interval before a sibling next looks). That
# still exceeds this window, so a leader failover restarts the dwell. Correct
# and deliberate: a crashed leader IS "a real outage" by this comment's own
# rule, and restarting costs at most one extra dwell before a genuine zombie
# fires, which is the fail-safe direction. Do not widen this constant to paper
# over it — claiming continuity across an interval nobody watched is the
# failure mode.
#
# (Before the lease's heartbeat the same window was ~1200s, because the TTL had
# to be sized to outlast a whole cycle. Shrinking it did not change the
# conclusion here, only the number — the failover window would have to drop
# under 600s to stop restarting the dwell, and it cannot: two of its three
# terms are the cycle interval itself.)
_MAX_OBSERVATION_GAP_SECONDS = 600

# Marker lifetime, refreshed on each positive observation. Bounds memory if an
# agent vanishes without a lifecycle hook firing (discarded ghost, purge).
_MARKER_TTL_SECONDS = 24 * 60 * 60

# Reserved marker field holding the container's `State.StartedAt` — the PID
# NAMESPACE GENERATION this dwell was measured in. Non-numeric on purpose:
# every other field in the HASH is `str(pid)`, so collision is impossible.
_GENERATION_FIELD = "__started_at"


def _redis():
    """Lazy import of the slot service's Redis client (mirrors E-02)."""
    from services.slot_service import get_slot_service

    return get_slot_service().redis


def _parse_iso_to_unix(ts: str) -> Optional[float]:
    """ISO-Z timestamp → unix epoch seconds. Mirrors S-03's helper."""
    if not ts:
        return None
    if ts.endswith("Z"):
        ts = ts[:-1]
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _decode_marker(raw: str) -> Optional[Tuple[float, int, float]]:
    """`"<first_seen>:<first_count>:<last_seen>"` → tuple, or None if unusable.

    A malformed value is treated as absent so the pid simply restarts its
    dwell — never as a reason to fire.
    """
    try:
        first_seen, first_count, last_seen = raw.split(":")
        return float(first_seen), int(first_count), float(last_seen)
    except (AttributeError, ValueError):
        return None


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per agent holding a zombie past the dwell window."""
    violations: List[ViolationReport] = []

    if not snapshot.zombie_counts and not snapshot.zombie_pids:
        # Docker wholesale-unavailable: nothing observed, nothing to judge.
        return violations

    now = _parse_iso_to_unix(snapshot.snapshot_time)
    if now is None:
        logger.warning("R-01: unparseable snapshot_time; skipping cycle")
        return violations

    try:
        redis_client = _redis()
    except Exception:
        logger.exception("R-01: redis unavailable; cannot judge persistence")
        return violations

    # Only agents we actually observed this cycle. An agent absent from
    # `zombie_pids` had its exec fail (already in `sources_unavailable`); its
    # marker is deliberately left untouched rather than cleared, so a genuine
    # leak is not reset by a flaky exec — the continuity check below is what
    # stops the gap from counting as dwell time.
    for agent_name in sorted(snapshot.zombie_pids):
        pids: Set[int] = snapshot.zombie_pids.get(agent_name) or set()
        key = f"{REDIS_KEY_PREFIX}{agent_name}"

        try:
            stored: Dict[str, str] = redis_client.hgetall(key) or {}
        except Exception:
            logger.exception("R-01: marker read failed for %s; skipping", agent_name)
            continue

        if not pids:
            # Observed zero — the condition genuinely resolved. Drop the whole
            # marker so a later leak starts a fresh dwell.
            if stored:
                try:
                    redis_client.delete(key)
                except Exception:
                    logger.exception("R-01: marker clear failed for %s", agent_name)
            continue

        # Container-generation check. A restart outside the backend gives a
        # fresh PID namespace, so every stored dwell was measured against pids
        # that no longer mean anything — drop them and start over. Done BEFORE
        # the per-pid loop so nothing can fire on an inherited `first_seen`.
        #
        # Only an OBSERVED MISMATCH invalidates. An agent absent from
        # `zombie_container_started_at` (field unreadable, sparse attrs) is a
        # non-signal, and restarting the dwell on a non-signal every cycle would
        # make the invariant permanently blind — the failure mode ent#337's own
        # first-write-WINS rule exists to avoid.
        generation = snapshot.zombie_container_started_at.get(agent_name)
        stored_generation = stored.get(_GENERATION_FIELD)
        if generation and stored_generation and stored_generation != generation:
            logger.info(
                "R-01: %s restarted (%s → %s); restarting zombie dwell",
                agent_name,
                stored_generation,
                generation,
            )
            try:
                redis_client.delete(key)
            except Exception:
                logger.exception("R-01: marker clear failed for %s", agent_name)
            stored = {}

        current_count = len(pids)
        live_fields = {str(p) for p in pids}
        # Reap marker fields for pids that are gone: those zombies were reaped,
        # which is the healthy outcome. Doing this per-pid is what makes a
        # succession of distinct transients self-clear WITHOUT ever needing an
        # observed zero — the case a count-based dwell cannot see.
        # `_GENERATION_FIELD` is not a pid and must survive the reap.
        departed = [
            f for f in stored if f != _GENERATION_FIELD and f not in live_fields
        ]

        oldest_first_seen: Optional[float] = None
        oldest_first_count: Optional[int] = None
        dwelled_pid: Optional[int] = None
        updates: Dict[str, str] = {}

        for pid in sorted(pids):
            marker = _decode_marker(stored.get(str(pid), ""))
            if marker is None:
                # New pid (or unusable marker) — start its dwell now.
                updates[str(pid)] = f"{now}:{current_count}:{now}"
                continue

            first_seen, first_count, last_seen = marker
            if now - last_seen > _MAX_OBSERVATION_GAP_SECONDS:
                # We were not watching across the gap, so we cannot claim this
                # pid persisted through it. Restart rather than fire.
                updates[str(pid)] = f"{now}:{current_count}:{now}"
                continue

            # Continuous observation — extend, keeping the ORIGINAL first_seen.
            # Writing `now` here every cycle is the classic dwell-timer bug: the
            # clock would reset each cycle and the dwell would never elapse,
            # leaving a permanently blind critical invariant.
            updates[str(pid)] = f"{first_seen}:{first_count}:{now}"

            if now - first_seen >= DWELL_SECONDS:
                if oldest_first_seen is None or first_seen < oldest_first_seen:
                    oldest_first_seen = first_seen
                    oldest_first_count = first_count
                    dwelled_pid = pid

        # Record (or refresh) the generation alongside the dwells it scopes, so
        # the very first observation of an agent already carries one and the
        # NEXT restart is detectable.
        if generation:
            updates[_GENERATION_FIELD] = generation

        try:
            if updates:
                redis_client.hset(key, mapping=updates)
            if departed:
                redis_client.hdel(key, *departed)
            redis_client.expire(key, _MARKER_TTL_SECONDS)
        except Exception:
            # A failed WRITE is the nastier half: the dwell stops advancing
            # while reads keep succeeding, so R-01 looks healthy and is blind.
            # Log loudly enough that it is greppable.
            logger.exception("R-01: marker write failed for %s", agent_name)

        if dwelled_pid is None:
            continue

        held_for = int(now - oldest_first_seen)
        first_seen_iso = (
            datetime.fromtimestamp(oldest_first_seen, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        violations.append(
            ViolationReport(
                invariant_id=INVARIANT_ID,
                tier=TIER,
                severity=SEVERITY,
                observed_state={
                    "agent_name": agent_name,
                    # Load-bearing key: the Slack renderers read it. Never
                    # rename — `.get("zombie_count", 0)` would silently render
                    # "0 zombie(s)" rather than failing loudly.
                    "zombie_count": current_count,
                    "first_seen_count": oldest_first_count,
                    "first_seen_at": first_seen_iso,
                    "held_for_seconds": held_for,
                    "dwell_seconds": DWELL_SECONDS,
                    "persisting_pid": dwelled_pid,
                    "snapshot_time": snapshot.snapshot_time,
                },
                signal_query=(
                    f"docker exec agent-{agent_name} sh -c "
                    "\"ps -eo stat,pid,comm | awk '$1 ~ /^Z/ && $3 == "
                    f"\\\"claude\\\" {{print $2}}'\" — pid {dwelled_pid} zombie "
                    f"for {held_for}s (dwell {DWELL_SECONDS}s); "
                    f"count {oldest_first_count} → {current_count}"
                ),
            )
        )

    return violations
