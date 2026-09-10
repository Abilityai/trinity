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
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable

from google import genai
from google.genai import types as genai_types

from config import GEMINI_API_KEY, VOICE_MODEL, VOICE_MAX_DURATION, REDIS_URL
from models import DEFAULT_CANVAS_ID
from services.canvas_blocks import WORKSPACE_ROOT, classify_image_src, map_panel_tool
from services.voice_tools import RUN_TASK, platform_default_tools, resolve_manifest

logger = logging.getLogger(__name__)

# Audio format constants
INPUT_SAMPLE_RATE = 16000   # 16kHz PCM input to Gemini
OUTPUT_SAMPLE_RATE = 24000  # 24kHz PCM output from Gemini

# ent#534 — a session must outlive the provider's connection.
#
# Gemini Live ends an audio-only session at ~15 minutes unless context-window
# compression is on, and recycles the underlying connection at ~10 minutes,
# announcing it with a `go_away` message first. The 300 s Agent Detail cap hid
# both; the Workspace's 30-minute cap does not. Every session therefore asks
# for compression (lifts the 15-minute wall) and session resumption (a handle
# the server refreshes as the call goes), and `connect_and_stream` reconnects
# with the latest handle when a `go_away` arrives — the browser WebSocket, the
# watchdog and the transcript all span the whole call; only the provider leg
# is swapped.
MAX_RECONNECTS_PER_SESSION = 8
# How far before the cap the model is asked to wrap up, out loud (ent#534).
CAP_WARNING_LEAD_SECONDS = 30
_CAP_WARNING_TEXT = (
    "[System notice: this call reaches its time limit in about thirty seconds. "
    "Tell the person the call is about to end and wrap up in one short sentence.]"
)

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


# ent#535 — how long the model waits before it must say something. Past this
# the turn keeps running and its answer lands in the chat; the call never
# freezes and the work is never thrown away. Deliberately shorter than any
# turn timeout: it bounds SPEECH, not the task.
_SPOKEN_BUDGET_SECONDS = 20.0

# What the model is told when the budget passes. Phrased as a fact about where
# the answer will appear, because the model reads it out and the person needs
# to know to look at the chat rather than keep waiting for a voice answer.
_STILL_WORKING_RESULT = (
    "Still working on that one. It is running in this chat and the answer will "
    "appear there when it is done — tell the user that, and carry on."
)

# Strong references for turns that outlived their spoken budget. asyncio holds
# only a weak reference to a bare `create_task`, so a detached turn could be
# collected mid-flight — losing work the person asked for, with the reply row
# never written and nothing to say why (the #1083 `_inflight` footgun).
_detached_turns: set = set()


def _session_manifest(session: "VoiceSession") -> frozenset:
    """The tool names this session may call.

    `None` means the manifest was never resolved (a session built directly, an
    older reconstruction) — fall back to the platform default, which is the
    pre-ent#535 surface and the safe answer.

    An EMPTY frozenset is a decision, not an absence: an agent that declared
    `voice.tools: []` wants no tools, and reading that as "unset" would hand the
    strongest possible narrowing the widest possible manifest. This is why the
    field is tri-state rather than falsy-checked — the first cut of this used
    `session.tool_manifest or default`, which had exactly that inversion.

    A value that is not a set at all cannot be trusted to answer `in`, so it is
    also treated as unresolved rather than crashing the audio loop.
    """
    manifest = getattr(session, "tool_manifest", None)
    if isinstance(manifest, (set, frozenset)):
        return frozenset(manifest)
    return platform_default_tools(
        workspace_mode=bool(getattr(session, "workspace_mode", False))
    )


def _manifest_from_meta(meta: dict) -> Optional[frozenset]:
    """Read a persisted tool manifest back, preserving the tri-state.

    `None` (absent or stored null) = never resolved; the caller falls back to
    the platform default. A list — INCLUDING an empty one — is a decision and
    comes back as a frozenset. Anything else (a dict, a string, a number: a
    hand-edited or corrupted blob) is not a manifest and is treated as
    unresolved rather than crashing the audio loop, which is the same rule
    `_session_manifest` applies to the field itself.
    """
    raw = (meta or {}).get("tool_manifest")
    if isinstance(raw, list):
        return frozenset(str(t) for t in raw)
    return None


