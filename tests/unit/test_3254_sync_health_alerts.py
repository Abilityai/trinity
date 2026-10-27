"""#3254 S2: the git sync health alerts on the platform-alert seam.

The sync-health poller raises four conditions: sync failing, sync diverged, a
`.git` over its size ceiling and a failing-maintenance streak. Each used to be
one row per crossing, filed directly and never ended. On the seam
(`services.platform_alerts`) each is one pending row per `(agent, kind)`:

* the row keeps the `type` its readers filter on (D2: `Kind.row_type`);
* two readings are one row; a falling edge ends it as the platform;
* a person's ending stands, and a re-reading inside the snooze files nothing;
* a condition that persists is re-read at most every 6 hours (R9), so its
  deadline moves while it holds, and only a condition nobody re-read (a clear
  lost to a fault) ends on its 30-day lifetime;
* git bloat is two conditions, two subjects (AC 5);
* the divergence row ends only when the `diverged_since` episode ends, never
  when sync failing takes precedence as the freeze cause;
* R1: the second upgrade sweep derives the subject `observe` uses from the
  pre-move ids (git bloat is exempt, residual stated in its test).

Driven through the REAL `SyncHealthService._poll_cycle` against a real DB
(db_harness); only the agent's `/api/git/status` response is faked. The clock
is pinned (T0) and moved by the test, never compared to the wall clock.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def pa(monkeypatch):
    from services import operator_resume_service, platform_alerts
    # The seam's follow-up (a thin broadcast) needs no loop here.
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return platform_alerts


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database
    return database


# --------------------------------------------------------------------------
# D2: the row type survives the move
# --------------------------------------------------------------------------

_ROW_TYPES = {
    "sync_failing": "sync_failing",
    "sync_diverged": "sync_diverged",
    "git_bloat_size": "git_bloat",
    "git_bloat_maintenance": "git_bloat",
}


@pytest.mark.parametrize("kind", sorted(_ROW_TYPES))
def test_the_seam_writes_the_kinds_row_type(db, pa, kind):
    """The `?type=` list filter, `/stats` by_type, the MCP list tool and the UI
    label read these types; the move must not turn them into `alert`."""
    assert pa.observe("alpha", kind, "alpha", title="t", question="q") == pa.OBSERVED_CREATED
    (row,) = db.list_operator_queue_items(agent_name="alpha", status="pending")
    assert row["type"] == _ROW_TYPES[kind]
    assert row["subject"] == f"{kind}:alpha"


@pytest.mark.parametrize("kind, row_type", [
    ("circuit_dormant", "alert"),      # no row_type, unbudgeted: unchanged
    ("queue_flood", "queue_flood"),    # no row_type, budgeted: its budgeted type
])
def test_a_kind_without_a_row_type_keeps_the_old_type(db, pa, kind, row_type):
    pa.observe("alpha", kind, "oversize_file", title="t", question="q")
    (row,) = db.list_operator_queue_items(agent_name="alpha", status="pending")
    assert row["type"] == row_type


def test_an_explicit_row_type_must_match_the_budgeted_type(pa):
    """The #1677 budget counts pending rows BY TYPE, so a budgeted kind that
    wrote another type would never meet its own cap."""
    with pytest.raises(ValueError, match="row_type"):
        pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                budgeted_type="x_budget", row_type="x_other")
    same = pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                   budgeted_type="x_budget", row_type="x_budget")
    assert same.row_type == "x_budget"
    unbudgeted = pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                         row_type="x_other")
    assert unbudgeted.row_type == "x_other"
