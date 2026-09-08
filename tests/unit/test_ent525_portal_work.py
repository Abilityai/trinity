"""The Workspace Work read — trinity-enterprise#525 (the visual half of ent#457).

What is proven, and why each is worth a test:

1. **The door.** A portal-token principal gets a uniform 404 BEFORE any read
   (ent#78's auth-path invariant): the router never reaches the service.
2. **Roster narrowing, no oracle.** Off-roster names are dropped, never
   answered; a delegated child on an agent outside the roster is still a step
   but carries no name (the ent#467 disclosure class).
3. **The words.** `work_kind` / `work_outcome` / `can_stop` are tables — a
   row's trigger + channel stamp decides the kind, a running row past the
   staleness bound is `lost` (never a stuck "running"), and Stop is offered
   exactly where the terminate route would accept it.
4. **The #919 read is hardened like the MCP tool**: ids are grammar-checked
   before any path is built, size is checked from the listing BEFORE the
   download and the download aborts past the cap, an instance older than the
   execution is not attributed, and every failure is a VERDICT (`unknown`),
   never an exception into the Work read.
5. **The projection leaks nothing**: titles and errors are masked and bounded;
   only a portal stamp becomes a `chat_id`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent525.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent525-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
AGENT = "scout"
OTHER = "sage"          # on the roster
HIDDEN = "vault"        # NOT on the roster
SESSION = "sess-1"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc():
    from client_portal.work import service as mod
    return mod


@pytest.fixture
def ps():
    from client_portal.work import pipeline_state as mod
    mod.clear_cache()
    yield mod
    mod.clear_cache()


def _recent(seconds_ago: int = 30) -> str:
    """A `started_at` relative to NOW: the service measures staleness against
    the real clock, so a fixed stamp would silently turn stale as the days pass."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(**over):
    base = dict(
        id="exec-1", agent_name=AGENT, status="running", started_at=_recent(),
        completed_at=None, duration_ms=None, message="Reconcile the invoices",
        triggered_by="public", source_user_email=EMAIL, source_agent_name=None,
        source_channel="portal", source_channel_chat_id=SESSION, loop_id=None,
        error_summary=None,
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 3. The words
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, kind", [
    (dict(triggered_by="public", source_channel="portal"), "turn"),
    (dict(triggered_by="mcp", source_channel="portal"), "delegated"),
    (dict(triggered_by="agent", source_channel="portal"), "delegated"),
    (dict(triggered_by="loop", source_channel=None), "loop"),
    (dict(triggered_by="mcp", loop_id="loop-1"), "loop"),
    (dict(triggered_by="room", source_channel=None), "room"),
    (dict(triggered_by="schedule", source_channel=None), "schedule"),
    (dict(triggered_by="mcp", source_channel=None), "other"),
    (dict(triggered_by="public", source_channel="telegram"), "other"),
    (dict(triggered_by="something-new", source_channel="portal"), "other"),
])
def test_work_kind_is_a_table(svc, row, kind):
    assert svc.work_kind(_row(**row)) == kind


@pytest.mark.parametrize("row, stale, outcome", [
    (dict(status="running"), False, "running"),
    (dict(status="queued"), False, "queued"),
    (dict(status="pending_retry"), False, "running"),
    (dict(status="running"), True, "lost"),
    (dict(status="queued"), True, "lost"),
    (dict(status="success"), False, "success"),
    (dict(status="cancelled"), False, "cancelled"),
    (dict(status="skipped"), False, "skipped"),
    (dict(status="failed", error_summary="boom"), False, "failed"),
    (dict(status="failed", error_summary="Execution timed out after 3600s"), False, "timeout"),
    (dict(status="error", error_summary="ReadTimeout"), False, "timeout"),
    (dict(status="weird"), False, "failed"),
])
def test_work_outcome_is_honest(svc, row, stale, outcome):
    assert svc.work_outcome(_row(**row), stale=stale) == outcome


