"""
H-01 — Collector blindness (CANARY-001 / Issue #1813, follow-up to #1540).

Every other invariant in this library answers "is the *system* broken?".
H-01 is the only one that answers "is the *harness* blind?" — which is why it
carries its own `H-` (harness health) id family rather than extending `G-`
(global/cross-cutting). A `G-` id would file a detector outage alongside real
system bugs in `/api/canary/violations` and read, to whoever is triaging, as a
platform defect rather than a broken instrument.

## The bug class

#1540 repointed the SQL-tier collectors off a hardcoded SQLite `/data/trinity.db`
onto the configured engine, so on PostgreSQL they read the live DB instead of a
frozen, near-empty file. It did not address the failure *shape*: a collector that
reads an empty or unreachable source returns **zero rows**, and zero rows is
indistinguishable from a genuinely clean fleet. Both produce a green cycle.

Empirically (verified against the real `collect_snapshot()` over the split
backend fixture): a two-agent fleet with a running execution, visible only to
the "wrong" database, yields `known_agents=set()`, `sources_unavailable=[]`, and
**zero violations across all invariants**. A silent all-clear while completely
blind — a smoke detector that goes quiet instead of chirping.

Prior partial coverage was worse than none: L-03 *does* fire in this state, but
only when an execution happens to hold a Redis slot (an idle fleet holds none),
and when it fires it reports `agent_name 'a1' ... absent from agent_ownership` —
sending the operator after a delete-cascade ghost-agent bug instead of a blind
collector. Intermittent AND mislabeled.

## The check

The roster read (`_collect_known_agents` → `snapshot.known_agents`) is the
load-bearing one: it gates the entire per-agent loop, so when it comes back
empty S-01/S-02/S-03/E-01/E-04/E-05/B-01/B-02/G-04 all go vacuously green.
H-01 is scoped to that read alone and deliberately NOT to "any SQL collector
reads zero rows": on a live-but-quiet fleet `terminal_exec_statuses`,
`terminal_rows`, `enabled_schedules` and `orphan_refs` are ALL legitimately
empty (verified), so a general rule would false-alarm on every idle install and
break the issue's own AC #3.

"Provably non-empty" comes from sources that do not touch the SQL tier under
test — otherwise the evidence is circular and the guard is decorative:

- **Docker** (`snapshot.docker_agent_names`) — running agent containers, read
  from the container list itself, not from `zombie_counts` (which is keyed by
  `exec_run` success and silently thins on a degraded container). This is the
  ONLY source that can confirm a contradiction, and it is collected FIRST in
  `collect_snapshot` — before the roster read, which returns early on failure —
  so it is available on every arm including `roster_read_failed`.
- **Redis** (`snapshot.orphan_redis_slots`) — with an empty roster, every
  `agent:slots:*` key buckets here. Slot keys exist only while an execution
  holds a slot, so an idle fleet legitimately has none. Strictly corroborating:
  it can raise a `roster_empty_unverifiable` into a louder signal_query, but it
  can never on its own make the outcome `critical` — see below.

| roster read      | independent evidence            | outcome              |
|------------------|---------------------------------|----------------------|
| non-empty        | —                               | pass, clear marker   |
| raised           | —                               | critical             |
| empty            | Docker proves agents exist      | critical             |
| empty            | Redis only                      | major                |
| empty            | source unavailable / not read   | major                |
| empty            | available, agrees empty         | pass, clear marker   |

Every arm that fires goes through the confirmation gate below — including
`roster_read_failed`. See "Sustained-condition confirmation".

## Why Redis alone cannot page critical

`orphan_redis_slots` is BY DEFINITION slot keys whose agent is absent from
`agent_ownership` — which is the leaked-slot state L-03 exists to report. On a
genuinely empty fleet holding one leaked slot key, Docker evidence is empty and
a naive union would fire `roster_empty_contradicted` / critical: a correct
roster, an unrelated Redis leak, and a critical page claiming the harness is
blind. So the severity ladder is asymmetric on purpose — Docker evidence is
required for `SEVERITY_CONFIRMED`; Redis-only evidence reports
`roster_empty_unverifiable` at `SEVERITY_UNVERIFIABLE`, which is the honest
description of that state (something is there, but nothing that can distinguish
"blind collector" from "L-03 leak"). Redis names still ride in
`evidence_sample`, so the operator sees them either way.

## Sustained-condition confirmation

Deleting an agent removes the DB row and the container as separate steps. If a
snapshot lands between them AND it was the last agent, the roster is empty while
Docker still shows a container — a false positive on an ordinary delete. Since
false positives are precisely how a safety net gets muted, a suspicious cycle
only *arms* a marker (`canary:h01:suspect_since`); the violation fires once the
condition has persisted for `CONFIRMATION_MIN_SECONDS`.

Confirmation is on **elapsed time, not on "a second cycle"**. Prod runs the
backend with `uvicorn --workers 2`. When this gate was written the canary
service held only a per-process `asyncio.Lock` and no cross-worker leader lease,
so two independent loops shared this one marker and a cycle-count rule would
have let worker B confirm worker A's sighting seconds later — collapsing the
gate to nothing exactly inside the teardown window it exists to ride out.

The service **does** hold a lease now (`canary:leader`, #1881, mirroring
`monitoring:leader` #1464 / `opqueue:leader` #1632), and that does not make this
rule redundant — do not "simplify" it back to a cycle count. Two reasons. First,
the lease is best-effort and **fails open to leader when Redis is unreachable**,
which puts every worker back to running concurrent cycles over this shared
marker — and Redis-down is not a rare corner here, it is one of the states the
harness exists to report. Second, and more fundamental: the thing being ridden
out is a *real-time* transient (a container finishing teardown), not "one more
observation". That is true with one worker, so cycle count was never the right
unit regardless of how many there are.

An elapsed-time rule is correct under one worker or five, and it absorbs
cross-worker clock skew (a negative elapsed simply does not confirm).

Cost: the alarm arrives at ~10 min rather than ~5. Irrelevant for a config
regression, which persists until a human fixes it.

The gate applies to **every** firing arm, `roster_read_failed` included. The
delete race is only one of the two transients it absorbs: a roster read that
*raises* is very often a momentary DB blip — a connection reset, a PG restart,
brief pool exhaustion — and paging critical on a single one of those is a false
positive of exactly the kind that gets a safety net muted. What the gate costs
on that arm is 60s of detection latency on a real outage, against which a
persistent outage is unaffected by definition.

The marker follows E-02's precedent (an invariant may hold cross-cycle state in
Redis via a lazy client import). It stores the *first* suspicious snapshot_time,
so the violation can report `blind_since` and a manual same-cycle
`POST /api/canary/run-cycle` replay cannot confirm itself.

It carries a TTL of `MARKER_TTL_SECONDS`, refreshed on every suspicious cycle.
The marker must survive a backend restart mid-episode (it tracks the condition,
not the process), so the TTL is sized at many cycles rather than one — but it
cannot be absent: `_clear_marker` is best-effort and swallows failures, and
`POST /api/canary/run-cycle` with an `invariant_ids` filter that excludes H-01
never reaches the clear path at all. Without an expiry a marker orphaned that
way stays armed forever, and the next genuine episode confirms instantly on its
first cycle instead of waiting out the race. The TTL bounds that staleness
without weakening the gate, because a live episode rewrites the key every cycle.

## Fail-loud, never fail-silent

If the marker itself is unreadable, H-01 fires **immediately** as unconfirmed
rather than skipping. A guard that cannot self-check must say so — skipping
would reproduce, one level up, the exact silent-green failure this invariant
exists to prevent (learnings.md 2026-07-26: a guard that has never been shown to
fire is decorative, and a decorative guard manufactures confidence).

Because the service's green→red transition detection only alerts on the flip, a
persistent condition alerts once rather than every 5 minutes — the chirp cannot
become spam.

## Known residuals (documented, not covered)

Docker is filtered to `status=running` and stopped agents hold no Redis slots,
so a blind SQL tier plus an *entirely stopped* fleet has no available evidence
and can only reach `roster_empty_unverifiable`. Partial blindness — a roster
returning 1 agent out of 20 — is also out of scope: a count comparison would
false-alarm on legitimate create/stop races between the two reads. H-01 covers
total blindness only, by deliberate choice.

**What `roster_read_failed` does and does not see.** It fires when the
`agent_ownership` SELECT itself raises. A whole-database outage used to be
invisible to it for a reason that had nothing to do with this invariant:
`canary_service._run_cycle_inner` read `get_latest_canary_violation_per_invariant()`
*before* collecting the snapshot, unguarded, so a DB-down cycle raised out of
the loop and H-01 never executed — the harness went quiet in precisely the
scenario it exists to announce. That read is now fail-open (#1813), with
transition detection falling back to a Redis-held record of the previous
cycle's red set, so the cycle completes and H-01 fires. What remains genuinely
out of scope is a failure that prevents the *process* from running the cycle at
all (the backend is down, the event loop is wedged) — no in-process guard can
cover that, and external liveness monitoring owns it.

Consumer: trinity-enterprise#202 (benchmark scorer over the canary invariants),
whose AC requires that any unavailable data source fails the score loud rather
than scoring green.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Set

from ..snapshot import COLLECTOR_DOCKER, COLLECTOR_REDIS, Snapshot, ViolationReport


logger = logging.getLogger(__name__)


INVARIANT_ID = "H-01"
TIER = "A"

# Confirmed blindness — the harness is provably not seeing the fleet.
SEVERITY_CONFIRMED = "critical"
# Cannot self-verify — genuinely-empty and blind are indistinguishable because
# the independent evidence source is itself unavailable.
SEVERITY_UNVERIFIABLE = "major"

# Redis key holding the snapshot_time of the FIRST cycle that saw the current
# suspicious condition. Cleared on any healthy cycle.
REDIS_KEY_SUSPECT_SINCE = "canary:h01:suspect_since"

# Reason codes (stable strings — trinity-enterprise#202 scores on these).
REASON_ROSTER_READ_FAILED = "roster_read_failed"
REASON_CONTRADICTED = "roster_empty_contradicted"
REASON_UNVERIFIABLE = "roster_empty_unverifiable"

CONFIRMED_SUSTAINED = "confirmed_sustained"
UNCONFIRMED_NO_MARKER = "unconfirmed_marker_unavailable"

# How long the suspicious condition must persist before it is believed.
# Confirmation is on ELAPSED TIME, not on "a second cycle". Prod runs the
# backend with `uvicorn --workers 2`; the canary service holds a `canary:leader`
# lease (#1881) but it is best-effort and FAILS OPEN when Redis is unreachable,
# so two independent canary loops can still share this marker. Under a
# cycle-count rule, worker B would confirm worker A's sighting seconds later,
# collapsing the gate to nothing precisely inside the container-teardown window
# it exists to ride out. The gate is also not merely a multi-worker workaround:
# what it rides out is a real-time transient, which is a single-worker property
# too. See the module docstring — do NOT relax this to a cycle count.
#
# 60s is comfortably longer than a container teardown and comfortably shorter
# than the 300s cycle, so the single-worker path still confirms on the very
# next cycle.
CONFIRMATION_MIN_SECONDS = 60

# Marker lifetime, refreshed on every suspicious cycle. Bounds the staleness of
# a marker that `_clear_marker` (best-effort) failed to delete, or that an
# `invariant_ids`-filtered `run-cycle` never reached — either leaves the gate
# armed forever, so the next genuine episode confirms on its first cycle
# instead of riding out the delete race. Sized at many cycles, not one: the
# marker deliberately survives a backend restart mid-episode, so it must
# outlive far more than the 300s interval. Mirrors R-01's `_MARKER_TTL_SECONDS`.
MARKER_TTL_SECONDS = 24 * 60 * 60

# `sources_unavailable` prefixes. These are the same load-bearing internal skip
# contract the other invariants match on (`startswith("redis")`,
# `startswith("sqlite.orphan_refs")`) — see the label-contract warning in
# `collect_snapshot`. Renaming a label without updating these silently disables
# the corresponding branch here.
_ROSTER_FAILURE_PREFIX = "sqlite.agent_ownership"
# The Docker/Redis prefixes are the `COLLECTOR_*` names imported above — the
# collector registry and the label prefix are deliberately the same string, so
# "did it run?" and "did it fail?" can be asked with one identifier.

# Bounded sample of evidence agent names in the violation payload. Counts and
# identifiers only, never raw source rows (#1644's lesson: an alarm's context
# must not become a second copy of the data it is warning about).
_EVIDENCE_SAMPLE_CAP = 10


def _redis():
    """Lazy import of the slot service's Redis client (mirrors E-02)."""
    from services.slot_service import get_slot_service

    return get_slot_service().redis


