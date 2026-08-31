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


def _sql_like(rows, limit, exclude_triggers):
    """What the accessor actually does now: WHERE before LIMIT.

    Every stub in this file models it, because a stub that limits first and
    filters second is the very bug under test and would make these tests pass
    against the broken implementation (review pass 2).
    """
    if exclude_triggers:
        rows = [r for r in rows
                if r.get("triggered_by") is None
                or r.get("triggered_by") not in exclude_triggers]
    return rows[:limit] if limit else rows


@pytest.fixture()
def stub_db(monkeypatch, page):
    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            return _sql_like(_rows(), limit, exclude_triggers)

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
# The filter must not starve the list (review blocker)
# ---------------------------------------------------------------------------
def test_a_loop_heavy_agent_still_shows_its_other_work(page, monkeypatch):
    """The list must not starve, at ANY run length — the review-pass-2 blocker.

    The first fix over-fetched `MAX_RECENT_WORK * 5 = 100` rows and filtered in
    Python. That is the same bug with a constant in front of it, and the
    constant loses: `models.MAX_RUNS_LIMIT` is 100, so a single loop at its
    DOCUMENTED MAXIMUM emits exactly 100 consecutive rows and fills the entire
    over-fetch window. The client page then reads "Nothing yet." for an agent
    that has been working all day.

    So this fixture uses 250 loops — more than any multiplier — and passes only
    because the exclusion is in SQL, where the LIMIT applies to rows that
    already survived the WHERE.
    """
    asked = {}
    loops = [{"id": f"l{i}", "status": "success", "triggered_by": "loop",
              "started_at": f"t{i:04d}", "completed_at": None, "duration_ms": 10,
              "schedule_id": None} for i in range(250)]
    chats = [{"id": f"c{i}", "status": "success", "triggered_by": "chat",
              "started_at": f"u{i}", "completed_at": None, "duration_ms": 10,
              "schedule_id": None} for i in range(5)]

    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            asked["limit"] = limit
            asked["exclude"] = exclude_triggers
            return _sql_like(loops + chats, limit, exclude_triggers)

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "_schedule_names", lambda a, r: {}, raising=False)

    out = page._recent_work("a", is_platform=False)
    assert out, "a loop-heavy agent rendered as having done nothing at all"
    assert all(r["triggered_by"] == "chat" for r in out)
    assert asked["exclude"] == page._CLIENT_HIDDEN_TRIGGERS, (
        "the exclusion must reach the accessor — filtering the RESULT is what "
        "starved the list in the first place"
    )


def test_the_client_page_does_not_over_fetch_either(page, monkeypatch):
    """The SQL filter also removes the extra read.

    Pinned because 'fetch more and drop some' is the tempting fix, and it costs
    five reads' worth of rows on every client page load to throw most away.
    """
    asked = {}

    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            asked["limit"] = limit
            return []

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "_schedule_names", lambda a, r: {}, raising=False)
    page._recent_work("a", is_platform=False)
    assert asked["limit"] == page.MAX_RECENT_WORK


def test_last_active_is_scoped_to_what_the_viewer_can_see(page, monkeypatch):
    """A header timestamp from a row the client cannot see is unexplainable.

    `_last_active` read the newest row unconditionally, so a client saw "active
    2 minutes ago" above a list whose newest entry was yesterday's — with
    nothing on the page able to reconcile the two.
    """
    rows = [{"id": "l", "triggered_by": "loop", "started_at": "2026-08-31T12:00:00Z"},
            {"id": "c", "triggered_by": "chat", "started_at": "2026-08-30T09:00:00Z"}]

    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            return _sql_like(rows, limit, exclude_triggers)

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    assert page._last_active("a", is_platform=False) == "2026-08-30T09:00:00Z"
    assert page._last_active("a", is_platform=True) == "2026-08-31T12:00:00Z"


def test_the_client_list_is_still_bounded(page, monkeypatch):
    """Over-fetching must not become an unbounded list — the cap moves, it does
    not disappear."""
    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            rows = [{"id": f"c{i}", "status": "success", "triggered_by": "chat",
                     "started_at": f"u{i}", "completed_at": None, "duration_ms": 10,
                     "schedule_id": None} for i in range(200)]
            return _sql_like(rows, limit, exclude_triggers)

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "_schedule_names", lambda a, r: {}, raising=False)
    assert len(page._recent_work("a", is_platform=False)) == page.MAX_RECENT_WORK


