"""#2423 — the Workspace told a client its agent ran 12 loops and gave it nowhere to go.

A client saw `Loops 12` in the activity legend and a run of `Loop` rows in
Recent work, with no way to open a loop, no way to see what one produced, and no
way to start or stop one. The strip that controls loops is gated on
`isPlatformSession` (ent#458, correctly — loops are an operator capability), and
the Workspace agent page has no Loops tab. So the loop COUNT was client-visible
while the loop OUTPUT was operator-only.

THE DIRECTION, AND WHY IT IS NOT A NEW PRODUCT CALL. `agent_page`'s own
docstring already decides this:

    It reports; it does not configure. ... The viewer may be an external client,
    not an operator.

and it already drops `alert` asks for being "operations telemetry, not something
the agent is asking a person". A loop run is the same kind of thing. The module
is subtractive by design, so the fix follows its existing rule rather than
inventing a second one.

BUT NOT SUBTRACTIVE FOR EVERYONE. The same page serves a platform user, who CAN
click through to Agent Detail -> Loops and read every run. Hiding it from them
would remove real signal to fix a client-only problem, so the split is by
principal — exactly the `include_owned=is_platform` pattern the roster and
`_require_roster` already use.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def page():
    from client_portal import agent_page as m
    return m


def _rows():
    return [
        {"id": "e1", "status": "success", "triggered_by": "loop", "started_at": "t1",
         "completed_at": "t2", "duration_ms": 12837, "schedule_id": None},
        {"id": "e2", "status": "success", "triggered_by": "chat", "started_at": "t3",
         "completed_at": "t4", "duration_ms": 18986, "schedule_id": None},
        {"id": "e3", "status": "success", "triggered_by": "loop", "started_at": "t5",
         "completed_at": "t6", "duration_ms": 7237, "schedule_id": None},
        {"id": "e4", "status": "success", "triggered_by": "schedule", "started_at": "t7",
         "completed_at": "t8", "duration_ms": 4100, "schedule_id": "s1"},
    ]


@pytest.fixture()
def stub_db(monkeypatch, page):
    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None):
            return _rows()

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "_schedule_names", lambda a, r: {"s1": "Nightly sweep"},
                        raising=False)


# ---------------------------------------------------------------------------
# recent_work
# ---------------------------------------------------------------------------
def test_a_client_does_not_see_loop_rows(page, stub_db):
    """The reported symptom: rows labelled `Loop` that lead nowhere."""
    out = page._recent_work("a", is_platform=False)
    triggers = [r["triggered_by"] for r in out]
    assert "loop" not in triggers, (
        "a client cannot open, control or explain a loop run, so a row for one "
        "is activity it can only misread"
    )
    # Everything else it CAN act on survives — this is a narrowing, not a purge.
    assert triggers == ["chat", "schedule"]
    assert out[-1]["schedule_name"] == "Nightly sweep"


def test_an_operator_still_sees_them(page, stub_db):
    """A platform user can reach Agent Detail -> Loops, so hiding costs them
    real signal and buys nothing."""
    out = page._recent_work("a", is_platform=True)
    assert [r["triggered_by"] for r in out] == ["loop", "chat", "loop", "schedule"]


def test_the_client_view_is_the_default(page, stub_db):
    """Fail-closed toward the stricter viewer: a caller that forgets to say who
    is looking gets the projection that leaks least."""
    assert [r["triggered_by"] for r in page._recent_work("a")] == ["chat", "schedule"]


def test_the_projection_still_drops_what_it_always_dropped(page, stub_db):
    """This must not become the only guard on that projection."""
    for row in page._recent_work("a", is_platform=True):
        for banned in ("message", "cost", "model_used", "source_user_email"):
            assert banned not in row


# ---------------------------------------------------------------------------
# the chart
# ---------------------------------------------------------------------------
def _analytics():
    return {
        "window_hours": 168,
        "total_executions": 17,
        "success_rate": 1.0,
        "timeline": [
            {"date": "2026-08-27", "total": 13, "by_type": {"Loops": 12, "Chat/Tasks": 1}},
            {"date": "2026-08-28", "total": 4, "by_type": {"Public": 4}},
        ],
        # The REAL shape (`db/schedules/analytics.py`): a list of rows at the
        # top level, a dict per timeline day. My first fixture used a dict for
        # both and passed against a payload the accessor never emits.
        "by_type": [
            {"bucket": "Chat/Tasks", "total": 1},
            {"bucket": "Public", "total": 4},
            {"bucket": "Loops", "total": 12},
        ],
        "buckets": ["Chat/Tasks", "Public", "Loops"],
    }


@pytest.fixture()
def stub_stats(monkeypatch, page):
    class _Db:
        def get_agent_analytics(self, agent_name, hours):
            return _analytics()

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "portal_db",
                        type("P", (), {"first_try_stats": staticmethod(lambda a, h: {})})(),
                        raising=False)


def test_the_loops_bucket_is_gone_for_a_client(page, stub_stats):
    """`Loops 12` in the legend is the same unexplained claim as the rows.

    Removing the rows and leaving the legend would be worse than either: a
    number with nothing behind it.
    """
    s = page._stats("a", "7d", is_platform=False)
    assert [r["bucket"] for r in s["by_type"]] == ["Chat/Tasks", "Public"]
    assert "Loops" not in s["buckets"]
    for day in s["timeline"]:
        assert "Loops" not in day.get("by_type", {})


def test_day_totals_are_recomputed_not_left_stale(page, stub_stats):
    """A day whose count still says 13 while its segments sum to 1 is a chart
    that reports its own filtering as missing data."""
    s = page._stats("a", "7d", is_platform=False)
    day = next(d for d in s["timeline"] if d["date"] == "2026-08-27")
    assert day["total"] == 1 == sum(day["by_type"].values())


def test_the_headline_total_agrees_with_the_chart(page, stub_stats):
    """17 executions above a chart that can only account for 5 is the same
    inconsistency one level up."""
    s = page._stats("a", "7d", is_platform=False)
    assert s["total_executions"] == 5


def test_an_operator_keeps_the_whole_chart(page, stub_stats):
    s = page._stats("a", "7d", is_platform=True)
    assert {r["bucket"]: r["total"] for r in s["by_type"]}["Loops"] == 12
    assert s["total_executions"] == 17
    assert "Loops" in s["buckets"]


def test_stats_failure_still_degrades_the_same_way(page, monkeypatch):
    """The unavailable path predates this and must not acquire a new shape."""
    class _Boom:
        def get_agent_analytics(self, agent_name, hours):
            raise RuntimeError("analytics down")

    monkeypatch.setattr(page, "db", _Boom(), raising=False)
    s = page._stats("a", "7d", is_platform=False)
    assert s["unavailable"] is True
    assert s["by_type"] == [] and s["timeline"] == []


# ---------------------------------------------------------------------------
# the seam
# ---------------------------------------------------------------------------
def test_build_page_threads_the_principal(page):
    """Both projections must key off the SAME flag the route already resolves,
    or the page can hide the rows and keep the legend."""
    import inspect
    src = inspect.getsource(page.build_page)
    assert "is_platform" in inspect.signature(page.build_page).parameters
    assert "_recent_work(agent_name, is_platform=is_platform)" in src
    assert "_stats(agent_name, window, is_platform=is_platform)" in src


def test_the_route_passes_who_is_looking(page):
    """`principal.is_platform` is already resolved for `get_agent_card`; this
    asserts the page gets the same answer rather than assuming a default."""
    import inspect
    from client_portal import router

    src = inspect.getsource(router.portal_agent_page)
    assert "is_platform=principal.is_platform" in src