def test_stale_bound_is_the_agents_turn_bound_with_a_floor(svc):
    assert svc.stale_bound_seconds(3600) == 5400
    assert svc.stale_bound_seconds(60) == svc.STALE_FLOOR_SECONDS
    assert svc.is_stale(5401, 3600) is True
    assert svc.is_stale(5400, 3600) is False
    assert svc.is_stale(None, 3600) is False


@pytest.mark.parametrize("kind, status, mine, on_roster, stale, expected", [
    ("turn", "running", True, True, False, True),
    ("delegated", "queued", True, True, False, True),
    ("turn", "running", False, True, False, False),   # someone else's
    ("turn", "running", True, False, False, False),   # agent not on roster → route would 404
    ("turn", "running", True, True, True, False),     # lost: nothing to stop
    ("loop", "running", True, True, False, False),    # loops stop from the Loops tab
    ("schedule", "running", True, True, False, False),
    ("turn", "success", True, True, False, False),
])
def test_can_stop_mirrors_the_terminate_route(svc, kind, status, mine, on_roster, stale, expected):
    assert svc.can_stop(kind, status, mine=mine, on_roster=on_roster, stale=stale) is expected


def test_parse_agents_dedupes_and_trims(svc):
    assert svc.parse_agents(" a, b ,a,, c ") == ["a", "b", "c"]
    assert svc.parse_agents(None) == []


# ---------------------------------------------------------------------------
# 5. The projection leaks nothing
# ---------------------------------------------------------------------------

def test_title_is_one_masked_bounded_line(svc):
    long = "Deploy with token sk-ant-api03-" + "A" * 60 + "\n\n  and   then\tsome more " + "x" * 200
    title = svc.clean_title(long)
    assert "\n" not in title and "\t" not in title
    assert "sk-ant-api03-" not in title
    assert len(title) <= svc.TITLE_MAX
    assert title.endswith("…")
    assert svc.clean_title("   ") == "(no message)"
    assert svc.clean_title(None) == "(no message)"


def test_error_is_bounded_and_none_when_empty(svc):
    assert svc.clean_error(None) is None
    assert svc.clean_error("  ") is None
    assert len(svc.clean_error("e" * 500)) <= svc.ERROR_MAX


def test_mask_never_returns_an_off_roster_name(svc):
    roster = {AGENT, OTHER}
    assert svc.mask(AGENT, roster) == AGENT
    assert svc.mask(HIDDEN, roster) is None
    assert svc.mask(None, roster) is None


def test_projection_fields(svc):
    from datetime import datetime, timezone
    now = datetime(2026, 9, 6, 10, 0, 30, tzinfo=timezone.utc)
    roster = {AGENT, OTHER}
    item = svc._project(_row(started_at="2026-09-06T10:00:00Z", source_agent_name=HIDDEN),
                        email=EMAIL, roster=roster,
                        turn_timeout=3600, now=now)
    assert item.kind == "turn" and item.outcome == "running"
    assert item.elapsed_seconds == 30 and item.stale is False
    assert item.mine is True and item.can_stop is True
    assert item.chat_id == SESSION
    assert item.delegated_by is None            # masked
    assert item.error is None and item.steps is None

    # A Telegram stamp is not a chat the client can open.
    tg = svc._project(_row(source_channel="telegram", source_channel_chat_id="12345",
                           triggered_by="mcp"), email=EMAIL, roster=roster,
                      turn_timeout=3600, now=now)
    assert tg.chat_id is None and tg.kind == "other"

    # A failed row carries its masked one-liner; a running one carries none.
    failed = svc._project(_row(status="failed", error_summary="key sk-ant-api03-" + "B" * 40),
                          email=EMAIL, roster=roster, turn_timeout=3600, now=now)
    assert failed.outcome == "failed" and failed.error and "sk-ant-api03-" not in failed.error
    assert failed.elapsed_seconds is None and failed.can_stop is False

    # A child on an agent the caller cannot see: a step, unnamed, unstoppable.
    child = svc._project(_row(id="exec-2", agent_name=HIDDEN, triggered_by="mcp",
                              source_agent_name=AGENT), email=EMAIL, roster=roster,
                         turn_timeout=3600, now=now)
    assert child.agent_name is None and child.kind == "delegated"
    assert child.delegated_by == AGENT and child.can_stop is False

    # Past the bound: lost, no clock, no Stop.
    old = svc._project(_row(started_at="2026-09-06T00:00:00Z"), email=EMAIL, roster=roster,
                       turn_timeout=3600, now=now)
    assert old.stale is True and old.outcome == "lost"
    assert old.elapsed_seconds is None and old.can_stop is False


