"""ent#336 — canary S-03's floor comes from the SLOT, not the agent cap.

S-03 built ``floor = agent_ownership.execution_timeout_seconds + 300``, but
``SlotService.acquire_slot`` sets the TTL from *this execution's* timeout, which
since #929 may legitimately be lower than the cap. Every execution whose
schedule set an explicit shorter ``timeout_seconds`` therefore fired a
**critical** violation for its entire runtime — 378 in ~13h on eu2, and unlike
E-06 it re-fires per execution, so it produced a fresh green→red page on
essentially every scheduled run.

The runtime had already reached the opposite conclusion:
``_cleanup_stale_slots_for_agent`` reads ``timeout_seconds`` back off the
metadata HASH for exactly this reason (#869). Cleanup was per-slot-timeout-aware
and the canary was not.

Tests live under ``tests/unit/`` because CI runs ``pytest unit/`` only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


T0 = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
T0_UNIX = T0.timestamp()


def _snap(
    *,
    agent_cap: int,
    ttl: int,
    stored_timeout: int | None,
    age_seconds: int = 0,
    slot_id: str = "e1",
):
    """One agent, one slot. Every time value is an offset from a literal T0.

    Neither side samples a real clock, so the floor boundary is exact rather
    than absorbed by a margin (learnings 2026-08-03 / #1909).
    """
    from canary.snapshot import AgentSnapshot, Snapshot

    return Snapshot(
        snapshot_time=T0.isoformat().replace("+00:00", "Z"),
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=agent_cap,
                slot_ids={slot_id},
                slot_scores={slot_id: T0_UNIX - age_seconds},
                slot_ttls={slot_id: ttl},
                slot_timeouts=({} if stored_timeout is None else {slot_id: stored_timeout}),
            )
        ],
    )


def _check(snap):
    from canary.invariants import s03_slot_ttl_floor as s03

    return s03.check(snap)


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------


def test_schedule_timeout_below_agent_cap_does_not_fire():
    """THE ent#336 regression, with eu2's exact numbers.

    Agent cap 3600 → old floor 3900. Schedule timeout 2700 → slot TTL 3000.
    An execution capped at 2700s cannot still be running at 3000s: the slot
    outlives it by the full 300s buffer, exactly as designed.
    """
    assert _check(_snap(agent_cap=3600, ttl=3000, stored_timeout=2700)) == []


@pytest.mark.parametrize("stored_timeout", [600, 900, 1200, 1800, 2700])
def test_every_eu2_configured_timeout_is_silent(stored_timeout):
    """The per-schedule timeouts actually in use on eu2, all correct."""
    snap = _snap(
        agent_cap=3600,
        ttl=stored_timeout + 300,
        stored_timeout=stored_timeout,
    )
    assert _check(snap) == []


def test_public_channel_900s_bound_is_silent():
    """`routers/public.py` bounds a public turn at 900s (commit d46281f6).

    That is a deliberate public-path bound, not a stale echo of the dead
    `acquire_slot(timeout_seconds=900)` signature default — and the resulting
    1200s slot TTL is correct, which is why S-03 firing on it was a false
    positive. This is the `cornelius-oracle` case ent#336 flagged as an open
    question (an agent with NO enabled schedules producing a 1200s slot).
    """
    assert _check(_snap(agent_cap=3600, ttl=1200, stored_timeout=900)) == []


# ---------------------------------------------------------------------------
# What `below_floor` still catches — and what it no longer does
# ---------------------------------------------------------------------------


def test_ttl_below_the_slots_own_stored_timeout_still_fires():
    """EXPIRE and HSET disagreeing is what this arm means now.

    `acquire_slot` derives both from one local three lines apart, so this is an
    internal-coherence check: it catches a future refactor computing the TTL
    from a different source than the field it records.
    """
    v = _check(_snap(agent_cap=3600, ttl=1200, stored_timeout=2700))

    assert len(v) == 1
    assert v[0].observed_state["kind"] == "below_floor"
    assert v[0].observed_state["floor_seconds"] == 3000
    assert v[0].observed_state["floor_source"] == "stored"
    assert v[0].observed_state["stored_timeout_seconds"] == 2700


def test_below_floor_no_longer_detects_a_wrong_caller_timeout():
    """Characterization of the ACCEPTED weakening — do not "fix" this test.

    The #913 class was a caller passing the wrong timeout. `acquire_slot` now
    records that same wrong value, so the check reads the caller's own echo and
    the pair is self-consistent. ent#336's own "preserved failure modes" list
    was wrong about this; S-03's docstring says so explicitly, and detecting it
    needs corroboration against the DECLARED timeout (tracked as the ent#336
    residual).

    A green S-03 is NOT evidence that dispatch timeouts are correct.
    """
    # A dispatch site hardcodes 900 on a 3600-cap agent: TTL 1200, stored 900.
    assert _check(_snap(agent_cap=3600, ttl=1200, stored_timeout=900)) == []


def test_natural_decay_does_not_fire():
    """#913 regression — decay must not read as a below-floor slot."""
    # Created 5s ago with the canonical EXPIRE=floor: TTL reads 2995, and
    # `ttl + age` reconstructs the initial 3000.
    snap = _snap(agent_cap=3600, ttl=2995, stored_timeout=2700, age_seconds=5)
    assert _check(snap) == []


# ---------------------------------------------------------------------------
# The load-bearing #226 arms are untouched
# ---------------------------------------------------------------------------


def test_missing_metadata_still_fires_without_a_stored_timeout():
    """`-2` is the load-bearing #226 case and is independent of the floor.

    The HASH is gone, so the timeout is unreadable — the arm must still fire.
    """
    v = _check(_snap(agent_cap=3600, ttl=-2, stored_timeout=None))

    assert len(v) == 1
    assert v[0].observed_state["kind"] == "missing"
    assert v[0].observed_state["floor_source"] == "unknown"
    assert v[0].observed_state["stored_timeout_seconds"] is None


def test_no_expiry_still_fires():
    v = _check(_snap(agent_cap=3600, ttl=-1, stored_timeout=3600))

    assert len(v) == 1
    assert v[0].observed_state["kind"] == "no_expiry"


# ---------------------------------------------------------------------------
# Unobservable timeout ⇒ skip, never an agent-cap fallback
# ---------------------------------------------------------------------------


def test_live_slot_without_a_stored_timeout_is_skipped_not_judged():
    """An agent-cap fallback would re-arm the fixed false positive.

    The TTL and the stored timeout are read in one pipeline, but the HASH can
    still expire in between. TTL 3000 against an agent-cap floor of 3900 fires;
    against the slot's real 2700 bound it does not. Skipping is the only answer
    that cannot manufacture the bug in a narrower window — and it matches the
    check's existing stance for a missing TTL or ZSET score.
    """
    assert _check(_snap(agent_cap=3600, ttl=3000, stored_timeout=None)) == []


def test_missing_zset_score_is_still_skipped():
    """Pre-existing defensive stance, re-pinned alongside the new one."""
    from canary.snapshot import AgentSnapshot, Snapshot

    snap = Snapshot(
        snapshot_time=T0.isoformat().replace("+00:00", "Z"),
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=3600,
                slot_ids={"e1"},
                slot_scores={},  # score dropped by a snapshot race
                slot_ttls={"e1": 100},
                slot_timeouts={"e1": 2700},
            )
        ],
    )
    assert _check(snap) == []


