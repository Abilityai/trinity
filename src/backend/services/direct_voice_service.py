"""The Workspace Direct call's audio bridge (trinity-enterprise#1046).

The socket hears and speaks; it never starts or answers anything. The browser sends an
endpointed utterance and gets the transcript back. It then sends that text through the
same route a typed message uses and hands the execution id here to be spoken. An
approval is answered on the person's own REST route. So this module has no path that
runs a turn or decides an ask under anyone's name.

Speech is harness-agnostic. Every reply is spoken from the finished turn. A runtime
that streams its messages starts sooner, and the persisted reply is then reconciled so
nothing repeats and no tail is lost.

Fixed strings below are the only words the platform adds. None paraphrases the agent.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional

from services.speech import SpeechError, SpeechProvider
from services.speech.spoken_text import SpokenBudget, sentences, unspoken_tail

logger = logging.getLogger(__name__)

DIRECT_PREFIX = "vd_"

# ~30 s of PCM16 at 16 kHz, base64-encoded, plus slack for the frame.
MAX_UTTERANCE_B64 = 1_400_000
MAX_UTTERANCES_PER_MINUTE = 30
MAX_CONCURRENT_TTS = 8
REPLY_POLL_SECONDS = 0.3
# A turn can legitimately run long; the socket's own cap ends the wait sooner.
REPLY_WAIT_SECONDS = 1800
# How long a terminal row may take to appear after the execution ended.
REPLY_SETTLE_SECONDS = 5.0
APPROVED_RUN_WAIT_SECONDS = 10.0
STATUS_MIN_GAP_SECONDS = 12.0
STILL_WORKING_AFTER_SECONDS = 45.0

SESSION_GRACE_SECONDS = 120
MARKER_LEASE_SECONDS = 60
MARKER_TICK_SECONDS = 15.0
# How long an execution stays "already spoken". Covers a reply's whole wait plus the
# call's grace, so a re-sent `speak` cannot buy a second synthesis while either lives.
SPOKEN_EXECUTION_TTL = REPLY_WAIT_SECONDS + SESSION_GRACE_SECONDS

LINE_NOT_HEARD = "I didn't catch that."
LINE_TURN_FAILED = "The agent couldn't answer that."
# A reply with no sentence to read aloud (only code, a table, links): said instead of
# silence, which reads as a dead call.
LINE_REPLY_IN_CHAT = "The reply is in the chat."
LINE_STILL_WORKING = "Still working on it."
LINE_APPROVED_LATER = "Approved. The reply will land in the chat."
LINE_REJECTED = "Okay, not doing that."
_TERMINAL = {"success", "failed", "cancelled"}

_TOOL_LINES = (
    (("bash", "shell"), "Running a command."),
    (("read", "grep", "glob", "filechange", "edit", "write"), "Checking the files."),
    (("chat_with_agent", "fan_out", "task", "agent"), "Asking another agent."),
    (("websearch", "webfetch"), "Looking that up."),
)
_TOOL_LINE_DEFAULT = "Working on it."

# Runtimes whose execution stream carries readable messages today.
EARLY_SPEECH_RUNTIMES = {"claude-code", "codex"}

_tts_slots: dict[str, asyncio.Semaphore] = {}


def supports_early_speech(runtime: Optional[str]) -> bool:
    return (runtime or "").lower() in EARLY_SPEECH_RUNTIMES


# ---- Sessions ----------------------------------------------------------------

@dataclass
class DirectVoiceSession:
    session_id: str
    agent_name: str
    user_id: int
    client_email: str
    portal_session_id: str
    max_duration: int
    voice_name: Optional[str] = None
    started_at: float = 0.0

    @property
    def is_direct(self) -> bool:
        return True


_local_sessions: dict[str, DirectVoiceSession] = {}


def _session_key(session_id: str) -> str:
    return f"voice_direct:{session_id}"


def _redis():
    from redis_breaker_util import get_breaker_redis
    return get_breaker_redis()


def create_session(*, agent_name: str, user_id: int, client_email: str, portal_session_id: str,
                   max_duration: int, voice_name: Optional[str] = None) -> DirectVoiceSession:
    session = DirectVoiceSession(
        session_id=f"{DIRECT_PREFIX}{secrets.token_urlsafe(16)}",
        agent_name=agent_name, user_id=user_id, client_email=client_email,
        portal_session_id=portal_session_id, max_duration=max_duration,
        voice_name=voice_name, started_at=time.time(),
    )
    _local_sessions[session.session_id] = session
    try:
        client = _redis()
        if client is not None:
            client.set(_session_key(session.session_id), json.dumps(asdict(session)),
                       ex=max_duration + SESSION_GRACE_SECONDS)
    except Exception as e:  # noqa: BLE001 - the local copy still serves this worker
        logger.warning("direct voice: session store failed for %s: %s", session.session_id, e)
    return session


def _expired(session: DirectVoiceSession) -> bool:
    return time.time() > session.started_at + session.max_duration + SESSION_GRACE_SECONDS


def get_session(session_id: str) -> Optional[DirectVoiceSession]:
    """Any worker may hold the socket, so Redis is the answer when it can give one: an
    entry gone from it means the call ended, even on the worker that started it and
    still has a local copy. The local copy serves only while Redis is unreachable."""
    if not (session_id or "").startswith(DIRECT_PREFIX):
        return None
    local = _local_sessions.get(session_id)
    found = local
    try:
        client = _redis()
        if client is not None:
            raw = client.get(_session_key(session_id))
            if raw:
                raw = raw.decode() if isinstance(raw, bytes) else raw
                found = DirectVoiceSession(**json.loads(raw))
            else:
                found = None
    except Exception as e:  # noqa: BLE001
        logger.warning("direct voice: session read failed for %s: %s", session_id, e)
    if found is None or _expired(found):
        _local_sessions.pop(session_id, None)
        return None
    return found


def discard_session(session_id: str) -> None:
    _local_sessions.pop(session_id, None)
    try:
        client = _redis()
        if client is not None:
            client.delete(_session_key(session_id))
    except Exception as e:  # noqa: BLE001 - the TTL is the backstop
        logger.warning("direct voice: session delete failed for %s: %s", session_id, e)


def turn_may_join_call(voice_call_id: str, *, agent_name: str, email: str,
                       portal_session_id: Optional[str]) -> bool:
    """May a turn on this route carry this call id? Only the caller's own live Direct
    call, on this agent and this thread. The id comes from a request, so all of it is
    checked. Live ids never pass: only that call's own dispatcher writes those."""
    session = get_session(voice_call_id)
    return bool(
        session
        and portal_session_id
        and session.agent_name == agent_name
        and session.client_email.lower() == (email or "").lower()
        and session.portal_session_id == portal_session_id
    )