# ---------------------------------------------------------------------------
# 2. The read: roster narrowing, children, bounds
# ---------------------------------------------------------------------------

class _Ledger:
    def __init__(self, running=(), queued=(), recent=(), total=0, children=(), fail=False):
        self.running, self.queued, self.recent = list(running), list(queued), list(recent)
        self.total, self.children, self.fail = total, list(children), fail
        self.calls = []

    def get_fleet_executions(self, agent_names, *, status=None, hours=24, limit=50, **_):
        if self.fail:
            raise RuntimeError("db down")
        self.calls.append(("fleet", tuple(agent_names), status, hours, limit))
        if status == "running":
            return self.running
        if status == "queued":
            return self.queued
        return self.recent

    def get_fleet_execution_stats(self, agent_names, hours=24):
        self.calls.append(("stats", tuple(agent_names), hours))
        return {"total": self.total}

    def get_running_for_chat(self, chat_id):
        self.calls.append(("chat", chat_id))
        return self.children


@pytest.fixture
def wired(svc, monkeypatch):
    """The service with every seam stubbed: roster, ledger, session ownership,
    the pipeline read and the timeout resolver."""
    ledger = _Ledger()
    monkeypatch.setattr(svc, "core_db", ledger)
    monkeypatch.setattr(svc, "roster_agent_names", lambda email, include_owned: {AGENT, OTHER})
    monkeypatch.setattr(svc.portal_db, "get_portal_session",
                        lambda sid, agent, email: {"id": sid} if sid == SESSION and agent == AGENT else None)
    monkeypatch.setattr(svc, "_resolve_timeout", lambda agent: 3600)

    reads = []

    async def fake_steps(agent, started_at=None, roster=None):
        reads.append(agent)
        return svc.WorkSteps(state="reported", current="publish")

    monkeypatch.setattr(svc.pipeline_state, "read_pipeline_steps", fake_steps)
    return SimpleNamespace(ledger=ledger, reads=reads)


def test_off_roster_names_are_dropped_not_answered(svc, wired):
    out = _run(svc.get_work(EMAIL, [HIDDEN, AGENT, "nope"]))
    assert out.agents == [AGENT]
    # The ledger was asked ONLY about the rostered name.
    assert all(call[1] == (AGENT,) for call in wired.ledger.calls if call[0] in ("fleet", "stats"))


def test_nothing_on_the_roster_reads_nothing(svc, wired):
    out = _run(svc.get_work(EMAIL, [HIDDEN]))
    assert out.now == [] and out.earlier == [] and out.earlier_total == 0
    assert wired.ledger.calls == []


def test_children_are_found_by_chat_and_merged_by_id(svc, wired):
    wired.ledger.running = [_row(id="turn-1")]
    wired.ledger.children = [
        _row(id="turn-1"),                                                    # the turn itself, again
        _row(id="child-1", agent_name=HIDDEN, triggered_by="mcp", source_agent_name=AGENT),
        _row(id="child-2", agent_name=OTHER, triggered_by="mcp", source_agent_name=AGENT),
    ]
    out = _run(svc.get_work(EMAIL, [AGENT], chat_id=SESSION))
    ids = [it.id for it in out.now]
    assert sorted(ids) == ["child-1", "child-2", "turn-1"]
    hidden = next(it for it in out.now if it.id == "child-1")
    assert hidden.agent_name is None and hidden.delegated_by == AGENT
    visible = next(it for it in out.now if it.id == "child-2")
    assert visible.agent_name == OTHER