def _read_marker() -> Optional[str]:
    return _redis().get(REDIS_KEY_SUSPECT_SINCE)


def _write_marker(snapshot_time: str) -> None:
    # `ex=` rather than a separate EXPIRE: one round-trip, and it makes the TTL
    # impossible to forget on a future write path — an un-expiring marker is
    # the failure this bounds.
    _redis().set(REDIS_KEY_SUSPECT_SINCE, snapshot_time, ex=MARKER_TTL_SECONDS)


def _refresh_marker_ttl() -> None:
    """Keep an armed marker alive for as long as the episode lasts.

    Best-effort: a failed refresh only brings the expiry forward, and an
    expired marker re-arms rather than fires. Without this the TTL would be
    an absolute lifetime rather than an idle timeout, and an episode outliving
    it would silently re-arm — going green for one cycle and re-alerting."""
    try:
        _redis().expire(REDIS_KEY_SUSPECT_SINCE, MARKER_TTL_SECONDS)
    except Exception:
        logger.exception("H-01: failed to refresh suspicion marker TTL")


def _clear_marker() -> None:
    """Best-effort. A stale marker only costs one early fire on the next
    genuine episode; failing the whole check over it would be worse."""
    try:
        _redis().delete(REDIS_KEY_SUSPECT_SINCE)
    except Exception:
        logger.exception("H-01: failed to clear suspicion marker")