def _tool_prompt(args: dict) -> str:
    """The `prompt` argument, trimmed and capped. One reader, so the container
    path and the chat path cannot disagree about what was asked."""
    prompt = str((args or {}).get("prompt", "")).strip()
    if len(prompt) > _TOOL_PROMPT_MAX:
        prompt = prompt[:_TOOL_PROMPT_MAX] + "..."
    return prompt


def _is_workspace_bound(session: "VoiceSession") -> bool:
    """Can this call run a turn in a chat? Both halves or neither — a thread id
    with no email cannot be attributed and an email with no thread has nowhere
    to land, and either alone would silently fall back to the container."""
    return bool(session.portal_session_id and session.client_email)


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
    """Tracks state for an active voice session.

    Two front doors share it (ent#534): the Agent Detail overlay binds the
    session to a `chat_session_id` (transcript → `chat_messages` at the end),
    the Workspace binds it to a `portal_session_id` + `client_email`
    (transcript → `enterprise_portal_messages`, turn by turn). Exactly one of
    the two is set.
    """
    session_id: str
    agent_name: str
    chat_session_id: Optional[str]
    user_id: int
    user_email: str
    system_prompt: str
    voice_name: str = "Kore"
    workspace_mode: bool = False
    portal_session_id: Optional[str] = None
    client_email: Optional[str] = None
    # How the call ended, for the words the surface shows and the row the chat
    # keeps: None while live or ended by the person; "cap" at the time limit;
    # "error" when the provider leg failed; "provider_closed" when it closed
    # without a go_away and no reconnect was possible.
    end_reason: Optional[str] = None
    end_message: Optional[str] = None
    # Max session length (seconds) before the watchdog auto-ends it. Browser voice
    # sessions use VOICE_MAX_DURATION; phone calls pass VOIP_MAX_CALL_DURATION.
    max_duration: int = VOICE_MAX_DURATION
    transcript: list = field(default_factory=list)
    # ent#536 — the audience this session may WRITE at. `operator` for both
    # front doors: an Agent Detail call is an operator surface, and so is a
    # Workspace call (ent#534) — an internal user's call, whose drawings the
    # Workspace shows them because a platform principal reads every audience
    # there, not because the call published wider. A voice write never lands on
    # a canvas WIDER than this.
    canvas_audience: str = "operator"
    # ent#535 review — whether the caller reached this call through the PLATFORM
    # door, carried on the session for the same reason `canvas_audience` is: the
    # turn path re-asserts it, and a constant at that line is a scope decision
    # made a long way from the gate that authorizes it.
    #
    # `include_owned` widens `agent_on_roster` past what was shared with the
    # caller. Today only `start_workspace_voice` writes `portal_session_id` +
    # `client_email`, and it refuses a non-platform caller outright — so the
    # constant is correct RIGHT NOW and wrong the first time an external-client
    # voice call sets those two fields, which would silently widen the roster
    # check with no change at that line (an Invariant #8 scope break).
    #
    # Defaults FALSE: a session assembled by some future path that does not
    # think about this gets the NARROW answer.
    is_platform: bool = False
    # ent#535 — the tool names this session may call, resolved ONCE at start
    # from the platform default narrowed by the agent's own declaration. The
    # dispatcher refuses everything outside it; nothing later can widen it.
    tool_manifest: Optional[frozenset] = None
    # ent#535 — turns still running past their spoken budget. The surface shows
    # a badge while this is non-zero; it is a count and not a flag because two
    # tasks can be in flight and the first to finish must not clear the badge.
    _pending_turns: int = 0
    _gemini_session: object = field(default=None, repr=False)
    _send_task: object = field(default=None, repr=False)
    _receive_task: object = field(default=None, repr=False)
    _timeout_task: object = field(default=None, repr=False)
    _audio_in_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _pending_tool_tasks: dict = field(default_factory=dict)  # call_id → asyncio.Task
    _active: bool = False
    _duration_seconds: float = 0.0
    _started_monotonic: float = 0.0
    # ent#534 — provider-connection lifetime (see MAX_RECONNECTS_PER_SESSION)
    _resumption_handle: Optional[str] = field(default=None, repr=False)
    _go_away: bool = False
    _reconnects: int = 0
    # ent#534 — the turn in progress, mirrored from the receive loop so that an
    # End pressed before the provider's `turn_complete` (the common case: the
    # person hangs up the moment the answer lands) still records what was said.
    _partial_user_text: str = field(default="", repr=False)
    _partial_assistant_text: str = field(default="", repr=False)
    # ent#534 — the Agent Detail save-at-end path must run once per call even
    # when the WebSocket `finally` and `/stop` both reach it.
    _transcript_saved: bool = False
    # Callbacks
    _on_audio_out: Optional[Callable] = field(default=None, repr=False)
    _on_transcript: Optional[Callable] = field(default=None, repr=False)
    _on_status: Optional[Callable] = field(default=None, repr=False)
    _on_tool_call: Optional[Callable] = field(default=None, repr=False)    # (name, args) → None
    _on_tool_result: Optional[Callable] = field(default=None, repr=False)  # (name, result) → None
    _on_turn: Optional[Callable] = field(default=None, repr=False)         # (role, text) → None, per completed turn


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
        chat_session_id: Optional[str],
        user_id: int,
        user_email: str,
        system_prompt: str,
        voice_name: str = "Kore",
        workspace_mode: bool = False,
        max_duration: Optional[int] = None,
        portal_session_id: Optional[str] = None,
        client_email: Optional[str] = None,
        canvas_audience: str = "operator",
        declared_tools: Any = None,
        is_platform: bool = False,
    ) -> VoiceSession:
        """Create a new voice session (does not connect yet).

        `max_duration` overrides the watchdog's auto-end timeout (seconds). The
        browser voice path leaves it None (→ VOICE_MAX_DURATION); the phone path
        passes VOIP_MAX_CALL_DURATION so calls aren't cut at the 5-min voice cap;
        the Workspace (ent#534) passes WORKSPACE_VOICE_MAX_DURATION.

        `portal_session_id` + `client_email` bind the call to a Workspace thread
        instead of a chat session (ent#534). `canvas_audience` is the widest
        audience the call may draw at (ent#536).

        `declared_tools` is the agent's own `template.yaml` `voice: tools:` list,
        if it has one. It may only NARROW the platform default (ent#535) — the
        file is agent-writable, so a declaration that could ADD would let an
        agent grant itself a capability by editing itself. The resolved set is
        locked onto the session here and never recomputed.
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
            portal_session_id=portal_session_id,
            client_email=client_email,
            canvas_audience=canvas_audience,
            tool_manifest=resolve_manifest(declared_tools, workspace_mode=workspace_mode),
            is_platform=is_platform,
        )
        self._sessions[session_id] = session

        # Persist metadata to Redis so any Uvicorn worker can validate the session.
        # The active streaming state (Gemini connection, asyncio tasks) stays in-process.
        # EVERY field a reconstructed session decides on must be here — a
        # worker that rebuilds the session from this blob and then streams the
        # call would otherwise draw at the default audience or save the
        # transcript to the wrong place (ent#534 review).
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
            "portal_session_id": portal_session_id,
            "client_email": client_email,
            "canvas_audience": canvas_audience,
            "is_platform": is_platform,
            # ent#535 review — WITHOUT this the "locked" manifest is silently
            # unlocked by the cross-worker rebuild: `get_session` would pass
            # nothing, `_session_manifest` would read `None` as "never
            # resolved", and the reconstruction would hand the model the FULL
            # platform default. Production runs `--workers 2` and the WebSocket
            # routinely lands on a worker other than the one `/voice/start` ran
            # on, so an agent that declared `voice.tools: [run_task]` to keep
            # the model off its canvas would get every canvas tool the moment
            # the call connected — in the config AND in the dispatcher, with no
            # log line, because from that worker's view nothing was narrowed.
            #
            # `sorted(...)`/`None`, not the frozenset: `json.dumps` cannot
            # serialize a set, so writing it raw would raise inside the try and
            # lose the WHOLE blob. The tri-state has to survive the round trip
            # (`None` ≠ `[]`) or this reintroduces the exact inversion #535
            # fixed — an agent declaring NO tools would read as "unset" and get
            # the widest manifest there is.
            "tool_manifest": (None if session.tool_manifest is None
                              else sorted(session.tool_manifest)),
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
        on_turn: Optional[Callable] = None,                    # (role, text) → None, per completed turn
    ):
        """
        Connect to Gemini Live API and begin streaming.

        This is the main loop that runs for the lifetime of the voice session.
        It spawns send/receive tasks and waits until the session ends.
        Tool calls are executed asynchronously against the agent container.

        ent#534: the provider CONNECTION may be replaced during the session. A
        `go_away` from the server ends the current leg; the loop reconnects with
        the latest resumption handle (bounded by MAX_RECONNECTS_PER_SESSION) and
        the call continues — the watchdog, the browser socket and the transcript
        are all session-scoped, not connection-scoped.
        """
        session = self._sessions.get(session_id)
        if not session:
            raise ValueError(f"Voice session {session_id} not found")

        session._on_audio_out = on_audio_out
        session._on_transcript = on_transcript
        session._on_status = on_status
        session._on_tool_call = on_tool_call
        session._on_tool_result = on_tool_result
        session._on_turn = on_turn
        session._active = True
        session._started_monotonic = time.monotonic()

        client = self._get_client()

        # The watchdog spans the CALL, so it lives outside the per-connection
        # TaskGroup below.
        session._timeout_task = asyncio.create_task(self._timeout_watchdog(session))

        try:
            await on_status("connecting")

            while session._active:
                session._go_away = False
                config = self._build_live_config(session)
                async with client.aio.live.connect(
                    model=VOICE_MODEL,
                    config=config,
                ) as gemini_session:
                    session._gemini_session = gemini_session
                    await on_status("listening")

                    # Run send and receive concurrently for this connection leg.
                    async with asyncio.TaskGroup() as tg:
                        session._send_task = tg.create_task(
                            self._send_audio_loop(session)
                        )
                        session._receive_task = tg.create_task(
                            self._receive_audio_loop(session)
                        )
                session._gemini_session = None

                if not session._active:
                    break
                if (session._go_away and session._resumption_handle
                        and session._reconnects < MAX_RECONNECTS_PER_SESSION):
                    session._reconnects += 1
                    logger.info(
                        "Voice session %s: provider go_away, reconnecting (%d/%d)",
                        session_id, session._reconnects, MAX_RECONNECTS_PER_SESSION,
                    )
                    await on_status("connecting")
                    continue
                # The provider leg ended and we cannot (or may not) reconnect:
                # the call is over, and the surface is told why.
                if session.end_reason is None:
                    session.end_reason = "provider_closed"
                    session.end_message = (
                        "The voice connection closed."
                        if not session._go_away
                        else "The voice connection could not be resumed."
                    )
                break

        except* asyncio.CancelledError:
            logger.info(f"Voice session {session_id} cancelled")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error(f"Voice session {session_id} error: {exc}")
            if session.end_reason is None:
                session.end_reason = "error"
                session.end_message = "The voice provider returned an error."
        finally:
            session._active = False
            session._duration_seconds = max(
                session._duration_seconds,
                time.monotonic() - session._started_monotonic if session._started_monotonic else 0.0,
            )
            if session._timeout_task and not session._timeout_task.done():
                session._timeout_task.cancel()
            await on_status("ended")
            logger.info(f"Voice session {session_id} ended, transcript entries: {len(session.transcript)}")

    def _build_live_config(self, session: VoiceSession):
        """The LiveConnectConfig for one connection leg of `session` (ent#534).

        Compression + resumption are requested through `getattr` so a stubbed
        or older SDK without those types still connects — the call then simply
        has the provider's default lifetime, which is the pre-ent#534 behaviour
        rather than a crash at connect time.
        """
        # ent#535: the manifest decides, not the mode. `tool_manifest` was
        # resolved once at session start (platform default ∩ the agent's own
        # declaration); building the config from it is what makes the lock real
        # rather than a comment — an agent that narrowed to `run_task` never
        # sees a canvas declaration, and the model is never told about a tool
        # the dispatcher would refuse.
        manifest = _session_manifest(session)
        tools = []
        if RUN_TASK in manifest:
            tools.append(_RUN_TASK_TOOL)
        panel_declared = [
            d for d in _PANEL_TOOLS.function_declarations if d.name in manifest
        ]
        if panel_declared:
            tools.append(genai_types.Tool(function_declarations=panel_declared))

        # AC 6: the prompt must not advertise a tool the lock removed. The
        # etiquette block is entirely about `run_task`'s spoken filler, so it
        # rides the manifest rather than every session.
        instruction = session.system_prompt
        if RUN_TASK in manifest:
            instruction += _TOOL_ETIQUETTE_INSTRUCTION
        kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction=instruction,
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=session.voice_name
                    )
                )
            ),
            tools=tools,
        )
        compression_cls = getattr(genai_types, "ContextWindowCompressionConfig", None)
        window_cls = getattr(genai_types, "SlidingWindow", None)
        if compression_cls and window_cls:
            kwargs["context_window_compression"] = compression_cls(sliding_window=window_cls())
        resumption_cls = getattr(genai_types, "SessionResumptionConfig", None)
        if resumption_cls:
            kwargs["session_resumption"] = resumption_cls(handle=session._resumption_handle)
        return genai_types.LiveConnectConfig(**kwargs)

    async def _send_audio_loop(self, session: VoiceSession):
        """Forward audio from the input queue to Gemini (one connection leg)."""
        while session._active and not session._go_away:
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
        # ent#534 review (I1): a connection leg that starts after a mid-turn
        # `go_away` resumes the turn in progress rather than overwriting the
        # mirrored partial text with an empty string on the first new chunk.
        current_user_text = session._partial_user_text
        current_assistant_text = session._partial_assistant_text

        while session._active:
            try:
                turn = session._gemini_session.receive()
                async for response in turn:
                    if not session._active:
                        return

                    # ent#534 — provider-connection lifetime signals. The
                    # resumption handle is refreshed by the server as the call
                    # goes; a go_away means THIS connection is about to close,
                    # and connect_and_stream reconnects with the latest handle.
                    update = getattr(response, 'session_resumption_update', None)
                    if update is not None:
                        handle = getattr(update, 'new_handle', None)
                        if handle and getattr(update, 'resumable', True):
                            session._resumption_handle = handle
                        continue
                    if getattr(response, 'go_away', None) is not None:
                        logger.info(
                            "Voice session %s: go_away (time_left=%s)",
                            session.session_id, getattr(response.go_away, 'time_left', None),
                        )
                        session._go_away = True
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
                            session._partial_user_text = current_user_text
                            if session._on_transcript:
                                await session._on_transcript("user", text)

                    # Output transcription (what Gemini said)
                    if hasattr(content, 'output_transcription') and content.output_transcription:
                        text = content.output_transcription.text
                        if text and text.strip():
                            current_assistant_text += text
                            session._partial_assistant_text = current_assistant_text
                            if session._on_transcript:
                                await session._on_transcript("assistant", text)

                    # Turn complete
                    if content.turn_complete:
                        if session._on_status:
                            await session._on_status("listening")

                        if current_user_text.strip():
                            await self._record_turn(session, "user", current_user_text.strip())
                            current_user_text = ""
                        if current_assistant_text.strip():
                            await self._record_turn(session, "assistant", current_assistant_text.strip())
                            current_assistant_text = ""
                        session._partial_user_text = ""
                        session._partial_assistant_text = ""

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if session._active:
                    logger.error(f"Receive audio error: {e}")
                break

        # Flush any remaining text — but NOT on a go_away: the turn continues
        # on the next connection leg and would otherwise be split in two.
        if not session._go_away:
            await self._flush_partial_turn(session)

    async def _flush_partial_turn(self, session: VoiceSession):
        """Record the turn in progress, if any (ent#534).

        Reached from the receive loop's own exit and from `end_session`: an End
        pressed a second after the answer landed is BEFORE the provider's
        `turn_complete`, and without this the call's last exchange — the one the
        person just heard — would be the one row missing from the chat.
        """
        user_text = session._partial_user_text.strip()
        assistant_text = session._partial_assistant_text.strip()
        session._partial_user_text = ""
        session._partial_assistant_text = ""
        if user_text:
            await self._record_turn(session, "user", user_text)
        if assistant_text:
            await self._record_turn(session, "assistant", assistant_text)

    async def _record_turn(self, session: VoiceSession, role: str, text: str):
        """Append a completed turn to the transcript and tell the front door.

        The transcript list is what the Agent Detail path saves at the end;
        `_on_turn` is how the Workspace path persists as it goes (ent#534). A
        failing callback must never take the audio loop down with it.
        """
        session.transcript.append(VoiceTranscriptEntry(role=role, text=text))
        if session._on_turn:
            try:
                await session._on_turn(role, text)
            except Exception as e:  # noqa: BLE001
                logger.warning("Voice session %s: on_turn callback failed: %s", session.session_id, e)

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
        # ent#535: no default. A tool call with no name is not a `run_task` —
        # reading it as one sent the model's arguments to the agent under a name
        # nobody chose, which is exactly what the manifest exists to stop.
        tool_name = getattr(fc, 'name', None) or ''
        args = dict(fc.args) if getattr(fc, 'args', None) else {}

        # Defence in depth (ent#535): the model can only call what the config
        # offered, so this should never fire — which is the point. The manifest
        # is the session's, not this function's, so a future path that builds a
        # config from somewhere else still cannot reach a tool the session was
        # not granted. Refuse by NAME, before any argument is read.
        manifest = _session_manifest(session)
        if tool_name not in manifest:
            logger.warning(
                "[ent#535] voice session %s called %r, which is not in its manifest %s — refused",
                session.session_id, tool_name, sorted(manifest),
            )
            await self._send_tool_response(
                session, call_id, tool_name or "unknown",
                "That tool is not available in this call.",
            )
            session._pending_tool_tasks.pop(call_id, None)
            return

        try:
            if session._on_tool_call:
                await session._on_tool_call(tool_name, args)

            if tool_name in _PANEL_TOOL_NAMES:
                result = self._execute_panel_tool(session, tool_name, args)
            elif _is_workspace_bound(session):
                # ent#535 — run it AS the agent, in the thread this call is
                # bound to. The routing lives here because this is where the
                # session is; `_execute_tool` keeps its container contract.
                result = await self._run_task_in_chat(session, _tool_prompt(args))
            else:
                # No chat to run in (VoIP, the legacy Agent Detail session):
                # the container path, with its own hard bound.
                session.transcript.append(
                    VoiceTranscriptEntry(role="system", text=f"[ran a task] {_tool_prompt(args)[:200]}")
                )
                result = await asyncio.wait_for(
                    self._execute_tool(session.agent_name, tool_name, args),
                    timeout=30.0,
                )
        except asyncio.TimeoutError:
            result = "Tool execution timed out."
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

        await self._send_tool_response(session, call_id, tool_name, result)

    async def _send_tool_response(self, session: VoiceSession, call_id: str,
                                  tool_name: str, result: str) -> None:
        """Hand one tool result back to the model. Extracted so the manifest
        refusal answers the call rather than leaving it hanging — a model that
        never receives a response for a call it made stops speaking."""
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
        """Route a tool call to the agent container via the task endpoint.

        The pre-ent#535 path, unchanged, and still the right one for a call with
        no chat to run in (VoIP). A Workspace call does NOT come here — the
        dispatcher routes it to `_run_task_in_chat` so the turn runs as the
        agent in its own thread.
        """
        from services.agent_client import get_agent_client, AgentNotReachableError, AgentRequestError

        prompt = _tool_prompt(args)
        if not prompt:
            return "No prompt provided."

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

    async def _run_task_in_chat(self, session: VoiceSession, prompt: str) -> str:
        """The Workspace path: one real turn in the bound thread, on a budget.

        The latency contract (ent#535 AC 2). The turn is started as its own
        task and raced against `_SPOKEN_BUDGET_SECONDS`:

        * back in time  → the model speaks the answer, as today;
        * past the budget → the model is told the work is still running and
          keeps the floor, while the turn CONTINUES. `portal_chat` persists the
          reply into the thread when it lands, so the result arrives as a chat
          turn (and on the canvas, if the agent drew) with the call still up.

        The budget is therefore a SPEAKING deadline, never a cancellation: a
        long task used to hit a 30s `wait_for` and be thrown away with its work
        already done and paid for. The task is strongly referenced until it
        finishes so it cannot be collected mid-flight (the #1083 footgun), and
        it deliberately outlives the call — a turn the person asked for is
        worth landing whether or not they are still on the line.
        """
        # The guard `_execute_tool` has always had (`"No prompt provided."`,
        # zero side effects), which the new path dropped: `portal_chat` calls
        # `_persist_user_turn` unconditionally, so a `run_task` with a blank
        # prompt would durably write an empty user row into the person's
        # Workspace thread and dispatch a real, cost-tracked execution.
        # `required=["prompt"]` makes that unlikely, not impossible — the
        # argument is model-generated.
        # Stripped, not merely falsy: the dispatcher already passes
        # `_tool_prompt(args)` so whitespace cannot arrive from there today, but
        # this method takes the string directly and a guard that lets `"   "`
        # through would persist a user row of spaces.
        if not str(prompt or "").strip():
            return "No prompt provided."

        turn = asyncio.create_task(self._portal_turn(session, prompt))
        _detached_turns.add(turn)
        turn.add_done_callback(_detached_turns.discard)
        session._pending_turns += 1
        turn.add_done_callback(lambda _t, s=session: setattr(s, "_pending_turns", max(0, s._pending_turns - 1)))

        done, _ = await asyncio.wait({turn}, timeout=_SPOKEN_BUDGET_SECONDS)
        if turn in done:
            try:
                return turn.result()
            except Exception as e:  # noqa: BLE001 — a failed turn is spoken, never raised at the model
                logger.error("[ent#535] voice turn failed for %s: %s", session.agent_name, e)
                return f"That did not go through: {str(e)[:200]}"
        logger.info(
            "[ent#535] voice turn past the %ss spoken budget for %s — it lands in the chat",
            _SPOKEN_BUDGET_SECONDS, session.agent_name,
        )
        # Tell the surface when the detached turn actually lands, so the badge
        # clears on the real event rather than on a timer. A notification, not a
        # tool response: the model was answered at the budget and must not be
        # spoken to again about a call it has already closed.
        turn.add_done_callback(
            lambda t, sess=session: self._spawn_turn_landed(sess, t)
        )
        return _STILL_WORKING_RESULT

    def _spawn_turn_landed(self, session: VoiceSession, turn: "asyncio.Task") -> None:
        """Fire the surface notification for a turn that outran its budget."""
        if not session._on_tool_result:
            return
        try:
            reply = turn.result()
        except Exception:  # noqa: BLE001 — the chat row carries the failure
            reply = "That task did not finish."
        try:
            note = asyncio.create_task(session._on_tool_result(RUN_TASK, reply))
        except RuntimeError:
            return  # no loop (shutdown) — the chat row is still the record
        _detached_turns.add(note)
        note.add_done_callback(_detached_turns.discard)

    async def _portal_turn(self, session: VoiceSession, prompt: str) -> str:
        """One `portal_chat` turn in the bound thread. Imported lazily: the
        portal service pulls in the whole execution stack, and the voice module
        is imported by the VoIP path too."""
        from client_portal.service import portal_chat

        result = await portal_chat(
            session.agent_name,
            prompt,
            email=session.client_email,
            session_id=session.portal_session_id,
            # From the SESSION, never a constant here (ent#535 review). The
            # authorization lives in `start_workspace_voice`, which refuses a
            # non-platform caller; re-asserting it as `True` at this line makes
            # the widening survive any future path that sets `portal_session_id`
            # + `client_email` without going through that gate — a scope break
            # with no diff here. `canvas_audience` already travels for exactly
            # this reason.
            include_owned=session.is_platform,
        )
        return (result or {}).get("response") or "The agent finished with no reply."

    async def _timeout_watchdog(self, session: VoiceSession):
        """Auto-end session after its max duration (per-session; phone calls
        use VOIP_MAX_CALL_DURATION, browser voice uses VOICE_MAX_DURATION, the
        Workspace uses WORKSPACE_VOICE_MAX_DURATION).

        ent#534 — never a silent drop: CAP_WARNING_LEAD_SECONDS before the cap
        the model is asked, as a text turn on the realtime channel, to say the
        call is ending and wrap up; at the cap `end_reason` is set BEFORE
        `end_session` (which cancels this very task), so the surface and the
        transcript both learn why.
        """
        lead = min(CAP_WARNING_LEAD_SECONDS, session.max_duration)
        await asyncio.sleep(max(0, session.max_duration - lead))
        if not session._active:
            return
        if session._gemini_session is not None and lead > 0:
            try:
                await session._gemini_session.send_realtime_input(text=_CAP_WARNING_TEXT)
            except Exception as e:  # noqa: BLE001 — the written notice still lands
                logger.warning("Voice session %s: cap warning not delivered: %s", session.session_id, e)
        await asyncio.sleep(lead)
        if session._active:
            logger.info(f"Voice session {session.session_id} hit max duration ({session.max_duration}s)")
            session.end_reason = "cap"
            session.end_message = f"The call reached its {max(1, round(session.max_duration / 60))}-minute limit."
            await self.end_session(session.session_id)

    async def claim_transcript_save(self, session_id: str) -> bool:
        """One save per call across workers (ent#534).

        The Agent Detail path saves its transcript at the end, and both the
        WebSocket `finally` and `/stop` reach that code — possibly on different
        uvicorn workers. A Redis SETNX decides who writes. Fail-OPEN on a Redis
        error: losing a transcript is worse than a duplicate, and the in-process
        `_transcript_saved` flag still covers the same-worker case.
        """
        try:
            r = await self._get_redis()
            return bool(await r.set(f"voice_session:{session_id}:saved", "1", nx=True, ex=600))
        except Exception as e:  # noqa: BLE001
            logger.warning("voice transcript save-claim failed for %s: %s", session_id, e)
            return True

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
        if session._started_monotonic:
            session._duration_seconds = max(
                session._duration_seconds, time.monotonic() - session._started_monotonic
            )
        # ent#534: the turn in progress is recorded BEFORE the receive task is
        # cancelled — cancellation skips its own exit flush.
        try:
            await self._flush_partial_turn(session)
        except Exception as e:  # noqa: BLE001 — never block the end on bookkeeping
            logger.warning("Voice session %s: partial-turn flush failed: %s", session_id, e)

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
            chat_session_id=meta.get("chat_session_id"),
            user_id=meta["user_id"],
            user_email=meta["user_email"],
            system_prompt=meta["system_prompt"],
            voice_name=meta.get("voice_name", "Kore"),
            workspace_mode=meta.get("workspace_mode", False),
            max_duration=meta.get("max_duration", VOICE_MAX_DURATION),
            portal_session_id=meta.get("portal_session_id"),
            client_email=meta.get("client_email"),
            canvas_audience=meta.get("canvas_audience") or "operator",
            # ent#535 review — the tri-state, restored. `.get(..., _MISSING)`
            # rather than `.get(...)`: an absent key (a session written by an
            # older worker mid-deploy) means "never resolved" and must fall back
            # to the platform default, while a stored `null` means the same and
            # a stored `[]` means "this agent declared no tools" — three inputs,
            # two of which a bare `or` would collapse into the widest possible
            # manifest.
            tool_manifest=_manifest_from_meta(meta),
            # Absent (an older blob) reads as the NARROW answer, matching the
            # dataclass default — the reconstruction must not be the widest
            # reading of a field it was never told about.
            is_platform=bool(meta.get("is_platform", False)),
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