def test_a_chat_id_the_caller_does_not_hold_is_ignored(svc, wired):
    wired.ledger.children = [_row(id="child-1", agent_name=OTHER, triggered_by="mcp")]
    out = _run(svc.get_work(EMAIL, [AGENT], chat_id="someone-elses"))
    assert [c for c in wired.ledger.calls if c[0] == "chat"] == []
    assert out.now == []


def test_earlier_is_bounded_and_the_total_counts_finished_work(svc, wired):
    wired.ledger.running = [_row(id="run-now")]
    wired.ledger.recent = [_row(id="run-now")] + [
        _row(id=f"done-{i}", status="success", completed_at="2026-09-06T09:00:00Z")
        for i in range(svc.EARLIER_LIMIT + 10)
    ]
    wired.ledger.total = 41
    out = _run(svc.get_work(EMAIL, [AGENT]))
    assert len(out.earlier) == svc.EARLIER_LIMIT
    assert all(it.outcome == "success" for it in out.earlier)
    assert out.earlier_total == 40          # the in-flight row inside the window is not "earlier"
    assert out.earlier_limit == svc.EARLIER_LIMIT and out.window_days == 30
    assert [it.id for it in out.now] == ["run-now"]


def test_steps_are_read_only_for_one_running_row_per_agent(svc, wired):
    wired.ledger.running = [_row(id="a-1"), _row(id="b-1", agent_name=OTHER),
                            _row(id="b-2", agent_name=OTHER)]
    out = _run(svc.get_work(EMAIL, [AGENT, OTHER]))
    assert wired.reads == [AGENT]
    by_id = {it.id: it for it in out.now}
    assert by_id["a-1"].steps.state == "reported"
    assert by_id["b-1"].steps.state == "unknown" and by_id["b-2"].steps.state == "unknown"


def test_a_stale_row_gets_no_pipeline_read(svc, wired):
    wired.ledger.running = [_row(id="old", started_at="2026-01-01T00:00:00Z")]
    out = _run(svc.get_work(EMAIL, [AGENT]))
    assert wired.reads == []
    assert out.now[0].stale is True and out.now[0].steps.state == "unknown"


def test_a_ledger_failure_is_a_503_not_an_empty_list(svc, wired):
    wired.ledger.fail = True
    with pytest.raises(svc.WorkError) as e:
        _run(svc.get_work(EMAIL, [AGENT]))
    assert e.value.status_code == 503


# ---------------------------------------------------------------------------
# 1. The door (router)
# ---------------------------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    from client_portal.work import router as r
    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    return r


def test_a_portal_token_gets_a_uniform_404_before_any_read(route, svc, monkeypatch):
    from fastapi import HTTPException
    from client_portal.portal_auth import PortalPrincipal

    called = []

    async def boom(*a, **k):
        called.append(a)
        raise AssertionError("the service must not be reached")

    monkeypatch.setattr(svc, "get_work", boom)
    monkeypatch.setattr(route.service, "get_work", boom)
    with pytest.raises(HTTPException) as e:
        _run(route.get_work(agents=AGENT, chat_id=None,
                            principal=PortalPrincipal("client@example.com", False)))
    assert e.value.status_code == 404
    assert called == []


def test_the_agents_cap_is_a_named_422(route, svc, monkeypatch):
    from fastapi import HTTPException
    from client_portal.portal_auth import PortalPrincipal
    too_many = ",".join(f"a{i}" for i in range(svc.MAX_AGENTS + 1))
    with pytest.raises(HTTPException) as e:
        _run(route.get_work(agents=too_many, chat_id=None, principal=PortalPrincipal(EMAIL, True)))
    assert e.value.status_code == 422 and str(svc.MAX_AGENTS) in str(e.value.detail)


