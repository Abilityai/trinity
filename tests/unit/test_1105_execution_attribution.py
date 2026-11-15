"""The execution-attribution seam (abilityai/trinity-enterprise#1105).

An edition may attribute an execution to a project. The platform's side is a
seam (``services/execution_attribution.py``) that is inert in OSS:

* every ``create_task_execution(`` call site offers the row it made to
  ``execution_attribution.record_row`` — or is listed below with the reason it
  is untagged by design. The census walks the platform backend, so a new
  creation site that forgets the call fails the build; a listed site that
  disappeared fails it too (no stale reasons);
* a request that names ``project_id`` (``/chat``, ``/task``, ``/fan-out``) is
  checked BEFORE anything is claimed or created — OSS answers 422
  ``project_attribution_unavailable``, never a silent drop;
* ``record_row``/``annotate`` fail open: an attributor that raises never fails
  the dispatch or the read;
* ``GET /api/agents/{name}/executions/{id}`` carries ``project_id`` from
  ``annotate``.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException  # noqa: E402

from services import execution_attribution as ea  # noqa: E402

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------

RECORD_CALL = "record_row"

# (path relative to src/backend, enclosing function) -> why it is untagged.
# Sites in the private edition tree (src/backend/enterprise) are out of this
# census: the member-agent intro turn is untagged by design and the
# projects steward wake is tagged by the projects module itself.
ALLOWLIST = {
    ("services/public_chat_service.py", "run_public_chat"):
        "public link — an external visitor's turn, never attributed to a project",
    ("routers/a2a.py", "_mint_a2a_execution"):
        "external A2A peer — not a platform principal, never attributed",
    ("services/brain_orb_postprocess.py", "dispatch_postprocess"):
        "post-processing turn over an existing turn, not work in its own right",
}

# Files where a `create_task_execution` definition/facade lives, not a creation.
_FACADES = {"database.py", "db/schedules/executions.py"}


def _enclosing_functions(tree: ast.AST):
    """Yield (function_node, [create_task_execution calls directly in it])."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls, records = [], False
        stack = list(node.body)
        while stack:
            child = stack.pop()
            # a nested function is its own scope — it is visited on its own
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                if child.func.attr == "create_task_execution":
                    calls.append(child)
                if (child.func.attr == RECORD_CALL
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "execution_attribution"):
                    records = True
            stack.extend(ast.iter_child_nodes(child))
        if calls:
            yield node, calls, records


def census(sources):
    """``sources``: {relpath: source}. Returns (unrecorded, seen_sites)."""
    unrecorded, seen = [], set()
    for rel, src in sources.items():
        tree = ast.parse(src)
        for fn, calls, records in _enclosing_functions(tree):
            site = (rel, fn.name)
            seen.add(site)
            if not records and site not in ALLOWLIST:
                unrecorded.append(f"{rel}:{calls[0].lineno} in {fn.name}()")
    return unrecorded, seen


def _platform_sources():
    out = {}
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith("enterprise/") or "/tests/" in f"/{rel}" or rel in _FACADES:
            continue
        src = path.read_text(encoding="utf-8")
        if "create_task_execution(" in src:
            out[rel] = src
    return out


def test_every_creation_site_records_or_is_listed_with_a_reason():
    unrecorded, _ = census(_platform_sources())
    assert not unrecorded, (
        "create_task_execution( call sites that neither call "
        "execution_attribution.record_row nor appear in ALLOWLIST with a reason:\n  "
        + "\n  ".join(unrecorded))


def test_no_stale_allowlist_entries():
    _, seen = census(_platform_sources())
    stale = sorted(set(ALLOWLIST) - seen)
    assert not stale, f"ALLOWLIST names sites that no longer create executions: {stale}"
    for reason in ALLOWLIST.values():
        assert reason.strip(), "every allowlisted site states why it is untagged"


def test_census_sees_the_known_recording_sites():
    _, seen = census(_platform_sources())
    for site in [
        ("services/chat_execution_service.py", "prepare_chat_execution"),
        ("services/chat_execution_service.py", "create_task_execution_and_activities"),
        ("services/task_execution_service.py", "execute_task"),
        ("services/loop_service.py", "_dispatch_run"),
        ("services/fan_out_service.py", "run_subtask"),
    ]:
        assert site in seen, site
    assert any(s[0] == "services/skill_gate_service.py" for s in seen)
    assert sum(1 for s in seen if s[0] == "client_portal/service.py") == 2


