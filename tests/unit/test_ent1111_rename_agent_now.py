"""`rename_agent_now` — the rename as a service primitive (abilityai/trinity-enterprise#1111, T3).

The whole rename lived in `routers/agent_rename.py`: human-only, dereferencing
`request` for its audit, and reachable only over HTTP. A platform caller acting
on its own behalf (`actor=None`) could not use it, so the alternative was a
second implementation — ~70 lines that would drift from the five fixes this one
has accumulated (#1560 Redis sweep, #1664 volume-base pin, #1671 base claim,
#3813 env re-bake, ent#751 gate cancel).

This file pins both halves:
  * the primitive — order of steps, `source="system"` when nobody asked, the
    container renamed BACK when the DB refuses, `was_running` reported, the
    agent left stopped, and every best-effort step swallowing its failure;
  * the route — it delegates, and every status code and response field it
    answered before is unchanged.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from services.agent_service import rename as rename_mod
from services.agent_service.rename import (
    AgentContainerMissing,
    RenameRefused,
    rename_agent_now,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _container(status="running"):
    c = MagicMock()
    c.status = status
    return c


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The primitive's collaborators, each recorded. Avatars point at a tmp dir
    so the rename of the cached files is real rather than a silent no-op."""
    calls = {"order": []}

    db = MagicMock()
    db.is_system_agent.return_value = False
    db.rename_agent.side_effect = lambda o, n: (calls["order"].append(f"db.rename:{o}->{n}")
                                                or True)
    monkeypatch.setattr(rename_mod, "db", db)

    container = _container()
    monkeypatch.setattr(rename_mod, "get_agent_container",
                        MagicMock(return_value=container))

    async def _stop(c):
        calls["order"].append("stop")
    async def _rename(c, name):
        calls["order"].append(f"container.rename:{name}")
    monkeypatch.setattr(rename_mod, "container_stop", AsyncMock(side_effect=_stop))
    monkeypatch.setattr(rename_mod, "container_rename", AsyncMock(side_effect=_rename))

    audit = AsyncMock(return_value="audit-1")
    monkeypatch.setattr(rename_mod.platform_audit_service, "log", audit)

    gate = AsyncMock(side_effect=lambda *a, **k: calls["order"].append("gate_cancel"))
    from services import skill_gate_service
    monkeypatch.setattr(skill_gate_service, "cancel_pending_for_agent", gate)

    cleared = []
    async def _clear(name):
        calls["order"].append(f"redis_clear:{name}")
        cleared.append(name)
    from services import agent_runtime_state
    monkeypatch.setattr(agent_runtime_state, "clear_agent_runtime_state", _clear)

    avatars = tmp_path / "avatars"
    avatars.mkdir()
    monkeypatch.setattr(rename_mod, "AVATAR_DIR", avatars)

    ws, filtered = AsyncMock(), AsyncMock()

    calls.update(db=db, container=container, audit=audit, gate=gate,
                 cleared=cleared, avatars=avatars, ws=ws, filtered=filtered)
    return calls


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------