def test_redis_unavailable_skips_cleanly():
    from canary.snapshot import AgentSnapshot, Snapshot

    snap = Snapshot(
        snapshot_time=T0.isoformat().replace("+00:00", "Z"),
        sources_unavailable=["redis: connection refused"],
        agents=[
            AgentSnapshot(
                name="a1",
                is_system=False,
                max_parallel=3,
                execution_timeout_seconds=3600,
                slot_ids={"e1"},
                slot_ttls={"e1": -2},
            )
        ],
    )
    assert _check(snap) == []


# ---------------------------------------------------------------------------
# The dropped AC #3 arm, replaced by a build-time bound check
# ---------------------------------------------------------------------------


def test_loop_timeout_above_the_agent_cap_does_not_fire():
    """Why ent#336 AC #3's upward arm was dropped rather than implemented.

    AC #3 asked S-03 to fire when a slot's stored timeout EXCEEDS the agent
    cap, described as "a schedule escaping the #929 cap". But loops are not
    clamped to the agent cap: `LoopStartRequest.timeout_per_run` is bounded
    only by `MAX_TIMEOUT_PER_RUN` (7200), and neither `routers/loops.py` nor
    `loop_service.py` compares it to the cap. A legal loop with
    `timeout_per_run=7200` on a 3600-cap agent would have paged critical, and
    S-03 cannot tell a loop slot from a schedule slot.

    (`/task` IS clamped — `routers/chat.py::_resolve_deprecated_task_timeout`,
    #1068; schedules by #929; reminders by #1296. Loops are the gap, filed
    separately.)
    """
    assert _check(_snap(agent_cap=3600, ttl=7500, stored_timeout=7200)) == []


def test_loop_and_agent_timeout_bounds_stay_in_step():
    """The build-time replacement for AC #3's runtime arm.

    AC #3's real intent was "catch a timeout that escaped its validator". A
    runtime arm can only notice that ~5 minutes after it ships, and only if a
    slot happens to be live when the canary samples. Every writer is bounded at
    ≤7200 today (agent cap range #665, `MAX_TIMEOUT_PER_RUN`), so pinning the
    two bounds against each other catches the regression on the PR that
    introduces it instead.

    If these legitimately diverge, update this test deliberately — and revisit
    `test_loop_timeout_above_the_agent_cap_does_not_fire` with it.
    """
    from models import MAX_TIMEOUT_PER_RUN

    # The agent-timeout ceiling (#665: PUT /api/agents/{name}/timeout, 60–7200).
    agent_timeout_max = 7200

    assert MAX_TIMEOUT_PER_RUN == agent_timeout_max, (
        "A loop's per-run timeout and the per-agent execution timeout no longer "
        "share a ceiling. A slot can now carry a timeout no validator bounds, "
        "which S-03 cannot detect — see ent#336 AC #3."
    )