@pytest.mark.parametrize("rel", [
    "services/chat_execution_service.py",
    "services/task_execution_service.py",
    "client_portal/service.py",
    "services/loop_service.py",
    "services/fan_out_service.py",
    "services/skill_gate_service.py",
])
def test_deleting_a_record_call_turns_the_census_red(rel):
    src = (_BACKEND / rel).read_text(encoding="utf-8")
    assert "execution_attribution.record_row(" in src
    # Drop the first record call (rename it, so the AST still parses).
    mutated = src.replace("execution_attribution.record_row(",
                          "execution_attribution.not_recorded(", 1)
    unrecorded, _ = census({rel: mutated})
    assert unrecorded, f"removing the record call in {rel} went unnoticed"


def test_internal_execute_task_records_the_scheduler_row():
    """The scheduler creates its row in its own process; the backend's
    execute-task route is where that row meets the seam."""
    tree = ast.parse((_BACKEND / "routers/internal.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "execute_task_internal")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == RECORD_CALL]
    assert calls, "execute_task_internal must offer the scheduler's row to execution_attribution"


# ---------------------------------------------------------------------------
# Seam behaviour
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _oss_seam():
    ea.clear_attributor()
    yield
    ea.clear_attributor()


class _Raising:
    def check(self, requested_project_id, *, principal, agent_name):
        return None

    def resolve(self, created):
        raise RuntimeError("boom")

    def record(self, created):
        raise RuntimeError("db down")

    def annotate(self, execution, principal):
        raise RuntimeError("boom")


ROW = SimpleNamespace(id="exec-1", agent_name="alpha", triggered_by="agent")


def test_oss_check_refuses_a_named_project_with_422():
    with pytest.raises(ea.AttributionRefused) as e:
        ea.check("proj-1", principal=None, agent_name="alpha")
    assert e.value.status_code == 422
    assert e.value.detail()["code"] == "project_attribution_unavailable"


def test_oss_check_without_a_project_is_a_no_op():
    assert ea.check(None, principal=None, agent_name="alpha") is None


def test_oss_record_resolve_annotate_do_nothing():
    assert ea.record_row(ROW, requested_project_id="p") is None
    assert ea.resolve(agent_name="alpha") is None
    assert ea.annotate(ROW, None) == {}


def test_a_raising_record_never_raises_and_reports_failed_for_an_explicit_request():
    ea.register_attributor(_Raising())
    assert ea.record_row(ROW, requested_project_id="p1") == ea.FAILED
    assert ea.record_row(ROW, inherited_project_id="p1") == ea.FAILED
    assert ea.record_row(ROW) is None


def test_raising_annotate_and_resolve_fail_open():
    ea.register_attributor(_Raising())
    assert ea.annotate(ROW, None) == {}
    assert ea.resolve(agent_name="alpha") is None


def test_an_attributor_refusal_carries_its_status_and_code():
    class _Refuses(_Raising):
        def check(self, requested_project_id, *, principal, agent_name):
            return (403, "turn_not_live", "no live turn")

    ea.register_attributor(_Refuses())
    with pytest.raises(ea.AttributionRefused) as e:
        ea.check("p1", principal=None, agent_name="alpha")
    assert (e.value.status_code, e.value.code) == (403, "turn_not_live")


def test_record_row_hands_the_attributor_the_row_and_the_principal():
    seen = []

    class _Records(_Raising):
        def record(self, created):
            seen.append(created)
            return ea.RECORDED

    ea.register_attributor(_Records())
    principal = SimpleNamespace(agent_name="caller", _acting_execution_id="turn-9")
    assert ea.record_row(ROW, principal=principal, requested_project_id="p1") == ea.RECORDED
    (c,) = seen
    assert (c.execution_id, c.agent_name, c.caller_agent, c.acting_execution_id,
            c.requested_project_id) == ("exec-1", "alpha", "caller", "turn-9", "p1")


# ---------------------------------------------------------------------------
# Routes: a named project is refused before anything is claimed (OSS)
# ---------------------------------------------------------------------------

_HUMAN = SimpleNamespace(id=1, username="admin", email="a@example.com", role="admin",
                         agent_name=None, mcp_scope=None)


def _tripwire(*_a, **_k):
    raise AssertionError("reached past the project check")


def test_check_requested_project_maps_the_refusal_to_http_422():
    from dependencies import check_requested_project
    with pytest.raises(HTTPException) as e:
        check_requested_project(SimpleNamespace(project_id="p1"), _HUMAN, "alpha")
    assert e.value.status_code == 422
    assert e.value.detail == {"code": "project_attribution_unavailable",
                              "message": e.value.detail["message"]}
    # no project_id → nothing to check
    assert check_requested_project(SimpleNamespace(project_id=None), _HUMAN, "alpha") is None


def test_task_route_refuses_a_named_project_before_admission(monkeypatch):
    import routers.chat as chat_router
    from models import ParallelTaskRequest
    monkeypatch.setattr(chat_router, "get_agent_container", _tripwire)
    with pytest.raises(HTTPException) as e:
        asyncio.run(chat_router.execute_parallel_task(
            request=ParallelTaskRequest(message="hi", project_id="p1"),
            name="alpha", current_user=_HUMAN, x_source_agent=None, x_via_mcp=None,
            idempotency_key=None, x_event_trigger=None, x_internal_secret=None,
            x_trinity_execution_id=None))
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "project_attribution_unavailable"


def test_chat_route_refuses_a_named_project_before_admission(monkeypatch):
    import routers.chat as chat_router
    from models import ChatMessageRequest
    monkeypatch.setattr(chat_router, "get_agent_container", _tripwire)
    with pytest.raises(HTTPException) as e:
        asyncio.run(chat_router.chat_with_agent(
            request=ChatMessageRequest(message="hi", project_id="p1"),
            name="alpha", current_user=_HUMAN, x_source_agent=None, x_via_mcp=None,
            idempotency_key=None, x_trinity_execution_id=None))
    assert e.value.status_code == 422


def test_fan_out_route_refuses_a_named_project_before_the_depth_guard(monkeypatch):
    import routers.fan_out as fan_out_router
    from models import FanOutRequest
    monkeypatch.setattr(fan_out_router.dispatch_admission_service,
                        "enforce_inter_agent_depth", _tripwire)
    monkeypatch.setattr(fan_out_router.idempotency_service, "begin", _tripwire)
    with pytest.raises(HTTPException) as e:
        asyncio.run(fan_out_router.fan_out(
            request=FanOutRequest(tasks=[{"id": "t1", "message": "go"}], project_id="p1"),
            name="alpha", current_user=_HUMAN, x_source_agent=None, x_via_mcp=None,
            idempotency_key=None))
    assert e.value.status_code == 422


@pytest.mark.parametrize("bad", ["", "x" * 65])
def test_request_models_bound_project_id(bad):
    from pydantic import ValidationError
    from models import ChatMessageRequest, FanOutRequest, ParallelTaskRequest
    for build in (
        lambda: ChatMessageRequest(message="m", project_id=bad),
        lambda: ParallelTaskRequest(message="m", project_id=bad),
        lambda: FanOutRequest(tasks=[{"id": "t", "message": "m"}], project_id=bad),
    ):
        with pytest.raises(ValidationError):
            build()


# ---------------------------------------------------------------------------
# Read: GET /api/agents/{name}/executions/{id} carries project_id
# ---------------------------------------------------------------------------

def _execution():
    from database import ScheduleExecution
    from datetime import datetime
    return ScheduleExecution(
        id="exec-1", schedule_id="__manual__", agent_name="alpha", status="success",
        started_at=datetime(2026, 11, 12), completed_at=None, duration_ms=None,
        message="m", response="r", error=None, triggered_by="agent")


def _stub_read(monkeypatch):
    import routers.schedules as sched
    monkeypatch.setattr(sched.db, "get_execution", lambda _id: _execution())
    monkeypatch.setattr(sched, "viewer_row_scope", lambda *_a: None)
    monkeypatch.setattr(sched.skill_gate_map_service, "self_approved_flags",
                        lambda *_a, **_k: {})
    return sched


def test_execution_response_accepts_project_id():
    from models import ExecutionResponse
    data = _execution().model_dump()
    assert ExecutionResponse(**data).project_id is None
    assert ExecutionResponse(**data, project_id="p1").project_id == "p1"


def test_get_execution_fills_project_id_from_annotate(monkeypatch):
    sched = _stub_read(monkeypatch)

    class _Annotates(_Raising):
        def annotate(self, execution, principal):
            assert execution.id == "exec-1"
            return {"project_id": "p1"}

    ea.register_attributor(_Annotates())
    out = asyncio.run(sched.get_execution(name="alpha", execution_id="exec-1",
                                          current_user=_HUMAN))
    assert out.project_id == "p1"


def test_get_execution_without_attribution_has_no_project_id(monkeypatch):
    sched = _stub_read(monkeypatch)
    out = asyncio.run(sched.get_execution(name="alpha", execution_id="exec-1",
                                          current_user=_HUMAN))
    assert out.project_id is None
    ea.register_attributor(_Raising())  # annotate raises → still 200, absent
    out = asyncio.run(sched.get_execution(name="alpha", execution_id="exec-1",
                                          current_user=_HUMAN))
    assert out.project_id is None


# ---------------------------------------------------------------------------
# Honest status: a named project that was checked and then not recorded is
# answered `project_attribution: "failed"` on /chat, /task and /fan-out
# (plan §2.2 rule 3, review I1 (c))
# ---------------------------------------------------------------------------

class _PassesCheck(_Raising):
    """check passes; record raises (the default) unless told to record."""

    def __init__(self, records=False, resolves=None):
        self.records, self.resolves, self.seen = records, resolves, []

    def resolve(self, created):
        if self.resolves is None:
            raise RuntimeError("db down")
        return self.resolves

    def record(self, created):
        self.seen.append(created)
        if self.records:
            return ea.RECORDED
        raise RuntimeError("db down")


_AGENT_KEY = SimpleNamespace(id=1, username="owner", email="o@example.com", role="admin",
                             agent_name="caller", mcp_scope="agent",
                             _acting_execution_id="turn-1")


def test_an_explicit_request_that_was_not_recorded_is_failed():
    class _Declines(_PassesCheck):
        def record(self, created):
            return None          # e.g. the record-time re-assert no longer holds

    ea.register_attributor(_Declines())
    ea.check("p1", principal=_AGENT_KEY, agent_name="alpha")
    assert ea.record_row(ROW, requested_project_id="p1") == ea.FAILED
    assert ea.explicit_status() == ea.FAILED


def test_check_starts_the_requests_status_afresh_and_success_is_recorded():
    ea.register_attributor(_PassesCheck(records=True))
    ea.check("p1", principal=_AGENT_KEY, agent_name="alpha")
    assert ea.record_row(ROW, requested_project_id="p1") == ea.RECORDED
    assert ea.explicit_status() == ea.RECORDED
    ea.check(None, principal=_AGENT_KEY, agent_name="alpha")
    assert ea.explicit_status() is None
    # an inherited project never sets the request's explicit status
    ea.record_row(ROW, inherited_project_id="p1")
    assert ea.explicit_status() is None


def test_report_names_only_a_failure_on_a_dict():
    assert ea.report({"a": 1}, ea.FAILED) == {"a": 1, "project_attribution": "failed"}
    assert ea.report({"a": 1}, ea.RECORDED) == {"a": 1}
    assert ea.report({"a": 1}, None) == {"a": 1}
    sentinel = object()
    assert ea.report(sentinel, ea.FAILED) is sentinel


def _recording_row(principal, request):
    """What the service does with the row it just created."""
    ea.record_row(ROW, principal=principal, requested_project_id=request.project_id)


@pytest.mark.parametrize("records", [False, True])
def test_task_route_answers_failed_when_the_named_project_was_not_recorded(monkeypatch, records):
    import routers.chat as chat_router
    from models import ParallelTaskRequest
    ea.register_attributor(_PassesCheck(records=records))
    monkeypatch.setattr(chat_router, "get_agent_container",
                        lambda name: SimpleNamespace(status="running"))

    async def _dispatch(*, request, current_user, **_):
        _recording_row(current_user, request)
        return {"status": "accepted", "task_execution_id": "exec-1"}

    monkeypatch.setattr(chat_router.chat_execution_service, "dispatch_parallel_task", _dispatch)
    out = asyncio.run(chat_router.execute_parallel_task(
        request=ParallelTaskRequest(message="hi", project_id="p1"),
        name="alpha", current_user=_AGENT_KEY, x_source_agent=None, x_via_mcp=None,
        idempotency_key=None, x_event_trigger=None, x_internal_secret=None,
        x_trinity_execution_id=None))
    if records:
        assert "project_attribution" not in out
    else:
        assert out["project_attribution"] == "failed"


@pytest.mark.parametrize("records", [False, True])
def test_chat_route_answers_failed_when_the_named_project_was_not_recorded(monkeypatch, records):
    import routers.chat as chat_router
    from models import ChatMessageRequest
    from services.chat_signals import ChatExecutionContext
    ea.register_attributor(_PassesCheck(records=records))
    monkeypatch.setattr(chat_router, "get_agent_container",
                        lambda name: SimpleNamespace(status="running"))

    async def _admit(**_):
        return SimpleNamespace(idem=None, execution_id="exec-1", capacity_result=None,
                               capacity=None, queue_result="admitted", chat_timeout=60,
                               chain_depth=None, gate=None)

    async def _prepare(*, request, current_user, **_):
        _recording_row(current_user, request)
        return ChatExecutionContext(
            execution=SimpleNamespace(id="exec-1"), task_execution_id="exec-1",
            triggered_by="agent", subscription_id=None, collaboration_activity_id=None,
            chat_activity_id=None, session=None, is_queued=False)

    async def _run(**_):
        return {"response": "ok", "execution": {"task_execution_id": "exec-1"}}

    monkeypatch.setattr(chat_router.dispatch_admission_service, "admit_chat_request", _admit)
    monkeypatch.setattr(chat_router.chat_execution_service, "prepare_chat_execution", _prepare)
    monkeypatch.setattr(chat_router.chat_execution_service, "run_chat_turn", _run)
    out = asyncio.run(chat_router.chat_with_agent(
        request=ChatMessageRequest(message="hi", project_id="p1"),
        name="alpha", current_user=_AGENT_KEY, x_source_agent=None, x_via_mcp=None,
        idempotency_key=None, x_trinity_execution_id=None))
    if records:
        assert "project_attribution" not in out
    else:
        assert out["project_attribution"] == "failed"


def _fan_out_world(monkeypatch, result_attribution=None):
    import routers.fan_out as fan_out_router
    from services.fan_out_service import FanOutResult
    seen = {}

    async def _depth(**_):
        return None

    class _Service:
        async def execute(self, **kw):
            seen.update(kw)
            return FanOutResult(fan_out_id="fo_1", status="accepted", total=1,
                                completed=0, failed=0, results=[],
                                project_attribution=result_attribution)

    monkeypatch.setattr(fan_out_router.dispatch_admission_service,
                        "enforce_inter_agent_depth", _depth)
    monkeypatch.setattr(fan_out_router.idempotency_service, "begin",
                        lambda *a, **k: SimpleNamespace(enabled=False, replay=False, in_flight=False,
                                                        snapshot=None))
    monkeypatch.setattr(fan_out_router.idempotency_service, "complete", lambda *a, **k: None)
    monkeypatch.setattr(fan_out_router.turn_tier_service, "turn_origin_user_id", lambda *a: 1)
    monkeypatch.setattr(fan_out_router.turn_tier_service, "turn_origin_email", lambda *a: "o@example.com")
    monkeypatch.setattr(fan_out_router, "get_fan_out_service", lambda: _Service())
    return fan_out_router, seen


def _call_fan_out(router_mod, project_id="p1"):
    from models import FanOutRequest
    return asyncio.run(router_mod.fan_out(
        request=FanOutRequest(tasks=[{"id": "t1", "message": "go"}], project_id=project_id,
                              async_mode=True),
        name="alpha", current_user=_AGENT_KEY, x_source_agent=None, x_via_mcp=None,
        idempotency_key=None))


def test_fan_out_answers_failed_when_the_named_project_did_not_resolve(monkeypatch):
    router_mod, seen = _fan_out_world(monkeypatch)
    ea.register_attributor(_PassesCheck(resolves=None))   # resolve raises → None
    out = _call_fan_out(router_mod)
    assert seen["project_id"] is None
    assert out.project_attribution == "failed"


def test_fan_out_carries_the_resolved_project_and_says_nothing_on_success(monkeypatch):
    router_mod, seen = _fan_out_world(monkeypatch)
    ea.register_attributor(_PassesCheck(resolves="p1"))
    out = _call_fan_out(router_mod)
    assert seen["project_id"] == "p1"
    assert out.project_attribution is None


def test_fan_out_answers_failed_when_a_subtask_was_not_recorded(monkeypatch):
    router_mod, _ = _fan_out_world(monkeypatch, result_attribution=ea.FAILED)
    ea.register_attributor(_PassesCheck(resolves="p1"))
    assert _call_fan_out(router_mod).project_attribution == "failed"


def test_a_fan_out_subtask_whose_record_failed_marks_the_batch(monkeypatch):
    """The service half: a child whose inherited project failed to record is
    named on the batch, so a sync caller hears it."""
    import services.fan_out_service as fos
    ea.register_attributor(_PassesCheck())       # record raises
    monkeypatch.setattr(fos.db, "get_agent_subscription_id", lambda name: None)
    monkeypatch.setattr(fos.db, "create_task_execution",
                        lambda **kw: SimpleNamespace(id="exec-c1", agent_name="alpha",
                                                     triggered_by="fan_out"))

    class _Tasks:
        async def execute_task(self, **kw):
            return SimpleNamespace(error_code=None, status="success")

    async def _join(_eid):
        return None

    monkeypatch.setattr(fos, "get_task_execution_service", lambda: _Tasks())
    monkeypatch.setattr(fos, "join_fan_out_on_terminal", _join)
    failed = []
    asyncio.run(fos.FanOutService()._dispatch_all(
        fan_out_id="fo_1", agent_name="alpha",
        tasks=[fos.FanOutTaskInput(id="t1", message="go")], error_codes={},
        max_concurrency=1, model=None, system_prompt=None, allowed_tools=None,
        source_user_id=None, source_user_email=None, source_agent_name="caller",
        source_mcp_key_id=None, source_mcp_key_name=None, project_id="p1",
        attribution_failed=failed))
    assert failed == ["t1"]


# ---------------------------------------------------------------------------
# Skill-gate replay: the project resolved at hold time is frozen into the gate
# record's `dispatch` and recorded at replay (plan §2.2 rule 2, review I1 (b))
# ---------------------------------------------------------------------------

class _Stop(Exception):
    """Raised by a stub to end the code under test at the point of interest."""


def test_frozen_project_resolves_now_and_marks_a_named_project():
    from services import skill_gate_service as sgs
    assert sgs.frozen_project(agent_name="alpha", principal=_AGENT_KEY) == {}   # OSS
    ea.register_attributor(_PassesCheck(resolves="p1"))
    assert sgs.frozen_project(agent_name="alpha", principal=_AGENT_KEY) == {"project_id": "p1"}
    assert sgs.frozen_project(agent_name="alpha", principal=_AGENT_KEY,
                              requested_project_id="p1") == {"project_id": "p1",
                                                             "project_requested": True}
    assert sgs.frozen_project("p2") == {"project_id": "p2"}


def _capture_enforce(monkeypatch, sgs):
    captured = {}

    async def _enforce(agent, **kw):
        captured.update(kw)
        raise _Stop

    monkeypatch.setattr(sgs, "enforce", _enforce)
    return captured


def test_a_held_chat_freezes_the_resolved_project(monkeypatch):
    from models import ChatMessageRequest
    from services import dispatch_admission_service as das
    from services import skill_gate_service as sgs
    ea.register_attributor(_PassesCheck(resolves="p1"))

    async def _depth(**_):
        return None

    monkeypatch.setattr(das, "enforce_inter_agent_depth", _depth)
    monkeypatch.setattr(das.turn_tier_service, "turn_origin_user_id", lambda *a: 1)
    monkeypatch.setattr(das.turn_tier_service, "turn_origin_email", lambda *a: "o@example.com")
    captured = _capture_enforce(monkeypatch, sgs)
    with pytest.raises(_Stop):
        asyncio.run(das.admit_chat_request(
            name="alpha", request=ChatMessageRequest(message="/pay", project_id="p1"),
            current_user=_AGENT_KEY, x_source_agent="caller", x_via_mcp="1",
            idempotency_key=None, x_trinity_execution_id="turn-1"))
    assert captured["dispatch"]["project_id"] == "p1"
    assert captured["dispatch"]["project_requested"] is True


def test_a_held_task_freezes_the_resolved_project(monkeypatch):
    from models import ParallelTaskRequest
    from services import chat_execution_service as ces
    from services import skill_gate_service as sgs
    ea.register_attributor(_PassesCheck(resolves="p-lineage"))

    async def _depth(**_):
        return None

    monkeypatch.setattr(ces.dispatch_admission_service, "enforce_inter_agent_depth", _depth)
    monkeypatch.setattr(ces.turn_tier_service, "turn_origin_user_id", lambda *a: 1)
    monkeypatch.setattr(ces.turn_tier_service, "turn_origin_email", lambda *a: "o@example.com")
    captured = _capture_enforce(monkeypatch, sgs)
    with pytest.raises(_Stop):
        asyncio.run(ces.dispatch_parallel_task(
            request=ParallelTaskRequest(message="/pay"), name="alpha",
            current_user=_AGENT_KEY, container=SimpleNamespace(status="running"),
            x_source_agent="caller", x_via_mcp="1", idempotency_key=None,
            x_event_trigger=None, x_internal_secret=None, x_trinity_execution_id="turn-1"))
    assert captured["dispatch"]["project_id"] == "p-lineage"
    assert "project_requested" not in captured["dispatch"]


def test_a_fan_out_subtask_hands_its_project_to_the_gate(monkeypatch):
    import services.fan_out_service as fos
    ea.register_attributor(_PassesCheck(records=True))
    monkeypatch.setattr(fos.db, "get_agent_subscription_id", lambda name: None)
    monkeypatch.setattr(fos.db, "create_task_execution",
                        lambda **kw: SimpleNamespace(id="exec-c1", agent_name="alpha",
                                                     triggered_by="fan_out"))
    seen = {}

    class _Tasks:
        async def execute_task(self, **kw):
            seen.update(kw)
            return SimpleNamespace(error_code=None, status="success")

    async def _join(_eid):
        return None

    monkeypatch.setattr(fos, "get_task_execution_service", lambda: _Tasks())
    monkeypatch.setattr(fos, "join_fan_out_on_terminal", _join)
    asyncio.run(fos.FanOutService()._dispatch_all(
        fan_out_id="fo_1", agent_name="alpha",
        tasks=[fos.FanOutTaskInput(id="t1", message="go")], error_codes={},
        max_concurrency=1, model=None, system_prompt=None, allowed_tools=None,
        source_user_id=None, source_user_email=None, source_agent_name="caller",
        source_mcp_key_id=None, source_mcp_key_name=None, project_id="p1"))
    assert seen["gate_replay"]["project_id"] == "p1"


@pytest.mark.parametrize("frozen, expect", [
    ({"project_id": "p1"}, (None, "p1")),
    ({"project_id": "p1", "project_requested": True}, ("p1", None)),
    ({}, (None, None)),
])
def test_the_approved_replay_records_the_frozen_project(monkeypatch, frozen, expect):
    from services import capacity_manager
    from services import skill_gate_service as sgs
    attributor = _PassesCheck(records=True)
    ea.register_attributor(attributor)
    monkeypatch.setattr(sgs.db, "create_task_execution",
                        lambda **kw: SimpleNamespace(id=kw["execution_id"],
                                                     agent_name=kw["agent_name"],
                                                     triggered_by=kw["triggered_by"]))
    monkeypatch.setattr(sgs.db, "get_execution_timeout", lambda agent: 60)

    def _stop():
        raise _Stop

    monkeypatch.setattr(capacity_manager, "get_capacity_manager", _stop)
    record = {"request_id": "gate-1", "agent_name": "alpha", "request_text": "/pay",
              "triggered_by": "agent",
              "dispatch": {"triggered_by": "agent", "source_agent_name": "caller", **frozen}}
    with pytest.raises(_Stop):
        asyncio.run(sgs._dispatch_approved(record, "exec-approved-1"))
    (created,) = attributor.seen
    assert created.execution_id == "exec-approved-1"
    assert (created.requested_project_id, created.inherited_project_id) == expect
