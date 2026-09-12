"""ent#551 — a long task runs in the background while the conversation continues.

Before this, `run_task` on a Workspace call held the model's turn until the
work came back (30 s `wait_for`, then ent#535's 20 s spoken budget): the line
was dead for the duration, and a task that outran the budget landed in the chat
but never re-entered the call. Two halves, both ruled by the operator 2026-09-07:

  * **It says what it is doing before it does it** — structurally. The etiquette
    (ent#576) asks the model to announce; `_ack_watch` nudges it when it does not.
  * **The task runs in the background.** Dispatch answers the model AT ONCE with
    a task id; the turn runs as the agent in the bound thread, its rows land
    there attributed to the call, and the outcome re-enters the live call as a
    system notice at a natural boundary — never across the person mid-sentence,
    never silently, and never lost when the call ends first.

Same harness as `test_ent535_voice_tool_set.py`: the real module, `portal_chat`
patched, the Live session faked where a notice has to be observed.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _voice_module():
    from services import gemini_voice
    return gemini_voice


def _svc(gv):
    return gv.GeminiVoiceService.__new__(gv.GeminiVoiceService)


def _session(gv, **over):
    base = dict(
        session_id="vs_call",
        agent_name="scout",
        chat_session_id=None,
        user_id=1,
        user_email="op@example.com",
        system_prompt="p",
        workspace_mode=True,
        portal_session_id="portal-1",
        client_email="op@example.com",
        is_platform=True,
    )
    base.update(over)
    s = gv.VoiceSession(**base)
    s._active = True
    return s


class _Live:
    """The one method a notice needs on the provider session."""
    def __init__(self):
        self.send_realtime_input = AsyncMock()

    @property
    def said(self):
        return [c.kwargs.get("text") for c in self.send_realtime_input.await_args_list]


def _blocked_chat(release: asyncio.Event, reply="the long answer"):
    async def _chat(*_a, **_kw):
        await release.wait()
        return {"response": reply}
    return _chat


async def _settle(n=4):
    for _ in range(n):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# AC 1 — dispatch returns immediately
# ---------------------------------------------------------------------------
class TestDispatchReturnsImmediately:
    def test_the_model_is_answered_before_the_turn_finishes(self):
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        release = asyncio.Event()

        async def _drive():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)) as chat:
                t0 = time.monotonic()
                spoken = await svc._dispatch_task_in_chat(session, "how many PRs are open?")
                assert time.monotonic() - t0 < 0.5
                # The acceptance carries the id and what was started.
                assert spoken.startswith('Started t1: "how many PRs are open?"')
                assert "system notice" in spoken
                assert "t1" in session._background_tasks
                # …and the turn is really running in the thread, as the agent.
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()
            return spoken

        asyncio.run(_drive())
        assert not session._background_tasks

    def test_the_turn_runs_in_the_thread_attributed_to_the_call(self):
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        chat = AsyncMock(return_value={"response": "seventeen"})

        async def _drive():
            with patch("client_portal.service.portal_chat", chat):
                await svc._dispatch_task_in_chat(session, "count them")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        asyncio.run(_drive())
        kwargs = chat.await_args.kwargs
        assert kwargs["session_id"] == "portal-1"
        assert kwargs["email"] == "op@example.com"
        assert kwargs["include_owned"] is True
        # AC 7: the rows carry the call's id — the attribution.
        assert kwargs["voice_call_id"] == "vs_call"

    def test_no_clock_bounds_the_task(self):
        gv = _voice_module()
        src = inspect.getsource(gv.GeminiVoiceService._dispatch_task_in_chat)
        assert "wait_for(" not in src
        assert "asyncio.wait(" not in src

    def test_the_surface_is_told_it_started(self):
        gv = _voice_module()
        session = _session(gv)
        events = []

        async def _on_task(ev): events.append(ev)
        session._on_task_event = _on_task
        svc = _svc(gv)
        release = asyncio.Event()

        async def _drive():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)):
                await svc._dispatch_task_in_chat(session, "go")
                assert events == [{"state": "started", "task_id": "t1", "label": "go", "running": 1}]
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        asyncio.run(_drive())
        assert events[-1]["state"] == "finished" and events[-1]["running"] == 0

    def test_a_blank_prompt_starts_nothing(self):
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        chat = AsyncMock()
        with patch("client_portal.service.portal_chat", chat):
            assert asyncio.run(svc._dispatch_task_in_chat(session, "   ")) == "No prompt provided."
        chat.assert_not_awaited()
        assert not session._background_tasks

    def test_the_label_is_one_line_and_bounded(self):
        gv = _voice_module()
        assert gv._task_label("  count\n\nthe   PRs ") == "count the PRs"
        long = gv._task_label("x" * 500)
        assert len(long) <= gv._TASK_LABEL_MAX and long.endswith("…")


# ---------------------------------------------------------------------------
# AC 6 — concurrency is bounded, with words
# ---------------------------------------------------------------------------
class TestConcurrencyIsBounded:
    def test_the_cap_refuses_with_the_running_tasks_named(self):
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        release = asyncio.Event()

        async def _drive():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)):
                for i in range(gv.MAX_BACKGROUND_TASKS_PER_CALL):
                    out = await svc._dispatch_task_in_chat(session, f"task {i}")
                    assert out.startswith(f"Started t{i + 1}")
                refused = await svc._dispatch_task_in_chat(session, "one more")
                assert refused.startswith("Not started")
                for i in range(gv.MAX_BACKGROUND_TASKS_PER_CALL):
                    assert f't{i + 1} "task {i}"' in refused
                assert len(session._background_tasks) == gv.MAX_BACKGROUND_TASKS_PER_CALL
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()
            # A landed task frees its slot, and ids are never reused.
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "ok"})):
                out = await svc._dispatch_task_in_chat(session, "after")
                assert out.startswith(f"Started t{gv.MAX_BACKGROUND_TASKS_PER_CALL + 1}")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        asyncio.run(_drive())

    def test_the_acceptance_names_what_else_is_running(self):
        # "Is that done yet?" is answerable from what the model was told.
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        release = asyncio.Event()

        async def _drive():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)):
                first = await svc._dispatch_task_in_chat(session, "deck")
                second = await svc._dispatch_task_in_chat(session, "numbers")
                assert "No other task is running." in first
                assert 'Also running: t1 "deck".' in second
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        asyncio.run(_drive())

    def test_the_cap_is_the_stated_proposal(self):
        gv = _voice_module()
        assert gv.MAX_BACKGROUND_TASKS_PER_CALL == 3


# ---------------------------------------------------------------------------
# AC 3 / AC 4 — completion and failure re-enter the call at a natural boundary
# ---------------------------------------------------------------------------
class TestCompletionReentersTheCall:
    def _run_to_landing(self, gv, session, svc, chat, *, before=None, quiet=0.01, hold=5.0):
        async def _drive():
            with patch("client_portal.service.portal_chat", chat), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", quiet), \
                 patch.object(gv, "_NOTICE_MAX_HOLD_SECONDS", hold), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 60):   # keep the nudge out of this test
                if before:
                    before()
                await svc._dispatch_task_in_chat(session, "the deck you asked for")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()], return_exceptions=True)
                for _ in range(40):                              # ≤ ~1 s for the notice loop
                    await asyncio.sleep(0.025)
                    if session._gemini_session.send_realtime_input.await_count:
                        break
        asyncio.run(_drive())

    def test_a_finished_task_is_raised_naming_the_request_and_the_result(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)
        self._run_to_landing(gv, session, svc, AsyncMock(return_value={"response": "twelve slides, done"}))
        said = session._gemini_session.said
        assert len(said) == 1
        assert 't1 ("the deck you asked for") finished' in said[0]
        assert "twelve slides, done" in said[0]
        assert "natural pause" in said[0] and "which request it answers" in said[0]
        # ent#576, written into the notice: once, and not the canvas aloud.
        assert "Do not repeat" in said[0] and "read the canvas aloud" in said[0]

    def test_a_failure_is_raised_with_its_reason_and_the_surface_told(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        events = []

        async def _on_task(ev): events.append(ev)
        session._on_task_event = _on_task
        svc = _svc(gv)
        self._run_to_landing(gv, session, svc, AsyncMock(side_effect=RuntimeError("quota exhausted")))
        said = session._gemini_session.said
        assert len(said) == 1
        assert "failed: quota exhausted" in said[0]
        assert "tell the user once, with the reason" in said[0]
        assert events[-1]["state"] == "failed" and events[-1]["task_id"] == "t1"

    def test_the_result_is_clipped_in_the_notice(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)
        self._run_to_landing(gv, session, svc, AsyncMock(return_value={"response": "y" * 5000}))
        [notice] = session._gemini_session.said
        assert "y" * gv._TASK_RESULT_MAX + "…" in notice
        assert "y" * (gv._TASK_RESULT_MAX + 1) not in notice

    def test_it_waits_while_the_model_is_speaking(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                session._model_speaking = True
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await asyncio.sleep(0.4)
                assert session._gemini_session.send_realtime_input.await_count == 0, "spoke across the model"
                session._model_speaking = False
                await asyncio.sleep(0.4)
                assert session._gemini_session.send_realtime_input.await_count == 1

        asyncio.run(_drive())

    def test_it_waits_while_the_person_is_talking(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.5), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                session._last_user_speech_monotonic = time.monotonic()      # mid-utterance
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await asyncio.sleep(0.2)
                assert session._gemini_session.send_realtime_input.await_count == 0, "cut across the person"
                await asyncio.sleep(0.6)                                    # quiet long enough
                assert session._gemini_session.send_realtime_input.await_count == 1

        asyncio.run(_drive())

    def test_the_hold_is_bounded_so_a_talker_is_still_told(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        def _never_quiet():
            session._model_speaking = True

        self._run_to_landing(gv, session, svc, AsyncMock(return_value={"response": "r"}),
                             before=_never_quiet, hold=0.1)
        assert session._gemini_session.send_realtime_input.await_count == 1

    def test_a_reconnect_in_progress_holds_the_notice_rather_than_dropping_it(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = None          # between provider legs (ent#534 go_away)
        svc = _svc(gv)
        live = _Live()

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0), \
                 patch.object(gv, "_NOTICE_MAX_HOLD_SECONDS", 0.05), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await asyncio.sleep(0.3)
                assert live.send_realtime_input.await_count == 0
                session._gemini_session = live   # the new leg is up
                await asyncio.sleep(0.4)
                assert live.send_realtime_input.await_count == 1

        asyncio.run(_drive())

    def test_a_task_landing_after_the_call_ended_says_nothing_and_loses_nothing(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)
        release = asyncio.Event()
        chat_done = asyncio.Event()

        async def _chat(*_a, **_kw):
            await release.wait()
            chat_done.set()
            return {"response": "late"}

        async def _drive():
            with patch("client_portal.service.portal_chat", _chat), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                session._active = False          # the person hung up
                release.set()
                await asyncio.wait_for(chat_done.wait(), 1)
                await _settle(8)
                # The turn ran to completion (its rows are in the chat)…
                assert chat_done.is_set()
                # …and nothing was spoken into a call that is over.
                assert session._gemini_session.send_realtime_input.await_count == 0

        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# AC 2 — the user is told, always
# ---------------------------------------------------------------------------
class TestTheUserIsToldAlways:
    def _drive(self, gv, session, svc, *, before=None, after=None):
        release = asyncio.Event()

        async def _run():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 0.05):
                if before:
                    before()
                await svc._dispatch_task_in_chat(session, "the numbers")
                if after:
                    after()
                await asyncio.sleep(0.2)
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()
                session._active = False          # stop any notice loop
        asyncio.run(_run())

    def test_silence_after_dispatch_is_nudged(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)
        self._drive(gv, session, svc)
        said = session._gemini_session.said
        assert any('started a task ("the numbers") and have not told the user' in s for s in said)

    def test_a_filler_spoken_just_before_the_call_counts(self):
        # The etiquette asks for exactly this; nudging after it would make the
        # model say the same thing twice — the ent#576 defect.
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        def _spoke_a_moment_ago():
            session._last_assistant_speech_monotonic = time.monotonic() - (gv._ACK_LOOKBACK_SECONDS / 2)

        self._drive(gv, session, svc, before=_spoke_a_moment_ago)
        assert not any("have not told the user" in s for s in session._gemini_session.said)

    def test_speech_after_dispatch_counts(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        def _spoke():
            session._last_assistant_speech_monotonic = time.monotonic()

        self._drive(gv, session, svc, after=_spoke)
        assert not any("have not told the user" in s for s in session._gemini_session.said)

    def test_speech_long_before_the_call_does_not_count(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        def _old_speech():
            session._last_assistant_speech_monotonic = time.monotonic() - (gv._ACK_LOOKBACK_SECONDS * 3)

        self._drive(gv, session, svc, before=_old_speech)
        assert any("have not told the user" in s for s in session._gemini_session.said)

    def test_a_task_that_already_landed_is_not_nudged(self):
        # The completion notice says what happened; a nudge on top would be
        # the second telling.
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _run():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "fast"})), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 0.05), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0):
                await svc._dispatch_task_in_chat(session, "quick one")
                await asyncio.sleep(0.3)
                session._active = False
        asyncio.run(_run())
        said = session._gemini_session.said
        assert not any("have not told the user" in s for s in said)
        assert any("finished" in s for s in said)


# ---------------------------------------------------------------------------
# AC 7 — the call ending does not lose the work
# ---------------------------------------------------------------------------
class TestTheCallEndingLosesNothing:
    def test_end_session_does_not_cancel_a_background_task(self):
        gv = _voice_module()
        svc = gv.GeminiVoiceService()
        session = _session(gv)
        svc._sessions[session.session_id] = session
        release = asyncio.Event()
        finished = asyncio.Event()

        async def _chat(*_a, **_kw):
            await release.wait()
            finished.set()
            return {"response": "landed after the call"}

        async def _drive():
            with patch("client_portal.service.portal_chat", _chat), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                turn = session._background_tasks["t1"].task
                await svc.end_session(session.session_id)
                assert not turn.cancelled()
                release.set()
                await asyncio.wait_for(finished.wait(), 1)
                await _settle()
                assert turn.done() and not turn.cancelled()

        asyncio.run(_drive())

    def test_background_tasks_are_not_in_the_map_end_session_cancels(self):
        gv = _voice_module()
        session = _session(gv)
        svc = _svc(gv)
        release = asyncio.Event()

        async def _drive():
            with patch("client_portal.service.portal_chat", _blocked_chat(release)), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                assert session._background_tasks and not session._pending_tool_tasks
                release.set()
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        asyncio.run(_drive())

    def test_the_attribution_reaches_both_rows(self):
        """`portal_chat` carries `voice_call_id` to the user row AND the reply
        row — typed rows (`source` stays NULL), so they render as ordinary turns
        and the #2694 spoken-delta logic, keyed on `source='voice'`, is untouched."""
        from client_portal import service as portal
        sig = inspect.signature(portal.portal_chat)
        assert "voice_call_id" in sig.parameters and sig.parameters["voice_call_id"].default is None
        src = inspect.getsource(portal.portal_chat)
        assert "_persist_user_turn(agent_name, email, session_id, client_message, voice_call_id=voice_call_id)" in src
        reply_write = src.split("db.add_portal_message(new_message_id", 1)[1].split("message_id = new_message_id", 1)[0]
        assert "**_voice_attribution(voice_call_id)" in reply_write
        user_src = inspect.getsource(portal._persist_user_turn)
        assert "**_voice_attribution(voice_call_id)" in user_src
        # Typed rows: never `source`, and a typed turn's write is unchanged.
        assert "source=" not in user_src and "source=" not in reply_write
        assert portal._voice_attribution(None) == {}
        assert portal._voice_attribution("vs_call") == {"voice_call_id": "vs_call"}


