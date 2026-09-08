"""
Voice chat routes for Trinity backend (VOICE-001).

Provides real-time voice conversations with agents via Gemini Live API.
Endpoints:
  POST /api/agents/{name}/voice/start - Initialize voice session
  POST /api/agents/{name}/voice/stop  - End voice session and save transcript
  WS   /ws/voice/{voice_session_id}   - Audio streaming bridge (audio + tool_call + tool_result)
"""

import asyncio
import base64
import json
import logging
import types

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query

from models import (
    DEFAULT_CANVAS_ID,
    User,
    VoiceStartRequest,
    VoiceStartResponse,
    VoiceStopRequest,
    VoiceStopResponse,
)
from dependencies import get_current_user, get_authorized_agent, get_owned_agent, assert_owns_or_admin
from database import db
from config import GEMINI_API_KEY, VOICE_ENABLED, DEFAULT_VOICE_NAME, GEMINI_VOICE_NAMES
from services import canvas_service
from services.gemini_voice import voice_service, WORKSPACE_PANEL_INSTRUCTIONS
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container
from services.platform_audit_service import platform_audit_service, AuditEventType
from services.runtime_secret_scrub import get_staged_values, scrub_text

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


# ── REST Endpoints ───────────────────────────────────────────────────────────

@router.post("/api/agents/{name}/voice/start", response_model=VoiceStartResponse)
async def voice_start(
    request: VoiceStartRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Initialize a voice session with an agent.

    1. Loads the agent's voice system prompt
    2. Summarizes prior chat history for context
    3. Creates a voice session ready for WebSocket connection
    """
    if not VOICE_ENABLED:
        raise HTTPException(status_code=503, detail="Voice chat is disabled")
    if not voice_service.is_available():
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured")

    # Get or create the chat session
    if request.session_id:
        chat_session = db.get_chat_session(request.session_id)
        if not chat_session:
            raise HTTPException(status_code=404, detail="Chat session not found")
        chat_session_id = chat_session.id
    else:
        chat_session = db.get_or_create_chat_session(
            agent_name=name,
            user_id=current_user.id,
            user_email=current_user.email or current_user.username,
        )
        chat_session_id = chat_session.id

    # Build the system prompt
    voice_prompt = await _get_voice_system_prompt(name)
    context_summary = _build_context_summary(chat_session_id)

    combined_prompt = voice_prompt
    if context_summary:
        combined_prompt += f"\n\n## Conversation so far:\n{context_summary}"
    if request.workspace_mode:
        combined_prompt += WORKSPACE_PANEL_INSTRUCTIONS

    voice_name = request.voice_name or _get_voice_name(name)

    session = await voice_service.create_session(
        agent_name=name,
        chat_session_id=chat_session_id,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        system_prompt=combined_prompt,
        voice_name=voice_name,
        workspace_mode=request.workspace_mode,
    )

    return VoiceStartResponse(
        voice_session_id=session.session_id,
        websocket_url=f"/ws/voice/{session.session_id}",
        chat_session_id=chat_session_id,
    )


@router.post("/api/agents/{name}/voice/stop", response_model=VoiceStopResponse)
async def voice_stop(
    request: VoiceStopRequest,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """End a voice session and save the transcript to chat messages."""
    # Ownership gate (#600): the path agent and the JWT user must both match
    # the session before any mutation happens — otherwise any authenticated
    # user with access to ANY agent could end and persist a transcript onto
    # someone else's session by passing its 128-bit id in the body.
    preview = await voice_service.get_session(request.voice_session_id)
    if not preview:
        raise HTTPException(status_code=404, detail="Voice session not found")
    if preview.agent_name != name:
        raise HTTPException(status_code=403, detail="Voice session does not belong to this agent")
    assert_owns_or_admin(current_user, preview.user_id, detail="Not authorized for this voice session")

    session = await voice_service.end_session(request.voice_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Voice session not found")

    # Save transcript as chat messages. A Workspace call (ent#534) persists
    # turn by turn on the worker holding the live socket and is closed there;
    # this route only ends it — a `/stop` that lands on the OTHER worker holds
    # a reconstructed session with no transcript and must write nothing.
    messages_saved = 0
    if not getattr(session, "portal_session_id", None) and await _claim_save(session.session_id):
        messages_saved = _save_transcript(session)

    # Clean up
    await voice_service.remove_session(request.voice_session_id)

    return VoiceStopResponse(
        transcript=[
            {"role": entry.role, "text": entry.text}
            for entry in session.transcript
        ],
        messages_saved=messages_saved,
        duration_seconds=session._duration_seconds,
    )


@router.get("/api/agents/{name}/voice/status")
async def voice_status(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Check voice chat availability for an agent."""
    return {
        "enabled": VOICE_ENABLED,
        "available": voice_service.is_available(),
        "voice_prompt_set": bool(db.get_voice_system_prompt(name)),
    }


@router.get("/api/agents/{name}/voice/{session_id}/panel")
async def get_voice_panel(
    session_id: str,
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Return the canvas a workspace voice session draws on (ent#536).

    The voice panel IS the agent's default canvas, so this is the canvas row —
    the same blocks the Canvas tab renders — not an in-memory copy. Returns an
    empty canvas shape (not 404) when the session has ended or nothing has been
    drawn yet, so the poll loop never raises during the teardown window.
    """
    session = await voice_service.get_session(session_id)
    if not session:
        return canvas_service.empty_canvas(name)
    if session.agent_name != name:
        raise HTTPException(status_code=403, detail="Session does not belong to this agent")
    assert_owns_or_admin(current_user, session.user_id, detail="Not authorized for this voice session")
    canvas = db.get_agent_canvas(name, DEFAULT_CANVAS_ID)
    if not canvas:
        return canvas_service.empty_canvas(name)
    return canvas_service.decorate([canvas], name)[0]


@router.get("/api/agents/{name}/voice/prompt")
async def get_voice_prompt(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Get the agent's voice system prompt."""
    prompt = db.get_voice_system_prompt(name)
    return {"voice_system_prompt": prompt}


@router.put("/api/agents/{name}/voice/prompt")
async def set_voice_prompt(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
    body: dict = None,
):
    """Set the agent's voice system prompt."""
    prompt = (body or {}).get("voice_system_prompt", "")
    db.set_voice_system_prompt(name, prompt)
    return {"ok": True, "voice_system_prompt": prompt}


@router.get("/api/agents/{name}/voice/name")
async def get_voice_name(
    name: str = Depends(get_authorized_agent),
    current_user: User = Depends(get_current_user),
):
    """Get the agent's persisted voice + the selectable voice list (#28)."""
    return {
        "voice_name": db.get_voice_name(name),
        "available_voices": list(GEMINI_VOICE_NAMES),
        "default_voice": DEFAULT_VOICE_NAME,
    }


@router.put("/api/agents/{name}/voice/name")
async def set_voice_name(
    name: str = Depends(get_owned_agent),
    current_user: User = Depends(get_current_user),
    body: dict = None,
):
    """Set the agent's persisted voice (owner-only) (#28).

    Validates against the canonical GEMINI_VOICE_NAMES set (400 on unknown). An
    empty/None value clears the override, reverting to DEFAULT_VOICE_NAME.
    """
    voice_name = (body or {}).get("voice_name")
    if voice_name in (None, ""):
        db.set_voice_name(name, None)
        return {"ok": True, "voice_name": db.get_voice_name(name)}
    if voice_name not in GEMINI_VOICE_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice '{voice_name}'. Allowed: {', '.join(GEMINI_VOICE_NAMES)}",
        )
    db.set_voice_name(name, voice_name)
    return {"ok": True, "voice_name": voice_name}


# ── WebSocket Handler ────────────────────────────────────────────────────────

@router.websocket("/ws/voice/{voice_session_id}")
async def voice_websocket(
    websocket: WebSocket,
    voice_session_id: str,
    token: str = Query(default=None),
):
    """
    WebSocket audio bridge: Browser ↔ Backend ↔ Gemini Live API.

    Client sends: {"type": "audio", "data": "<base64 PCM 16kHz mono>"}
    Server sends: {"type": "audio", "data": "<base64 PCM 24kHz mono>"}
                  {"type": "transcript", "role": "user|assistant", "text": "..."}
                  {"type": "status", "state": "connecting|listening|speaking|ended"}
    """
    # Authenticate via query param token (WebSocket can't use Authorization header)
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return

    # ent#534 review: the token is verified BEFORE the session is looked up, so
    # an unauthenticated caller cannot use 4004-vs-4001 to learn whether a
    # session id exists.
    from jose import jwt, JWTError
    from config import SECRET_KEY, ALGORITHM
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        await websocket.close(code=4001, reason="Invalid token")
        return

    username = payload.get("sub")
    if not username:
        await websocket.close(code=4001, reason="Invalid token claims")
        return

    user = db.get_user_by_username(username)
    if not user:
        await websocket.close(code=4001, reason="Unknown user")
        return

    session = await voice_service.get_session(voice_session_id)
    if not session:
        await websocket.close(code=4004, reason="Voice session not found")
        return

    # Ownership gate (#600): JWT user must own the voice session, or be admin.
    # Without this check, anyone holding a valid JWT who learns the 128-bit
    # session id (logs, browser inspection, XSS) can hijack the audio stream
    # and write tool calls under the victim's identity.
    if user["id"] != session.user_id and user.get("role") != "admin":
        logger.warning(
            "voice_ws ownership rejected: user_id=%s tried to attach to session owned by user_id=%s",
            user["id"], session.user_id,
        )
        await websocket.close(code=4003, reason="Not authorized for this voice session")
        return

    await websocket.accept()

    # Callbacks that forward Gemini output to the browser WebSocket
    async def on_audio_out(audio_bytes: bytes):
        try:
            await websocket.send_json({
                "type": "audio",
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            })
        except Exception:
            pass

    async def on_transcript(role: str, text: str):
        try:
            await websocket.send_json({
                "type": "transcript",
                "role": role,
                "text": text,
            })
        except Exception:
            pass

    async def on_status(state: str):
        frame = {"type": "status", "state": state}
        if state == "ended":
            # ent#534: never a silent drop — the surface is told WHY the call
            # ended (cap / error / provider_closed; absent when the person did).
            frame["reason"] = getattr(session, "end_reason", None)
            frame["message"] = getattr(session, "end_message", None)
        try:
            await websocket.send_json(frame)
        except Exception:
            pass

    # ent#534: a Workspace call writes each spoken turn into its thread as it
    # completes, on THIS worker (the one holding the live provider socket).
    portal_session_id = getattr(session, "portal_session_id", None)
    on_turn = None
    if portal_session_id:
        from client_portal.voice import persist_voice_turn

        async def on_turn(role: str, text: str):
            persist_voice_turn(session, role, text)

    async def on_tool_call(tool_name: str, args: dict):
        try:
            await websocket.send_json({
                "type": "tool_call",
                "tool": tool_name,
                "args": args,
            })
        except Exception:
            pass
        asyncio.create_task(platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="voice_tool_call",
            source="api",
            actor_user=types.SimpleNamespace(id=user["id"], email=user.get("email")),
            target_type="agent",
            target_id=session.agent_name,
            details={"tool": tool_name, "prompt_preview": str(args.get("prompt", ""))[:100]},
        ))

    async def on_tool_result(tool_name: str, result: str):
        try:
            await websocket.send_json({
                "type": "tool_result",
                "tool": tool_name,
                "result_preview": result[:200],
            })
        except Exception:
            pass

    # Start the Gemini connection in a background task
    gemini_task = asyncio.create_task(
        voice_service.connect_and_stream(
            voice_session_id,
            on_audio_out=on_audio_out,
            on_transcript=on_transcript,
            on_status=on_status,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            on_turn=on_turn,
        )
    )

    try:
        # Forward audio from browser to Gemini
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "audio" and msg.get("data"):
                    audio_bytes = base64.b64decode(msg["data"])
                    await voice_service.send_audio(voice_session_id, audio_bytes)
                elif msg.get("type") == "end":
                    break
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Invalid voice WS message: {e}")

    except WebSocketDisconnect:
        logger.info(f"Voice WebSocket disconnected: {voice_session_id}")
    finally:
        # End the session and persist the transcript
        ended = await voice_service.end_session(voice_session_id)
        messages_saved = 0
        if ended:
            if getattr(ended, "portal_session_id", None):
                # Workspace (ent#534): turns are already in the thread; close
                # the call with its one summary row.
                from client_portal.voice import persist_voice_call_end
                messages_saved = persist_voice_call_end(
                    ended, ended._duration_seconds, ended.end_reason, ended.end_message,
                )
            elif await _claim_save(voice_session_id):
                messages_saved = _save_transcript(ended)
            await voice_service.remove_session(voice_session_id)

        # Cancel Gemini task
        if not gemini_task.done():
            gemini_task.cancel()
            try:
                await gemini_task
            except (asyncio.CancelledError, Exception):
                pass

        # ent#534: the `ended` status frame precedes this write, so a client that
        # reloads its thread on `ended` races the DB. `saved` is the frame to
        # reload on; it is sent after the rows exist and before the close.
        try:
            await websocket.send_json({
                "type": "saved",
                "messages_saved": messages_saved,
                "duration_seconds": ended._duration_seconds if ended else 0.0,
                "reason": getattr(ended, "end_reason", None) if ended else None,
                "message": getattr(ended, "end_message", None) if ended else None,
            })
        except Exception:
            pass

        try:
            await websocket.close()
        except Exception:
            pass


# ── Helper Functions ─────────────────────────────────────────────────────────

async def _get_voice_system_prompt(agent_name: str) -> str:
    """The agent's voice system prompt — see `services/voice_prompt_service.py`.

    Kept as a name on this module (tests and the Agent Detail path call it here);
    the resolver itself moved to a service for ent#534 so the Workspace's start
    route in `client_portal/` can share it without importing a router.
    """
    from services.voice_prompt_service import get_voice_system_prompt
    return await get_voice_system_prompt(agent_name)


def _get_voice_name(agent_name: str) -> str:
    """Resolve the agent's persisted Gemini voice (#28).

    Delegates to the DB accessor, which falls back to DEFAULT_VOICE_NAME ('Kore')
    when unset or invalid. A per-session override (VoiceStartRequest.voice_name)
    still takes precedence at the call site (see voice_start).
    """
    return db.get_voice_name(agent_name)


def _build_context_summary(chat_session_id: str) -> str:
    """Build a concise context summary from recent chat messages."""
    messages = db.get_chat_messages(chat_session_id, limit=20)
    if not messages:
        return ""

    # Simple truncation approach for MVP (no LLM summarization)
    lines = []
    total_chars = 0
    max_chars = 3000  # ~750 tokens

    for msg in messages:
        role_label = "User" if msg.role == "user" else "Assistant"
        # Truncate individual messages
        content = msg.content[:500] + "..." if len(msg.content) > 500 else msg.content
        line = f"{role_label}: {content}"

        if total_chars + len(line) > max_chars:
            break
        lines.append(line)
        total_chars += len(line)

    return "\n".join(lines)


async def _claim_save(voice_session_id: str) -> bool:
    """Cross-worker "who writes the transcript" claim (ent#534 review).

    `voice_service.claim_transcript_save` is a Redis SETNX; a service stub
    without it (the unit harness) claims trivially, as does a Redis error.
    """
    claim = getattr(voice_service, "claim_transcript_save", None)
    if claim is None:
        return True
    try:
        return bool(await claim(voice_session_id))
    except Exception:  # noqa: BLE001 — never lose a transcript to the guard
        return True


def _save_transcript(session) -> int:
    """Save voice transcript entries as ChatMessage rows (the Agent Detail path).

    Once per call (ent#534 review): both the WebSocket `finally` and `/stop`
    reach this, on the same worker or on two. An empty transcript writes
    nothing (a reconstructed cross-worker session is always empty); the
    in-process flag covers the same-worker double call; the Redis claim covers
    the cross-worker one.
    """
    saved = 0
    if not getattr(session, "transcript", None):
        return 0
    if getattr(session, "_transcript_saved", False) is True:
        return 0
    session._transcript_saved = True
    # ent#279: one staged-set read for the whole transcript; scrub each entry's
    # text before it lands in chat_messages.content.
    _staged = get_staged_values()
    for entry in session.transcript:
        try:
            db.add_chat_message(
                session_id=session.chat_session_id,
                agent_name=session.agent_name,
                user_id=session.user_id,
                user_email=session.user_email,
                role=entry.role,
                content=scrub_text(_staged, entry.text),
                source="voice",
            )
            saved += 1
        except Exception as e:
            logger.error(f"Failed to save voice transcript entry: {e}")

    logger.info(f"Saved {saved} voice transcript messages for session {session.session_id}")
    return saved
