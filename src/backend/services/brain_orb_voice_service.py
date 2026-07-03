"""Brain Orb voice-token mint (#58 Phase 3, trinity-enterprise#60).

Client-held Gemini Live: the browser connects DIRECTLY to Gemini Live using a
short-lived **ephemeral token** minted here. The token's own constraints — model
lock, a fully **locked config** (including the tool surface), single new-session
use, and a short expiry — are the entire security envelope. Trinity's only job is
to JWT-gate the mint. There is NO Redis ticket/intent dance (that pattern exists
to auth a socket back to Trinity's own ``/ws``; here the browser talks to Google,
not Trinity).

Deliberately does **not** reuse ``GeminiVoiceService``'s cached client: ephemeral
``auth_tokens.create`` is a **v1alpha** API and the voice singleton is built
without ``api_version='v1alpha'`` (the Live transport rejects an ephemeral token
unless the client was built v1alpha). We build our own v1alpha client here and
never touch the voice one.

**Write tools are owner-gated (#58 Phase 4a):** shared-user sessions get the
read/visual/scope manifest verbatim (no capture/link). Only when the minting user
OWNS the agent (``can_write``, computed in the route) does the locked manifest add
the capture/link write tools — and the backend ``/action`` route is the hard gate
regardless (``OwnedAgentByName``), so the manifest is a UX affordance, not the
security boundary. ``run_skill`` (exec) stays out entirely (Phase 4b, #66). Because
the whole ``LiveConnectConfig`` is locked into the token, the browser cannot widen
the tool surface at connect time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from google import genai
from google.genai import types as genai_types

from config import GEMINI_API_KEY, VOICE_MODEL, VOICE_MAX_DURATION, DEFAULT_VOICE_NAME

logger = logging.getLogger(__name__)

# The ephemeral token must be SPENT (open a Live session) within this window of
# minting — a leaked token that isn't used to connect almost immediately is dead.
_NEW_SESSION_WINDOW_SECONDS = 60
# Cap any per-agent voice prompt we splice into the system instruction, mirroring
# gemini_voice._TOOL_PROMPT_MAX — bounds prompt-injection bloat.
_AGENT_PROMPT_MAX = 2000

# Phase-3 voice tool surface — the ONLY tools the voice may call. Mirrors the
# non-write half of orb.js ORB_TOOLS (visual + scope). The write tools
# (capture_note / find_connections / synthesize_insights) are DELIBERATELY absent
# — KB writes are Phase 4. Because this manifest is locked into the ephemeral
# token's config, the browser cannot re-introduce them.
_ORB_VOICE_TOOLS = genai_types.Tool(
    function_declarations=[
        genai_types.FunctionDeclaration(
            name="search_graph",
            description="Find notes in the currently rendered graph whose titles match a query. Returns matching note titles.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"query": genai_types.Schema(type=genai_types.Type.STRING, description="Words to match against note titles")},
                required=["query"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="highlight_related_notes",
            description="Light up a whole topic cluster on the orb — every related note plus the edges between them. Use when the user asks to 'show', 'highlight', or 'look at' a topic.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"topic": genai_types.Schema(type=genai_types.Type.STRING, description="The topic, concept, or scope to highlight")},
                required=["topic"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="navigate_to_note",
            description="Focus the camera on a single note by title and open it. Falls back to a read-only vault search if the note isn't on the current canvas.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"title": genai_types.Schema(type=genai_types.Type.STRING, description="The note title to open")},
                required=["title"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="list_converged_topics",
            description="Enumerate the agent's converged conclusions (its own synthesized answers) and highlight them on the orb. Call this instead of guessing.",
            parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
        ),
        genai_types.FunctionDeclaration(
            name="surface_tensions",
            description="Surface the productive contradictions (tension edges) detected in the graph.",
            parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
        ),
        genai_types.FunctionDeclaration(
            name="list_hubs",
            description="List and highlight the most-connected notes (hubs) in the graph.",
            parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
        ),
        genai_types.FunctionDeclaration(
            name="set_colour_mode",
            description="Recolour the orb by 'provenance', 'recency', or 'domains'.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"mode": genai_types.Schema(type=genai_types.Type.STRING, description="One of: provenance, recency, domains")},
                required=["mode"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="read_current_note",
            description="Read the full content of the note currently open on screen.",
            parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
        ),
        genai_types.FunctionDeclaration(
            name="list_scopes",
            description="List the agent's vault scopes (books, cognitive areas, reference) and which are currently mounted.",
            parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
        ),
        genai_types.FunctionDeclaration(
            name="mount_scope",
            description="Bring a scope (a book, company, or research area) INTO the orb — renders it and makes it searchable. Use for 'pull in', 'mount', or 'load' X.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"scope": genai_types.Schema(type=genai_types.Type.STRING, description="The scope name to mount")},
                required=["scope"],
            ),
        ),
        genai_types.FunctionDeclaration(
            name="unmount_scope",
            description="Remove a scope from the orb.",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"scope": genai_types.Schema(type=genai_types.Type.STRING, description="The scope name to unmount")},
                required=["scope"],
            ),
        ),
    ]
)

# Phase-4a owner-only WRITE tools (#58, trinity-enterprise#61). Added to the locked
# manifest ONLY when the minting user owns the agent (can_write). Mirrors the write
# half of orb.js ORB_TOOLS: capture a note, link two notes. run_skill (exec) is
# Phase 4b (trinity-enterprise#66) and deliberately absent here. Because the whole
# config is locked into the ephemeral token, a shared-user session (can_write=False)
# never receives these declarations and the browser cannot re-introduce them.
_ORB_VOICE_WRITE_TOOLS = [
    genai_types.FunctionDeclaration(
        name="capture_note",
        description="Capture a new note into the knowledge base inbox. Use when the user says to 'capture', 'save', 'jot down', or 'remember' a thought.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "title": genai_types.Schema(type=genai_types.Type.STRING, description="Optional short title for the note"),
                "body": genai_types.Schema(type=genai_types.Type.STRING, description="The note content to capture"),
            },
            required=["body"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="link_notes",
        description="Connect two existing notes with a link. Use when the user asks to 'link', 'connect', or 'relate' two notes.",
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "source": genai_types.Schema(type=genai_types.Type.STRING, description="Title of the note to link from"),
                "target": genai_types.Schema(type=genai_types.Type.STRING, description="Title of the note to link to"),
            },
            required=["source", "target"],
        ),
    ),
]

_ORB_VOICE_BASE_INSTRUCTION = (
    "You are the voice of this agent's mind, speaking alongside a live 3D "
    "knowledge-graph orb the user is looking at. Keep replies short and spoken — "
    "you are talking, not writing. Drive the orb with your tools: highlight topic "
    "clusters, open notes, surface tensions and hubs, recolour, and mount or "
    "unmount vault scopes when the user asks to bring something into view. Prefer "
    "calling a tool over describing what you would do. "
)
# Appended per session depending on the minting user's write access.
_ORB_VOICE_READONLY_CLAUSE = (
    "You can read and navigate the knowledge base but you cannot write to it in this session."
)
_ORB_VOICE_WRITE_CLAUSE = (
    "You can read and navigate the knowledge base, and you can capture new notes and "
    "link notes when the user explicitly asks — confirm what you are about to write first."
)

_client: Optional[genai.Client] = None


def _get_v1alpha_client() -> genai.Client:
    """Own v1alpha client — never the cached ``gemini_voice`` singleton (which is
    built without v1alpha, so an ephemeral mint through it would fail)."""
    global _client
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not configured")
    if _client is None:
        _client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=genai_types.HttpOptions(api_version="v1alpha"),
        )
    return _client


def _build_system_instruction(agent_prompt: Optional[str], can_write: bool) -> str:
    """Base orb persona + a read-only / write clause depending on the minting user's
    access, optionally prefixed with the agent's persisted voice prompt (capped) so
    the agent's own voice/personality carries through."""
    clause = _ORB_VOICE_WRITE_CLAUSE if can_write else _ORB_VOICE_READONLY_CLAUSE
    base = _ORB_VOICE_BASE_INSTRUCTION + clause
    if agent_prompt:
        agent_prompt = agent_prompt.strip()[:_AGENT_PROMPT_MAX]
    if agent_prompt:
        return f"{agent_prompt}\n\n{base}"
    return base


