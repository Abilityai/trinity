"""Client-facing row windowing on the Workspace report read (#2162).

The Workspace Reports tab dumped `JSON.stringify(payload)` at external clients.
Routing it through the shared typed renderers is a frontend change — except for
one acceptance criterion: a `table` payload must not transfer wholesale, and the
operator row reader (`GET /api/reports/{id}/rows`, #1537) is
`Depends(get_current_user)`, which a portal principal — a verified email with no
`users` row — structurally cannot satisfy (the #2128 fact).

The answer is two optional query params on the **existing** portal detail route,
not a second route. Three properties are worth pinning, and each is a way this
could go quietly wrong:

  1. **Additive.** With `rows_limit` absent the response is byte-for-byte what it
     was. This route ships today; a windowing bug that changes the default path
     would be a regression in every non-tabular report at once.

  2. **The SERVER decides tabularity, from the real payload.** `display_hint` is
     agent-authored and can disagree with what was actually filed, so a client
     that predicted the shape and asked for the wrong reader would need a 400 and
     a recovery re-fetch. Here a non-tabular payload with `rows_limit` set simply
     comes back whole with no `row_meta` — never a 400, never a mangled payload.

  3. **The gate is inherited, not re-derived.** The window rides `report_detail`,
     so a foreign report id and a missing one stay indistinguishable 404s
     (invariant #8) on the windowed path too. A second hand-written gate on a
     client-facing prefix is how those two answers drift apart.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# ent#365: the report read is scoped to who is asking, so every call names a
# reader. These suites keep their own subjects (agent isolation, row windowing);
# the audience rule itself is pinned in test_ent365_report_audience.py.
CLIENT_EMAIL = "client@example.com"

AGENT = "scribe"
OTHER = "recon"

TABULAR = {
    "columns": ["name", "score"],
    "rows": [[f"row-{i}", i] for i in range(250)],
    # A tabular report may carry siblings; windowing is subtractive on `rows`
    # only and must leave everything else exactly as filed.
    "generated_at": "2026-08-13T00:00:00Z",
}


def _row(payload, agent=AGENT):
    return {
        "id": "r1",
        "agent_name": agent,
        "report_type": "recon.leads",
        "title": "Leads",
        "display_hint": "table",
        "payload": payload,
        "period_start": None,
        "period_end": None,
        "created_at": "2026-08-13T00:00:00Z",
    }


@pytest.fixture
def agent_page(monkeypatch):
    from client_portal import agent_page as mod
    return mod


# ---------------------------------------------------------------------------
# 1. Additive: the default path is untouched
# ---------------------------------------------------------------------------

def test_without_rows_limit_the_response_is_unchanged(agent_page, monkeypatch):
    """The route ships today. `rows_limit=None` must return exactly what it
    returned before this feature existed — including no `row_meta` key at all,
    since the frontend keys "is this windowed?" off its presence."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL)

    assert got["payload"] == TABULAR
    assert len(got["payload"]["rows"]) == 250
    assert "row_meta" not in got


def test_windowing_does_not_mutate_the_source_payload(agent_page, monkeypatch):
    """The windowed copy is a new dict. `db.get_report` decodes fresh per call
    today, so this is hygiene rather than a live bug — but a shared-dict caller
    (or a cache added later) would otherwise find the row list truncated in
    place, i.e. data loss that only shows up on the second read."""
    payload = {"columns": ["a"], "rows": [[i] for i in range(10)]}
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(payload))

    agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=2)

    assert len(payload["rows"]) == 10


# ---------------------------------------------------------------------------
# 2. The window itself
# ---------------------------------------------------------------------------

def test_a_tabular_payload_is_windowed_with_a_true_total(agent_page, monkeypatch):
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_offset=0, rows_limit=100)

    assert got["payload"]["columns"] == ["name", "score"]
    assert got["payload"]["rows"] == TABULAR["rows"][:100]
    # The TRUE total, not the window size — this is what "Showing 100 of 250"
    # reads, and a windowed total would make the footer lie and hide the rest.
    assert got["row_meta"] == {"total": 250, "offset": 0, "limit": 100}
    # Siblings survive.
    assert got["payload"]["generated_at"] == "2026-08-13T00:00:00Z"


