"""The sync-health policy (trinity-enterprise#706).

`services/sync_freeze_policy.py::classify` is the one place that decides an
agent's sync state, its reason, its recommendation and whether its schedules
freeze. The backend surfaces and the scheduler's gate both call it (the
scheduler through a byte-identical mirror, pinned in
`test_ent706_sync_policy_parity.py`).

Every pinned value is NON-ambient (24 h ± 1 s, 31 behind, 7 ahead, 791 dirty),
so a default that happened to equal the pin cannot make a case pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import sync_freeze_policy as policy

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)
DAY = timedelta(hours=24)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _row(**over):
    row = {
        "last_check_at": _iso(NOW - timedelta(seconds=30)),
        "last_sync_at": _iso(NOW - timedelta(minutes=10)),
        "last_sync_status": "success",
        "consecutive_failures": 0,
        "last_error_summary": None,
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": 0,
        "behind_working": 0,
        "diverged_since": None,
        "dirty_files": 0,
        "dirty_since": None,
    }
    row.update(over)
    return row


def _cfg(source_mode=0, auto_sync=1, freeze=1, created_at=None):
    return {
        "source_mode": source_mode,
        "auto_sync_enabled": auto_sync,
        "freeze_schedules_if_sync_failing": freeze,
        "created_at": created_at,
    }


def _classify(row, cfg, **kw):
    return policy.classify(row, cfg, NOW, **kw)


# --------------------------------------------------------------------------
# Rule 0 — unknown
# --------------------------------------------------------------------------


def test_no_row_is_unknown_and_never_freezes():
    v = _classify(None, _cfg())
    assert v["state"] == "unknown"
    assert v["freeze"] is False


def test_never_observed_row_is_unknown():
    v = _classify(_row(last_check_at=None), _cfg())
    assert v["state"] == "unknown"
    assert v["freeze"] is False


def test_green_when_in_sync():
    v = _classify(_row(), _cfg())
    assert v["state"] == "green"
    assert v["freeze"] is False
    assert v["divergence_age_s"] is None


# --------------------------------------------------------------------------
# Rules 1 & 2 — failed syncs
# --------------------------------------------------------------------------


def test_one_failed_poll_is_red_but_not_frozen():
    v = _classify(
        _row(
            last_sync_status="failed",
            consecutive_failures=1,
            last_error_summary="rejected: fetch first",
        ),
        _cfg(),
    )
    assert v["state"] == "red"
    assert v["sync_failing"] is False
    assert v["freeze"] is False
    assert "rejected: fetch first" in v["reason"]


def test_sync_failing_freezes_any_binding():
    """#1808 unchanged: a deployment that is failing still freezes."""
    v = _classify(
        _row(
            last_sync_status="failed", consecutive_failures=3, last_error_summary="boom"
        ),
        _cfg(source_mode=1, auto_sync=0),
    )
    assert v["state"] == "red"
    assert v["sync_failing"] is True
    assert v["freeze"] is True
    assert v["freeze_cause"] == "sync_failing"
    assert "3 polls" in v["freeze_reason"]


def test_sync_failing_freeze_ignores_observation_freshness():
    """Rule 2 is the #1808 rule as it shipped — only the NEW divergence clause
    is freshness-guarded."""
    v = _classify(
        _row(
            last_sync_status="failed",
            consecutive_failures=5,
            last_check_at=_iso(NOW - timedelta(hours=3)),
        ),
        _cfg(),
    )
    assert v["freeze"] is True


def test_the_failed_reason_is_bounded_to_120_chars_of_error():
    err = "x" * 500
    v = _classify(
        _row(last_sync_status="failed", consecutive_failures=1, last_error_summary=err),
        _cfg(),
    )
    assert "x" * 120 in v["reason"]
    assert "x" * 121 not in v["reason"]


# --------------------------------------------------------------------------
# Rule 3 — divergence age, the 24 h boundary
# --------------------------------------------------------------------------


def test_work_agent_diverged_one_second_under_24h_is_yellow_and_runs():
    v = _classify(
        _row(ahead_working=7, diverged_since=_iso(NOW - DAY + timedelta(seconds=1))),
        _cfg(),
    )
    assert v["state"] == "yellow"
    assert v["freeze"] is False


def test_work_agent_diverged_one_second_over_24h_is_red_and_frozen():
    v = _classify(
        _row(ahead_working=7, diverged_since=_iso(NOW - DAY - timedelta(seconds=1))),
        _cfg(),
    )
    assert v["state"] == "red"
    assert v["freeze"] is True
    assert v["freeze_cause"] == "divergence"
    assert v["divergence_age_s"] == 86401
    assert v["freeze_reason"] == "diverged 0 behind / 7 ahead for 24h"


def test_reason_matches_the_issue_example():
    v = _classify(
        _row(behind_working=31, diverged_since=_iso(NOW - timedelta(hours=26))),
        _cfg(),
    )
    assert v["reason"].startswith("diverged 31 behind / 0 ahead for 26h")