def test_a_platform_principal_reaches_the_service(route, svc, monkeypatch):
    from client_portal.portal_auth import PortalPrincipal
    seen = []

    async def fake(email, names, chat_id=None):
        seen.append((email, names, chat_id))
        return svc.PortalWork(agents=names, now=[], earlier=[], earlier_total=0,
                              window_days=30, earlier_limit=30)

    monkeypatch.setattr(route.service, "get_work", fake)
    out = _run(route.get_work(agents=f"{AGENT}, {OTHER}", chat_id=SESSION,
                              principal=PortalPrincipal(EMAIL, True)))
    assert seen == [(EMAIL, [AGENT, OTHER], SESSION)]
    assert out.agents == [AGENT, OTHER]


def test_router_declares_its_mcp_surface_and_is_mounted():
    router_src = (_BACKEND / "client_portal" / "work" / "router.py").read_text()
    assert router_src.splitlines()[0].startswith("# mcp: none")
    main_src = (_BACKEND / "main.py").read_text()
    assert "from client_portal.work.router import router as portal_work_router" in main_src
    assert "app.include_router(portal_work_router)" in main_src


# ---------------------------------------------------------------------------
# 4. The #919 read, hardened
# ---------------------------------------------------------------------------

def _tree(*pipes):
    return [{"name": pid, "type": "directory",
             "children": [{"name": f"{iid}.json", "type": "file", "size": size, "modified": mod}
                          for iid, size, mod in files]}
            for pid, files in pipes]


def test_instance_candidates_validate_before_any_path_is_built(ps):
    tree = _tree(
        ("good", [("inst-2", 100, "2026-09-06T10:05:00Z"), ("inst-1", 100, "2026-09-06T10:00:00Z")]),
        ("../etc", [("x", 10, "2026-09-06T10:09:00Z")]),          # traversal in the pipeline id
        ("ok2", [("..", 10, "2026-09-06T10:09:00Z"), ("a/b", 10, "2026-09-06T10:09:00Z")]),
        ("big", [("huge", ps.MAX_FILE_BYTES + 1, "2026-09-06T10:09:00Z")]),   # size from the LISTING
        ("notjson", [("state", 10, "2026-09-06T10:09:00Z")]),
    )
    # the not-json entry is named without the .json suffix by the helper; make one explicitly
    tree[-1]["children"] = [{"name": "state.yaml", "type": "file", "size": 10, "modified": "z"}]
    out = ps.instance_candidates(tree)
    assert [(p, i) for p, i, _, _ in out] == [("good", "inst-2"), ("good", "inst-1")]
    assert ps.instance_candidates("not a list") == []
    assert ps.instance_candidates([{"name": "x", "type": "file"}]) == []


def test_instance_candidates_are_capped(ps):
    files = [(f"i{n:02d}", 1, f"2026-09-06T10:{n:02d}:00Z") for n in range(20)]
    assert len(ps.instance_candidates(_tree(("p", files)))) == ps.MAX_INSTANCES


def test_fold_orders_stages_and_masks_holders(ps):
    definition = {"name": "Weekly digest", "stages": [
        {"id": "collect", "name": "Collect", "agent": AGENT},
        {"id": "draft", "name": "Draft", "agent": HIDDEN},
        {"id": "publish"},
    ]}
    state = {"instance_id": "i1", "current_stage": "draft", "health": "green",
             "updated_at": "2026-09-06T10:00:00Z", "escalations": [],
             "stages": {"publish": {"agent": OTHER}}}
    steps = ps.fold(definition, state, executing_agent=AGENT, roster={AGENT, OTHER})
    assert steps.state == "reported" and steps.pipeline == "Weekly digest"
    assert [(s.id, s.state) for s in steps.stages] == [("collect", "done"), ("draft", "current"), ("publish", "pending")]
    assert steps.stages[0].holder == AGENT
    assert steps.stages[1].holder is None            # off-roster → masked
    assert steps.stages[2].holder == OTHER           # from the state's per-stage entry
    assert steps.stages[2].name == "publish"         # id when no name
    assert steps.current == "draft" and steps.holder is None