class TestRenameAgentNow:
    async def test_the_platform_acting_alone_audits_as_system(self, env):
        res = await rename_agent_now("spare-7", "regent-42", actor=None,
                                     reason="member agent hand-over")

        assert (res.old_name, res.new_name, res.was_running) == ("spare-7", "regent-42", True)
        kw = env["audit"].await_args.kwargs
        assert kw["source"] == "system"
        assert kw["actor_user"] is None
        assert kw["event_action"] == "rename"
        assert kw["target_id"] == "regent-42"
        assert kw["details"]["old_name"] == "spare-7"
        assert kw["details"]["new_name"] == "regent-42"
        assert kw["details"]["reason"] == "member agent hand-over"
        # No request to dereference: the audit's HTTP fields are simply absent.
        assert kw["actor_ip"] is None and kw["endpoint"] is None and kw["request_id"] is None

    async def test_a_person_audits_as_api_with_the_request_context(self, env):
        actor = MagicMock(email="alice@example.com")
        await rename_agent_now("staging", "prod", actor=actor,
                               audit_ctx={"actor_ip": "10.0.0.9",
                                          "endpoint": "/api/agents/staging/rename",
                                          "request_id": "req-7"})

        kw = env["audit"].await_args.kwargs
        assert kw["source"] == "api"
        assert kw["actor_user"] is actor
        assert kw["actor_ip"] == "10.0.0.9"
        assert kw["endpoint"] == "/api/agents/staging/rename"
        assert kw["request_id"] == "req-7"
        assert "reason" not in kw["details"]

    async def test_the_steps_run_in_the_route_s_order(self, env):
        await rename_agent_now("old", "new", actor=None)

        assert env["order"] == [
            "stop",
            "container.rename:agent-new",
            "db.rename:old->new",
            "gate_cancel",
            "redis_clear:old",
            "redis_clear:new",
        ]

    async def test_a_stopped_agent_is_not_stopped_again(self, env, monkeypatch):
        monkeypatch.setattr(rename_mod, "get_agent_container",
                            MagicMock(return_value=_container("exited")))

        res = await rename_agent_now("old", "new", actor=None)

        assert res.was_running is False
        assert "stop" not in env["order"]

    async def test_the_agent_is_left_stopped(self, env, monkeypatch):
        """The next `start_agent_internal` is what re-bakes AGENT_NAME, the
        label and the auth token (#3813, #1159) — so the primitive must NOT
        start it back up."""
        from services.agent_service import lifecycle
        start = AsyncMock()
        monkeypatch.setattr(lifecycle, "start_agent_internal", start)

        await rename_agent_now("old", "new", actor=None)

        env["container"].start.assert_not_called()
        start.assert_not_awaited()

    async def test_both_names_are_swept_from_redis(self, env):
        await rename_agent_now("old", "new", actor=None)
        assert env["cleared"] == ["old", "new"]

    async def test_the_cached_avatar_files_follow_the_name(self, env):
        avatars = env["avatars"]
        (avatars / "old.webp").write_bytes(b"a")
        (avatars / "old_ref.png").write_bytes(b"b")
        (avatars / "old_emotion_happy.webp").write_bytes(b"c")

        await rename_agent_now("old", "new", actor=None)

        assert (avatars / "new.webp").exists()
        assert (avatars / "new_ref.png").exists()
        assert (avatars / "new_emotion_happy.webp").exists()
        assert not (avatars / "old.webp").exists()

    async def test_the_rename_event_is_broadcast_on_both_channels(self, env):
        await rename_agent_now("old", "new", actor=None, ws_manager=env["ws"],
                               filtered_ws_manager=env["filtered"])

        payload = json.loads(env["ws"].broadcast.await_args.args[0])
        assert payload == {"event": "agent_renamed", "type": "agent_renamed",
                           "name": "new",
                           "data": {"old_name": "old", "new_name": "new"}}
        assert env["filtered"].broadcast_filtered.await_args.args[0] == payload

    async def test_no_manager_means_no_broadcast_and_no_failure(self, env):
        res = await rename_agent_now("old", "new", actor=None)
        assert res.new_name == "new"

    # -- refusals ---------------------------------------------------------

    async def test_a_system_agent_is_refused_before_any_side_effect(self, env):
        env["db"].is_system_agent.return_value = True

        with pytest.raises(HTTPException) as exc:
            await rename_agent_now("trinity-system", "mine", actor=None)

        assert exc.value.status_code == 403
        assert exc.value.detail == "System agents cannot be renamed"
        assert env["order"] == []
        env["db"].rename_agent.assert_not_called()

    async def test_a_missing_container_is_its_own_refusal(self, env, monkeypatch):
        monkeypatch.setattr(rename_mod, "get_agent_container",
                            MagicMock(return_value=None))

        with pytest.raises(AgentContainerMissing):
            await rename_agent_now("ghost", "new", actor=None)

        assert env["order"] == []
        env["db"].rename_agent.assert_not_called()

    async def test_a_db_refusal_renames_the_container_back(self, env):
        env["db"].rename_agent.side_effect = lambda o, n: False

        with pytest.raises(RenameRefused):
            await rename_agent_now("old", "taken", actor=None)

        assert env["order"] == ["stop", "container.rename:agent-taken",
                                "container.rename:agent-old"]
        env["audit"].assert_not_awaited()

    async def test_a_db_exception_also_renames_the_container_back(self, env):
        """`db.rename_agent` re-checks the volume-base claim inside its
        transaction; an exception there leaves the container renamed and the
        rows untouched unless we undo it (eng H3)."""
        env["db"].rename_agent.side_effect = RuntimeError("locked")

        with pytest.raises(RenameRefused):
            await rename_agent_now("old", "new", actor=None)

        assert env["order"][-1] == "container.rename:agent-old"

    @pytest.mark.parametrize("step", ["gate", "redis"])
    async def test_a_best_effort_step_cannot_fail_the_rename(self, env, monkeypatch, step):
        if step == "gate":
            from services import skill_gate_service
            monkeypatch.setattr(skill_gate_service, "cancel_pending_for_agent",
                                AsyncMock(side_effect=RuntimeError("boom")))
        else:
            from services import agent_runtime_state
            async def _boom(_name):
                raise RuntimeError("redis down")
            monkeypatch.setattr(agent_runtime_state, "clear_agent_runtime_state", _boom)

        res = await rename_agent_now("old", "new", actor=None)

        assert res.new_name == "new"
        env["audit"].assert_awaited_once()