# ---------------------------------------------------------------------------
# Degradation — the container path stays the synchronous fallback
# ---------------------------------------------------------------------------
class TestTheContainerPathIsTheSynchronousFallback:
    def test_an_unbound_session_runs_the_bounded_container_path(self):
        gv = _voice_module()
        session = _session(gv, workspace_mode=False, portal_session_id=None, client_email=None)
        svc = _svc(gv)
        sent = {}

        async def _send(_self, _session, call_id, tool_name, result):
            sent.update(result=result)

        chat = AsyncMock()
        with patch("client_portal.service.portal_chat", chat), \
             patch.object(gv.GeminiVoiceService, "_execute_tool", AsyncMock(return_value="from the container")), \
             patch.object(gv.GeminiVoiceService, "_send_tool_response", _send):
            asyncio.run(svc._execute_and_respond(
                session, "c1", SimpleNamespace(name=gv.RUN_TASK, args={"prompt": "hi"})))
        assert sent["result"] == "from the container"
        chat.assert_not_awaited()
        assert not session._background_tasks

    def test_the_container_path_keeps_its_stated_budget(self):
        gv = _voice_module()
        src = inspect.getsource(gv.GeminiVoiceService._execute_and_respond)
        assert "timeout=30.0" in src


# ---------------------------------------------------------------------------
# The receive loop knows where the floor is
# ---------------------------------------------------------------------------
class _FakeGemini:
    """Yields the frames once; the second `receive()` ends the loop."""
    def __init__(self, frames):
        self._frames = frames
        self._calls = 0

    def receive(self):
        self._calls += 1
        if self._calls > 1:
            raise RuntimeError("closed")

        async def gen():
            for f in self._frames:
                yield f
        return gen()


