"""
S-03 — Slot TTL ≥ the slot's own effective timeout (CANARY-001 / #411 Phase 3).

For every member of `agent:slots:A`, the companion `agent:slot:A:{eid}`
HASH must have been created with a TTL of at least **that execution's own**
effective timeout + 300 (SLOT_TTL_BUFFER). An initial TTL below that floor
means the slot will expire while the execution might still legitimately be
running — the premature-expiry bug class behind Issue #226.

## The floor comes from the SLOT, not from the agent cap (ent#336)

This check originally built its floor from `agent_ownership.
execution_timeout_seconds` — the agent **cap**. But `SlotService.acquire_slot`
sets the TTL from the timeout passed for *this execution*, which since #929 may
legitimately be **lower** than the cap (a schedule may set an explicit shorter
`timeout_seconds`; a public-channel turn is bounded at 900s; a loop uses its
`timeout_per_run`). So every normally-configured scheduled run fired a
**critical** violation for its entire runtime — 378 in ~13h on eu2, and unlike
E-06 it re-fires on each new execution, so it produced a fresh green→red page on
essentially every scheduled run.

The premise "the slot will expire while the execution might still be running"
simply does not hold there: an execution capped at 2700s cannot still be running
at 3000s. The slot outlives it by the full 300s buffer, exactly as designed.

The runtime had already reached this conclusion and the canary had not:
`SlotService._cleanup_stale_slots_for_agent` reads `timeout_seconds` back off
the metadata HASH precisely because the agent-cap approach was wrong (#869 —
it killed live executions at ~65 min when the configured limit was 120 min).
Cleanup is per-slot-timeout-aware; this check now agrees with it.

## What `below_floor` does and does NOT catch (read before trusting it)

Be honest about the strength of this arm. `acquire_slot` derives BOTH sides
from one local variable, three lines apart, and is the **sole** writer of that
HASH:

    slot_ttl = timeout_seconds + SLOT_TTL_BUFFER   # → EXPIRE
    "timeout_seconds": str(timeout_seconds)        # → HSET

So `initial_ttl >= stored_timeout + buffer` is close to true by construction.
`below_floor` is therefore an **internal-coherence check** — it catches the
EXPIRE and the HSET drifting apart (a future refactor computing the TTL from a
different source than the field it records), and nothing wider.

It specifically does NOT catch a caller passing the *wrong* timeout — the #913
class — because the check now reads that caller's own echo. Detecting that needs
corroboration against the execution's DECLARED timeout (schedule row / loop row
/ agent cap by provenance), which is a join layer this check does not have.
Tracked as the ent#336 residual; do not read a green S-03 as evidence that
dispatch timeouts are correct.

The `-1` and `-2` arms are unaffected and remain the load-bearing #226 coverage.

## Why the check is decay-invariant

Redis `TTL` returns the *current* remaining seconds, which decays
linearly from the moment `EXPIRE` was set. The slot's initial TTL exactly
equals its floor, so a raw `ttl < floor` check would fire on every cycle the
moment any wall-clock time has passed — a 1-second false positive on
fresh slots.

The fix is to compare the *initial* TTL against the floor, reconstructed
as `ttl + age`, where `age = snapshot_time - slot_score` and
`slot_score` is the unix epoch at acquire (recorded by SlotService in
the ZSET). A slot created with `EXPIRE(floor)` then ages `t` seconds
has current TTL `floor - t`, so `ttl + age = floor` — exactly at the
floor regardless of when the snapshot is taken.

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

## Why the buffer is hard-coded

300s matches `services/slot_service.py:SLOT_TTL_BUFFER`. Hard-coded here
rather than imported so a change to the runtime buffer is a deliberate
review — same pattern as E-01's `SLOT_TTL_BUFFER_SECONDS`. If the two
constants ever drift, the next cycle will fire S-03 violations and
force the conversation.

## Missing `timeout_seconds` ⇒ SKIP, never fall back to the agent cap

The TTL and the stored timeout are read in one pipeline, but the HASH can still
expire between the ZRANGE and that pipeline. Falling back to the agent cap there
would re-arm the exact false positive above in a narrower window, so an
unobservable timeout skips the slot instead — the same stance the check already
takes for a missing TTL and a missing ZSET score. Genuinely pre-#913 slots
self-heal within ~2h of deploy anyway (a slot lives at most `timeout + 300`
≤ 7500s), so the fallback would be near-dead as well as unsafe.

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

            # ent#336: the floor is THIS slot's own effective timeout, not the
            # agent cap. `floor_source` is carried into observed_state so a
            # persisted violation shows which derivation ran.
            stored_timeout = agent.slot_timeouts.get(eid)
            if stored_timeout is not None:
                floor = stored_timeout + SLOT_TTL_BUFFER_SECONDS
                floor_source = "stored"
            else:
                # Unobservable timeout. The `-1`/`-2` sentinels below are
                # independent of the floor, so they still fire; only the
                # floor-dependent `below_floor` arm has to skip (see the
                # module docstring — an agent-cap fallback would re-arm the
                # false positive this issue removes).
                floor = agent.execution_timeout_seconds + SLOT_TTL_BUFFER_SECONDS
                floor_source = "unknown"

            # Two-state sentinels first — independent of age AND of the floor.
            if ttl == -2:
                kind = "missing"          # Metadata HASH already expired (#226).
            elif ttl == -1:
                kind = "no_expiry"        # `redis.expire()` never set.
            elif ttl > 0:
                if floor_source != "stored":
                    continue  # cannot judge this slot's floor — skip, don't guess
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
                # ent#336: `floor_seconds` is now derived from the slot's own
                # stored timeout, so the persisted row must say which value it
                # came from — on a `-1`/`-2` slot the HASH may be gone and the
                # reported floor is the agent-cap placeholder, not a judgement.
                "stored_timeout_seconds": stored_timeout,
                "floor_source": floor_source,
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
                        f"({kind}); floor = {floor}s "
                        f"(from {floor_source})"
                    ),
                )
            )

    return violations
