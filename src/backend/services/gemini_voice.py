"""
Gemini Live API voice service for Trinity (VOICE-001).

Provides a wrapper around the google-genai SDK's Live API for real-time
speech-to-speech conversations with agents via Gemini 2.5 Flash Native Audio.

Architecture:
  Browser (mic) → WebSocket → Backend → Gemini Live API → Backend → WebSocket → Browser (speaker)
  Gemini tool_call → Backend._execute_tool → Agent container → tool_response → Gemini
"""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from google import genai
from google.genai import types as genai_types

from config import GEMINI_API_KEY, VOICE_MODEL, VOICE_MAX_DURATION, REDIS_URL
from models import DEFAULT_CANVAS_ID
from services.canvas_blocks import WORKSPACE_ROOT, classify_image_src, map_panel_tool

logger = logging.getLogger(__name__)

# Audio format constants
INPUT_SAMPLE_RATE = 16000   # 16kHz PCM input to Gemini
OUTPUT_SAMPLE_RATE = 24000  # 24kHz PCM output from Gemini

# Max chars for tool call prompts (prevent injection via very long args)
_TOOL_PROMPT_MAX = 2000

# ent#536 — the voice panel IS the agent's default canvas. The panel tools are
# thin verbs over the canvas block rules (`services/canvas_blocks.py`): the
# same image confinement gate, the same block ids, the same write path the
# agent's own `set_canvas` uses. Re-exported names keep the #979 tests and any
# external importer working.
_WORKSPACE_ROOT = WORKSPACE_ROOT
_classify_image_src = classify_image_src

_PANEL_TOOL_NAMES = {
    "show_markdown", "update_panel", "append_to_panel", "clear_panel",
    "show_diagram", "show_image",
}

_PANEL_TOOLS = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="show_markdown",
            description=(
                "Display markdown content in the visual canvas panel visible to the user. "
                "Use for notes, summaries, analysis, action items, frameworks. "
                "This is your default panel tool — use it most often."
            ),
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "content": genai_types.Schema(type=genai_types.Type.STRING, description="Markdown content to display"),
                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Optional panel title"),
                },
                required=["content"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="update_panel",
            description="Replace the canvas panel with custom HTML for richer layouts, tables, or structured data.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "html": genai_types.Schema(type=genai_types.Type.STRING, description="HTML content to display"),
                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Optional panel title"),
                },
                required=["html"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="append_to_panel",
            description="Append HTML to the existing panel without clearing it. Use to build content incrementally.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "html": genai_types.Schema(type=genai_types.Type.STRING, description="HTML to append to the panel"),
                },
                required=["html"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="clear_panel",
            description="Clear the canvas panel when moving to a new topic.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={},
            ),
        ),
        genai_types.FunctionDeclaration(
            name="show_diagram",
            description=(
                "Render a Mermaid diagram in the canvas panel — flowcharts, sequence "
                "diagrams, mindmaps, timelines, state/class/ER diagrams. Pass the raw "
                "Mermaid source (e.g. 'graph TD; A-->B'). Use this to visualize "
                "structure, flow, or relationships you're explaining out loud."
            ),
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "diagram": genai_types.Schema(type=genai_types.Type.STRING, description="Mermaid diagram source code"),
                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Optional panel title"),
                },
                required=["diagram"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="show_image",
            description=(
                "Display an image in the canvas panel. `src` is either a web URL "
                "(https://...) or a path to a file in your workspace "
                "(e.g. 'content/chart.png' or '/home/developer/content/chart.png'). "
                "Use to show a generated chart, screenshot, or diagram asset."
            ),
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "src": genai_types.Schema(type=genai_types.Type.STRING, description="Web URL or workspace file path"),
                    "title": genai_types.Schema(type=genai_types.Type.STRING, description="Optional panel title"),
                    "caption": genai_types.Schema(type=genai_types.Type.STRING, description="Optional caption shown under the image"),
                },
                required=["src"],
            ),
        ),
    ]
)

