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
import posixpath
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

from google import genai
from google.genai import types as genai_types

from config import GEMINI_API_KEY, VOICE_MODEL, VOICE_MAX_DURATION, REDIS_URL

logger = logging.getLogger(__name__)

# Audio format constants
INPUT_SAMPLE_RATE = 16000   # 16kHz PCM input to Gemini
OUTPUT_SAMPLE_RATE = 24000  # 24kHz PCM output from Gemini

# Max chars for tool call prompts (prevent injection via very long args)
_TOOL_PROMPT_MAX = 2000
# Max bytes stored in panel_state["content"] to bound memory per session
_PANEL_CONTENT_MAX = 524_288  # 512 KB

# Agent workspace root inside the container; show_image file paths must resolve under it.
_WORKSPACE_ROOT = "/home/developer"

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

You have a visual canvas panel visible to the user on the right side of the screen. Use it proactively alongside your voice responses.

Panel tools:
- `show_markdown(content, title?)` — Render markdown. Use most often for notes, summaries, action items, analysis.
- `show_diagram(diagram, title?)` — Render a Mermaid diagram. Use for flowcharts, sequence diagrams, mindmaps, timelines, state/class/ER diagrams — anytime structure or flow is easier shown than spoken.
- `show_image(src, title?, caption?)` — Display an image by web URL or workspace file path.
- `update_panel(html, title?)` — Replace panel with HTML for richer layouts.
- `append_to_panel(html)` — Add to existing panel without clearing.
- `clear_panel()` — Clear when shifting to a new topic.

Guidelines:
- The panel is a persistent whiteboard — voice is transient, the panel is the artefact. Each update is kept in history, so the user can scroll back through what you drew.
- Use `show_markdown` by default. Reach for `show_diagram` when a picture of the structure helps, `update_panel` only when custom layout genuinely adds value.
- Don't mirror every voice response in the panel — use it when structured content helps.
- Clear when the topic changes significantly.

Mermaid rule (for `show_diagram`):
- Pass raw Mermaid source only (no ```mermaid fences). Example: `graph TD; Start-->Stop`.
- Keep diagrams focused; invalid syntax shows a contained error in the panel.

HTML rule (for `update_panel`):
- Panel HTML is sanitized before display: scripts do NOT execute. Use it for static layout only — tables, headings, lists, styled `<div>`s, inline `style=` attributes, images.
- Do NOT use `<script>`, `<canvas>` + JS charting, or any JS-driven rendering — it will be stripped and show nothing.
- For data visualisation, prefer `show_diagram` (Mermaid: fl/pie/quadrant/xychart) or `show_image` (a chart image by URL or workspace path). Reserve `update_panel` for rich static layouts that markdown can't express.
"""

def _classify_image_src(src: str) -> Optional[tuple[str, str]]:
    """Classify a show_image src as a web URL or a workspace-confined file path.

    Returns (value, kind) where kind is "url" or "path", or None if the src is
    neither an allowed web URL nor a path that resolves inside the agent
    workspace. The frontend renders "url" directly and fetches "path" through the
    authenticated /files/preview endpoint, so this is the only confinement gate
    on the panel side — and it is stricter than the agent-server's prefix check,
    rejecting sibling escapes like /home/developer-evil and any '..' traversal.
    """
    if not src:
        return None
    low = src.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return (src, "url")
    if low.startswith("data:") or "://" in low:
        # data: URIs (unbounded inline bytes) and other schemes (file:, ftp:, …)
        # are not allowed.
        return None

    # Treat as a workspace file path. Accept absolute (/home/developer/...) or
    # relative (resolved against the workspace root). '~/' is workspace-relative.
    path = src[2:] if src.startswith("~/") else src
    # A ':' in the first path segment is a URI scheme (javascript:, mailto:, …),
    # not a real path segment — reject before resolving.
    if ":" in path.split("/", 1)[0]:
        return None
    if path.startswith("/"):
        candidate = posixpath.normpath(path)
    else:
        candidate = posixpath.normpath(posixpath.join(_WORKSPACE_ROOT, path))
    if candidate != _WORKSPACE_ROOT and not candidate.startswith(_WORKSPACE_ROOT + "/"):
        return None
    return (candidate, "path")


# Single tool declaration for all voice sessions
_RUN_TASK_TOOL = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="run_task",
            description=(
                "Execute a task in the agent's workspace — look something up, "
                "read a file, search for information, or perform an action. "
                "Use this when you need live data or agent capabilities to answer accurately. "
                "Returns a text response from the agent."
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
    transcript: list = field(default_factory=list)
    panel_state: dict = field(default_factory=lambda: {
        "type": "empty", "content": "", "title": None, "updated_at": None
    })
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


_REDIS_SESSION_TTL = VOICE_MAX_DURATION + 60  # grace buffer beyond max session length


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
    ) -> VoiceSession:
        """Create a new voice session (does not connect yet)."""
        session_id = f"vs_{secrets.token_urlsafe(16)}"
        session = VoiceSession(
            session_id=session_id,
            agent_name=agent_name,
            chat_session_id=chat_session_id,
            user_id=user_id,
            user_email=user_email,
            system_prompt=system_prompt,
            voice_name=voice_name,
            workspace_mode=workspace_mode,
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
        }
        try:
            r = await self._get_redis()
            await r.setex(f"voice_session:{session_id}", _REDIS_SESSION_TTL, json.dumps(metadata))
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
            system_instruction=session.system_prompt,
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

    def _execute_panel_tool(self, session: VoiceSession, tool_name: str, args: dict) -> str:
        """Handle panel tools in-process (no agent container call)."""
        now = datetime.now(timezone.utc).isoformat()
        if tool_name == "show_markdown":
            session.panel_state = {
                "type": "markdown",
                "content": args.get("content", ""),
                "title": args.get("title"),
                "updated_at": now,
            }
        elif tool_name == "show_diagram":
            session.panel_state = {
                "type": "mermaid",
                "content": args.get("diagram", ""),
                "title": args.get("title"),
                "updated_at": now,
            }
        elif tool_name == "show_image":
            src = str(args.get("src", "")).strip()
            if not src:
                return "No image source provided."
            kind = _classify_image_src(src)
            if kind is None:
                # Not a web URL and not a workspace-confined file path — reject.
                return (
                    "Image rejected: src must be an https:// URL or a path inside "
                    "your workspace (/home/developer). Path traversal is not allowed."
                )
            value, image_kind = kind
            session.panel_state = {
                "type": "image",
                "content": value,
                "image_kind": image_kind,   # "url" | "path"
                "caption": args.get("caption"),
                "title": args.get("title"),
                "updated_at": now,
            }
        elif tool_name == "update_panel":
            session.panel_state = {
                "type": "html",
                "content": args.get("html", ""),
                "title": args.get("title"),
                "updated_at": now,
            }
        elif tool_name == "append_to_panel":
            combined = session.panel_state.get("content", "") + args.get("html", "")
            if len(combined) > _PANEL_CONTENT_MAX:
                combined = combined[-_PANEL_CONTENT_MAX:]
            session.panel_state = {
                "type": session.panel_state.get("type", "html"),
                "content": combined,
                "title": session.panel_state.get("title"),
                "updated_at": now,
            }
        elif tool_name == "clear_panel":
            session.panel_state = {
                "type": "empty", "content": "", "title": None, "updated_at": now,
            }
        return "Panel updated."

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
        """Auto-end session after max duration."""
        await asyncio.sleep(VOICE_MAX_DURATION)
        if session._active:
            logger.info(f"Voice session {session.session_id} hit max duration ({VOICE_MAX_DURATION}s)")
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