def test_a_later_page_starts_where_the_previous_one_ended(agent_page, monkeypatch):
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_offset=100, rows_limit=100)

    assert got["payload"]["rows"] == TABULAR["rows"][100:200]
    assert got["row_meta"] == {"total": 250, "offset": 100, "limit": 100}


def test_an_offset_past_the_end_is_empty_with_an_honest_total(agent_page, monkeypatch):
    """Not an error: the table shrank, or a stale client asked. Empty rows plus
    the real total lets the UI show "0 of N" and stop, rather than dead-end."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_offset=9999, rows_limit=100)

    assert got["payload"]["rows"] == []
    assert got["row_meta"]["total"] == 250


def test_limit_is_clamped_at_the_shared_ceiling(agent_page, monkeypatch):
    """The route declares `le=REPORT_ROWS_PAGE_MAX`, so an over-large limit is a
    422 before it reaches here. The service clamps anyway: it is importable and
    the clamp is the thing that actually bounds the response, so it must not
    depend on one caller's validation being present."""
    from models import REPORT_ROWS_PAGE_MAX

    big = {"columns": ["a"], "rows": [[i] for i in range(REPORT_ROWS_PAGE_MAX + 500)]}
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(big))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=REPORT_ROWS_PAGE_MAX * 10)

    assert len(got["payload"]["rows"]) == REPORT_ROWS_PAGE_MAX
    assert got["row_meta"]["limit"] == REPORT_ROWS_PAGE_MAX


def test_a_negative_offset_reads_from_the_start(agent_page, monkeypatch):
    """Python would treat a negative offset as "from the end" and silently serve
    the WRONG rows under an honest-looking total."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_offset=-50, rows_limit=10)

    assert got["payload"]["rows"] == TABULAR["rows"][:10]
    assert got["row_meta"]["offset"] == 0


# ---------------------------------------------------------------------------
# 3. Non-tabular payloads: whole, silent, never a 400
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"tiles": [{"label": "Leads", "value": 12}]},          # kpi
    {"markdown": "# Weekly\n\nAll good."},                  # markdown
    {"columns": ["a"], "rows": "not-a-list"},               # half-tabular
    {"rows": [[1]]},                                        # rows without columns
    {},                                                     # empty
    [1, 2, 3],                                              # root-level array
    None,                                                   # nothing filed
])
def test_a_non_tabular_payload_is_returned_whole_with_no_row_meta(
    agent_page, monkeypatch, payload,
):
    """`display_hint` is agent-authored and may disagree with what was filed, so
    the client is allowed to ask for a window on anything. The server holds the
    payload and answers honestly: whole payload, no `row_meta`, no 400. The
    frontend then renders no "Load more" footer, which is exactly right for a
    document with no row axis."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(payload))

    got = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=100)

    assert got["payload"] == payload
    assert "row_meta" not in got


# ---------------------------------------------------------------------------
# 4. The inherited gate still holds on the windowed path
# ---------------------------------------------------------------------------

def test_a_foreign_report_stays_unreadable_when_windowed(agent_page, monkeypatch):
    """Report ids are global and the roster gate only proves the caller may
    reach THIS agent. The window rides the same read, so it cannot become a
    second, laxer door onto every report in the install."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row(TABULAR, agent=OTHER))

    assert agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=100) is None


def test_missing_and_foreign_stay_indistinguishable_when_windowed(agent_page, monkeypatch):
    """Both None → the same 404. A different answer on the windowed path would
    reopen the existence oracle invariant #8 closed on the unwindowed one."""
    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: None)
    missing = agent_page.report_detail(AGENT, "nope", client_email=CLIENT_EMAIL, rows_limit=100)

    monkeypatch.setattr(agent_page.db, "get_report_for_client", lambda rid, _email: _row({}, agent=OTHER))
    foreign = agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=100)

    assert missing is None and foreign is None


