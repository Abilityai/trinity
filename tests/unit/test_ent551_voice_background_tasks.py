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
                assert "platform notice" in spoken
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

    def test_the_acceptance_says_there_is_no_result_yet(self):
        # First live run: "let me recount those files. There are sixty four
        # files in there right now" — an invented answer, seconds before the
        # real one. The acceptance must say it holds no result.
        gv = _voice_module()
        assert "do not guess or state one" in gv._TASK_ACCEPTED

    def test_the_acceptance_does_not_ask_for_a_second_announcement(self):
        # Second live run: "Let me check the files… / I've started a process to
        # count those files… / That file count is ready" back to back, because
        # the acceptance asked for "one short line about what you started" from
        # a model that had already said its filler. The ack watch covers the
        # silent case; the acceptance must not ask for speech already given.
        gv = _voice_module()
        assert "if you have, do not say it again" in gv._TASK_ACCEPTED
        assert "Say one short line about what you started, then" not in gv._TASK_ACCEPTED

    def test_the_spoken_request_is_recorded_before_the_tasks_rows(self):
        """Second live run: the task's ask row sat ABOVE the spoken request
        that caused it, because spoken rows persist at `turn_complete` and the
        task's rows at dispatch. The dispatcher now flushes the turn in
        progress first."""
        gv = _voice_module()
        session = _session(gv)
        session._partial_user_text = "count the files please"
        session._partial_assistant_text = "let me count those"
        order = []

        async def _on_turn(role, text): order.append(("turn", role, text))
        session._on_turn = _on_turn

        async def _chat(*_a, **kw):
            order.append(("task", kw.get("voice_call_id")))
            return {"response": "ten"}

        async def _drive():
            with patch("client_portal.service.portal_chat", _chat), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "count the files")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await _settle()

        svc = _svc(gv)
        asyncio.run(_drive())
        assert order[:3] == [
            ("turn", "user", "count the files please"),
            ("turn", "assistant", "let me count those"),
            ("task", "vs_call"),
        ]
        assert session._partial_user_text == "" and session._partial_assistant_text == ""

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
        # ent#576, written into the notice: once, short, and the canvas is
        # pointed at (after drawing) rather than read.
        assert "Do not repeat" in said[0] and "one or two sentences" in said[0]
        assert "point at it rather than reading it" in said[0]

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
# The acceptance is context, not a turn (the structural fix for "said twice")
# ---------------------------------------------------------------------------
class TestTheAcceptanceIsNotATurn:
    """Third live run: "Let me count the files for you." → task → "I'm checking
    on that now. Anything else you're curious about while we wait?" — a second
    line after the filler, no matter how the acceptance was worded, because a
    blocking call's result IS a turn and the model answers it. The Live API's
    own shape: declare the function NON_BLOCKING and return the acceptance
    SILENT. Verified live: one line before the call, nothing after.
    """

    def test_a_workspace_session_declares_run_task_non_blocking(self):
        gv = _voice_module()
        t = gv.genai_types                      # the real SDK, or the shared stub
        cfg = _svc(gv)._build_live_config(_session(gv, tool_manifest=frozenset({gv.RUN_TASK})))
        [decl] = [d for tool in cfg.tools for d in tool.function_declarations]
        assert decl.behavior == t.Behavior.NON_BLOCKING

    def test_a_thread_less_session_keeps_the_blocking_declaration(self):
        # VoIP / legacy Agent Detail: the result IS the answer and must be spoken.
        gv = _voice_module()
        t = gv.genai_types
        session = _session(gv, workspace_mode=False, portal_session_id=None, client_email=None,
                           tool_manifest=frozenset({gv.RUN_TASK}))
        cfg = _svc(gv)._build_live_config(session)
        [decl] = [d for tool in cfg.tools for d in tool.function_declarations]
        assert decl.behavior in (None, t.Behavior.BLOCKING)
        assert gv._RUN_TASK_TOOL.function_declarations[0].behavior in (None, t.Behavior.BLOCKING)

    def _dispatch(self, gv, session, chat):
        live = _Live()
        live.send_tool_response = AsyncMock()
        session._gemini_session = live
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", chat), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._execute_and_respond(session, "c1", SimpleNamespace(name=gv.RUN_TASK, args={"prompt": "count"}))
                await asyncio.gather(*[b.task for b in session._background_tasks.values() if b.task is not None],
                                     return_exceptions=True)
                await _settle()
                session._active = False
        asyncio.run(_drive())
        [call] = live.send_tool_response.await_args_list
        [fr] = call.kwargs["function_responses"]
        return fr

    def test_the_accepted_dispatch_is_returned_silent(self):
        gv = _voice_module()
        fr = self._dispatch(gv, _session(gv), AsyncMock(return_value={"response": "ten"}))
        assert fr.response["output"].startswith(gv._ACCEPTED_PREFIX)
        assert fr.scheduling == gv.genai_types.FunctionResponseScheduling.SILENT

    def test_a_refusal_is_spoken_not_silent(self):
        gv = _voice_module()
        session = _session(gv)
        for i in range(gv.MAX_BACKGROUND_TASKS_PER_CALL):
            session._background_tasks[f"t{i}"] = gv.BackgroundTask(f"t{i}", "x", "x", 0.0)
        fr = self._dispatch(gv, session, AsyncMock(return_value={"response": "never"}))
        assert fr.response["output"].startswith("Not started")
        assert fr.scheduling is None

    def test_the_container_path_result_is_spoken(self):
        gv = _voice_module()
        session = _session(gv, workspace_mode=False, portal_session_id=None, client_email=None)
        live = _Live(); live.send_tool_response = AsyncMock()
        session._gemini_session = live
        with patch.object(gv.GeminiVoiceService, "_execute_tool", AsyncMock(return_value="from the container")):
            asyncio.run(_svc(gv)._execute_and_respond(session, "c1", SimpleNamespace(name=gv.RUN_TASK, args={"prompt": "hi"})))
        [fr] = live.send_tool_response.await_args_list[0].kwargs["function_responses"]
        assert fr.response["output"] == "from the container" and fr.scheduling is None