WORKSPACE_PANEL_INSTRUCTIONS = """
## Visual Canvas

You have a visual canvas visible to the user beside the conversation. It is the agent's own canvas — the same one you keep with `set_canvas` in text chat — so what you draw here persists after the call, on the Canvas tab. Use it proactively alongside your voice responses.

Panel tools (each draws a block on the canvas):
- `show_markdown(content, title?)` — Render markdown. Use most often for notes, summaries, action items, analysis. It may embed ```chart / ```kpi / ```table fences (JSON inside) and ```mermaid fences, which render as figures.
- `show_diagram(diagram, title?)` — Render a Mermaid diagram. Use for flowcharts, sequence diagrams, mindmaps, timelines, state/class/ER diagrams — anytime structure or flow is easier shown than spoken.
- `show_image(src, title?, caption?)` — Display an image by https URL or a file path in your workspace.
- `update_panel(html, title?)` — Replace your panel block with static HTML for richer layouts.
- `append_to_panel(html)` — Add to your panel block without clearing it.
- `clear_panel()` — Remove what you drew when shifting to a new topic. Blocks written earlier with `set_canvas` are left alone.

Guidelines:
- Voice is transient, the canvas is the artefact — it stays after the call.
- Use `show_markdown` by default. Reach for `show_diagram` when a picture of the structure helps, `update_panel` only when custom layout genuinely adds value.
- Don't mirror every voice response on the canvas — use it when structured content helps.
- Clear when the topic changes significantly.

Mermaid rule (for `show_diagram`):
- Pass raw Mermaid source only (no ```mermaid fences). Example: `graph TD; Start-->Stop`.
- Keep diagrams focused; invalid syntax shows a contained error on the canvas.

HTML rule (for `update_panel`):
- HTML is sanitized before display: scripts do NOT execute. Use it for static layout only — tables, headings, lists, `<div>`s, images.
- Style with the canvas kit classes only: `ck-card` (+ `ck-card-title`), `ck-grid-2/3/4`, `ck-section`, `ck-callout ck-info|ck-success|ck-warning|ck-danger`, `ck-chip`, `ck-kpi`, `ck-table`, `ck-figure` + `ck-caption`, `ck-muted`. Other classes, inline styles and `<style>` are dropped.
- Do NOT use `<script>`, `<canvas>` + JS charting, or any JS-driven rendering — it will be stripped and show nothing.
- For data visualisation, put a ```chart fence in `show_markdown` (Trinity draws it from the data), or use `show_diagram` / `show_image`.
"""


# Single tool declaration for all voice sessions
_RUN_TASK_TOOL = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="run_task",
            description=(
                "Execute a task in the agent's workspace — look something up, "
                "read a file, search for information, or perform an action. "
                "Use this when you need live data or agent capabilities to answer accurately. "
                "Returns a text response from the agent. "
                "This can take several seconds and you cannot speak while it runs — "
                "ALWAYS say a brief out-loud filler (e.g. 'let me check that') before "
                "calling it so the user isn't left in silence."
            ),
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    "prompt": genai_types.Schema(
                        type=genai_types.Type.STRING,
                        description="Clear description of what to look up or do",
                    )
                },
                required=["prompt"],
            ),
        )
    ]
)


# Spoken-filler etiquette appended to every voice session's system_instruction
# (browser + VoIP). `run_task` round-trips to the agent container and can take
# several seconds, during which Gemini — a *blocking* function call — emits no
# audio. On a phone call that dead air reads as a dropped line. Instruct the
# model to verbally acknowledge BEFORE it calls run_task so the caller knows it
# is still working. Cheap, model-side fix; no SDK change required.
_TOOL_ETIQUETTE_INSTRUCTION = """

## Looking things up out loud
When you use the `run_task` tool it can take several seconds to return, and you
cannot speak while it runs. Before EVERY `run_task` call, first say a short,
natural filler so the user knows you're working and the line never falls
silent — for example "let me check that for you", "one moment while I look that
up", or "give me a second to pull that up". Vary the wording so it sounds
natural. Never call `run_task` silently.
"""


@dataclass
class VoiceTranscriptEntry:
    """A single transcript entry from the voice session."""
    role: str          # "user" or "assistant"
    text: str


