"""#2703 — assigning a library skill DELIVERS it, and every playbook list is told.

Before this, every assign path (`POST /agents/{n}/skills/{s}`, the bulk `PUT`,
the Library control, MCP) wrote an `agent_skills` row and stopped; the package
reached the agent only on a manual Sync or the next start, while unassigning
already removed the package (ent#236). These tests pin the write-side twin:

  * `skill_service.deliver_assigned` — running → the START-PATH injection (every
    assigned name, `force=False`; a subset call would rewrite CLAUDE.md's
    Platform Skills section to that subset), stopped → `pending_start`, Docker
    unreadable → `not_delivered/docker_unavailable` (never a guess), busy →
    one retry then `not_delivered/injection_in_progress`, over budget →
    `in_progress` with the work kept alive
  * the under-lock re-read in `_inject_skills_locked` — a name unassigned
    between commit and lock is `unassigned_meanwhile`, never landed
  * the routes — row first, 200 always, `delivery` in the body, the thin
    `agent_skills_changed` trigger `{type, agent_name}` on every listing change,
    404/422 still fire BEFORE any write

Every test EXECUTES the path with the agent/Docker boundary stubbed; none reads
source text. No real Redis, no real Docker, no real agent.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import services.skill_service as skill_service_module
from services.skill_service import SkillService, SkillInjectionBusy

AGENT = "acme-bot"


class _FakeManager:
    def __init__(self):
        self.messages: list[str] = []

    async def broadcast(self, message):  # /ws (SCOPE_ALL) — a JSON string
        self.messages.append(message)


@pytest.fixture
def svc():
    return SkillService()


@pytest.fixture
def ws(monkeypatch):
    mgr = _FakeManager()
    skill_service_module.set_websocket_manager(mgr)
    yield mgr
    skill_service_module.set_websocket_manager(None)


def _state(monkeypatch, value):
    """Stub the tri-state container read (#2196) the delivery gate uses."""
    from services import docker_utils
    monkeypatch.setattr(docker_utils, "agent_container_state_async", AsyncMock(return_value=value))


def _inject_result(**per_skill):
    results = {}
    for name, status in per_skill.items():
        results[name] = {"success": status != "failed", "status": status, "files_written": 1,
                         "warnings": [], **({"error": "boom"} if status == "failed" else {})}
    return {"success": all(v["status"] != "failed" for v in results.values()),
            "skills_injected": sum(1 for v in results.values() if v["status"] == "injected"),
            "skills_unchanged": sum(1 for v in results.values() if v["status"] == "unchanged"),
            "skills_failed": sum(1 for v in results.values() if v["status"] == "failed"),
            "results": results}


# ---- deliver_assigned -------------------------------------------------------

def test_running_agent_gets_the_start_path_injection_not_a_subset(svc, monkeypatch):
    """The load-bearing premise correction from /autoplan: `_inject_skills_locked`
    rewrites CLAUDE.md's section to the names of the run it is in, so delivery
    must pass EVERY assigned name (None → assigned) with `force=False`."""
    _state(monkeypatch, "running")
    inject = AsyncMock(return_value=_inject_result(research="injected", writing="unchanged"))
    monkeypatch.setattr(svc, "inject_skills", inject)
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())

    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))

    inject.assert_awaited_once_with(AGENT, None, force=False, assigned_only=True)
    assert report["status"] == "injected"
    assert report["skills"] == {"research": {"status": "injected"}}   # projected onto the request


def test_unchanged_sibling_counts_as_delivered_for_the_requested_name(svc, monkeypatch):
    """Re-assign after a deferred removal: the files are still there and match
    the library SHA, so the start path says `unchanged` — the skill IS present."""
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(return_value=_inject_result(research="unchanged")))
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert report["status"] == "injected"


def test_stopped_agent_is_pending_start_and_never_injects(svc, monkeypatch):
    _state(monkeypatch, "stopped")
    inject = AsyncMock()
    monkeypatch.setattr(svc, "inject_skills", inject)
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert report["status"] == "pending_start"
    assert report["skills"]["research"]["status"] == "pending_start"
    inject.assert_not_awaited()


def test_docker_unreadable_is_not_delivered_never_pending_start(svc, monkeypatch):
    """`None` means Docker could not be asked — promising a start-path delivery
    would be a lie about a container that may be running right now."""
    _state(monkeypatch, None)
    inject = AsyncMock()
    monkeypatch.setattr(svc, "inject_skills", inject)
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert report["status"] == "not_delivered"
    assert report["reason"] == "docker_unavailable"
    inject.assert_not_awaited()