class TestTheNoticeKeepsTheCanvasHonest:
    def test_a_drawing_session_is_told_the_result_is_not_on_the_canvas(self):
        # Third live run: "the canvas shows the breakdown of your files" and
        # "I've put the weather up there" — with no canvas call and the canvas
        # row untouched since 2026-09-07.
        gv = _voice_module()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        session = _session(gv, tool_manifest=PLATFORM_VOICE_TOOLS)
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                for _ in range(40):
                    await asyncio.sleep(0.025)
                    if session._gemini_session.send_realtime_input.await_count:
                        break
        asyncio.run(_drive())
        [notice] = session._gemini_session.said
        assert "NOT on the canvas" in notice
        assert "put it on the canvas with `show_markdown` BEFORE you speak" in notice
        assert "one or two sentences" in notice

    def test_a_session_without_a_canvas_gets_no_canvas_hint(self):
        gv = _voice_module()
        session = _session(gv, tool_manifest=frozenset({gv.RUN_TASK}))
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                for _ in range(40):
                    await asyncio.sleep(0.025)
                    if session._gemini_session.send_realtime_input.await_count:
                        break
        asyncio.run(_drive())
        [notice] = session._gemini_session.said
        assert "show_markdown" not in notice


# ---------------------------------------------------------------------------
# A platform notice the model reads aloud is not the agent's line
# ---------------------------------------------------------------------------
class TestAnEchoedNoticeIsNotTranscribed:
    """Second live run: the assistant row at 10:45:54 was the raw notice —
    `[System notice: background task t3 … finished…` — read aloud verbatim as
    the person hung up. The speech cannot be unsaid; the transcript can refuse
    to record the platform's words as the agent's."""

    def test_every_platform_text_opens_with_the_marker_and_the_instruction(self):
        gv = _voice_module()
        for text in (gv._ACK_NUDGE, gv._TASK_DONE_NOTICE, gv._TASK_FAILED_NOTICE):
            assert text.startswith(gv._NOTICE_OPEN)
            assert "never read this aloud" in text
        assert gv._CAP_WARNING_TEXT.startswith(gv._PLATFORM_NOTICE_MARKERS)

    def test_the_scrub_drops_the_notice_and_keeps_what_followed(self):
        gv = _voice_module()
        assert gv._scrub_platform_notice("[Platform notice — x. Result: \"\"\"y\"\"\"]") == ""
        assert gv._scrub_platform_notice("[System notice: wrap up.] Okay, we have to stop soon.") == "Okay, we have to stop soon."
        assert gv._scrub_platform_notice("  [Platform notice — unterminated") == ""
        assert gv._scrub_platform_notice("There are ten files.") == "There are ten files."

    def test_record_turn_drops_a_notice_only_assistant_row_and_keeps_a_users_words(self):
        gv = _voice_module()
        session = _session(gv)
        seen = []

        async def _on_turn(role, text): seen.append((role, text))
        session._on_turn = _on_turn
        svc = _svc(gv)
        asyncio.run(svc._record_turn(session, "assistant", gv._TASK_DONE_NOTICE.format(task_id="t3", label="x", result="r", canvas="")))
        asyncio.run(svc._record_turn(session, "assistant", "[System notice: wrap up.] We have to stop soon."))
        # A person quoting the marker is still the person.
        asyncio.run(svc._record_turn(session, "user", "[System notice: is that what it said?"))
        assert seen == [("assistant", "We have to stop soon."), ("user", "[System notice: is that what it said?")]
        assert [e.text for e in session.transcript] == ["We have to stop soon.", "[System notice: is that what it said?"]


