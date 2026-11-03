"""trinity-enterprise#1046: Workspace Direct calls.

Pinned here, each at its own layer:
  * `speech.spoken_text`: what is read aloud, and how a stream reconciles with the
    persisted reply;
  * `direct_voice_service.normalize_stream_entry`: the three runtimes;
  * `DirectBridge`: every runtime speaks; nothing repeats; hearing bounds; a stranger's
    execution or ask stays silent; hang-up leaves the turn alone;
  * `turn_may_join_call`: a call id from a request is checked in full;
  * `client_portal.db`: Direct rows do not re-enter the #2694 delta.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_2694_voice_delta_context import AGENT as P_AGENT, ALICE as P_ALICE, SESSION as P_SESSION, portal  # noqa: E402,F401

from services import direct_voice_service as dvs  # noqa: E402
from services.speech.base import SpeechError, Transcript, pcm_to_padded_wav  # noqa: E402
from services.speech.spoken_text import (  # noqa: E402
    REST_IN_CHAT,
    SpokenBudget,
    sentences,
    speakable,
    unspoken_tail,
)

CLAUDE_ASSISTANT = {
    "type": "assistant", "parent_tool_use_id": None,
    "message": {"content": [{"type": "text", "text": "Let me check. "},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "date"}}]},
}
CLAUDE_SUBAGENT = {
    "type": "assistant", "parent_tool_use_id": "toolu_1",
    "message": {"content": [{"type": "text", "text": "Internal subagent chatter."}]},
}
# The shape `codex_runtime._process_codex_event` reads.
CODEX_MESSAGE = {"type": "item.completed", "item": {"type": "agent_message", "text": "Done with that."}}
CODEX_TOOL = {"type": "item.started", "item": {"type": "command_execution", "command": "date"}}


# ---- spoken_text --------------------------------------------------------------

def test_speakable_removes_what_should_only_be_seen():
    out = speakable(
        "Here it is.\n<screen>secret table</screen>\n```python\nprint(1)\n```\n"
        "See [the docs](https://example.com/x) or https://example.com/y.\n"
        "| a | b |\n**Bold** words."
    )
    assert "secret table" not in out and "print" not in out and "|" not in out
    assert "example.com" not in out and "**" not in out
    assert "the docs" in out and "Bold words." in out


def test_an_unclosed_screen_block_is_not_read_aloud():
    assert "hidden" not in speakable("Said. <screen>hidden and never closed")


def test_sentences_split_without_breaking_abbreviations():
    assert sentences("First one. Second, e.g. this one! Third?") == [
        "First one.", "Second, e.g. this one!", "Third?"]


def test_unspoken_tail_never_repeats_and_never_loses_the_end():
    spoken = ["Let me check."]
    assert unspoken_tail("Let me check. The date is Monday.", spoken) == ["The date is Monday."]
    assert unspoken_tail("All done.", ["Let me check.", "All done."]) == []


def test_unspoken_tail_keeps_a_real_repeat():
    assert unspoken_tail("Yes. Yes.", ["Yes."]) == ["Yes."]


def test_budget_closes_once_with_the_fixed_line():
    budget = SpokenBudget(limit=10)
    assert budget.admit("short") == "short"
    assert budget.admit("much too long now") == REST_IN_CHAT
    assert budget.admit("anything") is None


def test_stt_audio_is_padded_with_silence():
    wav = pcm_to_padded_wav(b"\x01\x00" * 100)
    assert len(wav) > 16000 * 2 * 1.4


# ---- runtimes -----------------------------------------------------------------

def test_claude_entry_yields_text_and_tool():
    items = dvs.normalize_stream_entry("claude-code", CLAUDE_ASSISTANT)
    assert [i.text for i in items if i.text] == ["Let me check. "]
    assert [i.tool for i in items if i.tool] == ["Bash"]


def test_subagent_chatter_is_never_spoken():
    assert dvs.normalize_stream_entry("claude-code", CLAUDE_SUBAGENT) == []


def test_codex_entries():
    assert dvs.normalize_stream_entry("codex", CODEX_MESSAGE)[0].text == "Done with that."
    assert dvs.normalize_stream_entry("codex", CODEX_TOOL)[0].tool == "Shell"


def test_a_runtime_without_a_stream_yields_nothing_and_says_so():
    assert dvs.normalize_stream_entry("gemini-cli", CLAUDE_ASSISTANT) == []
    assert dvs.supports_early_speech("gemini-cli") is False
    assert dvs.supports_early_speech("claude-code") and dvs.supports_early_speech("codex")


# ---- the bridge ---------------------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)

    def of(self, kind):
        return [f for f in self.frames if f["type"] == kind]


class FakeProvider:
    name = "fake"

    def __init__(self, transcript="hello", stt_error=False, tts_error=False):
        self.transcript = transcript
        self.stt_error = stt_error
        self.tts_error = tts_error
        self.said = []
        self.heard = []

    async def transcribe(self, pcm):
        self.heard.append(pcm)
        if self.stt_error:
            raise SpeechError("down")
        return Transcript(self.transcript)

    async def synthesize_stream(self, text, voice=None):
        if self.tts_error:
            raise SpeechError("down")
            yield b""
        self.said.append(text)
        yield b"\x00\x01"


def _session():
    return dvs.DirectVoiceSession(session_id="vd_x", agent_name="scribe", user_id=1,
                                  client_email="alice@example.com", portal_session_id="thread",
                                  max_duration=60)


def _stream(*entries):
    def attach(_agent, _execution):
        async def gen():
            for e in entries:
                yield f"data: {json.dumps(e)}\n\n"
            yield 'data: {"type": "stream_end"}\n\n'
        return gen()
    return attach


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(dvs, "REPLY_POLL_SECONDS", 0)
    monkeypatch.setattr(dvs, "REPLY_SETTLE_SECONDS", 0)
    monkeypatch.setattr("services.runtime_secret_scrub.get_staged_values", lambda: [])
    return monkeypatch


def _turn(patched, *, runtime, entries=(), status="success", reply=None, gate_hold=False, provider=None):
    row = {"content": reply, "gate_hold": gate_hold} if reply is not None else None
    patched.setattr(dvs, "_read_turn_end", lambda _e: (status, row))
    socket = FakeSocket()
    provider = provider or FakeProvider()

    async def go():
        bridge = dvs.DirectBridge(socket, _session(), provider, _stream(*entries), runtime=runtime)
        speaker = asyncio.ensure_future(bridge.run_speaker())
        await bridge.speak_execution("e1")
        while not bridge._speech.empty():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        speaker.cancel()
        return bridge

    asyncio.run(go())
    return socket, provider


def test_a_runtime_with_no_stream_still_speaks_the_finished_reply(patched):
    _, provider = _turn(patched, runtime="gemini-cli", reply="It is Monday. Anything else?")
    assert provider.said == ["It is Monday.", "Anything else?"]


def test_a_streaming_runtime_speaks_early_and_does_not_repeat(patched):
    socket, provider = _turn(patched, runtime="claude-code", entries=[CLAUDE_ASSISTANT],
                             reply="Let me check. It is Monday.")
    assert provider.said == ["Let me check.", "It is Monday."]
    assert socket.of("activity")[0]["tool"] == "Bash"


def test_subagent_text_in_a_real_turn_stays_silent(patched):
    _, provider = _turn(patched, runtime="claude-code", entries=[CLAUDE_SUBAGENT], reply="Done.")
    assert provider.said == ["Done."]


def test_codex_speaks_through_its_own_shape(patched):
    _, provider = _turn(patched, runtime="codex", entries=[CODEX_MESSAGE], reply="Done with that.")
    assert provider.said == ["Done with that."]


def test_a_failed_turn_gets_the_fixed_line_and_nothing_invented(patched):
    _, provider = _turn(patched, runtime="claude-code", status="failed", reply=None)
    assert provider.said == [dvs.LINE_TURN_FAILED]


def test_a_reply_with_nothing_to_say_aloud_still_answers(patched):
    # A reply that is only code (test-echo's fenced echo), a table or links has no
    # speakable sentence. Silence there reads as a dead call; the fixed line says
    # where the reply is.
    _, provider = _turn(patched, runtime="claude-code",
                        reply="```\nEcho: Tell me something please.\nWords: 4\n```")
    assert provider.said == [dvs.LINE_REPLY_IN_CHAT]


def test_a_spoken_reply_does_not_also_get_the_in_chat_line(patched):
    _, provider = _turn(patched, runtime="claude-code", reply="Done.\n\n```\nls\n```")
    assert provider.said == ["Done."]


def test_a_cancelled_turn_is_silent(patched):
    _, provider = _turn(patched, runtime="claude-code", status="cancelled", reply=None)
    assert provider.said == []


def test_a_held_turn_does_not_read_the_platform_notice(patched):
    _, provider = _turn(patched, runtime="claude-code", reply="Not run. Waiting on approval.", gate_hold=True)
    assert provider.said == []


def test_a_long_reply_is_cut_with_the_fixed_line_and_stays_whole_in_the_chat(patched):
    long_reply = " ".join(f"This is sentence number {i} of a long reply." for i in range(80))
    _, provider = _turn(patched, runtime="gemini-cli", reply=long_reply)
    assert provider.said[-1] == REST_IN_CHAT
    assert sum(len(s) for s in provider.said) < 1300


def test_speech_failure_says_so_once_and_the_call_goes_on(patched):
    socket, _ = _turn(patched, runtime="gemini-cli", reply="One. Two. Three.",
                      provider=FakeProvider(tts_error=True))
    notices = socket.of("notice")
    assert len(notices) == 1 and notices[0]["code"] == "tts_failed"
    assert socket.of("speak_state")[-1]["state"] == "idle"


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()


def _bridge(provider=None, clock=None):
    socket = FakeSocket()
    kwargs = {"clock": clock} if clock else {}
    return socket, dvs.DirectBridge(socket, _session(), provider or FakeProvider(), _stream(), **kwargs)


def _pcm():
    return base64.b64encode(b"\x00\x01" * 10).decode("ascii")


def test_hearing_returns_the_transcript_and_starts_nothing():
    socket, bridge = _bridge(FakeProvider(transcript="what time is it"))
    asyncio.run(bridge.hear(1, _pcm()))
    assert socket.of("transcript_live")[0]["text"] == "what time is it"
    assert "stt_ms" in socket.of("transcript_live")[0]
    assert bridge.turns == 0


def test_an_oversized_utterance_never_reaches_the_provider():
    provider = FakeProvider()
    socket, bridge = _bridge(provider)
    asyncio.run(bridge.hear(1, "A" * (dvs.MAX_UTTERANCE_B64 + 1)))
    assert socket.of("not_heard")[0]["reason"] == "too_long" and provider.heard == []


def test_bad_audio_is_refused():
    socket, bridge = _bridge()
    asyncio.run(bridge.hear(1, "not base64 !!"))
    assert socket.of("not_heard")[0]["reason"] == "bad_audio"


def test_utterances_are_rate_limited():
    from services import rate_limiter
    rate_limiter.clear_inprocess()
    provider = FakeProvider()
    socket, bridge = _bridge(provider, clock=lambda: 100.0)

    async def go():
        for i in range(dvs.MAX_UTTERANCES_PER_MINUTE + 1):
            await bridge.hear(i, _pcm())

    asyncio.run(go())
    assert len(provider.heard) == dvs.MAX_UTTERANCES_PER_MINUTE
    assert socket.of("not_heard")[-1]["reason"] == "rate_limited"


def test_the_limit_is_per_person_across_their_calls():
    from services import rate_limiter
    rate_limiter.clear_inprocess()
    first, second = FakeProvider(), FakeProvider()
    _, a = _bridge(first, clock=lambda: 100.0)
    socket_b, b = _bridge(second, clock=lambda: 100.0)

    async def go():
        for i in range(dvs.MAX_UTTERANCES_PER_MINUTE):
            await a.hear(i, _pcm())
        await b.hear(0, _pcm())

    asyncio.run(go())
    assert second.heard == [] and socket_b.of("not_heard")[0]["reason"] == "rate_limited"


def test_transcription_failure_is_said_and_starts_nothing():
    socket, bridge = _bridge(FakeProvider(stt_error=True))
    asyncio.run(bridge.hear(1, _pcm()))
    assert socket.of("not_heard")[0]["reason"] == "stt_failed"
    assert bridge._speech.get_nowait()[1] == dvs.LINE_NOT_HEARD


def test_silence_is_ignored_without_a_word():
    socket, bridge = _bridge(FakeProvider(transcript=""))
    asyncio.run(bridge.hear(1, _pcm()))
    assert socket.of("not_heard")[0]["reason"] == "empty" and bridge._speech.empty()


def test_a_strangers_execution_is_never_spoken(monkeypatch):
    socket, bridge = _bridge()
    monkeypatch.setattr(bridge, "_authorized_execution", lambda _e: False)
    started = []
    monkeypatch.setattr(bridge, "_start_turn", started.append)
    asyncio.run(bridge.speak("someone-elses"))
    assert started == [] and bridge.turns == 0


def test_the_ownership_check_is_the_portals_own_rule_on_this_calls_person(monkeypatch):
    from client_portal import service as portal
    asked = []

    def belongs(execution_id, agent_name, email):
        asked.append((execution_id, agent_name, email))
        return False

    monkeypatch.setattr(portal, "execution_belongs_to_caller", belongs)
    _, bridge = _bridge()
    started = []
    monkeypatch.setattr(bridge, "_start_turn", started.append)
    asyncio.run(bridge.speak("someone-elses"))
    assert started == []
    assert asked == [("someone-elses", bridge.session.agent_name, bridge.session.client_email)]


def test_interrupt_drops_what_is_queued_and_old_speech_cannot_play():
    socket, bridge = _bridge()

    async def go():
        bridge.enqueue("one")
        stale_generation = bridge._generation
        bridge.interrupt()
        assert bridge._speech.empty()
        await bridge._speak_sentence(stale_generation, "stale")

    asyncio.run(go())
    assert socket.of("audio") == []


def test_interrupt_frame_tells_the_browser_to_flush():
    socket, bridge = _bridge()
    assert asyncio.run(bridge.handle({"type": "interrupt"})) is True
    assert socket.of("audio_flush")


def test_end_frame_ends_the_call():
    _, bridge = _bridge()
    assert asyncio.run(bridge.handle({"type": "end"})) is False


def test_a_strangers_ask_stays_silent(monkeypatch):
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_read_ask", lambda _a: None)
    asyncio.run(bridge.speak_ask("not-mine"))
    assert bridge._speech.empty()


def test_ask_line_names_the_persons_own_request():
    line = dvs.ask_line({"type": "approval", "title": "Approve merge on trinity",
                         "proposal": {"input": "merge PR 4099"}})
    assert "merge PR 4099" in line and line.endswith("Yes or no?")


def test_a_question_is_read_but_not_offered_as_yes_or_no():
    line = dvs.ask_line({"type": "question", "title": "Which account?"})
    assert "Which account?" in line and "Yes or no" not in line


def test_rejecting_says_so_and_runs_nothing(monkeypatch):
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_read_ask", lambda _a: {"request_id": "r"})
    started = []
    monkeypatch.setattr(bridge, "_start_turn", started.append)
    asyncio.run(bridge.ask_followup("a1", "rejected"))
    assert bridge._speech.get_nowait()[1] == dvs.LINE_REJECTED and started == []


def test_approving_speaks_the_run_it_started(monkeypatch):
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_read_ask", lambda _a: {"request_id": "r"})

    async def run(_row):
        return "e9"

    monkeypatch.setattr(bridge, "_approved_run", run)
    monkeypatch.setattr(bridge, "_authorized_execution", lambda _e: True)
    started = []
    monkeypatch.setattr(bridge, "_start_turn", started.append)
    asyncio.run(bridge.ask_followup("a1", "approved"))
    assert started == ["e9"]


def test_an_approver_never_hears_another_persons_run(monkeypatch):
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_read_ask", lambda _a: {"request_id": "r"})

    async def run(_row):
        return "their-run"

    monkeypatch.setattr(bridge, "_approved_run", run)
    monkeypatch.setattr(bridge, "_authorized_execution", lambda _e: False)
    started = []
    monkeypatch.setattr(bridge, "_start_turn", started.append)
    asyncio.run(bridge.ask_followup("a1", "approved"))
    assert started == [] and bridge._speech.get_nowait()[1] == dvs.LINE_APPROVED_LATER


def test_approving_with_no_run_yet_says_where_it_will_land(monkeypatch):
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_read_ask", lambda _a: {"request_id": "r"})

    async def run(_row):
        return None

    monkeypatch.setattr(bridge, "_approved_run", run)
    asyncio.run(bridge.ask_followup("a1", "approved"))
    assert bridge._speech.get_nowait()[1] == dvs.LINE_APPROVED_LATER


# ---- sessions and the id on the chat route ---------------------------------------

@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(dvs, "_redis", lambda: None)
    dvs._local_sessions.clear()


def _call(no_redis_unused=None):
    return dvs.create_session(agent_name="scribe", user_id=1, client_email="alice@example.com",
                              portal_session_id="thread", max_duration=60)


def test_a_turn_joins_only_the_callers_own_call(no_redis):
    call = _call()
    ok = dict(agent_name="scribe", email="alice@example.com", portal_session_id="thread")
    assert dvs.turn_may_join_call(call.session_id, **ok)
    assert not dvs.turn_may_join_call(call.session_id, **{**ok, "email": "bob@example.com"})
    assert not dvs.turn_may_join_call(call.session_id, **{**ok, "agent_name": "other"})
    assert not dvs.turn_may_join_call(call.session_id, **{**ok, "portal_session_id": "another"})
    assert not dvs.turn_may_join_call(call.session_id, **{**ok, "portal_session_id": None})


def test_a_live_call_id_can_never_be_claimed_from_a_request(no_redis):
    assert not dvs.turn_may_join_call("vs_abc", agent_name="scribe", email="alice@example.com",
                                      portal_session_id="thread")


class _Store:
    def __init__(self):
        self.data = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


def test_a_call_ended_on_the_other_worker_is_over_here_too(monkeypatch):
    store = _Store()
    monkeypatch.setattr(dvs, "_redis", lambda: store)
    dvs._local_sessions.clear()
    call = _call()
    store.delete(dvs._session_key(call.session_id))   # the socket's worker hung up
    assert dvs.get_session(call.session_id) is None
    assert call.session_id not in dvs._local_sessions


def test_the_local_copy_serves_only_while_redis_is_unreachable(monkeypatch):
    class Down:
        def get(self, key):
            raise ConnectionError("down")

        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(dvs, "_redis", lambda: Down())
    dvs._local_sessions.clear()
    call = _call()
    assert dvs.get_session(call.session_id).session_id == call.session_id


def test_a_session_whose_socket_never_opened_expires(no_redis):
    call = _call()
    call.started_at -= call.max_duration + dvs.SESSION_GRACE_SECONDS + 1
    assert dvs.get_session(call.session_id) is None
    assert call.session_id not in dvs._local_sessions


def test_a_second_socket_on_the_thread_is_refused_and_its_owner_keeps_the_lease(monkeypatch):
    store = _Store()
    monkeypatch.setattr(dvs, "_redis", lambda: store)
    assert dvs.claim_call("t", "vd_a") is True
    assert dvs.claim_call("t", "vd_b") is False
    assert dvs.claim_call("t", "vd_a") is True
    socket = ScriptedSocket([])
    session = _session()
    session.session_id = "vd_b"
    session.portal_session_id = "t"
    asyncio.run(dvs.run_bridge(socket, session, provider=FakeProvider(), stream_attach=_stream()))
    assert socket.of("status")[0]["reason"] == "busy" and socket.of("saved")[0]["rows"] == 0
    assert store.get(dvs._active_key("t")) == "vd_a"


def test_a_session_survives_landing_on_the_other_worker(monkeypatch):
    store = {}

    class FakeRedis:
        def set(self, key, value, ex=None):
            store[key] = value

        def get(self, key):
            return store.get(key)

        def delete(self, key):
            store.pop(key, None)

    monkeypatch.setattr(dvs, "_redis", lambda: FakeRedis())
    dvs._local_sessions.clear()
    call = _call()
    dvs._local_sessions.clear()   # the other worker never had it in memory
    found = dvs.get_session(call.session_id)
    assert found.client_email == "alice@example.com" and found.portal_session_id == "thread"


def test_a_call_marker_is_only_cleared_by_its_owner(monkeypatch):
    store = {}

    class FakeRedis:
        def set(self, key, value, ex=None):
            store[key] = value

        def get(self, key):
            return store.get(key)

        def delete(self, key):
            store.pop(key, None)

    monkeypatch.setattr(dvs, "_redis", lambda: FakeRedis())
    dvs.mark_call_active("t", "vd_new")
    dvs.clear_call_active("t", "vd_old")
    assert dvs.call_active("t")
    dvs.clear_call_active("t", "vd_new")
    assert not dvs.call_active("t")


def test_session_lookup_refuses_a_foreign_prefix(no_redis):
    assert dvs.get_session("vs_live") is None


# ---- the bridge's lifetime ----------------------------------------------------------

class ScriptedSocket(FakeSocket):
    def __init__(self, inbound):
        super().__init__()
        self.inbound = list(inbound)

    async def receive_text(self):
        from fastapi import WebSocketDisconnect
        await asyncio.sleep(0)
        if not self.inbound:
            raise WebSocketDisconnect()
        return self.inbound.pop(0)


def _run_call(monkeypatch, inbound, turns_seen=None):
    closes = []
    monkeypatch.setattr(dvs, "_redis", lambda: None)
    import client_portal.voice as voice
    monkeypatch.setattr(voice, "persist_direct_call_end",
                        lambda s, d, turns, reason=None: closes.append((turns, reason)) or (1 if turns else 0))
    socket = ScriptedSocket(inbound)
    session = _session()
    dvs._local_sessions[session.session_id] = session
    asyncio.run(dvs.run_bridge(socket, session, provider=FakeProvider(), stream_attach=_stream()))
    return socket, closes, session


def test_hanging_up_says_saved_and_clears_the_session(monkeypatch):
    socket, closes, session = _run_call(monkeypatch, [json.dumps({"type": "end"})])
    assert socket.of("saved")[0]["rows"] == 0
    assert closes == [(0, None)]
    assert dvs.get_session(session.session_id) is None


def test_a_dropped_socket_still_closes_the_call(monkeypatch):
    socket, closes, _ = _run_call(monkeypatch, [])
    assert closes and socket.of("saved")


def test_garbage_frames_do_not_end_the_call(monkeypatch):
    socket, closes, _ = _run_call(monkeypatch, ["not json", "[]", json.dumps({"type": "nope"}),
                                                json.dumps({"type": "end"})])
    assert closes == [(0, None)]


def test_no_provider_ends_the_call_in_words(monkeypatch):
    monkeypatch.setattr(dvs, "_redis", lambda: None)
    import services.speech as speech
    monkeypatch.setattr(speech, "resolve_direct_provider", lambda *_: None)
    socket = ScriptedSocket([])
    asyncio.run(dvs.run_bridge(socket, _session(), stream_attach=_stream()))
    assert "speech key" in socket.of("status")[0]["message"] and socket.of("saved")


# ---- production wiring: start, label, prompt, settings, the socket's door -----------

def _direct_start_env(monkeypatch, *, live=False, direct=False):
    import client_portal.service as svc
    import client_portal.voice as wv
    import config
    from database import db as core_db
    from services import docker_service
    from services.settings_service import settings_service
    from services import speech

    dvs._local_sessions.clear()
    monkeypatch.setattr(dvs, "_redis", lambda: None)
    monkeypatch.setattr(config, "VOICE_ENABLED", True)
    monkeypatch.setattr(config, "WORKSPACE_VOICE_MAX_DURATION", 1800)
    monkeypatch.setattr(svc, "agent_on_roster", lambda *a, **k: True)
    monkeypatch.setattr(wv.db, "get_portal_session", lambda sid, a, e: {"id": sid} if sid == "thread" else None)
    monkeypatch.setattr(wv.db, "get_turn_inflight", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(settings_service, "is_direct_voice_enabled", lambda: True)
    monkeypatch.setattr(speech, "direct_voice_readiness", lambda *a: ("gemini", None))
    monkeypatch.setattr(docker_service, "get_agent_runtime", lambda name: "gemini-cli")
    monkeypatch.setattr(core_db, "get_voice_name", lambda name: None)
    monkeypatch.setattr(wv, "voice_call_active", lambda sid: live)
    monkeypatch.setattr(dvs, "call_active", lambda sid: direct)
    # A Live start reads its own capability first; without this the `live` case
    # passed only on a host with a realtime provider key in its env.
    live_on = wv.realtime_voice_capability(True).model_copy(update={"available": True, "reason": None})
    monkeypatch.setattr(wv, "realtime_voice_capability", lambda _p: live_on)


def _direct_start(**over):
    from client_portal.voice import start_workspace_voice
    kwargs = dict(agent_name="scribe", email="alice@example.com", is_platform=True,
                  portal_session_id="thread", user_id=7, user_label="alice", mode="direct")
    kwargs.update(over)
    return asyncio.run(start_workspace_voice(**kwargs))


def test_a_direct_start_returns_a_direct_session_and_states_the_runtime(monkeypatch):
    _direct_start_env(monkeypatch)
    out = _direct_start()
    assert out["voice_session_id"].startswith("vd_") and out["mode"] == "direct"
    assert out["websocket_url"] == f"/ws/voice/{out['voice_session_id']}"
    assert out["runtime"] == "gemini-cli" and out["early_speech"] is False
    assert dvs.get_session(out["voice_session_id"]).portal_session_id == "thread"


@pytest.mark.parametrize("live,direct,mode", [
    (True, False, "direct"),   # Live is on, Direct refused
    (False, True, "direct"),   # Direct is on, a second Direct refused
    (False, True, "live"),     # Direct is on, Live refused
])
def test_one_call_per_thread_whichever_kinds(monkeypatch, live, direct, mode):
    from client_portal.service import ClientPortalError
    _direct_start_env(monkeypatch, live=live, direct=direct)
    with pytest.raises(ClientPortalError) as exc:
        _direct_start(mode=mode)
    assert exc.value.status_code == 409


def test_a_stranger_gets_the_uniform_404_before_any_capability_read(monkeypatch):
    from client_portal.service import ClientPortalError
    _direct_start_env(monkeypatch)
    with pytest.raises(ClientPortalError) as exc:
        _direct_start(portal_session_id="not-mine")
    assert exc.value.status_code == 404


def test_the_roster_field_is_closed_for_a_token_client_and_when_off(monkeypatch):
    from client_portal.voice import direct_voice_capability
    from services.settings_service import settings_service
    _direct_start_env(monkeypatch)
    assert direct_voice_capability(False).available is False
    assert direct_voice_capability(False).reason is None
    assert direct_voice_capability(True).provider == "gemini"
    monkeypatch.setattr(settings_service, "is_direct_voice_enabled", lambda: False)
    assert direct_voice_capability(True).available is False


def test_a_call_with_turns_leaves_one_label_and_a_silent_one_leaves_nothing(monkeypatch):
    import client_portal.voice as wv
    added, touched = [], []
    monkeypatch.setattr(wv.db, "add_portal_message", lambda *a, **k: added.append((a, k)))
    monkeypatch.setattr(wv.db, "touch_portal_session", lambda *a, **k: touched.append(k))
    assert wv.persist_direct_call_end(_session(), 120, 0) == 0 and added == []
    assert wv.persist_direct_call_end(_session(), 120, 3) == 1
    args, kw = added[0]
    assert args[3:5] == ("system", "Direct call · 2 min")
    assert kw["voice_call_id"] == "vd_x" and touched == [{"added": 1}]


class _NotAdmin:
    id = 2
    role = "creator"
    username = "bob"
    agent_name = None
    connector_agent = None
    mcp_scope = None


def test_direct_call_settings_are_admin_only_on_read_and_write():
    from fastapi import HTTPException
    from models import DirectVoiceSettingsUpdate
    from routers.settings import integrations as routes
    with pytest.raises(HTTPException) as read:
        asyncio.run(routes.get_direct_voice_settings(_NotAdmin()))
    with pytest.raises(HTTPException) as write:
        asyncio.run(routes.update_direct_voice_settings(DirectVoiceSettingsUpdate(enabled=False), None, _NotAdmin()))
    assert read.value.status_code == write.value.status_code == 403


def test_direct_call_settings_write_and_name_no_key(monkeypatch):
    from models import DirectVoiceSettingsUpdate
    from routers.settings import integrations as routes
    from services.settings_service import settings_service
    from services import speech
    written = {}
    monkeypatch.setattr(routes.db, "set_setting", lambda k, v: written.__setitem__(k, v))
    monkeypatch.setattr(settings_service, "is_direct_voice_enabled", lambda: written.get("direct_voice_enabled") == "true")
    monkeypatch.setattr(settings_service, "get_direct_voice_provider", lambda: written.get("direct_voice_provider", "auto"))
    monkeypatch.setattr(speech, "direct_voice_readiness", lambda choice=None: ("gemini", None))
    logged = []

    async def log(**kw):
        logged.append(kw)

    monkeypatch.setattr(routes.platform_audit_service, "log", log)
    admin = _NotAdmin()
    admin.role = "admin"
    from types import SimpleNamespace
    request = SimpleNamespace(client=None, scope={"path": "/api/settings/direct-voice"}, state=SimpleNamespace())
    out = asyncio.run(routes.update_direct_voice_settings(
        DirectVoiceSettingsUpdate(enabled=True, provider="gemini"), request, admin))
    assert out == {"enabled": True, "provider": "gemini", "ready_provider": "gemini", "reason": None}
    assert logged[0]["details"]["changed"]["provider"] == {"old": "auto", "new": "gemini"}
    asyncio.run(routes.update_direct_voice_settings(DirectVoiceSettingsUpdate(provider="gemini"), request, admin))
    assert len(logged) == 1   # no change, nothing to record


def test_an_unknown_provider_choice_is_refused_by_the_model():
    from models import DirectVoiceSettingsUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DirectVoiceSettingsUpdate(provider="other")


class _DoorSocket:
    def __init__(self):
        self.closed = None
        self.accepted = False

    async def close(self, code=None, reason=None):
        self.closed = code

    async def accept(self):
        self.accepted = True


def _knock(monkeypatch, *, session_user, caller_id, caller_role="user"):
    import dependencies
    from routers import voice as door

    dvs._local_sessions.clear()
    monkeypatch.setattr(dvs, "_redis", lambda: None)
    session = dvs.create_session(agent_name="scribe", user_id=session_user, client_email="alice@example.com",
                                 portal_session_id="thread", max_duration=60)
    ran = []

    async def bridge(ws, s, **kw):
        ran.append((s.session_id, kw))

    monkeypatch.setattr(dvs, "run_bridge", bridge)
    from services import docker_service
    monkeypatch.setattr(docker_service, "get_agent_runtime", lambda name: "claude-code")
    import jose.jwt
    monkeypatch.setattr(jose.jwt, "decode", lambda *a, **k: {"sub": "someone", "jti": "j"})
    monkeypatch.setattr(dependencies, "is_token_revoked", lambda jti: False)
    monkeypatch.setattr(dependencies, "session_predates_user_revoke", lambda payload: False)
    monkeypatch.setattr(door.db, "get_user_by_username", lambda u: {"id": caller_id, "role": caller_role, "username": u})
    socket = _DoorSocket()
    asyncio.run(door.voice_websocket(socket, session.session_id, token="t"))
    return socket, ran, session


def test_the_owner_reaches_the_bridge_with_the_real_stream_and_runtime(monkeypatch):
    socket, ran, session = _knock(monkeypatch, session_user=7, caller_id=7)
    from client_portal.router import direct_call_stream
    assert socket.accepted and ran[0][0] == session.session_id
    assert ran[0][1] == {"stream_attach": direct_call_stream, "runtime": "claude-code"}


def test_an_admin_cannot_attach_to_someone_elses_direct_call(monkeypatch):
    socket, ran, _ = _knock(monkeypatch, session_user=7, caller_id=1, caller_role="admin")
    assert socket.closed == 4003 and not socket.accepted and ran == []


def _stream_route(monkeypatch, voice_call_id, *, is_platform=True, joins=True):
    from types import SimpleNamespace

    from client_portal import router as r
    from client_portal.models import PortalChatRequest
    from services import idempotency_service, rate_limiter

    seen = {}

    async def start(agent, message, email, **kw):
        seen.update(kw)
        return {"execution_id": "e1", "session_id": "thread"}

    monkeypatch.setattr(r, "_require_roster", lambda *a, **k: None)
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(r.service, "start_portal_turn", start)
    monkeypatch.setattr(r.service, "reply_context", lambda *a, **k: None)
    monkeypatch.setattr(r.service, "resolve_chat_tags", lambda *a, **k: None)
    monkeypatch.setattr(r.service, "validated_open_canvas", lambda *a, **k: None)
    monkeypatch.setattr(r.service, "validate_requested_model", lambda *a, **k: None)
    monkeypatch.setattr(idempotency_service, "begin", lambda *a, **k: SimpleNamespace(replay=False, in_flight=False))
    monkeypatch.setattr(idempotency_service, "complete", lambda *a, **k: None)
    monkeypatch.setattr(idempotency_service, "fail", lambda *a, **k: None)
    monkeypatch.setattr(dvs, "turn_may_join_call", lambda *a, **k: joins)
    principal = SimpleNamespace(email="alice@example.com", is_platform=is_platform, self_approves=True, credential=None)
    body = PortalChatRequest(message="hi", session_id="thread", voice_call_id=voice_call_id)
    request = SimpleNamespace(headers={})
    asyncio.run(r.portal_chat_stream("scribe", body, request, principal))
    return seen


def test_the_stream_route_hands_a_validated_call_id_to_the_turn(monkeypatch):
    assert _stream_route(monkeypatch, "vd_a")["voice_call_id"] == "vd_a"


def test_a_typed_turn_carries_no_call_id(monkeypatch):
    assert _stream_route(monkeypatch, None)["voice_call_id"] is None


@pytest.mark.parametrize("over", [dict(joins=False), dict(is_platform=False)])
def test_a_call_id_the_caller_cannot_claim_is_a_uniform_404_and_starts_nothing(monkeypatch, over):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        _stream_route(monkeypatch, "vd_a", **over)
    assert exc.value.status_code == 404


def test_the_sync_route_has_no_way_to_carry_a_call_id():
    import inspect

    from client_portal import router as r
    assert "voice_call_id" not in inspect.getsource(r.portal_chat)


def test_a_direct_turn_is_told_it_will_be_read_aloud_and_a_typed_turn_is_not(portal):
    from client_portal.service import DIRECT_CALL_ETIQUETTE
    svc, state = portal
    asyncio.run(svc.portal_chat(P_AGENT, "typed", P_ALICE, session_id=P_SESSION))
    asyncio.run(svc.portal_chat(P_AGENT, "said", P_ALICE, session_id=P_SESSION, voice_call_id="vd_a"))
    asyncio.run(svc.portal_chat(P_AGENT, "task", P_ALICE, session_id=P_SESSION, voice_call_id="vs_a"))
    typed, said, task = state.recorder.calls
    assert not (typed.get("system_prompt") or "")
    assert said["system_prompt"] == DIRECT_CALL_ETIQUETTE
    assert not (task.get("system_prompt") or "")


def test_a_direct_turn_writes_both_its_rows_with_the_call_id(portal):
    svc, state = portal
    asyncio.run(svc.portal_chat(P_AGENT, "said", P_ALICE, session_id=P_SESSION, voice_call_id="vd_a"))
    roles = {a[3]: kw.get("voice_call_id") for a, kw in state.persisted}
    assert roles == {"user": "vd_a", "assistant": "vd_a"}


@pytest.mark.parametrize("env,stored,expected", [
    (None, None, True), ("false", None, False), ("0", None, False), ("true", None, True),
    ("false", "true", True), ("true", "false", False), (None, "false", False),
])
def test_direct_calls_default_on_and_env_can_turn_them_off_until_an_admin_decides(monkeypatch, env, stored, expected):
    from services import settings_service as module
    if env is None:
        monkeypatch.delenv("DIRECT_VOICE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DIRECT_VOICE_ENABLED", env)
    monkeypatch.setattr(module.settings_service, "get_setting",
                        lambda key, default=None: stored if key == "direct_voice_enabled" and stored else default)
    assert module.settings_service.is_direct_voice_enabled() is expected


def test_what_is_spoken_is_scrubbed_of_staged_secrets(monkeypatch):
    monkeypatch.setattr("services.runtime_secret_scrub.get_staged_values", lambda: ["s3cret-value"])
    provider = FakeProvider()
    _, bridge = _bridge(provider)
    asyncio.run(bridge._speak_sentence(0, "The token is s3cret-value."))
    assert provider.said and "s3cret-value" not in provider.said[0]


# -- an execution is spoken once (merge-train 2026-11-02: a re-sent `speak` restarted
#    the SpokenBudget and bought the same reply's synthesis again)

class _NxRedis:
    def __init__(self, held=()):
        self.keys = set(held)

    def set(self, key, _value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True


def _counting_bridge(monkeypatch, redis):
    monkeypatch.setattr(dvs, "_redis", lambda: redis)
    _, bridge = _bridge()
    monkeypatch.setattr(bridge, "_authorized_execution", lambda _e: True)
    spoken = []

    async def fake_speak(execution_id):
        spoken.append(execution_id)

    monkeypatch.setattr(bridge, "speak_execution", fake_speak)
    return bridge, spoken


def test_a_resent_speak_frame_never_speaks_the_same_run_twice(monkeypatch):
    bridge, spoken = _counting_bridge(monkeypatch, _NxRedis())

    async def go():
        for _ in range(5):
            await bridge.handle({"type": "speak", "execution_id": "e1"})
            await asyncio.sleep(0)
        await bridge.handle({"type": "speak", "execution_id": "e2"})
        await asyncio.sleep(0)

    asyncio.run(go())
    assert spoken == ["e1", "e2"]


def test_a_run_spoken_on_another_call_is_not_spoken_again(monkeypatch):
    bridge, spoken = _counting_bridge(monkeypatch, _NxRedis(held={"voice_direct_spoken:e1"}))

    async def go():
        await bridge.handle({"type": "speak", "execution_id": "e1"})
        await asyncio.sleep(0)

    asyncio.run(go())
    assert spoken == []


def test_without_redis_one_socket_still_speaks_a_run_once(monkeypatch):
    class Down:
        def set(self, *a, **k):
            raise ConnectionError("down")

    bridge, spoken = _counting_bridge(monkeypatch, Down())

    async def go():
        for _ in range(3):
            await bridge.handle({"type": "speak", "execution_id": "e1"})
            await asyncio.sleep(0)

    asyncio.run(go())
    assert spoken == ["e1"]


def test_the_spoken_claim_outlives_the_reply_wait():
    assert dvs.SPOKEN_EXECUTION_TTL >= dvs.REPLY_WAIT_SECONDS


# -- the backend half of the store↔backend contract (merge-train 2026-11-02): each
#    line below could be deleted with every test green

def test_the_start_route_hands_the_requested_mode_to_the_call(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from client_portal import router, voice as workspace_voice
    from client_portal.models import PortalVoiceStartRequest
    from client_portal.portal_auth import PortalPrincipal
    from services import rate_limiter

    seen = {}

    async def fake_start(**kwargs):
        seen.update(kwargs)
        return {"voice_session_id": "vd_x", "websocket_url": "/ws/voice/vd_x",
                "portal_session_id": kwargs["portal_session_id"], "max_duration_seconds": 60,
                "mode": kwargs["mode"]}

    async def fake_user(_request, _token):
        return SimpleNamespace(id=7, email="alice@example.com", username="alice")

    monkeypatch.setattr(router, "_require_roster", lambda *a, **k: None)
    monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
    monkeypatch.setattr(router, "resolve_platform_user_unfloored", fake_user)
    monkeypatch.setattr(workspace_voice, "start_workspace_voice", fake_start)
    out = asyncio.run(router.portal_voice_start(
        "scout", PortalVoiceStartRequest(portal_session_id="ps1", mode="direct"),
        request=MagicMock(), principal=PortalPrincipal("alice@example.com", True), token="t"))
    assert seen["mode"] == "direct" and out.mode == "direct"


def test_the_roster_carries_the_direct_voice_capability(monkeypatch):
    from types import SimpleNamespace
    from client_portal import service as svc, voice as workspace_voice

    async def none_map(_names):
        return {}

    async def stt(_ready):
        return False

    from services import tts_service
    monkeypatch.setattr(tts_service, "is_available", lambda: False)
    monkeypatch.setattr(svc, "_stt_ready", stt)
    monkeypatch.setattr(svc, "_default_voice_id", lambda: None)
    monkeypatch.setattr(svc, "_multi_agent_chat_available", lambda: False)
    monkeypatch.setattr(svc, "_roster_rows", lambda *a: [])
    monkeypatch.setattr(svc, "_availability_map", none_map)
    monkeypatch.setattr(svc, "_runtime_map", none_map)
    monkeypatch.setattr(svc, "_projects_available", lambda *a: False)
    off = workspace_voice.realtime_voice_capability(True).model_copy(
        update={"available": False, "reason": "off"})
    monkeypatch.setattr(workspace_voice, "realtime_voice_capability", lambda _p: off)
    marker = workspace_voice.direct_voice_capability(True).model_copy(
        update={"available": True, "reason": None, "provider": "gemini"})
    monkeypatch.setattr(workspace_voice, "direct_voice_capability", lambda _p: marker)
    roster = asyncio.run(svc.get_roster("alice@example.com", include_owned=True))
    assert roster.direct_voice.available is True and roster.direct_voice.provider == "gemini"