def _frame(**content):
    base = dict(model_turn=None, interrupted=None, input_transcription=None,
                output_transcription=None, turn_complete=False)
    base.update(content)
    return SimpleNamespace(session_resumption_update=None, go_away=None, tool_call=None,
                           server_content=SimpleNamespace(**base))


class TestTheReceiveLoopTracksTheFloor:
    def _loop(self, gv, frames, **over):
        session = _session(gv, **over)
        session._gemini_session = _FakeGemini(frames)
        asyncio.run(_svc(gv)._receive_audio_loop(session))
        return session

    def test_a_model_turn_takes_the_floor(self):
        gv = _voice_module()
        s = self._loop(gv, [_frame(model_turn=SimpleNamespace(parts=[]))])
        assert s._model_speaking is True

    def test_turn_complete_gives_it_back(self):
        gv = _voice_module()
        s = self._loop(gv, [_frame(model_turn=SimpleNamespace(parts=[])), _frame(turn_complete=True)])
        assert s._model_speaking is False

    def test_an_interruption_gives_it_back_too(self):
        gv = _voice_module()
        s = self._loop(gv, [_frame(model_turn=SimpleNamespace(parts=[])), _frame(interrupted=True)])
        assert s._model_speaking is False

    def test_the_person_speaking_is_timestamped(self):
        gv = _voice_module()
        s = self._loop(gv, [_frame(input_transcription=SimpleNamespace(text="hello"))])
        assert s._last_user_speech_monotonic > 0
        assert s._last_assistant_speech_monotonic == 0

    def test_the_model_speaking_is_timestamped(self):
        gv = _voice_module()
        s = self._loop(gv, [_frame(output_transcription=SimpleNamespace(text="working on it"))])
        assert s._last_assistant_speech_monotonic > 0
        assert s._last_user_speech_monotonic == 0


# ---------------------------------------------------------------------------
# The surface frame
# ---------------------------------------------------------------------------
def test_the_bridge_forwards_task_events_as_a_task_frame():
    from routers import voice as voice_router
    src = inspect.getsource(voice_router.voice_websocket)
    assert '{"type": "task", **event}' in src
    assert "on_task_event=on_task_event" in src