def test_a_read_failure_is_still_swallowed_into_a_404(agent_page, monkeypatch):
    """Unchanged behaviour, re-asserted through the new parameter: a DB fault
    must not become a 500 that distinguishes itself from a missing report."""
    def boom(_rid):
        raise RuntimeError("db down")

    monkeypatch.setattr(agent_page.db, "get_report_for_client", boom)

    assert agent_page.report_detail(AGENT, "r1", client_email=CLIENT_EMAIL, rows_limit=100) is None


# ---------------------------------------------------------------------------
# 5. The route wiring
# ---------------------------------------------------------------------------

def _bound(query_param, name: str):
    """Read a `ge`/`le` off a FastAPI `Query`, whichever shape it carries.

    Pydantic v2 moved the constraints from attributes onto `metadata` as
    annotated-types markers, so reading only `q.le` silently yields None — i.e.
    a bounds assertion that passes against an UNBOUNDED param. Checked both ways
    rather than pinned to the installed version.
    """
    direct = getattr(query_param, name, None)
    if direct is not None:
        return direct
    for marker in getattr(query_param, "metadata", []) or []:
        if hasattr(marker, name):
            return getattr(marker, name)
    return None


def test_the_route_declares_both_params_within_the_shared_bounds():
    """The page size is a shared constant (`models.REPORT_ROWS_PAGE_MAX`), not a
    third hand-typed mirror — a mirrored constant drifts and the mirror's own
    tests then pin the drift."""
    import inspect

    from client_portal import router as portal_router
    from models import REPORT_ROWS_PAGE_MAX

    sig = inspect.signature(portal_router.portal_agent_report_detail)
    assert "rows_offset" in sig.parameters
    assert "rows_limit" in sig.parameters

    limit = sig.parameters["rows_limit"].default
    assert limit.default is None, "rows_limit must default to None (unwindowed)"
    assert _bound(limit, "le") == REPORT_ROWS_PAGE_MAX
    assert _bound(limit, "ge") == 1
    assert _bound(sig.parameters["rows_offset"].default, "ge") == 0


def test_the_windowed_read_is_rate_limited():
    """Each windowed request re-reads and re-parses the whole (≤5 MiB) blob, so
    paging MULTIPLIES server work — the route that exists to cut transfer raises
    reads. Acceptable behind a JWT; on a client-facing prefix a loopable
    amplifier needs a limiter, like its `portal_tts`/`portal_stt` siblings."""
    import inspect

    from client_portal import router as portal_router

    src = inspect.getsource(portal_router.portal_agent_report_detail)
    assert "rate_limiter.enforce" in src
    assert "portal_report_detail:" in src
    # Access-first: the limiter must never key on an unvalidated path param.
    assert src.index("_require_roster") < src.index("rate_limiter.enforce")


def test_fastapi_can_build_the_route_with_the_new_params():
    """`client_portal/router.py` uses `from __future__ import annotations`, so
    every annotation is a STRING that FastAPI must resolve at include time. An
    `Optional[int]` whose name is missing from the module namespace fails there
    — at app startup, not import — which no other test in this file would catch,
    since importing the module works either way."""
    from fastapi import FastAPI

    from client_portal.router import router

    app = FastAPI()
    app.include_router(router)

    route = next(
        r for r in app.routes
        if getattr(r, "path", "").endswith("/agents/{agent_name}/reports/{report_id}")
    )
    params = {p.name: p for p in route.dependant.query_params}
    assert {"rows_offset", "rows_limit"} <= set(params)
    # Both optional: the unwindowed call must stay a bare GET.
    assert params["rows_limit"].field_info.default is None
    assert params["rows_offset"].field_info.default == 0