# ---- One call per thread -----------------------------------------------------
#
# Separate from the Live call's lease on purpose. That lease also refuses typed turns,
# and a Direct call's thread stays open to them.

def _active_key(portal_session_id: str) -> str:
    return f"portal_direct_active:{portal_session_id}"


def claim_call(portal_session_id: str, owner: str) -> bool:
    """Take the thread for this call. Two starts can both pass the start route's check
    before either socket opens, so the socket is where one of them is refused.
    Fail-open, like `call_active`: a Redis outage must not end a call."""
    try:
        client = _redis()
        if client is None:
            return True
        if client.set(_active_key(portal_session_id), owner, nx=True, ex=MARKER_LEASE_SECONDS):
            return True
        current = client.get(_active_key(portal_session_id))
        current = current.decode() if isinstance(current, bytes) else current
        return current == owner
    except Exception as e:  # noqa: BLE001
        logger.warning("direct voice: active claim failed for %s: %s", portal_session_id, e)
        return True


def mark_call_active(portal_session_id: str, owner: str) -> None:
    try:
        client = _redis()
        if client is not None:
            client.set(_active_key(portal_session_id), owner, ex=MARKER_LEASE_SECONDS)
    except Exception as e:  # noqa: BLE001 - a marker failure must not end the call
        logger.warning("direct voice: active mark failed for %s: %s", portal_session_id, e)


def clear_call_active(portal_session_id: str, owner: str) -> None:
    try:
        client = _redis()
        if client is None:
            return
        current = client.get(_active_key(portal_session_id))
        current = current.decode() if isinstance(current, bytes) else current
        if current == owner:
            client.delete(_active_key(portal_session_id))
    except Exception as e:  # noqa: BLE001 - the lease expiry is the backstop
        logger.warning("direct voice: active clear failed for %s: %s", portal_session_id, e)