class TestACompletionWaitsForBothSidesToBeQuiet:
    def test_the_models_own_speech_counts_as_noise(self):
        # Second live run: with a task finishing in seconds the result landed
        # on the heels of the model's acknowledgement and the person never got
        # a gap. The quiet window is measured against the assistant's last
        # speech too, and is long enough to speak into.
        gv = _voice_module()
        assert gv._NOTICE_QUIET_SECONDS >= 2.0
        src = inspect.getsource(gv.GeminiVoiceService._deliver_task_notice)
        assert "_last_assistant_speech_monotonic" in src and "_last_user_speech_monotonic" in src

    def test_it_waits_while_the_model_just_spoke(self):
        gv = _voice_module()
        session = _session(gv)
        session._gemini_session = _Live()
        svc = _svc(gv)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.5), \
                 patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                session._last_assistant_speech_monotonic = time.monotonic()   # it is finishing a sentence
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                await asyncio.sleep(0.2)
                assert session._gemini_session.send_realtime_input.await_count == 0
                await asyncio.sleep(0.7)
                assert session._gemini_session.send_realtime_input.await_count == 1

        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# The call's own turn passes the #2694 live-call guard
# ---------------------------------------------------------------------------
class TestTheCallsOwnTurnPassesTheLiveCallGuard:
    """Found in the first live run: every `run_task` failed at once with
    "A voice call is on in this chat — end it, then send." #2694 landed on dev
    after ent#535 and its guard — right for a typed turn from a second tab —
    refused the call's own turns too, so a call could not run a single task.
    """

    def test_a_typed_turn_is_still_refused_during_a_call(self, monkeypatch):
        from client_portal import service as portal, voice as pv
        monkeypatch.setattr(pv, "voice_call_active", lambda sid: True)
        with pytest.raises(portal.ClientPortalError) as ei:
            portal._refuse_turn_during_voice_call("thread-1")
        assert ei.value.category == "voice_call_active"

    def test_the_calls_own_turn_passes(self, monkeypatch):
        from client_portal import service as portal, voice as pv
        monkeypatch.setattr(pv, "voice_call_active", lambda sid: True)
        portal._refuse_turn_during_voice_call("thread-1", voice_call_id="vs_call")   # no raise

    def test_portal_chat_hands_the_guard_the_call_id(self):
        from client_portal import service as portal
        src = inspect.getsource(portal.portal_chat)
        assert "_refuse_turn_during_voice_call(session_id, voice_call_id=voice_call_id)" in src
        # The streaming entry is typed-only and keeps the plain refusal.
        stream_src = inspect.getsource(portal.portal_chat_stream) if hasattr(portal, "portal_chat_stream") else ""
        assert "voice_call_id=voice_call_id" not in stream_src.split("_refuse_turn_during_voice_call(", 1)[-1][:60]

    def test_the_only_writer_of_the_call_id_is_the_voice_dispatcher(self):
        # The bypass is safe because nothing a request can carry sets the id.
        from client_portal import router
        assert "voice_call_id" not in inspect.getsource(router)


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