def test_busy_lock_is_retried_once_then_reported(svc, monkeypatch):
    """An assign that lands while the START holds the lock: the start read its
    list before the row existed, so 'applies on next start' would never come.
    One bounded retry is the honest fix; two busies is a real contention."""
    _state(monkeypatch, "running")
    monkeypatch.setattr(skill_service_module, "_DELIVERY_BUSY_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())
    calls = {"n": 0}

    async def _inject(agent, names, force, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SkillInjectionBusy("busy")
        return _inject_result(research="injected")

    monkeypatch.setattr(svc, "inject_skills", _inject)
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert calls["n"] == 2
    assert report["status"] == "injected"

    calls["n"] = 0

    async def _always_busy(agent, names, force, **kw):
        calls["n"] += 1
        raise SkillInjectionBusy("busy")

    monkeypatch.setattr(svc, "inject_skills", _always_busy)
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert calls["n"] == 2
    assert report == {"status": "not_delivered", "reason": "injection_in_progress",
                      "skills": {"research": {"status": "not_delivered"}}}


def test_over_budget_answers_in_progress_and_keeps_the_work(svc, monkeypatch, ws):
    """The 6-month-regret guard: a slow restore must not turn a committed row
    into a client-side 'save failed'. The injection keeps running, and the WS
    trigger fires when it lands so open surfaces still converge."""
    _state(monkeypatch, "running")
    monkeypatch.setattr(skill_service_module, "SKILL_DELIVERY_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())
    finished = asyncio.Event()

    async def _slow(agent, names, force, **kw):
        await asyncio.sleep(0.2)
        finished.set()
        return _inject_result(research="injected")

    monkeypatch.setattr(svc, "inject_skills", _slow)

    async def _run():
        report = await svc.deliver_assigned(AGENT, ["research"])
        assert report["status"] == "in_progress"
        assert not finished.is_set()
        assert skill_service_module._BACKGROUND_DELIVERIES     # strong ref held
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0.05)                               # let the done-callback run
        return report

    asyncio.run(_run())
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]
    assert not skill_service_module._BACKGROUND_DELIVERIES


def test_connection_refused_on_a_running_container_is_agent_not_ready(svc, monkeypatch):
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(side_effect=ConnectionError("connection refused")))
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert report["status"] == "not_delivered"
    assert report["reason"] == "agent_not_ready"


def test_a_generic_injection_failure_is_named_and_never_raises(svc, monkeypatch):
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(side_effect=RuntimeError("tar exploded")))
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research"]))
    assert report == {"status": "not_delivered", "reason": "injection_error",
                      "skills": {"research": {"status": "not_delivered"}}}


def test_partial_names_which_requested_skill_failed(svc, monkeypatch):
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills",
                        AsyncMock(return_value=_inject_result(research="injected", writing="failed")))
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())
    report = asyncio.run(svc.deliver_assigned(AGENT, ["research", "writing"]))
    assert report["status"] == "partial"
    assert report["skills"]["research"] == {"status": "injected"}
    assert report["skills"]["writing"] == {"status": "failed", "error": "boom"}


def test_traversal_names_never_reach_delivery(svc, monkeypatch):
    inject = AsyncMock()
    monkeypatch.setattr(svc, "inject_skills", inject)
    _state(monkeypatch, "running")
    report = asyncio.run(svc.deliver_assigned(AGENT, ["../etc", ""]))
    assert report == {"status": "injected", "skills": {}}
    inject.assert_not_awaited()


# ---- the under-lock re-read ----------------------------------------------

def test_a_name_unassigned_between_commit_and_lock_is_not_landed(svc, monkeypatch):
    """PUT on worker A commits + injects X; DELETE X on worker B commits and
    defers (busy). Without the re-read, A lands X with no row behind it.
    Opt-in (`assigned_only`) — only the delivery path asks for it; the start
    path, the sweep and manual Sync keep their explicit lists verbatim."""
    monkeypatch.setattr(skill_service_module.db, "get_agent_skill_names", lambda agent: ["research"])
    monkeypatch.setattr(svc, "_resolution", lambda: {})
    monkeypatch.setattr(svc, "_read_agent_skill_metas", AsyncMock(return_value={}))
    monkeypatch.setattr(svc, "_update_claude_md_skills_section", AsyncMock())
    monkeypatch.setattr(svc, "_finalize_injection", AsyncMock(), raising=False)
    monkeypatch.setattr(skill_service_module, "get_agent_client", lambda name: SimpleNamespace())

    result = asyncio.run(svc._inject_skills_locked(
        AGENT, ["research", "writing"], False, assigned_only=True))

    assert result["results"]["writing"]["status"] == "unassigned_meanwhile"
    # `research` IS assigned but absent from the library resolution → its own
    # named failure, proving the loop still ran for the surviving name.
    assert result["results"]["research"]["status"] == "failed"
    assert result["results"]["research"]["error"] == "Skill not found in library"