@dataclass
class VoiceSession:
    """Tracks state for an active voice session."""
    session_id: str
    agent_name: str
    chat_session_id: str
    user_id: int
    user_email: str
    system_prompt: str
    voice_name: str = "Kore"
    workspace_mode: bool = False
    # Max session length (seconds) before the watchdog auto-ends it. Browser voice
    # sessions use VOICE_MAX_DURATION; phone calls pass VOIP_MAX_CALL_DURATION.
    max_duration: int = VOICE_MAX_DURATION
    transcript: list = field(default_factory=list)
    # ent#536 — the audience this session may WRITE at. `operator` unless the
    # front door says otherwise: an Agent Detail call is an operator surface,
    # and a Workspace call (ent#534) sets `roster` so the person in the call can
    # see what was drawn. A voice write never lands on a canvas WIDER than this.
    canvas_audience: str = "operator"
    _gemini_session: object = field(default=None, repr=False)
    _send_task: object = field(default=None, repr=False)
    _receive_task: object = field(default=None, repr=False)
    _timeout_task: object = field(default=None, repr=False)
    _audio_in_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _pending_tool_tasks: dict = field(default_factory=dict)  # call_id → asyncio.Task
    _active: bool = False
    _duration_seconds: float = 0.0
    # Callbacks
    _on_audio_out: Optional[Callable] = field(default=None, repr=False)
    _on_transcript: Optional[Callable] = field(default=None, repr=False)
    _on_status: Optional[Callable] = field(default=None, repr=False)
    _on_tool_call: Optional[Callable] = field(default=None, repr=False)    # (name, args) → None
    _on_tool_result: Optional[Callable] = field(default=None, repr=False)  # (name, result) → None