def test_the_operator_side_asks_for_no_exclusion(page, monkeypatch):
    """A platform viewer sees every row, so no WHERE clause is added for them."""
    asked = {}

    class _Db:
        def get_agent_executions_summary(self, agent_name, limit=None,
                                         *, exclude_triggers=None):
            asked["limit"] = limit
            asked["exclude"] = exclude_triggers
            return []

    monkeypatch.setattr(page, "db", _Db(), raising=False)
    monkeypatch.setattr(page, "_schedule_names", lambda a, r: {}, raising=False)
    page._recent_work("a", is_platform=True)
    assert asked["limit"] == page.MAX_RECENT_WORK
    assert asked["exclude"] is None


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


# ---------------------------------------------------------------------------
# The stats strip must not contradict itself (review pass 2, non-blocking)
# ---------------------------------------------------------------------------
def test_rates_are_withheld_when_every_row_was_hidden(page, monkeypatch):
    """"0 executions" beside "89% success" is three numbers describing work the
    same strip says did not happen.

    `success_rate` and `first_try` are deliberately NOT re-derived over the
    filtered set — a filtered numerator over an unfiltered denominator is worse
    than a figure that is merely broad. That argument holds only while there is
    visible work to be broad ABOUT; at exactly zero it stops being broad and
    becomes a contradiction the client cannot resolve.
    """
    monkeypatch.setattr(page.db, "get_agent_analytics", lambda a, h: {
        "window_hours": h, "total_executions": 12, "success_rate": 89.0,
        "timeline": [{"date": "2026-08-30", "total": 12,
                      "by_type": {"Loops": 12}}],
        "by_type": [{"bucket": "Loops", "total": 12}],
        "buckets": ["Loops"],
    }, raising=False)
    monkeypatch.setattr(page.portal_db, "first_try_stats",
                        lambda a, h: {"terminal": 37, "first_try": 33, "rate": 89.2},
                        raising=False)

    out = page._stats("a", page.DEFAULT_WINDOW, is_platform=False)
    assert out["total_executions"] == 0
    assert out["success_rate"] is None, "a rate over zero visible work is a contradiction"
    assert out["first_try"]["rate"] is None
    assert out["unavailable"] is False, (
        "withheld is not the same as unavailable — the read succeeded"
    )
    # Withheld, never zeroed: 0% reads as "it fails every time".
    assert out["first_try"]["rate"] != 0


def test_rates_survive_when_any_visible_work_remains(page, monkeypatch):
    """The suppression is exactly at zero — one surviving row keeps the figures,
    broad as they are."""
    monkeypatch.setattr(page.db, "get_agent_analytics", lambda a, h: {
        "window_hours": h, "total_executions": 13, "success_rate": 89.0,
        "timeline": [{"date": "2026-08-30", "total": 13,
                      "by_type": {"Loops": 12, "Chat": 1}}],
        "by_type": [{"bucket": "Loops", "total": 12}, {"bucket": "Chat", "total": 1}],
        "buckets": ["Loops", "Chat"],
    }, raising=False)
    monkeypatch.setattr(page.portal_db, "first_try_stats",
                        lambda a, h: {"terminal": 37, "first_try": 33, "rate": 89.2},
                        raising=False)

    out = page._stats("a", page.DEFAULT_WINDOW, is_platform=False)
    assert out["total_executions"] == 1
    assert out["success_rate"] == 89.0
    assert out["first_try"]["rate"] == 89.2


def test_an_operator_never_has_rates_withheld(page, monkeypatch):
    """Nothing is hidden from them, so a zero total is a real zero."""
    monkeypatch.setattr(page.db, "get_agent_analytics", lambda a, h: {
        "window_hours": h, "total_executions": 12, "success_rate": 89.0,
        "timeline": [], "by_type": [{"bucket": "Loops", "total": 12}],
        "buckets": ["Loops"],
    }, raising=False)
    monkeypatch.setattr(page.portal_db, "first_try_stats",
                        lambda a, h: {"terminal": 37, "first_try": 33, "rate": 89.2},
                        raising=False)
    out = page._stats("a", page.DEFAULT_WINDOW, is_platform=True)
    assert out["total_executions"] == 12 and out["success_rate"] == 89.0