# ---- the broadcast ----------------------------------------------------------

def test_broadcast_is_thin_and_best_effort(ws):
    asyncio.run(skill_service_module.broadcast_skills_changed(AGENT))
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]
    assert set(json.loads(ws.messages[0])) == {"type", "agent_name"}     # never skill names

    class _Broken:
        async def broadcast(self, message):
            raise RuntimeError("socket gone")

    skill_service_module.set_websocket_manager(_Broken())
    asyncio.run(skill_service_module.broadcast_skills_changed(AGENT))    # does not raise


def test_broadcast_without_a_manager_is_a_noop():
    skill_service_module.set_websocket_manager(None)
    asyncio.run(skill_service_module.broadcast_skills_changed(AGENT))


# ---- the routes -------------------------------------------------------------

@pytest.fixture
def router(monkeypatch, ws):
    from routers import skills as mod
    monkeypatch.setattr(mod.skill_service, "get_skill", lambda name: {"name": name} if name != "ghost" else None)
    return mod


def _user():
    return SimpleNamespace(id=1, username="owner", email="o@example.com", role="user",
                           agent_name=None, connector_agent=None, mcp_scope=None)


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                           url=SimpleNamespace(path="/x"), state=SimpleNamespace(request_id="r1"))


def test_post_assign_delivers_and_broadcasts(router, monkeypatch, ws):
    monkeypatch.setattr(router.db, "assign_skill", lambda a, s, u: {"agent_name": a, "skill_name": s})
    deliver = AsyncMock(return_value={"status": "injected", "skills": {"research": {"status": "injected"}}})
    monkeypatch.setattr(router.skill_service, "deliver_assigned", deliver)

    body = asyncio.run(router.assign_skill("research", agent_name=AGENT, current_user=_user()))

    deliver.assert_awaited_once_with(AGENT, ["research"])
    assert body["success"] is True and body["message"] == "Skill assigned"
    assert body["delivery"]["status"] == "injected"
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]


def test_post_already_assigned_still_delivers(router, monkeypatch):
    """The Library control has no Sync button — a re-click after `not_delivered`
    is the only retry it has, so the idempotent branch must deliver too."""
    monkeypatch.setattr(router.db, "assign_skill", lambda a, s, u: None)
    deliver = AsyncMock(return_value={"status": "injected", "skills": {"research": {"status": "injected"}}})
    monkeypatch.setattr(router.skill_service, "deliver_assigned", deliver)
    body = asyncio.run(router.assign_skill("research", agent_name=AGENT, current_user=_user()))
    deliver.assert_awaited_once()
    assert body["message"] == "Skill already assigned"
    assert body["delivery"]["status"] == "injected"


def test_post_row_survives_a_delivery_that_raises_and_still_answers_200(router, monkeypatch):
    monkeypatch.setattr(router.db, "assign_skill", lambda a, s, u: {"agent_name": a, "skill_name": s})
    monkeypatch.setattr(router.skill_service, "deliver_assigned", AsyncMock(side_effect=RuntimeError("x")))
    body = asyncio.run(router.assign_skill("research", agent_name=AGENT, current_user=_user()))
    assert body["success"] is True
    assert body["delivery"] == {"status": "not_delivered", "reason": "injection_error",
                                "skills": {"research": {"status": "not_delivered"}}}


def test_post_unknown_skill_is_404_before_any_write_or_delivery(router, monkeypatch):
    from fastapi import HTTPException
    assign = lambda a, s, u: (_ for _ in ()).throw(AssertionError("wrote a row"))
    monkeypatch.setattr(router.db, "assign_skill", assign)
    deliver = AsyncMock()
    monkeypatch.setattr(router.skill_service, "deliver_assigned", deliver)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.assign_skill("ghost", agent_name=AGENT, current_user=_user()))
    assert exc.value.status_code == 404
    deliver.assert_not_awaited()


