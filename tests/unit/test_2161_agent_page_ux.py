"""Workspace agent page UX repairs (#2161).

Four defects, and the two that reach the backend both turn on the same question:
how much context can a row carry when the reader may be an **external client**?

ent#360 answered "none" and projected `recent_work` down to shape — status,
trigger, timing. Correct for `message` (another user's prompt) and for
`cost`/`model_used` (AC #7), but it left every scheduled row rendering the same
three words, which is the complaint #2161 was filed over. The resolution is one
field: the schedule's NAME, which is a short operator-authored label, and never
its `message`, which is a prompt and is exactly what the page must not show.

So these tests are mostly about the seam around that one field:

* it appears, resolved, for rows that have one — the happy path, because a
  fail-soft wrapper that is only ever exercised by its own failure branch tests
  nothing;
* it is absent for the sentinel/soft-deleted/foreign cases, rather than leaking
  a stale or another agent's label;
* it costs ONE query for the whole page, because per-row resolution is an N+1
  that also throws away the agent scoping the map shape gets for free;
* and a schedules read that falls over costs the labels, never the rows.

The chart half is one assertion: the canonical stack order is forwarded from the
analytics accessor rather than re-derived, so the portal and the operator surface
cannot disagree about it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "alice@example.com"


def _schedule(sid, name, message="do the thing"):
    """`list_agent_schedules` returns pydantic models, not dicts — attribute
    access, and carrying the `message` the mapping must never read."""
    return SimpleNamespace(id=sid, name=name, message=message)


def _rows(*specs):
    return [
        {
            "id": f"e{i}", "status": "success", "triggered_by": trig,
            "started_at": "2026-08-13T10:00:00Z",
            "completed_at": "2026-08-13T10:00:09Z", "duration_ms": 9000,
            "schedule_id": sid,
        }
        for i, (trig, sid) in enumerate(specs)
    ]


# ---------------------------------------------------------------------------
# The name resolves — the happy path
# ---------------------------------------------------------------------------

def test_a_scheduled_row_carries_the_schedules_name(monkeypatch):
    """The defect: eight rows all reading "Scheduled run". The name is what
    makes them different from each other."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("schedule", "sched-1")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules",
                        lambda name: [_schedule("sched-1", "Weekly invoice digest")])

    assert agent_page._recent_work(AGENT)[0]["schedule_name"] == "Weekly invoice digest"


def test_a_webhook_that_fires_a_schedule_is_named_too(monkeypatch):
    """Keyed on the id resolving, NOT on `triggered_by == "schedule"`: a webhook
    that fires a schedule *is* running that schedule, and naming it is the whole
    point. Restricting by trigger would blank a row that has an answer."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("webhook", "sched-1")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules",
                        lambda name: [_schedule("sched-1", "Nightly sync")])

    assert agent_page._recent_work(AGENT)[0]["schedule_name"] == "Nightly sync"


def test_the_schedules_message_is_never_read(monkeypatch):
    """The `Schedule` model carries the prompt. The mapping is id → name, and
    this is the pin that says so — the whole page exists to not show prompts."""
    from client_portal import agent_page

    secret = "Email every client about the Q3 shortfall"
    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("schedule", "sched-1")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules",
                        lambda name: [_schedule("sched-1", "Client comms", message=secret)])

    assert secret not in repr(agent_page._recent_work(AGENT))


# ---------------------------------------------------------------------------
# The name does NOT resolve — every miss is a fallback, never a stale label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schedule_id", ["__manual__", None, ""])
def test_rows_with_no_real_schedule_resolve_to_nothing(monkeypatch, schedule_id):
    """Chat turns and reminders carry the `__manual__` sentinel. There is no
    name to find, so there is no query to make either."""
    from client_portal import agent_page

    def unexpected(name):
        raise AssertionError("no schedule id on the page — should not have queried")

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("manual", schedule_id)))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules", unexpected)

    assert agent_page._recent_work(AGENT)[0]["schedule_name"] is None


def test_a_soft_deleted_schedule_leaves_the_row_unlabelled(monkeypatch):
    """`list_agent_schedules` excludes soft-deleted rows (#834), which are kept
    for up to 30 days — so their executions outlive their name. A bare
    "Scheduled run" is the honest answer; inventing one is not."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("schedule", "deleted-sched")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules", lambda name: [])

    assert agent_page._recent_work(AGENT)[0]["schedule_name"] is None


