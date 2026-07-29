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
  `exec_run` success and silently thins on a degraded container).
- **Redis** (`snapshot.orphan_redis_slots`) — with an empty roster, every
  `agent:slots:*` key buckets here. Strong when it fires, but slot keys exist
  only while an execution holds a slot, so an idle fleet legitimately has none.
  Corroborating evidence, never the sole basis for "the fleet is empty".

| roster read      | independent evidence            | outcome              |
|------------------|---------------------------------|----------------------|
| non-empty        | —                               | pass, clear marker   |
| raised           | (not needed — definitive)       | critical             |
| empty            | proves agents exist             | critical             |
| empty            | source unavailable              | major                |
| empty            | available, agrees empty         | pass, clear marker   |

## Sustained-condition confirmation

Deleting an agent removes the DB row and the container as separate steps. If a
snapshot lands between them AND it was the last agent, the roster is empty while
Docker still shows a container — a false positive on an ordinary delete. Since
false positives are precisely how a safety net gets muted, a suspicious cycle
only *arms* a marker (`canary:h01:suspect_since`); the violation fires once the
condition has persisted for `CONFIRMATION_MIN_SECONDS`.

Confirmation is on **elapsed time, not on "a second cycle"**. Prod runs the
backend with `uvicorn --workers 2`, and the canary service holds only a
per-process `asyncio.Lock` — it has no cross-worker leader lease (unlike
`monitoring:leader` #1464 or `opqueue:leader` #1632) — so two independent canary
loops share this one marker. Under a cycle-count rule worker B would confirm
worker A's sighting seconds later, collapsing the gate to nothing exactly inside
the teardown window it exists to ride out. An elapsed-time rule is correct under
one worker or five, and it absorbs cross-worker clock skew (a negative elapsed
simply does not confirm).

Cost: the alarm arrives at ~10 min rather than ~5. Irrelevant for a config
regression, which persists until a human fixes it.

The marker follows E-02's precedent (an invariant may hold cross-cycle state in
Redis via a lazy client import). It stores the *first* suspicious snapshot_time,
so the violation can report `blind_since` and a manual same-cycle
`POST /api/canary/run-cycle` replay cannot confirm itself. No TTL: the marker
tracks the condition, not the process, so it correctly survives a backend
restart mid-episode.

## Fail-loud, never fail-silent

If the marker itself is unreadable, H-01 fires **immediately** as unconfirmed
rather than skipping. A guard that cannot self-check must say so — skipping
would reproduce, one level up, the exact silent-green failure this invariant
exists to prevent (learnings.md 2026-07-26: a guard that has never been shown to
fire is decorative, and a decorative guard manufactures confidence).

Because the service's green→red transition detection only alerts on the flip, a
persistent condition alerts once rather than every 5 minutes — the chirp cannot
become spam.

## Known residual (documented, not covered)

Docker is filtered to `status=running` and stopped agents hold no Redis slots,
so a blind SQL tier plus an *entirely stopped* fleet has no available evidence
and can only reach `roster_empty_unverifiable`. Partial blindness — a roster
returning 1 agent out of 20 — is also out of scope: a count comparison would
false-alarm on legitimate create/stop races between the two reads. H-01 covers
total blindness only, by deliberate choice.

Consumer: trinity-enterprise#202 (benchmark scorer over the canary invariants),
whose AC requires that any unavailable data source fails the score loud rather
than scoring green.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional, Set

from ..snapshot import Snapshot, ViolationReport


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
# Confirmation is on ELAPSED TIME, not on "a second cycle": prod runs the
# backend with `uvicorn --workers 2` and the canary service has only a
# per-process `asyncio.Lock` — no cross-worker leader lease (unlike
# `monitoring:leader` #1464 or `opqueue:leader` #1632) — so two independent
# canary loops share this marker. Under a cycle-count rule, worker B would
# confirm worker A's sighting seconds later, collapsing the gate to nothing
# precisely inside the container-teardown window it exists to ride out.
#
# 60s is comfortably longer than a container teardown and comfortably shorter
# than the 300s cycle, so the single-worker path still confirms on the very
# next cycle.
CONFIRMATION_MIN_SECONDS = 60

# `sources_unavailable` prefixes. These are the same load-bearing internal skip
# contract the other invariants match on (`startswith("redis")`,
# `startswith("sqlite.orphan_refs")`) — see the label-contract warning in
# `collect_snapshot`. Renaming a label without updating these silently disables
# the corresponding branch here.
_ROSTER_FAILURE_PREFIX = "sqlite.agent_ownership"
_DOCKER_PREFIX = "docker"
_REDIS_PREFIX = "redis"

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
    _redis().set(REDIS_KEY_SUSPECT_SINCE, snapshot_time)


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
            "docker_available": not _has(snapshot, _DOCKER_PREFIX),
            "redis_available": not _has(snapshot, _REDIS_PREFIX),
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

    # Independent, non-SQL proof that agents exist. Redis contributes only the
    # names it can attribute to a slot key; both sources are unioned so either
    # one alone is sufficient.
    evidence: Set[str] = set(snapshot.docker_agent_names) | set(
        snapshot.orphan_redis_slots
    )

    if roster_failed:
        reason, severity = REASON_ROSTER_READ_FAILED, SEVERITY_CONFIRMED
    elif evidence:
        reason, severity = REASON_CONTRADICTED, SEVERITY_CONFIRMED
    elif _has(snapshot, _DOCKER_PREFIX) or _has(snapshot, _REDIS_PREFIX):
        # Ordering note: a PER-AGENT `docker.exec[name]` failure also carries the
        # `docker` prefix, but it cannot reach this branch — the container's name
        # is recorded in `docker_agent_names` BEFORE the exec is attempted, so
        # `evidence` is non-empty and the contradiction branch above wins. Only a
        # wholesale Docker failure (`docker: client unavailable`, `docker.list`,
        # `docker.import`) leaves evidence empty, which is exactly the
        # "cannot verify" case this branch is for.
        reason, severity = REASON_UNVERIFIABLE, SEVERITY_UNVERIFIABLE
    else:
        # Roster empty, every evidence source reachable, all agree there is
        # nothing to see. A genuinely empty fleet — AC #3, must not alarm.
        _clear_marker()
        return []

    # Two-cycle confirmation gate.
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