def test_put_is_symmetric_added_delivered_dropped_removed(router, monkeypatch, ws):
    monkeypatch.setattr(router.db, "get_agent_skill_names", lambda a: ["old", "keep"])
    monkeypatch.setattr(router.db, "set_agent_skills", lambda **kw: len(kw["skill_names"]))
    deliver = AsyncMock(return_value={"status": "injected", "skills": {"new": {"status": "injected"}}})
    remove = AsyncMock(return_value={"success": True, "skills_removed": 1, "skills_failed": 0, "results": {}})
    monkeypatch.setattr(router.skill_service, "deliver_assigned", deliver)
    monkeypatch.setattr(router.skill_service, "remove_skills", remove)
    monkeypatch.setattr(router.platform_audit_service, "log", AsyncMock())

    body = asyncio.run(router.update_agent_skills(
        SimpleNamespace(skills=["keep", "new"]), _request(), agent_name=AGENT, current_user=_user()))

    deliver.assert_awaited_once_with(AGENT, ["new"])
    remove.assert_awaited_once_with(AGENT, ["old"])
    assert body["delivery"]["status"] == "injected"
    assert body["removal"]["status"] == "completed"
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]


def test_put_with_no_delta_neither_delivers_nor_broadcasts(router, monkeypatch, ws):
    monkeypatch.setattr(router.db, "get_agent_skill_names", lambda a: ["keep"])
    monkeypatch.setattr(router.db, "set_agent_skills", lambda **kw: 1)
    deliver = AsyncMock(); remove = AsyncMock()
    monkeypatch.setattr(router.skill_service, "deliver_assigned", deliver)
    monkeypatch.setattr(router.skill_service, "remove_skills", remove)
    body = asyncio.run(router.update_agent_skills(
        SimpleNamespace(skills=["keep"]), _request(), agent_name=AGENT, current_user=_user()))
    deliver.assert_not_awaited(); remove.assert_not_awaited()
    assert body["delivery"] is None and body["removal"] is None
    assert ws.messages == []


def test_put_invalid_name_is_422_before_any_write(router, monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(router.db, "set_agent_skills",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("wrote")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.update_agent_skills(
            SimpleNamespace(skills=["../x"]), _request(), agent_name=AGENT, current_user=_user()))
    assert exc.value.status_code == 422


def test_delete_broadcasts_on_the_row_change_even_when_removal_defers(router, monkeypatch, ws):
    monkeypatch.setattr(router.db, "unassign_skill", lambda a, s: True)
    monkeypatch.setattr(router.skill_service, "remove_skills", AsyncMock(side_effect=SkillInjectionBusy("busy")))
    body = asyncio.run(router.unassign_skill("research", _request(), agent_name=AGENT, current_user=_user()))
    assert body["removal"]["status"] == "deferred"
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]


def test_delete_of_a_missing_row_is_silent(router, monkeypatch, ws):
    monkeypatch.setattr(router.db, "unassign_skill", lambda a, s: False)
    asyncio.run(router.unassign_skill("research", _request(), agent_name=AGENT, current_user=_user()))
    assert ws.messages == []


def test_manual_sync_broadcasts(router, monkeypatch, ws):
    monkeypatch.setattr(router.skill_service, "inject_skills", AsyncMock(return_value={"success": True}))
    asyncio.run(router.inject_skills(agent_name=AGENT, current_user=_user()))
    assert [json.loads(m) for m in ws.messages] == [{"type": "agent_skills_changed", "agent_name": AGENT}]


def test_manual_sync_busy_stays_409_and_broadcasts_nothing(router, monkeypatch, ws):
    from fastapi import HTTPException
    monkeypatch.setattr(router.skill_service, "inject_skills", AsyncMock(side_effect=SkillInjectionBusy("busy")))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(router.inject_skills(agent_name=AGENT, current_user=_user()))
    assert exc.value.status_code == 409
    assert ws.messages == []


def test_the_narrowing_is_opt_in(svc, monkeypatch):
    """Manual Sync / start / sweep pass explicit lists and must not be narrowed
    by a row read they did not ask for (the ent#183 suites inject with no rows)."""
    monkeypatch.setattr(skill_service_module.db, "get_agent_skill_names", lambda agent: [])
    monkeypatch.setattr(svc, "_resolution", lambda: {})
    monkeypatch.setattr(svc, "_read_agent_skill_metas", AsyncMock(return_value={}))
    monkeypatch.setattr(svc, "_update_claude_md_skills_section", AsyncMock())
    monkeypatch.setattr(skill_service_module, "get_agent_client", lambda name: SimpleNamespace())
    result = asyncio.run(svc._inject_skills_locked(AGENT, ["research"], False))
    assert result["results"]["research"]["status"] == "failed"      # not-in-library, NOT unassigned_meanwhile