def call_active(portal_session_id: str) -> bool:
    """Fail-open: a Redis outage must not stop every call from starting."""
    try:
        client = _redis()
        return bool(client is not None and client.get(_active_key(portal_session_id)) is not None)
    except Exception as e:  # noqa: BLE001
        logger.warning("direct voice: active read failed for %s: %s", portal_session_id, e)
        return False


# ---- Reading an execution's stream --------------------------------------------

@dataclass(frozen=True)
class StreamItem:
    text: str = ""
    tool: str = ""


def normalize_stream_entry(runtime: Optional[str], entry: dict) -> list[StreamItem]:
    """One runtime's stream entry as speakable text and tool names. Anything not
    recognised yields nothing: the persisted reply still gets spoken."""
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    rt = (runtime or "").lower()
    if rt == "claude-code":
        # A non-null parent means a subagent is talking to its parent, not to the person.
        if kind != "assistant" or entry.get("parent_tool_use_id"):
            return []
        out = []
        for block in (entry.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (block.get("text") or "").strip():
                out.append(StreamItem(text=block["text"]))
            elif block.get("type") == "tool_use" and block.get("name"):
                out.append(StreamItem(tool=str(block["name"])))
        return out
    if rt == "codex":
        item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
        item_type = item.get("type") or (item.get("details") or {}).get("type")
        if kind == "item.completed" and item_type == "agent_message":
            text = item.get("text") or item.get("message") or ""
            return [StreamItem(text=text)] if text.strip() else []
        if kind == "item.started" and item_type in {"command_execution", "file_change",
                                                    "mcp_tool_call", "web_search"}:
            return [StreamItem(tool={"command_execution": "Shell", "file_change": "FileChange",
                                     "mcp_tool_call": "McpTool", "web_search": "WebSearch"}[item_type])]
    return []


def status_line_for_tool(tool: str) -> str:
    name = (tool or "").lower()
    for keys, line in _TOOL_LINES:
        if any(k in name for k in keys):
            return line
    return _TOOL_LINE_DEFAULT


async def _sse_entries(chunks: AsyncIterator[str]) -> AsyncIterator[dict]:
    """The `data:` lines of a stream as dicts, across chunk boundaries."""
    buffer = ""
    async for chunk in chunks:
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                entry = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(entry, dict):
                yield entry


def _read_turn_end(execution_id: str) -> tuple[Optional[str], Optional[dict]]:
    """`(status, the turn's assistant row)`. Blocking: callers go off-loop."""
    from client_portal import db as portal_db
    from database import db as core_db
    execution = core_db.get_execution(execution_id)
    status = getattr(execution, "status", None) if execution else None
    return status, portal_db.get_turn_reply_row(execution_id)


# ---- The bridge --------------------------------------------------------------

class DirectBridge:
    def __init__(self, websocket, session: DirectVoiceSession, provider: SpeechProvider,
                 stream_attach: Callable[[str, str], AsyncIterator[str]],
                 runtime: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.ws = websocket
        self.session = session
        self.provider = provider
        self.stream_attach = stream_attach
        self.runtime = runtime
        self.clock = clock
        self.turns = 0
        self._send_lock = asyncio.Lock()
        self._speech: asyncio.Queue = asyncio.Queue()
        self._generation = 0
        self._speaker: Optional[asyncio.Task] = None
        self._turn: Optional[asyncio.Task] = None
        self._background: set[asyncio.Task] = set()
        self._utterance_times: list[float] = []
        self._tts_failed_notified = False
        self._turn_first_audio: Optional[asyncio.Future] = None
        self._last_spoke_at = 0.0
        self._last_status_at = 0.0
        self._ask_lines: set[str] = set()
        self._spoken: set[str] = set()

    # -- plumbing

    async def send(self, frame: dict) -> None:
        try:
            async with self._send_lock:
                await self.ws.send_json(frame)
        except Exception:  # noqa: BLE001 - a closed socket surfaces in the receive loop
            pass

    def _spawn(self, coro: Awaitable) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def enqueue(self, sentence: str) -> None:
        self._speech.put_nowait((self._generation, sentence))

    async def run_speaker(self) -> None:
        while True:
            generation, sentence = await self._speech.get()
            if generation != self._generation:
                continue
            await self._speak_sentence(generation, sentence)

    async def _speak_sentence(self, generation: int, sentence: str) -> None:
        # The floor for every line spoken here, the fixed lines and ask lines
        # included. The reply's own text is scrubbed earlier, in speak_execution.
        from services.runtime_secret_scrub import SPOKEN_PLACEHOLDER, scrub_staged
        text = scrub_staged(sentence, marker=SPOKEN_PLACEHOLDER)
        slot = _tts_slots.setdefault(self.provider.name, asyncio.Semaphore(MAX_CONCURRENT_TTS))
        try:
            async with slot:
                said_any = False
                # The session's voice is a Gemini voice name; ElevenLabs takes a voice id
                # and uses its configured default.
                voice = self.session.voice_name if self.provider.name == "gemini" else None
                async for chunk in self.provider.synthesize_stream(text, voice):
                    if generation != self._generation:
                        return
                    if not said_any:
                        said_any = True
                        await self.send({"type": "spoken", "text": text,
                                         "ask": sentence in self._ask_lines})
                    # ent#1087: an interrupt can land while the `spoken` send above is in
                    # flight. Its `audio_flush` is then already queued, so a chunk sent now
                    # would arrive after the flush and play.
                    if generation != self._generation:
                        return
                    # First audio is recorded only once a chunk is really going out, so a
                    # sentence cut during its `spoken` send records none.
                    if self._turn_first_audio is not None and not self._turn_first_audio.done():
                        self._turn_first_audio.set_result(self.clock())
                    self._last_spoke_at = self.clock()
                    await self.send({"type": "audio",
                                     "data": base64.b64encode(chunk).decode("ascii")})
        except SpeechError as e:
            logger.warning("direct voice: speech failed on %s: %s", self.session.session_id, e)
            if not self._tts_failed_notified:
                self._tts_failed_notified = True
                await self.send({"type": "notice", "code": "tts_failed",
                                 "text": "Speech isn't working. The reply is in the chat."})

    def interrupt(self) -> None:
        """Drop everything queued or being said. Cancelling the running turn is the
        browser's call, on the route it already uses for Stop."""
        self._generation += 1
        while not self._speech.empty():
            self._speech.get_nowait()
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        self._turn = None

    # -- hearing

    def _within_rate(self) -> bool:
        """Per person across every call they hold, since each call is a socket and the
        provider's cost is the platform's. Fail-open, like every limiter here."""
        now = self.clock()
        self._utterance_times = [t for t in self._utterance_times if now - t < 60]
        if len(self._utterance_times) >= MAX_UTTERANCES_PER_MINUTE:
            return False
        try:
            from services import rate_limiter
            key = f"direct_voice_utterance:{self.session.client_email.lower()}"
            if not rate_limiter.check(key, MAX_UTTERANCES_PER_MINUTE, 60).allowed:
                return False
        except Exception as e:  # noqa: BLE001
            logger.warning("direct voice: utterance limit read failed: %s", e)
        self._utterance_times.append(now)
        return True

    async def hear(self, seq, data: str) -> None:
        if not isinstance(data, str) or len(data) > MAX_UTTERANCE_B64:
            await self.send({"type": "not_heard", "seq": seq, "reason": "too_long"})
            return
        if not self._within_rate():
            await self.send({"type": "not_heard", "seq": seq, "reason": "rate_limited"})
            return
        try:
            pcm = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            await self.send({"type": "not_heard", "seq": seq, "reason": "bad_audio"})
            return
        started = self.clock()
        try:
            transcript = await self.provider.transcribe(pcm)
        except SpeechError as e:
            logger.warning("direct voice: transcription failed on %s: %s", self.session.session_id, e)
            await self.send({"type": "not_heard", "seq": seq, "reason": "stt_failed"})
            self.enqueue(LINE_NOT_HEARD)
            return
        stt_ms = int((self.clock() - started) * 1000)
        logger.info("direct voice: utterance stt_ms=%s provider=%s", stt_ms, self.provider.name)
        if not transcript.text:
            await self.send({"type": "not_heard", "seq": seq, "reason": "empty", "stt_ms": stt_ms})
            return
        await self.send({"type": "transcript_live", "seq": seq, "text": transcript.text, "stt_ms": stt_ms})

    # -- speaking a turn

    def _authorized_execution(self, execution_id: str) -> bool:
        from client_portal.service import execution_belongs_to_caller
        return execution_belongs_to_caller(execution_id, self.session.agent_name,
                                           self.session.client_email)

    async def speak(self, execution_id) -> None:
        if not isinstance(execution_id, str) or not execution_id:
            return
        if not await asyncio.to_thread(self._authorized_execution, execution_id):
            logger.warning("direct voice: refused to speak an execution the caller does not own")
            return
        self.turns += 1
        self._start_turn(execution_id)

    def _claim_execution(self, execution_id: str) -> bool:
        """An execution is spoken once. Each turn's SpokenBudget bounds one synthesis;
        without this, re-sending `speak` for the same run restarts the budget and buys
        the same reply again, across calls and workers. Redis carries the claim between
        them; this bridge's own set holds when Redis cannot answer (fail-open, like
        every limiter here, but never twice on one socket)."""
        if execution_id in self._spoken:
            return False
        self._spoken.add(execution_id)
        try:
            client = _redis()
            if client is not None:
                key = f"voice_direct_spoken:{execution_id}"
                if not client.set(key, "1", nx=True, ex=SPOKEN_EXECUTION_TTL):
                    return False
        except Exception as e:  # noqa: BLE001
            logger.warning("direct voice: spoken-claim read failed: %s", e)
        return True

    def _start_turn(self, execution_id: str) -> None:
        if not self._claim_execution(execution_id):
            logger.warning("direct voice: refused to speak execution %s a second time", execution_id)
            return
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        self._turn = self._spawn(self.speak_execution(execution_id))

    async def speak_execution(self, execution_id: str) -> None:
        # ent#279 / #4359: staged credential values come out of the reply's text
        # BEFORE sentences() runs speakable(), which rewrites the text (it drops
        # `**`, `__` and `~~` anywhere), so a value scrubbed after it may no longer
        # match. The persisted reply already carries the stored marker for a value
        # staged before it was saved; it is said as the spoken placeholder too.
        from services.runtime_secret_scrub import SPOKEN_PLACEHOLDER, scrub_staged
        from utils.credential_sanitizer import REDACTION_PLACEHOLDER

        started = self.clock()
        loop = asyncio.get_running_loop()
        self._turn_first_audio = loop.create_future()
        self._tts_failed_notified = False
        await self.send({"type": "speak_state", "state": "thinking", "execution_id": execution_id})
        budget = SpokenBudget()
        queued: list[str] = []
        closed = {"value": False}

        def say(sentence: str) -> None:
            admitted = budget.admit(sentence)
            if admitted is None:
                closed["value"] = True
                return
            queued.append(sentence)
            # Counts as speech now: a status line must not talk over words about to play.
            self._last_spoke_at = self.clock()
            self.enqueue(admitted)

        watchdog = self._spawn(self._still_working(started))
        try:
            async for entry in _sse_entries(self.stream_attach(self.session.agent_name, execution_id)):
                if entry.get("type") == "stream_end":
                    break
                for item in normalize_stream_entry(self.runtime, entry):
                    if item.tool:
                        await self._on_tool(item.tool)
                    for sentence in sentences(scrub_staged(item.text, marker=SPOKEN_PLACEHOLDER)):
                        say(sentence)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the persisted reply below still speaks
            logger.warning("direct voice: stream read failed on %s: %s", execution_id, e)

        try:
            status, row = await self._await_turn_end(execution_id)
        finally:
            watchdog.cancel()

        if status == "cancelled":
            await self.send({"type": "speak_state", "state": "idle", "execution_id": execution_id})
            return
        if row and row.get("gate_hold"):
            # The hold notice is the platform's text. The browser reads the ask card.
            pass
        elif row and (row.get("content") or "").strip():
            reply = scrub_staged(row["content"], marker=SPOKEN_PLACEHOLDER).replace(
                REDACTION_PLACEHOLDER, SPOKEN_PLACEHOLDER)
            for sentence in unspoken_tail(reply, queued):
                say(sentence)
            if not queued:
                self.enqueue(LINE_REPLY_IN_CHAT)
        elif status == "failed" and not queued:
            self.enqueue(LINE_TURN_FAILED)

        await self._report_timing(execution_id, started)
        await self.send({"type": "speak_state", "state": "idle", "execution_id": execution_id})

    async def _await_turn_end(self, execution_id: str) -> tuple[Optional[str], Optional[dict]]:
        deadline = self.clock() + REPLY_WAIT_SECONDS
        terminal_at: Optional[float] = None
        status, row = None, None
        while self.clock() < deadline:
            status, row = await asyncio.to_thread(_read_turn_end, execution_id)
            if row is not None:
                return status, row
            if status in _TERMINAL:
                terminal_at = terminal_at if terminal_at is not None else self.clock()
                if self.clock() - terminal_at >= REPLY_SETTLE_SECONDS:
                    return status, None
            await asyncio.sleep(REPLY_POLL_SECONDS)
        return status, row

    async def _report_timing(self, execution_id: str, started: float) -> None:
        first = self._turn_first_audio
        if first is None:
            return
        first_audio_ms = None
        if not self._speech.empty() or first.done():
            try:
                at = await asyncio.wait_for(asyncio.shield(first), timeout=10)
                first_audio_ms = int((at - started) * 1000)
            except asyncio.TimeoutError:
                pass
        logger.info("direct voice: turn %s first_audio_ms=%s runtime=%s", execution_id,
                    first_audio_ms, self.runtime)
        await self.send({"type": "timing", "execution_id": execution_id,
                         "first_audio_ms": first_audio_ms, "turn_ms": int((self.clock() - started) * 1000)})

    async def _on_tool(self, tool: str) -> None:
        await self.send({"type": "activity", "tool": tool})
        now = self.clock()
        if now - self._last_status_at >= STATUS_MIN_GAP_SECONDS and now - self._last_spoke_at >= 2.5:
            self._last_status_at = now
            self.enqueue(status_line_for_tool(tool))

    async def _still_working(self, started: float) -> None:
        await asyncio.sleep(STILL_WORKING_AFTER_SECONDS)
        if self.clock() - self._last_spoke_at >= STATUS_MIN_GAP_SECONDS:
            self._last_status_at = self.clock()
            self.enqueue(LINE_STILL_WORKING)

    # -- asks

    def _read_ask(self, ask_id: str) -> Optional[dict]:
        """The ask, only if this caller may read it. A miss and a stranger's ask are
        the same None, like the answer route's uniform 404."""
        from client_portal.asks.service import AskError, _owned_ask
        try:
            return _owned_ask(ask_id, self.session.client_email, True)
        except AskError:
            return None

    async def speak_ask(self, ask_id) -> None:
        if not isinstance(ask_id, str) or not ask_id:
            return
        row = await asyncio.to_thread(self._read_ask, ask_id)
        if row is not None:
            line = ask_line(row)
            self._ask_lines.add(line)
            self.enqueue(line)

    async def ask_followup(self, ask_id, outcome) -> None:
        """After the person answered on the REST route: say what happens next, and
        speak the approved run when one starts."""
        if not isinstance(ask_id, str) or not ask_id:
            return
        row = await asyncio.to_thread(self._read_ask, ask_id)
        if row is None:
            return
        if outcome != "approved":
            self.enqueue(LINE_REJECTED)
            return
        execution_id = await self._approved_run(row)
        # The caller may read an ask as its approver without owning the run behind it.
        # Reading the ask is not leave to hear another person's reply.
        if execution_id and await asyncio.to_thread(self._authorized_execution, execution_id):
            self.turns += 1
            self._start_turn(execution_id)
        else:
            self.enqueue(LINE_APPROVED_LATER)

    async def _approved_run(self, row: dict) -> Optional[str]:
        request_id = row.get("request_id")
        if not request_id:
            return None
        from database import db as core_db
        deadline = self.clock() + APPROVED_RUN_WAIT_SECONDS
        while self.clock() < deadline:
            record = await asyncio.to_thread(core_db.get_gate_request, request_id)
            execution_id = (record or {}).get("dispatched_execution_id")
            if execution_id:
                return execution_id
            await asyncio.sleep(0.5)
        return None

    # -- the socket

    async def handle(self, frame: dict) -> bool:
        """One inbound frame. Returns False when the call should end."""
        kind = frame.get("type")
        if kind == "utterance":
            self._spawn(self.hear(frame.get("seq"), frame.get("data")))
        elif kind == "speak":
            await self.speak(frame.get("execution_id"))
        elif kind == "interrupt":
            self.interrupt()
            await self.send({"type": "audio_flush"})
        elif kind == "speak_ask":
            await self.speak_ask(frame.get("ask_id"))
        elif kind == "ask_followup":
            self._spawn(self.ask_followup(frame.get("ask_id"), frame.get("outcome")))
        elif kind == "end":
            return False
        return True

    async def close(self) -> None:
        self._generation += 1
        for task in [self._speaker, self._turn, *list(self._background)]:
            if task is not None and not task.done():
                task.cancel()


def ask_line(row: dict) -> str:
    """The spoken form of an ask. Approvals name the person's own request where the
    gate recorded one, so the words decided on are the words asked for."""
    title = (row.get("title") or "A decision").strip()
    if row.get("type") == "approval" or row.get("kind") == "approval":
        proposal = row.get("proposal") or {}
        if isinstance(proposal, str):
            try:
                proposal = json.loads(proposal)
            except ValueError:
                proposal = {}
        request = str((proposal or {}).get("input") or "").strip()
        if request:
            return f"I need your approval. {title}. You asked: {request[:160]}. Yes or no?"
        return f"I need your approval. {title}. Yes or no?"
    return f"The agent has a question for you: {title}. It's on the screen."


async def _renew_marker(portal_session_id: str, owner: str, max_seconds: float) -> None:
    elapsed = 0.0
    while elapsed < max_seconds:
        await asyncio.sleep(MARKER_TICK_SECONDS)
        elapsed += MARKER_TICK_SECONDS
        await asyncio.to_thread(mark_call_active, portal_session_id, owner)


async def run_bridge(websocket, session: DirectVoiceSession, *,
                     provider: Optional[SpeechProvider] = None,
                     stream_attach: Callable[[str, str], AsyncIterator[str]],
                     runtime: Optional[str] = None) -> None:
    """Hold one Direct call until the person hangs up, the socket drops or the cap
    ends it. A running turn is left to land in the chat: it belongs to the route that
    started it, not to this socket."""
    from client_portal.voice import persist_direct_call_end
    from fastapi import WebSocketDisconnect
    from services.speech import resolve_direct_provider

    started = time.monotonic()
    provider = provider or resolve_direct_provider()
    if provider is None:
        await websocket.send_json({"type": "status", "state": "ended", "reason": "error",
                                   "message": "Direct calls need a speech key."})
        await websocket.send_json({"type": "saved", "reason": "error", "rows": 0})
        discard_session(session.session_id)
        return

    if not claim_call(session.portal_session_id, session.session_id):
        await websocket.send_json({"type": "status", "state": "ended", "reason": "busy",
                                   "message": "A call is already on in this chat."})
        await websocket.send_json({"type": "saved", "reason": "busy", "rows": 0})
        discard_session(session.session_id)
        return

    bridge = DirectBridge(websocket, session, provider, stream_attach, runtime=runtime)
    bridge._speaker = asyncio.ensure_future(bridge.run_speaker())
    renewer = asyncio.ensure_future(_renew_marker(session.portal_session_id, session.session_id,
                                                  session.max_duration + SESSION_GRACE_SECONDS))
    await bridge.send({"type": "status", "state": "listening"})
    end_reason = None
    try:
        while True:
            remaining = session.max_duration - (time.monotonic() - started)
            if remaining <= 0:
                end_reason = "cap"
                break
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                end_reason = "cap"
                break
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if not await bridge.handle(frame):
                break
    except WebSocketDisconnect:
        pass
    finally:
        renewer.cancel()
        await bridge.close()
        clear_call_active(session.portal_session_id, session.session_id)
        rows = persist_direct_call_end(session, time.monotonic() - started, bridge.turns, end_reason)
        discard_session(session.session_id)
        await bridge.send({"type": "status", "state": "ended", "reason": end_reason})
        await bridge.send({"type": "saved", "rows": rows, "reason": end_reason})