def test_fold_without_a_definition_still_reports(ps):
    state = {"instance_id": "i1", "current_stage": "measure", "health": "yellow",
             "updated_at": "2026-09-06T10:00:00Z", "escalations": [], "pipeline_id": "digest"}
    steps = ps.fold(None, state, executing_agent=AGENT, roster={AGENT})
    assert steps.state == "reported" and steps.pipeline == "digest"
    assert [(s.id, s.state, s.holder) for s in steps.stages] == [("measure", "current", AGENT)]
    assert ps.fold({}, "not a dict", executing_agent=AGENT).state == "unknown"


class _Resp:
    def __init__(self, status, body=b"", json_body=None):
        self.status_code = status
        self._body = body
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    async def aiter_bytes(self):
        step = 1024
        for i in range(0, len(self._body), step):
            yield self._body[i:i + step]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Client:
    """A fake `agent_httpx_client`: `routes` maps a (method, path, path-param) to a response."""

    def __init__(self, listing, files, *, hang=False):
        self.listing, self.files, self.hang = listing, files, hang
        self.downloads = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if self.hang:
            await asyncio.sleep(10)
        return self.listing

    def stream(self, method, url, params=None):
        path = (params or {}).get("path")
        self.downloads.append(path)
        return self.files.get(path, _Resp(404))


def _patch_client(ps, monkeypatch, client):
    import services.agent_auth as auth
    monkeypatch.setattr(auth, "agent_httpx_client", lambda name, **kw: client)


def test_read_none_when_the_agent_publishes_nothing(ps, monkeypatch):
    _patch_client(ps, monkeypatch, _Client(_Resp(404), {}))
    assert _run(ps.read_pipeline_steps(AGENT)).state == "none"
    ps.clear_cache()
    _patch_client(ps, monkeypatch, _Client(_Resp(200, json_body={"tree": []}), {}))
    assert _run(ps.read_pipeline_steps(AGENT)).state == "none"


def test_read_unknown_when_the_agent_is_unreachable_or_slow(ps, monkeypatch):
    import httpx
    client = _Client(_Resp(200, json_body={"tree": []}), {})

    async def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    client.get = refuse
    _patch_client(ps, monkeypatch, client)
    assert _run(ps.read_pipeline_steps(AGENT)).state == "unknown"

    ps.clear_cache()
    monkeypatch.setattr(ps, "WALL_BUDGET_SECONDS", 0.05)
    _patch_client(ps, monkeypatch, _Client(_Resp(200, json_body={"tree": []}), {}, hang=True))
    assert _run(ps.read_pipeline_steps(AGENT)).state == "unknown"


def test_read_happy_path_and_the_cache(ps, monkeypatch):
    state = {"instance_id": "i1", "current_stage": "draft", "health": "green",
             "updated_at": "2026-09-06T10:05:00Z", "escalations": []}
    definition = "name: Digest\nstages:\n  - id: collect\n  - id: draft\n  - id: publish\n"
    listing = _Resp(200, json_body={"tree": _tree(("digest", [("i1", 200, "2026-09-06T10:05:00Z")]))})
    files = {
        f"{ps.STATE_DIR}/digest/i1.json": _Resp(200, json.dumps(state).encode()),
        f"{ps.PIPELINES_DIR}/digest.yaml": _Resp(200, definition.encode()),
    }
    client = _Client(listing, files)
    _patch_client(ps, monkeypatch, client)
    steps = _run(ps.read_pipeline_steps(AGENT, "2026-09-06T10:00:00Z", {AGENT}))
    assert steps.state == "reported" and steps.pipeline == "Digest"
    assert [s.state for s in steps.stages] == ["done", "current", "pending"]
    assert steps.holder == AGENT
    # The second read inside the TTL issues no request at all.
    n = len(client.downloads)
    again = _run(ps.read_pipeline_steps(AGENT, "2026-09-06T10:00:00Z", {AGENT}))
    assert again is steps and len(client.downloads) == n