# ---------------------------------------------------------------------------
# The route delegates, with every code unchanged
# ---------------------------------------------------------------------------

def _person():
    user = MagicMock()
    user.agent_name = None
    user.scope = "user"
    user.username = "alice"
    user.email = "alice@example.com"
    return user


def _request():
    req = MagicMock()
    req.client.host = "10.0.0.9"
    req.scope = {"path": "/api/agents/staging/rename"}
    req.state.request_id = "req-7"
    return req


@pytest.fixture
def route(monkeypatch):
    """The route with its DB and Docker reads stubbed and the primitive recorded."""
    import routers.agent_rename as mod

    db = MagicMock()
    db.can_user_rename_agent.return_value = True
    db.is_system_agent.return_value = False
    db.is_volume_base_reserved.return_value = False
    monkeypatch.setattr(mod, "db", db)
    monkeypatch.setattr(mod, "get_agent_container", MagicMock(return_value=None))

    primitive = AsyncMock(return_value=rename_mod.RenameResult("staging", "prod", True))
    monkeypatch.setattr(mod, "rename_agent_now", primitive)
    monkeypatch.setattr(mod, "manager", None)
    monkeypatch.setattr(mod, "filtered_manager", None)
    return MagicMock(mod=mod, db=db, primitive=primitive)