def test_freeze_flag_off_is_red_but_never_frozen():
    v = _classify(
        _row(ahead_working=7, diverged_since=_iso(NOW - timedelta(hours=30))),
        _cfg(freeze=0),
    )
    assert v["state"] == "red"
    assert v["freeze"] is False


def test_deployment_behind_31_for_26h_is_yellow_and_runs():
    """AC 3: a deployment being behind is normal."""
    v = _classify(
        _row(behind_working=31, diverged_since=_iso(NOW - timedelta(hours=26))),
        _cfg(source_mode=1, auto_sync=0),
    )
    assert v["state"] == "yellow"
    assert v["binding"] == "deployment"
    assert v["work_agent"] is False
    assert v["freeze"] is False


def test_fork_to_own_freezes():
    """source_mode=1 in the DB but auto-syncing to its own fork: a work agent."""
    v = _classify(
        _row(ahead_working=7, diverged_since=_iso(NOW - timedelta(hours=30))),
        _cfg(source_mode=1, auto_sync=1),
    )
    assert v["work_agent"] is True
    assert v["binding"] == "agent"
    assert v["freeze"] is True


def test_source_mode_with_auto_sync_off_never_freezes_on_divergence():
    v = _classify(
        _row(ahead_working=7, diverged_since=_iso(NOW - timedelta(hours=300))),
        _cfg(source_mode=1, auto_sync=0),
    )
    assert v["freeze"] is False
    assert v["state"] == "yellow"


def test_stale_observation_stays_red_but_does_not_freeze():
    """D6: the poller writes nothing for an unreachable agent, so the last row
    can be arbitrarily old. A stale divergence must not keep an agent frozen."""
    v = _classify(
        _row(
            ahead_working=7,
            diverged_since=_iso(NOW - timedelta(hours=30)),
            last_check_at=_iso(NOW - timedelta(minutes=16)),
        ),
        _cfg(),
    )
    assert v["state"] == "red"
    assert v["stale_observation"] is True
    assert v["freeze"] is False
    assert "last observed 16m ago" in v["reason"]


def test_fresh_observation_within_15_minutes_freezes():
    v = _classify(
        _row(
            ahead_working=7,
            diverged_since=_iso(NOW - timedelta(hours=30)),
            last_check_at=_iso(NOW - timedelta(minutes=14)),
        ),
        _cfg(),
    )
    assert v["stale_observation"] is False
    assert v["freeze"] is True


def test_aware_and_naive_timestamps_give_the_same_verdict():
    """The scheduler reads TEXT from SQLite and the backend may hold datetimes;
    a naive value is UTC (the `parse_scheduler_ts` convention)."""
    diverged = NOW - DAY - timedelta(seconds=1)
    aware = _classify(_row(ahead_working=7, diverged_since=_iso(diverged)), _cfg())
    naive_str = _classify(
        _row(ahead_working=7, diverged_since=diverged.replace(tzinfo=None).isoformat()),
        _cfg(),
    )
    naive_dt = _classify(
        _row(
            ahead_working=7,
            diverged_since=diverged.replace(tzinfo=None),
            last_check_at=(NOW - timedelta(seconds=30)).replace(tzinfo=None),
        ),
        _cfg(),
    )
    offset = _classify(
        _row(
            ahead_working=7,
            diverged_since=diverged.astimezone(
                timezone(timedelta(hours=3))
            ).isoformat(),
        ),
        _cfg(),
    )
    for v in (naive_str, naive_dt, offset):
        assert (v["state"], v["freeze"], v["divergence_age_s"]) == (
            aware["state"],
            aware["freeze"],
            aware["divergence_age_s"],
        )
    assert aware["freeze"] is True


def test_garbage_timestamp_is_ignored_not_raised():
    v = _classify(_row(ahead_working=7, diverged_since="not-a-date"), _cfg())
    assert v["freeze"] is False
    assert v["divergence_age_s"] is None


def test_behind_main_is_information_only():
    """D7/AC 6: a human push to `main` is not divergence of a working branch."""
    v = _classify(_row(behind_main=4), _cfg())
    assert v["state"] == "green"
    v2 = _classify(
        _row(
            ahead_working=7,
            behind_main=4,
            diverged_since=_iso(NOW - timedelta(hours=2)),
        ),
        _cfg(),
    )
    assert "(main is 4 ahead)" in v2["reason"]


# --------------------------------------------------------------------------
# Rule 4 — dirt
# --------------------------------------------------------------------------


def test_work_agent_dirty_one_second_over_24h_is_red_but_not_frozen():
    v = _classify(
        _row(dirty_files=791, dirty_since=_iso(NOW - DAY - timedelta(seconds=1))),
        _cfg(),
    )
    assert v["state"] == "red"
    assert v["freeze"] is False
    assert v["dirty_age_s"] == 86401
    assert "dirty: 791 files uncommitted for 24h" in v["reason"]


