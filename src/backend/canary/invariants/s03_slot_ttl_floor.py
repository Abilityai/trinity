"""
S-03 — Slot TTL ≥ execution timeout (CANARY-001 / Issue #411 — Phase 3).

For every member of `agent:slots:A`, the companion `agent:slot:A:{eid}`
HASH must have been created with a TTL of at least the agent's
`execution_timeout_seconds + 300` (SLOT_TTL_BUFFER). An initial TTL
below that floor means the slot will expire while the execution might
still legitimately be running — exactly the premature-expiry bug class
behind Issue #226.

## Why the check is decay-invariant

Redis `TTL` returns the *current* remaining seconds, which decays
linearly from the moment `EXPIRE` was set. After #913, the slot's
initial TTL exactly equals `execution_timeout + SLOT_TTL_BUFFER` (the
floor), so the raw `ttl < floor` check would fire on every cycle the
moment any wall-clock time has passed — a 1-second false positive on
fresh slots.

The fix is to compare the *initial* TTL against the floor, reconstructed
as `ttl + age`, where `age = snapshot_time - slot_score` and
`slot_score` is the unix epoch at acquire (recorded by SlotService in
the ZSET). A slot created with `EXPIRE(floor)` then ages `t` seconds
has current TTL `floor - t`, so `ttl + age = floor` — exactly at the
floor regardless of when the snapshot is taken. A #913-class bug where
the initial TTL was set to `900 + 300 = 1200s` but the floor is
`3600 + 300 = 3900s` shows up as `ttl + age = 1200` < `3900` — caught.

## TTL sentinel values from `redis.ttl()`

Three special return values, each meaning a different failure mode:

- `>0` (normal case): current seconds until expiry. Reconstruct the
  initial TTL via `ttl + age` and compare against the floor.
- `-1`: key exists with **no expiry**. The slot has been turned into a
  leak — `redis.expire()` was never called or got cleared. Violation
  regardless of the floor (a slot with no TTL eventually traps capacity
  forever once cleanup misses it).
- `-2`: key **does not exist**. The metadata HASH expired before the
  slot was released, leaving the ZSET pointing at nothing. This is the
  load-bearing #226 case — the slot will never get cleaned up, and the
  bijection check (S-01) doesn't catch it because the ZSET membership
  is fine.

All three count as violations. The observed_state distinguishes them so
the alert reader can tell at a glance which of the three is happening.

## Drain sentinels

The snapshot collector skips drain sentinels (`drain-*` members) when
populating `slot_ttls`. They're intentionally short-lived; the metadata
HASH for a sentinel is written with the same TTL as a real slot but
they're cycled fast enough that catching them mid-flight would be
noise, not signal.

## Why the floor is hard-coded

300s matches `services/slot_service.py:SLOT_TTL_BUFFER`. Hard-coded here
rather than imported so a change to the runtime buffer is a deliberate
review — same pattern as E-01's `SLOT_TTL_BUFFER_SECONDS`. If the two
constants ever drift, the next cycle will fire S-03 violations and
force the conversation.

Tier A, severity critical. A slot whose TTL is below the floor is a
ticking timebomb on capacity correctness.
"""

from datetime import datetime, timezone
from typing import List, Optional

from ..snapshot import Snapshot, ViolationReport


INVARIANT_ID = "S-03"
TIER = "A"
SEVERITY = "critical"

# Matches services/slot_service.py SLOT_TTL_BUFFER. See module docstring.
SLOT_TTL_BUFFER_SECONDS = 300

DRAIN_PREFIX = "drain-"


def _parse_iso_to_unix(ts: str) -> Optional[float]:
    """Convert an ISO-Z timestamp into unix epoch seconds.

    Mirrors `_parse_iso` in e01_terminal_state_closure.py — strips the
    trailing 'Z' that `fromisoformat` rejects on Python <3.11, and
    forces UTC tz on naive results so the subtract is sane.
    """
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


def check(snapshot: Snapshot) -> List[ViolationReport]:
    """Emit one violation per slot whose initial TTL was below the floor."""
    violations: List[ViolationReport] = []

    # Same gate as S-01 / S-02 / E-02: Redis failed → skip cleanly.
    if any(s.startswith("redis") for s in snapshot.sources_unavailable):
        return violations

    snapshot_unix = _parse_iso_to_unix(snapshot.snapshot_time)

    for agent in snapshot.agents:
        floor = agent.execution_timeout_seconds + SLOT_TTL_BUFFER_SECONDS

        for eid in sorted(agent.slot_ids):
            # Drain sentinels are filtered upstream — defence in depth.
            if eid.startswith(DRAIN_PREFIX):
                continue
            # If the collector skipped the slot (per-slot TTL call failed),
            # don't fabricate a violation — operators rely on Redis errors
            # surfacing through `sources_unavailable`, not via false-fire.
            if eid not in agent.slot_ttls:
                continue

            ttl = agent.slot_ttls[eid]

            # Two-state sentinels first — independent of age.
            if ttl == -2:
                kind = "missing"          # Metadata HASH already expired (#226).
            elif ttl == -1:
                kind = "no_expiry"        # `redis.expire()` never set.
            elif ttl > 0:
                # Reconstruct the slot's initial TTL by adding back the age
                # since acquisition. `slot_scores[eid]` is the unix epoch
                # recorded by SlotService at ZADD time; absent score means
                # the snapshot dropped it (rare race) — skip rather than
                # fabricate a violation. Same defensive stance the rest of
                # the canary takes when an input is incomplete.
                score = agent.slot_scores.get(eid)
                if score is None or snapshot_unix is None:
                    continue
                age = max(0.0, snapshot_unix - float(score))
                initial_ttl = ttl + age
                # 1-second tolerance absorbs the float→int rounding that
                # Redis `TTL` does on the wire. Without it, a slot created
                # with `EXPIRE(3900)` and observed instantly can read
                # `ttl=3899, age=0` → `initial_ttl=3899 < 3900` — exactly
                # the false-positive #913 surfaced.
                if initial_ttl >= floor - 1:
                    continue
                kind = "below_floor"
            else:
                continue  # ttl == 0 means just expired; let the next cycle pick it up.

            observed_state = {
                "agent_name": agent.name,
                "execution_id": eid,
                "redis_ttl_seconds": ttl,
                "execution_timeout_seconds": agent.execution_timeout_seconds,
                "slot_ttl_buffer_seconds": SLOT_TTL_BUFFER_SECONDS,
                "floor_seconds": floor,
                "kind": kind,
                "snapshot_time": snapshot.snapshot_time,
            }
            if kind == "below_floor":
                # Surface the reconstructed initial TTL so the alert reader
                # can see the actual bug magnitude, not the decayed
                # remainder.
                observed_state["initial_ttl_seconds"] = int(initial_ttl)
                observed_state["age_seconds"] = int(age)

            violations.append(
                ViolationReport(
                    invariant_id=INVARIANT_ID,
                    tier=TIER,
                    severity=SEVERITY,
                    observed_state=observed_state,
                    signal_query=(
                        f"TTL(agent:slot:{agent.name}:{eid}) = {ttl} "
                        f"({kind}); floor = {floor}s"
                    ),
                )
            )

    return violations