def _mint_sync(voice_name: str, system_instruction: str, can_write: bool) -> dict:
    """Blocking SDK mint — run under ``asyncio.to_thread`` from the async route."""
    client = _get_v1alpha_client()
    # Owner sessions get the write tools folded into the read/visual/scope surface;
    # shared-user sessions get the read-only manifest unchanged (Phase 3 parity).
    fn_decls = list(_ORB_VOICE_TOOLS.function_declarations)
    if can_write:
        fn_decls = fn_decls + _ORB_VOICE_WRITE_TOOLS
    tools = genai_types.Tool(function_declarations=fn_decls)
    config = genai_types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        speech_config=genai_types.SpeechConfig(
            voice_config=genai_types.VoiceConfig(
                prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        tools=[tools],
        # #61 Phase 4b — enable input/output transcription so the client receives
        # per-turn text it can log for the saved transcript. Locked into the token
        # (constrained path), mirroring the original Cornelius voice setup.
        input_audio_transcription=genai_types.AudioTranscriptionConfig(),
        output_audio_transcription=genai_types.AudioTranscriptionConfig(),
    )
    now = datetime.now(timezone.utc)
    expire_time = now + timedelta(seconds=VOICE_MAX_DURATION)
    new_session_expire_time = now + timedelta(seconds=_NEW_SESSION_WINDOW_SECONDS)
    # Lock the WHOLE config (no lock_additional_fields → the entire LiveConnect
    # params are pinned to `config`; the browser cannot widen model, prompt, or
    # tool surface). uses=1: one new session (resume doesn't count).
    token = client.auth_tokens.create(
        config=genai_types.CreateAuthTokenConfig(
            uses=1,
            expire_time=expire_time,
            new_session_expire_time=new_session_expire_time,
            live_connect_constraints=genai_types.LiveConnectConstraints(
                model=VOICE_MODEL,
                config=config,
            ),
        )
    )
    return {
        # NB: field is intentionally NOT named "token" — orb.js initActions()
        # reads `.token` from a different (Phase-4 write) surface; a `token` here
        # would flip the deferred write tools on. See #60 review F1.
        "ephemeral_token": token.name,
        "model": VOICE_MODEL,
        "voice_name": voice_name,
        "expires_at": expire_time.isoformat(),
        "new_session_window_seconds": _NEW_SESSION_WINDOW_SECONDS,
        "tools": [fd.name for fd in fn_decls],
    }


async def mint_voice_token(
    agent_name: str, *, voice_name: str, agent_prompt: Optional[str], can_write: bool = False
) -> dict:
    """Mint a short-lived, config-locked Gemini Live ephemeral token for the orb's
    client-held voice tile. Returns the token + capabilities (model, voice, tool
    names). When ``can_write`` (the minting user owns the agent, #58 Phase 4a), the
    locked manifest adds the owner-only capture/link write tools; otherwise the
    read-only Phase-3 manifest is minted verbatim. Raises ``ValueError`` when no
    Gemini key is configured; any SDK error propagates for the router to map to 502."""
    system_instruction = _build_system_instruction(agent_prompt, can_write)
    return await asyncio.to_thread(
        _mint_sync, voice_name or DEFAULT_VOICE_NAME, system_instruction, can_write
    )