def test_dirt_under_24h_is_not_a_state():
    v = _classify(
        _row(dirty_files=791, dirty_since=_iso(NOW - DAY + timedelta(seconds=1))),
        _cfg(),
    )
    assert v["state"] == "green"


def test_deployment_dirty_over_24h_is_yellow():
    v = _classify(
        _row(dirty_files=791, dirty_since=_iso(NOW - timedelta(hours=30))),
        _cfg(source_mode=1, auto_sync=0),
    )
    assert v["state"] == "yellow"


# --------------------------------------------------------------------------
# Rule 5 — the 7-day no-heartbeat floor
# --------------------------------------------------------------------------


def test_auto_sync_on_without_a_heartbeat_for_9_days_is_red():
    v = _classify(
        _row(last_sync_at=_iso(NOW - timedelta(days=9)), last_sync_status="success"),
        _cfg(),
    )
    assert v["state"] == "red"
    assert "auto-sync on, no heartbeat for 9d" in v["reason"]
    assert v["freeze"] is False


def test_heartbeat_six_days_old_is_not_the_floor():
    v = _classify(_row(last_sync_at=_iso(NOW - timedelta(days=6))), _cfg())
    assert v["state"] == "green"


def test_never_heartbeat_is_measured_from_the_binding_creation():
    fresh = _classify(
        _row(last_sync_at=None, last_sync_status="never"),
        _cfg(created_at=_iso(NOW - timedelta(minutes=5))),
    )
    assert fresh["state"] == "green"
    old = _classify(
        _row(last_sync_at=None, last_sync_status="never"),
        _cfg(created_at=_iso(NOW - timedelta(days=8))),
    )
    assert old["state"] == "red"
    assert "no heartbeat for 8d" in old["reason"]


def test_no_heartbeat_floor_does_not_apply_with_auto_sync_off():
    v = _classify(
        _row(last_sync_at=_iso(NOW - timedelta(days=30))),
        _cfg(source_mode=0, auto_sync=0),
    )
    assert v["state"] == "green"


# --------------------------------------------------------------------------
# Multiple clauses and recommendations
# --------------------------------------------------------------------------


def test_first_red_names_the_reason_and_the_rest_append():
    v = _classify(
        _row(
            last_sync_status="failed",
            consecutive_failures=1,
            last_error_summary="boom",
            ahead_working=7,
            diverged_since=_iso(NOW - timedelta(hours=30)),
        ),
        _cfg(),
    )
    assert v["reason"].startswith("last sync failed")
    assert "diverged 0 behind / 7 ahead for 30h" in v["reason"]
    # The freeze reason names ONLY the clause that froze.
    assert v["freeze_reason"] == "diverged 0 behind / 7 ahead for 30h"


@pytest.mark.parametrize(
    ("row_over", "cfg_over", "kw", "expected"),
    [
        (
            {
                "last_sync_status": "failed",
                "consecutive_failures": 1,
                "last_error_summary": "remote: Write access to repository not granted.",
            },
            {},
            {"push_denied": True},
            "credential is read-only",
        ),
        (
            {
                "last_sync_status": "failed",
                "consecutive_failures": 1,
                "last_error_summary": "refused: source-mode on main",
            },
            {},
            {},
            "deployment: turn auto-sync off",
        ),
        (
            {
                "last_sync_status": "failed",
                "consecutive_failures": 1,
                "last_error_summary": "rejected (fetch first)",
            },
            {},
            {},
            "push via git_sync strategy=pull_first",
        ),
        ({"ahead_working": 7}, {"auto_sync": 0}, {}, "enable auto-sync"),
        ({"behind_working": 31}, {}, {}, "pull via git_pull"),
        ({}, {}, {}, None),
    ],
)
def test_recommendation_mapping(row_over, cfg_over, kw, expected):
    v = _classify(_row(**row_over), _cfg(**cfg_over), **kw)
    assert v["recommendation"] == expected


def test_constants_are_the_ruling():
    assert policy.SYNC_FAILURE_FREEZE_THRESHOLD == 3
    assert policy.DIVERGENCE_RED_SECONDS == 86400
    assert policy.DIRTY_RED_SECONDS == 86400
    assert policy.NO_HEARTBEAT_FLOOR_SECONDS == 7 * 86400
    assert policy.OBSERVATION_FRESH_SECONDS == 900


def test_classify_never_raises_on_hostile_input():
    """Agent-authored numbers reach this function via the DB; a wrong type
    must degrade, never raise into the scheduler's fire path."""
    v = _classify(
        _row(
            ahead_working="7",
            behind_working=None,
            dirty_files=[1],
            consecutive_failures="x",
            diverged_since=12345,
        ),
        {"source_mode": None, "auto_sync_enabled": "1"},
    )
    assert v["state"] in {"green", "yellow", "red", "unknown"}