# ---------------------------------------------------------------------------
# The model knows what is on the canvas
# ---------------------------------------------------------------------------
class TestTheCanvasIsInContext:
    """Sixth live run: "I don't have a current table on the canvas" — with the
    sales table on screen. The call starts with a text rendering of the canvas,
    and a task that changed the canvas says so when it lands."""

    CANVAS = {
        "title": "Pipeline", "updated_at": "2026-09-12T11:53:00Z",
        "blocks": [
            {"id": "b1", "kind": "markdown", "slot": "header", "payload": {"markdown": "## Pipeline · week 36\nUpdated hourly."}},
            {"id": "b2", "kind": "kpi", "payload": {"tiles": [{"label": "Open", "value": 42}, {"label": "Won", "value": 7, "unit": "this week"}]}},
            {"id": "b3", "kind": "chart", "payload": {"type": "bar", "series": [{"label": "Won", "points": [{"ts": "W36", "value": 7}]}]}},
            {"id": "b4", "kind": "table", "title": "Stalled", "payload": {"columns": ["Deal", "Value"], "rows": [["Acme", "$48k"], ["Globex", "$31k"]]}},
            {"id": "voice", "kind": "markdown", "payload": {"markdown": "| Quarter | FY26 |\n|---|---|\n| Q1 | $2.4M |"}},
            {"id": "b5", "kind": "html", "payload": {"html": "<div class=\"ck-card\"><b>Needs a decision</b> Two deals idle.</div>"}},
        ],
    }

    def test_every_block_is_one_readable_line(self):
        gv = _voice_module()
        text = gv.canvas_context_text(self.CANVAS)
        assert text.startswith("Title: Pipeline")
        assert "- markdown [header]: ## Pipeline · week 36 Updated hourly." in text
        assert "- kpi: Open: 42; Won: 7 this week" in text
        assert "- chart: bar chart; series Won" in text
        assert '- table “Stalled”: columns Deal, Value; 2 rows; first row ["Acme", "$48k"]' in text
        assert "(your voice block): | Quarter | FY26 |" in text
        assert "- html: Needs a decision Two deals idle." in text          # tags stripped

    def test_an_empty_or_missing_canvas_says_so(self):
        gv = _voice_module()
        assert gv.canvas_context_text(None) == "The canvas is empty."
        assert gv.canvas_context_text({"blocks": []}) == "The canvas is empty."

    def test_the_text_is_bounded(self):
        gv = _voice_module()
        big = {"blocks": [{"kind": "markdown", "payload": {"markdown": "x" * 400}} for _ in range(30)]}
        assert len(gv.canvas_context_text(big, limit=1500)) <= 1500

    def test_the_workspace_call_starts_with_the_canvas_in_its_prompt(self):
        import inspect
        from client_portal import voice as pv
        src = inspect.getsource(pv.start_workspace_voice)
        assert "prompt += canvas_context_section(agent_name)" in src
        gv = _voice_module()
        with patch("database.db.get_agent_canvas", return_value=self.CANVAS):
            section = gv.canvas_context_section("scout")
        assert section.startswith("\n\n## On the canvas now")
        assert "Title: Pipeline" in section
        with patch("database.db.get_agent_canvas", side_effect=RuntimeError("db down")):
            assert gv.canvas_context_section("scout") == ""             # best-effort, never blocks the call

    def _land(self, gv, session, states):
        """Drive one task to its notice with `_canvas_state` answering `states` in order."""
        session._gemini_session = _Live()
        svc = _svc(gv)
        answers = iter(states)

        async def _drive():
            with patch("client_portal.service.portal_chat", AsyncMock(return_value={"response": "r"})), \
                 patch.object(gv.GeminiVoiceService, "_canvas_state", lambda _self, _a: next(answers)), \
                 patch.object(gv, "_NOTICE_QUIET_SECONDS", 0.0), patch.object(gv, "_ACK_WINDOW_SECONDS", 60):
                await svc._dispatch_task_in_chat(session, "x")
                await asyncio.gather(*[b.task for b in session._background_tasks.values()])
                for _ in range(40):
                    await asyncio.sleep(0.025)
                    if session._gemini_session.send_realtime_input.await_count:
                        break
        asyncio.run(_drive())
        [notice] = session._gemini_session.said
        return notice

    def test_a_task_that_changed_the_canvas_says_what_it_shows_now(self):
        gv = _voice_module()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        notice = self._land(gv, _session(gv, tool_manifest=PLATFORM_VOICE_TOOLS),
                            [("t0", "old"), ("t1", "- markdown: the new table")])
        assert "The canvas changed while this task ran and now shows:" in notice
        assert "- markdown: the new table" in notice
        assert "show_markdown` BEFORE" not in notice

    def test_an_unchanged_canvas_keeps_the_draw_it_yourself_hint(self):
        gv = _voice_module()
        from services.voice_tools import PLATFORM_VOICE_TOOLS
        notice = self._land(gv, _session(gv, tool_manifest=PLATFORM_VOICE_TOOLS), [("t0", "same"), ("t0", "same")])
        assert "NOT on the canvas" in notice and "show_markdown` BEFORE" in notice
        assert "changed while this task ran" not in notice