class TestRouteDelegates:
    async def test_it_calls_the_primitive_with_the_sanitized_name(self, route):
        user, req = _person(), _request()

        out = await route.mod.rename_agent_endpoint(
            "staging", MagicMock(new_name="  Prod Agent  "), req, user)

        args, kwargs = route.primitive.await_args
        assert args[:2] == ("staging", "prod-agent")
        assert kwargs["actor"] is user
        assert kwargs["audit_ctx"] == {"actor_ip": "10.0.0.9",
                                       "endpoint": "/api/agents/staging/rename",
                                       "request_id": "req-7"}
        assert out == {
            "message": "Agent renamed from 'staging' to 'prod-agent'",
            "old_name": "staging",
            "new_name": "prod-agent",
            "was_running": True,
            "note": "Agent needs to be restarted to apply all changes",
        }

    async def test_a_stopped_agent_gets_no_restart_note(self, route):
        route.primitive.return_value = rename_mod.RenameResult("staging", "prod", False)

        out = await route.mod.rename_agent_endpoint(
            "staging", MagicMock(new_name="prod"), _request(), _person())

        assert out["was_running"] is False and out["note"] is None

    async def test_the_websocket_managers_are_handed_through(self, route, monkeypatch):
        monkeypatch.setattr(route.mod, "manager", "WS")
        monkeypatch.setattr(route.mod, "filtered_manager", "FILTERED")

        await route.mod.rename_agent_endpoint(
            "staging", MagicMock(new_name="prod"), _request(), _person())

        kwargs = route.primitive.await_args.kwargs
        assert kwargs["ws_manager"] == "WS"
        assert kwargs["filtered_ws_manager"] == "FILTERED"

    async def test_an_agent_principal_is_still_refused(self, route):
        agent = MagicMock()
        agent.agent_name = "bot"
        agent.scope = "agent"

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), agent)

        assert exc.value.status_code == 403
        route.primitive.assert_not_awaited()

    @pytest.mark.parametrize("is_system,detail", [
        (True, "System agents cannot be renamed"),
        (False, "You don't have permission to rename this agent"),
    ])
    async def test_permission_refusals_are_unchanged(self, route, is_system, detail):
        route.db.can_user_rename_agent.return_value = False
        route.db.is_system_agent.return_value = is_system

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert (exc.value.status_code, exc.value.detail) == (403, detail)
        route.primitive.assert_not_awaited()

    @pytest.mark.parametrize("new_name,detail", [
        ("   ", "New name cannot be empty"),
        ("///", "Invalid agent name after sanitization"),
        ("x" * 64, "Agent name too long (max 63 characters)"),
        ("staging", "New name is the same as current name"),
    ])
    async def test_the_name_400s_are_unchanged(self, route, new_name, detail):
        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name=new_name), _request(), _person())

        assert (exc.value.status_code, exc.value.detail) == (400, detail)
        route.primitive.assert_not_awaited()

    async def test_a_taken_name_still_409s(self, route, monkeypatch):
        monkeypatch.setattr(route.mod, "get_agent_container",
                            MagicMock(return_value=_container()))

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert exc.value.status_code == 409
        assert "already exists" in exc.value.detail
        route.primitive.assert_not_awaited()

    async def test_a_claimed_volume_base_still_409s_before_the_primitive(self, route):
        """#1671: the refusal must leave nothing half-done, so it fires before
        the primitive is entered at all."""
        route.db.is_volume_base_reserved.return_value = True

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert exc.value.status_code == 409
        assert "data volumes" in str(exc.value.detail)
        route.primitive.assert_not_awaited()
        assert route.db.is_volume_base_reserved.call_args.kwargs.get(
            "exclude_agent") == "staging"

    async def test_a_missing_container_still_404s(self, route):
        route.primitive.side_effect = AgentContainerMissing("staging")

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert (exc.value.status_code, exc.value.detail) == (404, "Agent not found")

    async def test_a_db_refusal_still_500s_with_the_same_text(self, route):
        route.primitive.side_effect = RenameRefused("staging -> prod")

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert exc.value.status_code == 500
        assert exc.value.detail == (
            "Failed to update database. Agent name may already be taken.")

    async def test_any_other_failure_still_500s(self, route):
        route.primitive.side_effect = RuntimeError("docker exploded")

        with pytest.raises(HTTPException) as exc:
            await route.mod.rename_agent_endpoint(
                "staging", MagicMock(new_name="prod"), _request(), _person())

        assert exc.value.status_code == 500
        assert "Failed to rename agent" in exc.value.detail

    async def test_a_request_without_a_client_still_renames(self, route):
        req = _request()
        req.client = None

        await route.mod.rename_agent_endpoint(
            "staging", MagicMock(new_name="prod"), req, _person())

        assert route.primitive.await_args.kwargs["audit_ctx"]["actor_ip"] is None


class TestTheMutationLivesInOnePlace:
    """One rename, not two. The route must not keep a copy of the steps."""

    def test_the_route_no_longer_mutates_anything_itself(self):
        import ast
        import inspect

        import routers.agent_rename as mod
        tree = ast.parse(inspect.getsource(mod))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        called |= {n.func.id for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        forbidden = {"container_stop", "container_rename", "rename_agent",
                     "cancel_pending_for_agent", "clear_agent_runtime_state",
                     "broadcast", "broadcast_filtered"}
        assert not (called & forbidden), (
            f"the route still performs the rename itself: {called & forbidden}")
        assert "rename_agent_now" in called