def test_read_skips_an_instance_older_than_the_execution(ps, monkeypatch):
    state = {"instance_id": "i1", "current_stage": "draft", "health": "green",
             "updated_at": "2026-09-06T09:00:00Z", "escalations": []}
    listing = _Resp(200, json_body={"tree": _tree(("digest", [("i1", 200, "2026-09-06T09:00:00Z")]))})
    files = {f"{ps.STATE_DIR}/digest/i1.json": _Resp(200, json.dumps(state).encode())}
    _patch_client(ps, monkeypatch, _Client(listing, files))
    assert _run(ps.read_pipeline_steps(AGENT, "2026-09-06T10:00:00Z", {AGENT})).state == "none"


def test_read_aborts_a_download_past_the_cap_and_survives_bad_documents(ps, monkeypatch):
    # The listing said 200 bytes; the body is larger — the STREAM cap catches it.
    listing = _Resp(200, json_body={"tree": _tree(("p", [("i1", 200, "2026-09-06T10:05:00Z"),
                                                          ("i0", 200, "2026-09-06T10:04:00Z")]))})
    good = {"instance_id": "i0", "current_stage": "x", "health": "green",
            "updated_at": "2026-09-06T10:04:00Z", "escalations": []}
    files = {
        f"{ps.STATE_DIR}/p/i1.json": _Resp(200, b"{" + b"x" * (ps.MAX_FILE_BYTES + 5)),
        f"{ps.STATE_DIR}/p/i0.json": _Resp(200, json.dumps(good).encode()),
        f"{ps.PIPELINES_DIR}/p.yaml": _Resp(200, b"stages: [\n"),   # malformed YAML
    }
    client = _Client(listing, files)
    _patch_client(ps, monkeypatch, client)
    steps = _run(ps.read_pipeline_steps(AGENT, "2026-09-06T10:00:00Z", {AGENT}))
    assert steps.state == "reported" and steps.current == "x"     # fell through to i0, no definition
    assert steps.pipeline is None
    # Never a path from a value the grammar rejected.
    assert all("/.." not in (p or "") for p in client.downloads)


# ---------------------------------------------------------------------------
# The facade trap (ent#277's class, named in the learnings ledger)
# ---------------------------------------------------------------------------

def test_the_facade_exposes_every_ledger_read_the_service_makes():
    """`database.db` delegates by NAME, not by `__getattr__`: a mixin method the
    facade does not re-export raises AttributeError on first use — and the
    service's broad `except` would turn that into a 503 that every stubbed test
    stays green over. Derived from the service source, so a new `core_db.x(`
    call fails here the day it is written."""
    import re
    from database import db
    service_src = (_BACKEND / "client_portal" / "work" / "service.py").read_text()
    calls = set(re.findall(r"core_db\.([a-zA-Z_]+)\(", service_src))
    assert calls, "the service reads the ledger through core_db"
    missing = sorted(name for name in calls if not callable(getattr(db, name, None)))
    assert missing == [], f"facade does not re-export: {missing}"


def test_the_fleet_dashboard_payload_gains_no_channel_destination_ids():
    """The three columns ride the shared SELECT for the Work projection only.
    `FleetExecutionSummary` must not carry them: a Telegram chat id or a portal
    session id is not the operator dashboard's business, and the model drops
    extra keys, so the dashboard's wire shape is unchanged."""
    from models import FleetExecutionSummary
    fields = set(FleetExecutionSummary.model_fields)
    assert not fields & {"source_channel", "source_channel_chat_id", "loop_id"}
    row = {"id": "x", "schedule_id": "", "agent_name": "a", "status": "success",
           "started_at": "2026-09-06T10:00:00Z", "message": "m", "triggered_by": "public",
           "source_channel_chat_id": "sess-1", "loop_id": "l1", "source_channel": "portal"}
    assert not hasattr(FleetExecutionSummary(**row), "source_channel_chat_id")
