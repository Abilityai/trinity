"""ent#535 — the voice call acts AS the agent, over a locked tool surface.

Three things this pins, and the first is the one the issue exists for.

**run_task runs in the chat.** It routed to the agent container's task endpoint:
a stateless run, 30s timeout, no thread, no memory of the conversation the
person is in — "what makes voice just chat today", in the issue's words. A
Workspace call now runs the turn through `portal_chat`, the same pipeline a
typed message takes, so the agent has its own skills, files, memory and
mid-work state and the answer lands in that thread as a turn.

**The spoken budget is not a cancellation.** Past 20s the model is told the work
is still running and keeps the floor; the turn CONTINUES and its reply lands in
the chat. The old 30s `wait_for` threw the work away with the task already done
and paid for.

**The manifest is locked.** Resolved once at session start, narrowing-only, and
refused by name at the dispatcher — so a tool the session was not granted cannot
be reached even by a path that builds a config from somewhere else.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _load_voice_tools():
    """Import the policy leaf without dragging in `services/__init__`, which
    pulls the whole template stack (and a live config) behind it."""
    spec = importlib.util.spec_from_file_location(
        "ent535_voice_tools", _BACKEND / "services" / "voice_tools.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vt = _load_voice_tools()


# ---------------------------------------------------------------------------
# The manifest: narrowing only, allowlisted, and a decision about fleet tools
# ---------------------------------------------------------------------------
class TestManifest:
    def test_workspace_default_is_run_task_plus_the_canvas(self):
        assert vt.resolve_manifest(workspace_mode=True) == vt.PLATFORM_VOICE_TOOLS

    def test_a_call_with_no_canvas_gets_no_canvas_tools(self):
        # A phone call has nothing to draw on; offering the tools would
        # advertise a surface the caller cannot see.
        assert vt.resolve_manifest(workspace_mode=False) == frozenset({vt.RUN_TASK})

    def test_a_declaration_may_narrow(self):
        assert vt.resolve_manifest([vt.RUN_TASK], workspace_mode=True) == frozenset({vt.RUN_TASK})

    def test_a_declaration_may_NOT_widen(self):
        """`template.yaml` is agent-writable. A declaration that could add a
        tool would let an agent grant itself a capability by editing itself."""
        got = vt.resolve_manifest(
            [vt.RUN_TASK, "chat_with_agent", "list_agents", "fan_out"], workspace_mode=True
        )
        assert got == frozenset({vt.RUN_TASK})

    def test_fleet_tools_are_not_on_offer_at_all(self):
        # AC 4, as a decision rather than an omission: they cannot enter the
        # manifest because they are not in the platform set.
        for name in ("list_agents", "chat_with_agent", "fan_out", "deploy_system"):
            assert name not in vt.PLATFORM_VOICE_TOOLS

    def test_an_explicit_empty_list_is_not_the_same_as_no_declaration(self):
        # `[]` means "no tools"; absence means "the default". Collapsing them
        # would make an empty declaration unexpressible.
        assert vt.resolve_manifest([], workspace_mode=True) == frozenset()
        assert vt.resolve_manifest(None, workspace_mode=True) == vt.PLATFORM_VOICE_TOOLS

    @pytest.mark.parametrize("bad", ["run_task", 42, {"run_task": True}])
    def test_a_malformed_declaration_falls_back_rather_than_disarming(self, bad):
        # A typo in a YAML file must not silently cost the agent its voice.
        assert vt.resolve_manifest(bad, workspace_mode=True) == vt.PLATFORM_VOICE_TOOLS

    def test_unknown_names_are_dropped_not_fatal(self):
        assert vt.resolve_manifest(
            [vt.RUN_TASK, "teleport"], workspace_mode=True
        ) == frozenset({vt.RUN_TASK})


# ---------------------------------------------------------------------------
# run_task: as the agent, in the chat — and the budget that never cancels
# ---------------------------------------------------------------------------
def _voice_module():
    """`services.gemini_voice` with its heavy imports stubbed."""
    from services import gemini_voice
    return gemini_voice


def _session(gv, **over):
    base = dict(
        session_id="vs_test",
        agent_name="scout",
        chat_session_id=None,
        user_id=1,
        user_email="op@example.com",
        system_prompt="p",
        workspace_mode=True,
        portal_session_id="portal-1",
        client_email="op@example.com",
    )
    base.update(over)
    return gv.VoiceSession(**base)


class TestRunTaskRunsInTheChat:
    def test_a_workspace_call_runs_the_turn_in_its_thread(self):
        gv = _voice_module()
        session = _session(gv)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)

        chat = AsyncMock(return_value={"response": "seventeen open PRs"})
        with patch("client_portal.service.portal_chat", chat):
            out = asyncio.run(svc._run_task_in_chat(session, "how many PRs?"))

        assert out == "seventeen open PRs"
        chat.assert_awaited_once()
        kwargs = chat.await_args.kwargs
        # The thread, and the caller — this is what makes it the agent's own
        # session rather than a stateless run.
        assert kwargs["session_id"] == "portal-1"
        assert kwargs["email"] == "op@example.com"
        assert kwargs["include_owned"] is True

    def test_a_call_with_no_thread_keeps_the_container_path(self):
        # VoIP, and the legacy Agent Detail session: there is no chat to run in.
        gv = _voice_module()
        session = _session(gv, workspace_mode=False, portal_session_id=None, client_email=None)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)

        # The dispatcher owns the routing, so drive it: an unbound session must
        # reach the container path and never `portal_chat`.
        chat = AsyncMock()
        container = AsyncMock(return_value="from the container")
        sent = {}

        async def _send(_self, _session, call_id, tool_name, result):
            sent.update(result=result)

        session._pending_tool_tasks = {"c1": object()}
        with patch("client_portal.service.portal_chat", chat), \
             patch.object(gv.GeminiVoiceService, "_execute_tool", container), \
             patch.object(gv.GeminiVoiceService, "_send_tool_response", _send):
            asyncio.run(svc._execute_and_respond(
                session, "c1", SimpleNamespace(name=gv.RUN_TASK, args={"prompt": "hi"})))

        assert sent["result"] == "from the container"
        chat.assert_not_awaited()
        # …and by agent NAME, the container contract, not the session.
        assert container.await_args.args[0] == "scout"

    def test_half_a_binding_is_not_a_binding(self):
        # A thread with no email cannot be attributed; an email with no thread
        # has nowhere to land. Either alone must not silently run in a chat.
        gv = _voice_module()
        assert gv._is_workspace_bound(_session(gv)) is True
        assert gv._is_workspace_bound(_session(gv, client_email=None)) is False
        assert gv._is_workspace_bound(_session(gv, portal_session_id=None)) is False


class TestSpokenBudget:
    def test_a_slow_turn_keeps_the_floor_and_still_lands(self):
        """The contract: the model is answered inside the budget, the work is
        NOT cancelled, and the reply reaches the chat."""
        gv = _voice_module()
        session = _session(gv)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        landed = asyncio.Event()

        async def _slow(*_a, **_kw):
            await asyncio.sleep(0.05)
            landed.set()
            return {"response": "the long answer"}

        async def _drive():
            with patch("client_portal.service.portal_chat", _slow), \
                 patch.object(gv, "_SPOKEN_BUDGET_SECONDS", 0.01):
                spoken = await svc._run_task_in_chat(session, "long one")
            # The model was answered promptly, with the still-working line...
            assert spoken == gv._STILL_WORKING_RESULT
            # ...and the turn was NOT cancelled: it runs to completion, which is
            # what puts the reply in the chat.
            await asyncio.wait_for(landed.wait(), timeout=1)

        asyncio.run(_drive())

    def test_a_detached_turn_is_strongly_referenced(self):
        # asyncio holds only a weak reference to a bare `create_task`, so a
        # detached turn could be collected mid-flight — losing work the person
        # asked for, with no reply row and nothing to say why.
        gv = _voice_module()
        session = _session(gv)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        release = asyncio.Event()

        async def _slow(*_a, **_kw):
            await release.wait()
            return {"response": "done"}

        async def _drive():
            with patch("client_portal.service.portal_chat", _slow), \
                 patch.object(gv, "_SPOKEN_BUDGET_SECONDS", 0.01):
                await svc._run_task_in_chat(session, "x")
            assert gv._detached_turns, "the detached turn was dropped"
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_drive())

    def test_a_failing_turn_is_spoken_not_raised(self):
        gv = _voice_module()
        session = _session(gv)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        with patch("client_portal.service.portal_chat", AsyncMock(side_effect=RuntimeError("nope"))):
            out = asyncio.run(svc._run_task_in_chat(session, "x"))
        assert "did not go through" in out

    def test_the_budget_is_shorter_than_any_turn_timeout(self):
        # It bounds SPEECH, not the task. A budget at or above the turn timeout
        # would make the detach unreachable and restore the old behaviour.
        gv = _voice_module()
        assert 0 < gv._SPOKEN_BUDGET_SECONDS <= 30


# ---------------------------------------------------------------------------
# The lock, enforced at the dispatcher
# ---------------------------------------------------------------------------
class TestDispatcherRefusesOutsideTheManifest:
    def _dispatch(self, gv, session, name):
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        sent = {}

        async def _send(_self, _session, call_id, tool_name, result):
            sent.update(call_id=call_id, tool=tool_name, result=result)

        session._pending_tool_tasks = {"c1": object()}
        with patch.object(gv.GeminiVoiceService, "_send_tool_response", _send), \
             patch.object(gv.GeminiVoiceService, "_execute_tool", AsyncMock(return_value="ran")) as ran:
            asyncio.run(svc._execute_and_respond(session, "c1", SimpleNamespace(name=name, args={})))
        return sent, ran

    def test_an_agent_that_declared_NO_tools_gets_none(self):
        """The strongest narrowing must not invert into the widest manifest.

        The first cut read the manifest as `session.tool_manifest or default`,
        so an explicit `voice.tools: []` — an empty frozenset, which is falsy —
        fell through to the full platform set. The field is tri-state for this
        reason: `None` is "never resolved", `frozenset()` is a decision.
        """
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset())
        assert gv._session_manifest(session) == frozenset()
        sent, ran = self._dispatch(gv, session, gv.RUN_TASK)
        assert "not available" in sent["result"]
        ran.assert_not_awaited()

    def test_an_unresolved_manifest_falls_back_to_the_platform_default(self):
        # A session built directly, or reconstructed by an older path: the safe
        # answer is the pre-ent#535 surface, never "refuse everything".
        gv = _voice_module()
        session = _session(gv)
        assert session.tool_manifest is None
        assert gv._session_manifest(session) == vt.PLATFORM_VOICE_TOOLS

    def test_a_manifest_that_cannot_answer_membership_fails_safe(self):
        # Not a set ⇒ unresolved, rather than crashing the audio loop on `in`.
        gv = _voice_module()
        session = _session(gv, tool_manifest="run_task")
        assert gv._session_manifest(session) == vt.PLATFORM_VOICE_TOOLS

    def test_a_tool_outside_the_manifest_is_refused_by_name(self):
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({gv.RUN_TASK}))
        sent, ran = self._dispatch(gv, session, "show_markdown")
        assert "not available" in sent["result"]
        ran.assert_not_awaited()

    def test_a_nameless_call_is_not_read_as_run_task(self):
        """It defaulted to `run_task`, so a call with no name sent the model's
        arguments to the agent under a name nobody chose."""
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({gv.RUN_TASK}))
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        sent = {}

        async def _send(_self, _session, call_id, tool_name, result):
            sent.update(result=result)

        session._pending_tool_tasks = {"c1": object()}
        with patch.object(gv.GeminiVoiceService, "_send_tool_response", _send), \
             patch.object(gv.GeminiVoiceService, "_execute_tool", AsyncMock()) as ran:
            asyncio.run(svc._execute_and_respond(session, "c1", SimpleNamespace(args={"prompt": "x"})))
        assert "not available" in sent["result"]
        ran.assert_not_awaited()

    def test_the_refusal_still_answers_the_call(self):
        # A model that never receives a response for a call it made stops
        # speaking, so a refusal must be an answer, not silence.
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({gv.RUN_TASK}))
        sent, _ = self._dispatch(gv, session, "teleport")
        assert sent["call_id"] == "c1"
        assert sent["tool"] == "teleport"


class TestConfigIsBuiltFromTheManifest:
    def test_a_narrowed_session_is_never_offered_the_canvas(self):
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({gv.RUN_TASK}))
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        cfg = svc._build_live_config(session)
        names = {d.name for t in (cfg.tools or []) for d in (t.function_declarations or [])}
        assert names == {gv.RUN_TASK}

    def test_a_session_without_run_task_is_not_told_to_narrate_it(self):
        # AC 6: the prompt must not advertise a tool the lock removed. The
        # etiquette block is entirely about run_task's spoken filler.
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({"show_markdown"}))
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        cfg = svc._build_live_config(session)
        assert "run_task" not in (cfg.system_instruction or "")

    def test_the_default_workspace_session_gets_both(self):
        gv = _voice_module()
        session = _session(gv, tool_manifest=vt.PLATFORM_VOICE_TOOLS)
        svc = gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)
        cfg = svc._build_live_config(session)
        names = {d.name for t in (cfg.tools or []) for d in (t.function_declarations or [])}
        assert gv.RUN_TASK in names and "show_markdown" in names