class GeminiVoiceService:
    """Manages Gemini Live API voice sessions."""

    def __init__(self):
        self._client: Optional[genai.Client] = None
        self._sessions: dict[str, VoiceSession] = {}
        self._redis = None  # lazy-init async Redis client

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    def is_available(self) -> bool:
        """Check if Gemini voice is configured."""
        return bool(GEMINI_API_KEY)

    def _get_client(self) -> genai.Client:
        """Get or create the Gemini client."""
        if not self._client:
            if not GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY not configured")
            self._client = genai.Client(api_key=GEMINI_API_KEY)
        return self._client

    async def create_session(
        self,
        agent_name: str,
        chat_session_id: str,
        user_id: int,
        user_email: str,
        system_prompt: str,
        voice_name: str = "Kore",
        workspace_mode: bool = False,
        max_duration: Optional[int] = None,
    ) -> VoiceSession:
        """Create a new voice session (does not connect yet).

        `max_duration` overrides the watchdog's auto-end timeout (seconds). The
        browser voice path leaves it None (→ VOICE_MAX_DURATION); the phone path
        passes VOIP_MAX_CALL_DURATION so calls aren't cut at the 5-min voice cap.
        """
        session_id = f"vs_{secrets.token_urlsafe(16)}"
        effective_max_duration = max_duration if max_duration is not None else VOICE_MAX_DURATION
        session = VoiceSession(
            session_id=session_id,
            agent_name=agent_name,
            chat_session_id=chat_session_id,
            user_id=user_id,
            user_email=user_email,
            system_prompt=system_prompt,
            voice_name=voice_name,
            workspace_mode=workspace_mode,
            max_duration=effective_max_duration,
        )
        self._sessions[session_id] = session

        # Persist metadata to Redis so any Uvicorn worker can validate the session.
        # The active streaming state (Gemini connection, asyncio tasks) stays in-process.
        metadata = {
            "session_id": session_id,
            "agent_name": agent_name,
            "chat_session_id": chat_session_id,
            "user_id": user_id,
            "user_email": user_email,
            "voice_name": voice_name,
            "workspace_mode": workspace_mode,
            "system_prompt": system_prompt,
            "max_duration": effective_max_duration,
        }
        try:
            r = await self._get_redis()
            # TTL spans the session's own max duration (+grace), not the shared
            # voice cap — otherwise a 10-min phone session's metadata expires at
            # ~6min and cross-worker validation would fail mid-call.
            redis_ttl = effective_max_duration + 60
            await r.setex(f"voice_session:{session_id}", redis_ttl, json.dumps(metadata))
        except Exception as e:
            # Fail loudly: better to 500 at /voice/start than issue a session_id
            # that will intermittently 403 when the WebSocket lands on another worker.
            self._sessions.pop(session_id, None)
            raise RuntimeError(f"Failed to persist voice session metadata to Redis: {e}") from e

        logger.info(f"Voice session created: {session_id} for agent {agent_name}")
        return session

    async def connect_and_stream(
        self,
        session_id: str,
        on_audio_out: Callable[[bytes], Awaitable[None]],
        on_transcript: Callable[[str, str], Awaitable[None]],  # (role, text)
        on_status: Callable[[str], Awaitable[None]],           # status string
        on_tool_call: Optional[Callable] = None,               # (name, args) → None
        on_tool_result: Optional[Callable] = None,             # (name, result) → None
    ):
        """
        Connect to Gemini Live API and begin streaming.

        This is the main loop that runs for the lifetime of the voice session.
        It spawns send/receive tasks and waits until the session ends.
        Tool calls are executed asynchronously against the agent container.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Voice session {session_id} not found")

        session._on_audio_out = on_audio_out
        session._on_transcript = on_transcript
        session._on_status = on_status
        session._on_tool_call = on_tool_call
        session._on_tool_result = on_tool_result
        session._active = True

        client = self._get_client()

        tools = [_RUN_TASK_TOOL]
        if session.workspace_mode:
            tools.append(_PANEL_TOOLS)

        config = genai_types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=session.system_prompt + _TOOL_ETIQUETTE_INSTRUCTION,
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=session.voice_name
                    )
                )
            ),
            tools=tools,
        )

        try:
            await on_status("connecting")

            async with client.aio.live.connect(
                model=VOICE_MODEL,
                config=config,
            ) as gemini_session:
                session._gemini_session = gemini_session
                await on_status("listening")

                # Run send and receive concurrently with a timeout
                async with asyncio.TaskGroup() as tg:
                    session._send_task = tg.create_task(
                        self._send_audio_loop(session)
                    )
                    session._receive_task = tg.create_task(
                        self._receive_audio_loop(session)
                    )
                    session._timeout_task = tg.create_task(
                        self._timeout_watchdog(session)
                    )

        except* asyncio.CancelledError:
            logger.info(f"Voice session {session_id} cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error(f"Voice session {session_id} error: {exc}")
        finally:
            session._active = False
            await on_status("ended")
            logger.info(f"Voice session {session_id} ended, transcript entries: {len(session.transcript)}")

    async def _send_audio_loop(self, session: VoiceSession):
        """Forward audio from the input queue to Gemini."""
        while session._active:
            try:
                chunk = await asyncio.wait_for(
                    session._audio_in_queue.get(), timeout=1.0
                )
                if chunk is None:
                    # Poison pill — stop sending
                    break
                await session._gemini_session.send_realtime_input(
                    audio={"data": chunk, "mime_type": f"audio/pcm;rate={INPUT_SAMPLE_RATE}"}
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Send audio error: {e}")
                break

    async def _receive_audio_loop(self, session: VoiceSession):
        """Receive audio, transcriptions, and tool calls from Gemini."""
        current_user_text = ""
        current_assistant_text = ""

        while session._active:
            try:
                turn = session._gemini_session.receive()
                async for response in turn:
                    if not session._active:
                        return

                    # Tool calls — spawn async task per call, keyed by call_id
                    if hasattr(response, 'tool_call') and response.tool_call:
                        fc_list = getattr(response.tool_call, 'function_calls', []) or []
                        for fc in fc_list:
                            call_id = getattr(fc, 'id', None) or secrets.token_hex(8)
                            task = asyncio.create_task(
                                self._execute_and_respond(session, call_id, fc)
                            )
                            session._pending_tool_tasks[call_id] = task
                        continue

                    content = response.server_content
                    if not content:
                        continue

                    # Audio output
                    if content.model_turn:
                        if session._on_status:
                            await session._on_status("speaking")
                        for part in content.model_turn.parts:
                            if part.inline_data and isinstance(part.inline_data.data, bytes):
                                if session._on_audio_out:
                                    await session._on_audio_out(part.inline_data.data)

                    # Input transcription (what the user said)
                    if hasattr(content, 'input_transcription') and content.input_transcription:
                        text = content.input_transcription.text
                        if text and text.strip():
                            current_user_text += text
                            if session._on_transcript:
                                await session._on_transcript("user", text)

                    # Output transcription (what Gemini said)
                    if hasattr(content, 'output_transcription') and content.output_transcription:
                        text = content.output_transcription.text
                        if text and text.strip():
                            current_assistant_text += text
                            if session._on_transcript:
                                await session._on_transcript("assistant", text)

                    # Turn complete
                    if content.turn_complete:
                        if session._on_status:
                            await session._on_status("listening")

                        if current_user_text.strip():
                            session.transcript.append(
                                VoiceTranscriptEntry(role="user", text=current_user_text.strip())
                            )
                            current_user_text = ""
                        if current_assistant_text.strip():
                            session.transcript.append(
                                VoiceTranscriptEntry(role="assistant", text=current_assistant_text.strip())
                            )
                            current_assistant_text = ""

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if session._active:
                    logger.error(f"Receive audio error: {e}")
                break

        # Flush any remaining text
        if current_user_text.strip():
            session.transcript.append(
                VoiceTranscriptEntry(role="user", text=current_user_text.strip())
            )
        if current_assistant_text.strip():
            session.transcript.append(
                VoiceTranscriptEntry(role="assistant", text=current_assistant_text.strip())
            )

    # ent#536 — the voice panel IS the agent's default canvas.
    #
    # There is no in-session copy of the panel any more: the canvas row is the
    # state, the live poll reads the row, and the Canvas tab shows the same
    # blocks after the call. Each panel verb is mapped onto a block edit by
    # `canvas_blocks.map_panel_tool` — `show_*` replaces the `voice` block,
    # `append_to_panel` grows it, `clear_panel` removes the voice blocks and
    # nothing else — and written through the SAME `canvas_service.write_canvas`
    # the agent's `set_canvas` uses, so caps and per-kind rules cannot diverge.
    #
    # Audience is a property of the WRITE, not the canvas: the session carries
    # the widest audience it may publish at (`canvas_audience`, default
    # `operator`). A canvas stored WIDER than that is refused with a reason the
    # model can voice — an operator's call must never land on a customer's
    # Workspace because the agent had published its board there — and a canvas
    # stored narrower keeps its stored audience, so a call never widens either.
    _CANVAS_ID = DEFAULT_CANVAS_ID

    def _execute_panel_tool(self, session: VoiceSession, tool_name: str, args: dict) -> str:
        """Handle panel tools in-process (no agent container call).

        Fail-soft on the store: the voice turn is the thing the person is
        watching, and a canvas write that fails must not break the tool result
        the model is waiting on. The result SAYS the canvas could not be saved
        rather than claiming success, so the model does not describe a drawing
        nobody can see.
        """
        try:
            from database import db
            from services import canvas_service

            current = db.get_agent_canvas(session.agent_name, self._CANVAS_ID)
            stored_audience = (current or {}).get("audience") or session.canvas_audience
            if not canvas_service.audience_within(stored_audience, session.canvas_audience):
                return (
                    f"Canvas not updated: your '{self._CANVAS_ID}' canvas is published to "
                    f"'{stored_audience}' readers and this call may only write for "
                    f"'{session.canvas_audience}'. Drawing here would show this conversation "
                    "to them, so nothing was drawn."
                )
            blocks, message = map_panel_tool(
                tool_name, args, (current or {}).get("blocks") or []
            )
            if blocks is None:
                return message
            canvas_service.write_canvas(
                session.agent_name,
                self._CANVAS_ID,
                blocks,
                title=(current or {}).get("title"),
                audience=stored_audience,
                execution_id=None,
                # ent#537 — a voice edit keeps the board's layout; every writer
                # of the row carries the same fields (the 2026-08-24 rule).
                template=(current or {}).get("template"),
            )
            return message
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "voice panel: canvas write failed for %s: %s", session.agent_name, e
            )
            return f"Canvas could not be saved: {str(e)[:200]}"

    async def _execute_and_respond(self, session: VoiceSession, call_id: str, fc):
        """Execute a Gemini tool call and send the response back. Runs as a background task."""
        tool_name = getattr(fc, 'name', 'run_task')
        args = dict(fc.args) if getattr(fc, 'args', None) else {}

        try:
            if session._on_tool_call:
                await session._on_tool_call(tool_name, args)

            if tool_name in _PANEL_TOOL_NAMES:
                result = self._execute_panel_tool(session, tool_name, args)
            else:
                result = await asyncio.wait_for(
                    self._execute_tool(session.agent_name, tool_name, args),
                    timeout=30.0,
                )
        except asyncio.TimeoutError:
            result = "Tool execution timed out after 30 seconds."
            logger.warning(f"Voice tool call timed out: {tool_name} session={session.session_id}")
        except Exception as e:
            result = f"Tool error: {str(e)[:200]}"
            logger.error(f"Voice tool call error: {e}")
        finally:
            session._pending_tool_tasks.pop(call_id, None)

        if session._on_tool_result:
            try:
                await session._on_tool_result(tool_name, result)
            except Exception:
                pass

        if session._gemini_session and session._active:
            try:
                await session._gemini_session.send_tool_response(
                    function_responses=[
                        genai_types.FunctionResponse(
                            id=call_id,
                            name=tool_name,
                            response={"output": result},
                        )
                    ]
                )
            except Exception as e:
                logger.error(f"Failed to send tool response for {call_id}: {e}")

    async def _execute_tool(self, agent_name: str, tool_name: str, args: dict) -> str:
        """Route a tool call to the agent container via the task endpoint."""
        from services.agent_client import get_agent_client, AgentNotReachableError, AgentRequestError

        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return "No prompt provided."
        if len(prompt) > _TOOL_PROMPT_MAX:
            prompt = prompt[:_TOOL_PROMPT_MAX] + "..."

        logger.info(f"Voice tool call: agent={agent_name} tool={tool_name} prompt={prompt[:80]!r}")
        try:
            client = get_agent_client(agent_name)
            response = await client.task(prompt, timeout=28.0)
            return response.response_text or "Task completed with no response."
        except AgentNotReachableError:
            return f"Agent {agent_name!r} is not currently running."
        except AgentRequestError as e:
            return f"Task error: {str(e)[:200]}"
        except Exception as e:
            logger.error(f"Voice tool execution error for {agent_name}: {e}")
            return f"Execution error: {str(e)[:200]}"

    async def _timeout_watchdog(self, session: VoiceSession):
        """Auto-end session after its max duration (per-session; phone calls
        use VOIP_MAX_CALL_DURATION, browser voice uses VOICE_MAX_DURATION)."""
        await asyncio.sleep(session.max_duration)
        if session._active:
            logger.info(f"Voice session {session.session_id} hit max duration ({session.max_duration}s)")
            await self.end_session(session.session_id)

    async def send_audio(self, session_id: str, audio_data: bytes):
        """Queue audio data for sending to Gemini."""
        session = self._sessions.get(session_id)
        if session and session._active:
            await session._audio_in_queue.put(audio_data)

    async def end_session(self, session_id: str) -> Optional[VoiceSession]:
        """End a voice session and return it with transcript."""
        session = self._sessions.get(session_id)
        if not session:
            return None

        session._active = False

        # Send poison pill to unblock send loop
        await session._audio_in_queue.put(None)

        # Cancel pending tool tasks
        for task in list(session._pending_tool_tasks.values()):
            if not task.done():
                task.cancel()
        session._pending_tool_tasks.clear()

        # Cancel send/receive/timeout tasks
        for task in [session._send_task, session._receive_task, session._timeout_task]:
            if task and not task.done():
                task.cancel()

        logger.info(f"Voice session {session_id} ended")
        return session

    async def get_session(self, session_id: str) -> Optional[VoiceSession]:
        """Get a voice session by ID.

        Checks in-process memory first. If not found (cross-worker scenario),
        falls back to Redis metadata and reconstructs a VoiceSession so the
        WebSocket handler on any worker can validate ownership and stream.
        """
        session = self._sessions.get(session_id)
        if session is not None:
            return session

        # Cross-worker fallback: reconstruct from Redis metadata
        try:
            r = await self._get_redis()
            raw = await r.get(f"voice_session:{session_id}")
            if not raw:
                return None
            meta = json.loads(raw)
        except Exception as e:
            logger.warning(f"Redis fallback failed for voice session {session_id}: {e}")
            return None

        session = VoiceSession(
            session_id=meta["session_id"],
            agent_name=meta["agent_name"],
            chat_session_id=meta["chat_session_id"],
            user_id=meta["user_id"],
            user_email=meta["user_email"],
            system_prompt=meta["system_prompt"],
            voice_name=meta.get("voice_name", "Kore"),
            workspace_mode=meta.get("workspace_mode", False),
            max_duration=meta.get("max_duration", VOICE_MAX_DURATION),
        )
        self._sessions[session_id] = session
        logger.info(f"Voice session {session_id} reconstructed from Redis on worker")
        return session

    async def remove_session(self, session_id: str):
        """Remove a session from tracking and clean up Redis metadata."""
        self._sessions.pop(session_id, None)
        try:
            r = await self._get_redis()
            await r.delete(f"voice_session:{session_id}")
        except Exception as e:
            logger.warning(f"Failed to delete voice session Redis key {session_id}: {e}")


# Singleton
voice_service = GeminiVoiceService()