def test_a_foreign_schedule_id_cannot_pull_in_another_agents_label(monkeypatch):
    """The map is built from THIS agent's schedules, so a stale or foreign id
    simply misses. That scoping is a property of the shape, not a check that
    could be forgotten — which is why it is a map and not a per-id lookup."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("schedule", "someone-elses-sched")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules",
                        lambda name: [_schedule("mine-1", "My own schedule")])

    assert agent_page._recent_work(AGENT)[0]["schedule_name"] is None


# ---------------------------------------------------------------------------
# Cost and degradation
# ---------------------------------------------------------------------------

def test_the_whole_page_costs_one_schedules_query(monkeypatch):
    """Per-row resolution is the regression mode here, and it is invisible in
    the output — twenty rows look identical either way. So the call count is
    what has to be asserted."""
    from client_portal import agent_page

    calls = []
    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary", lambda *a, **k: _rows(
        ("schedule", "s1"), ("schedule", "s2"), ("schedule", "s1"), ("webhook", "s2"),
    ))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules",
                        lambda name: (calls.append(name),
                                      [_schedule("s1", "One"), _schedule("s2", "Two")])[1])

    got = agent_page._recent_work(AGENT)

    assert len(calls) == 1
    assert [r["schedule_name"] for r in got] == ["One", "Two", "One", "Two"]


def test_a_failing_schedules_read_costs_the_labels_not_the_rows(monkeypatch):
    """Recent work is the section; the names are a garnish on it. Losing the
    whole list because the schedules table is unhappy inverts that."""
    from client_portal import agent_page

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_page.db, "get_agent_executions_summary",
                        lambda *a, **k: _rows(("schedule", "sched-1")))
    monkeypatch.setattr(agent_page.db, "list_agent_schedules", boom)

    got = agent_page._recent_work(AGENT)

    assert len(got) == 1 and got[0]["schedule_name"] is None


# ---------------------------------------------------------------------------
# The chart's stack order
# ---------------------------------------------------------------------------

def test_stats_forward_the_canonical_bucket_order(monkeypatch):
    """`get_agent_analytics` already computes the stack order (`_BUCKET_ORDER`
    filtered to what occurred). Forwarding it keeps ONE ordering in the install;
    deriving a second one in the portal would be equivalent today and free to
    drift from the operator surface tomorrow."""
    from client_portal import agent_page

    monkeypatch.setattr(agent_page.db, "get_agent_analytics", lambda *a, **k: {
        "window_hours": 168, "total_executions": 3, "success_rate": 1.0,
        "timeline": [], "by_type": [{"bucket": "Scheduled", "total": 3}],
        "buckets": ["Chat/Tasks", "Scheduled"],
    })
    monkeypatch.setattr(agent_page.portal_db, "first_try_stats",
                        lambda *a, **k: {"terminal": 0, "first_try": 0, "rate": None})

    assert agent_page._stats(AGENT, "7d")["buckets"] == ["Chat/Tasks", "Scheduled"]


def test_unavailable_stats_still_carry_an_empty_bucket_list(monkeypatch):
    """The degraded envelope has to have the same SHAPE as the healthy one — the
    chart reads `buckets` either way, and a missing key would be a render error
    on top of an already-degraded page."""
    from client_portal import agent_page

    def boom(*a, **k):
        raise RuntimeError("analytics down")

    monkeypatch.setattr(agent_page.db, "get_agent_analytics", boom)

    stats = agent_page._stats(AGENT, "7d")

    assert stats["unavailable"] is True and stats["buckets"] == []