def _to_utc(ts: str) -> datetime:
    """ISO-8601 → aware UTC datetime (same shape as E-06's `_to_utc`)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _has(snapshot: Snapshot, prefix: str) -> bool:
    return any(s.startswith(prefix) for s in snapshot.sources_unavailable)


def _availability(snapshot: Snapshot, collector: str) -> Optional[bool]:
    """Tri-state: True = ran and was fine, False = ran and failed, None = never
    ran.

    `sources_unavailable` cannot express the third state — a collector that was
    skipped records nothing, which is byte-identical to a collector that
    succeeded. On the `roster_read_failed` arm `collect_snapshot` returns before
    Redis is read, so a two-state answer reported `redis=up` on a cycle where
    Redis had not been consulted at all. For the one alarm whose entire job is
    legibility, that read as "everything else is fine" — the opposite of the
    truth. `None` is rendered as "not read" by the Slack surfaces.
    """
    if not snapshot.collector_ran(collector):
        return None
    return not _has(snapshot, collector)


def _violation(
    snapshot: Snapshot,
    reason: str,
    severity: str,
    evidence: Set[str],
    confirmation: str,
    blind_since: Optional[str],
) -> ViolationReport:
    return ViolationReport(
        invariant_id=INVARIANT_ID,
        tier=TIER,
        severity=severity,
        observed_state={
            "reason": reason,
            "confirmation": confirmation,
            "blind_since": blind_since,
            "snapshot_time": snapshot.snapshot_time,
            "known_agent_count": len(snapshot.known_agents),
            "evidence_agent_count": len(evidence),
            "evidence_sample": sorted(evidence)[:_EVIDENCE_SAMPLE_CAP],
            # Tri-state (True / False / None-means-never-ran) — see
            # `_availability`. A consumer reading these as booleans (including
            # trinity-enterprise#202's scorer) gets `None` as falsy, i.e.
            # "not up", which is the safe direction to be wrong in.
            "docker_available": _availability(snapshot, COLLECTOR_DOCKER),
            "redis_available": _availability(snapshot, COLLECTOR_REDIS),
            # Named separately so "we could not confirm" is greppable without
            # having to know that None means never-ran.
            "docker_agent_count": len(snapshot.docker_agent_names),
        },
        signal_query=(
            f"agent roster read returned {len(snapshot.known_agents)} agents "
            f"({reason}); independent sources report {len(evidence)} live agent(s)"
        ),
    )


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Fire when the SQL roster is unusable while the fleet is provably alive."""
    roster_failed = _has(snapshot, _ROSTER_FAILURE_PREFIX)

    # Healthy: the roster read worked and returned agents. Nothing to check.
    if not roster_failed and snapshot.known_agents:
        _clear_marker()
        return []

    # Independent, non-SQL proof that agents exist. Both sources ride in
    # `evidence` (so the operator sees every name we found), but they are NOT
    # interchangeable for severity — only Docker can confirm a contradiction.
    docker_evidence: Set[str] = set(snapshot.docker_agent_names)
    evidence: Set[str] = docker_evidence | set(snapshot.orphan_redis_slots)

    if roster_failed:
        reason, severity = REASON_ROSTER_READ_FAILED, SEVERITY_CONFIRMED
    elif docker_evidence:
        # Ordering note: a PER-AGENT `docker.exec[name]` failure also carries the
        # `docker` prefix, but it cannot reach the unverifiable branch — the
        # container's name is recorded in `docker_agent_names` BEFORE the exec is
        # attempted, so `docker_evidence` is non-empty and this branch wins.
        # Only a wholesale Docker failure (`docker: client unavailable`,
        # `docker.list`, `docker.import`) leaves it empty.
        reason, severity = REASON_CONTRADICTED, SEVERITY_CONFIRMED
    elif evidence:
        # Redis-only evidence. `orphan_redis_slots` is by definition slot keys
        # whose agent is ABSENT from `agent_ownership` — the leaked-slot state
        # L-03 exists to report. On a genuinely empty fleet holding one leaked
        # key, treating this as a contradiction would page critical over a
        # correct roster and an unrelated Redis leak. It is real evidence that
        # something is there, but not evidence that the ROSTER is wrong, so it
        # lands on the honest arm: cannot verify.
        reason, severity = REASON_UNVERIFIABLE, SEVERITY_UNVERIFIABLE
    elif _availability(snapshot, COLLECTOR_DOCKER) is not True or _availability(
        snapshot, COLLECTOR_REDIS
    ) is not True:
        # `is not True` covers BOTH "ran and failed" and "never ran" — the
        # second is reachable here because `collect_snapshot` returns early on a
        # roster failure, and a two-state test would silently call an
        # unconsulted source "available" and fall through to the all-clear.
        reason, severity = REASON_UNVERIFIABLE, SEVERITY_UNVERIFIABLE
    else:
        # Roster empty, every evidence source actually ran and was reachable,
        # all agree there is nothing to see. A genuinely empty fleet — AC #3,
        # must not alarm.
        _clear_marker()
        return []

    # Elapsed-time confirmation gate (CONFIRMATION_MIN_SECONDS) — deliberately
    # NOT "a second cycle": the marker is shared across workers, so cycle count
    # is not a delay. See the module docstring.
    try:
        blind_since = _read_marker()
    except Exception:
        # Cannot self-check → fail loud rather than skip.
        logger.exception("H-01: suspicion marker unreadable; reporting unconfirmed")
        return [
            _violation(
                snapshot,
                reason,
                severity,
                evidence,
                UNCONFIRMED_NO_MARKER,
                blind_since=None,
            )
        ]

    if blind_since is None:
        # First sighting — arm and wait out the last-agent delete race, where
        # the DB row is already gone but the container is still tearing down.
        try:
            _write_marker(snapshot.snapshot_time)
        except Exception:
            # A write that keeps failing while reads keep returning None
            # (read-only replica, OOM) would re-arm forever and NEVER fire —
            # a silent hole in the one guard that exists to close silent
            # holes. Fire unconfirmed instead; a false positive here is
            # strictly cheaper than a permanently muted detector.
            logger.exception(
                "H-01: failed to arm suspicion marker; reporting unconfirmed"
            )
            return [
                _violation(
                    snapshot,
                    reason,
                    severity,
                    evidence,
                    UNCONFIRMED_NO_MARKER,
                    blind_since=None,
                )
            ]
        return []

    try:
        elapsed = (
            _to_utc(snapshot.snapshot_time) - _to_utc(blind_since)
        ).total_seconds()
    except ValueError:
        # Marker is unparseable (hand-edited, or written by a future format).
        # Not evidence of blindness — re-arm from now and let the next cycle
        # decide rather than firing on a garbage timestamp.
        logger.warning(
            "H-01: unparseable suspicion marker %r; re-arming", blind_since
        )
        try:
            _write_marker(snapshot.snapshot_time)
        except Exception:
            logger.exception("H-01: failed to re-arm suspicion marker")
        return []

    # The episode is still live, so keep the marker alive with it. Makes the
    # TTL an idle timeout rather than an absolute lifetime — an episode that
    # outlives `MARKER_TTL_SECONDS` would otherwise silently re-arm, go green
    # for one cycle, and re-alert.
    _refresh_marker_ttl()

    # `<` also absorbs a negative elapsed (cross-worker clock skew).
    if elapsed < CONFIRMATION_MIN_SECONDS:
        return []

    return [
        _violation(
            snapshot,
            reason,
            severity,
            evidence,
            CONFIRMED_SUSTAINED,
            blind_since,
        )
    ]
