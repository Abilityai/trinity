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
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel

from models import User
from dependencies import get_current_user, get_authorized_agent
from database import db
from config import GEMINI_API_KEY, VOICE_ENABLED
from services.gemini_voice import voice_service
from services.docker_service import get_agent_container
from services.platform_audit_service import platform_audit_service, AuditEventType, AuditActorType

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


# ── Request/Response Models ──────────────────────────────────────────────────

class VoiceStartRequest(BaseModel):
    session_id: Optional[str] = None  # Existing chat session to continue
    voice_name: Optional[str] = None  # Gemini voice name (e.g. "Kore", "Puck")


class VoiceStartResponse(BaseModel):
    voice_session_id: str
    websocket_url: str
    chat_session_id: str


class VoiceStopRequest(BaseModel):
    voice_session_id: str


class VoiceStopResponse(BaseModel):
    transcript: list
    messages_saved: int
    duration_seconds: float


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

    voice_name = request.voice_name or _get_voice_name(name)

    session = voice_service.create_session(
        agent_name=name,
        chat_session_id=chat_session_id,
        user_id=current_user.id,
        user_email=current_user.email or current_user.username,
        system_prompt=combined_prompt,
        voice_name=voice_name,
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
    preview = voice_service.get_session(request.voice_session_id)
    if not preview:
        raise HTTPException(status_code=404, detail="Voice session not found")
    if preview.agent_name != name:
        raise HTTPException(status_code=403, detail="Voice session does not belong to this agent")
    if preview.user_id != current_user.id and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized for this voice session")

    session = await voice_service.end_session(request.voice_session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Voice session not found")

    # Save transcript as chat messages
    messages_saved = _save_transcript(session)

    # Clean up
    voice_service.remove_session(request.voice_session_id)

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
    session = voice_service.get_session(voice_session_id)
    if not session:
        await websocket.close(code=4004, reason="Voice session not found")
        return

    # Authenticate via query param token (WebSocket can't use Authorization header)
    if not token:
        await websocket.close(code=4001, reason="Authentication required")
        return

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
        try:
            await websocket.send_json({
                "type": "status",
                "state": state,
            })
        except Exception:
            pass

    async def on_tool_call(tool_name: str, args: dict):
        try:
            await websocket.send_json({
                "type": "tool_call",
                "tool": tool_name,
                "args": args,
            })
        except Exception:
            pass
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="voice_tool_call",
            actor_type=AuditActorType.USER,
            actor_id=str(session.user_id),
            actor_email=session.user_email,
            target_type="agent",
            target_id=session.agent_name,
            details={"tool": tool_name, "prompt_preview": str(args.get("prompt", ""))[:100]},
            source="api",
        )

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
        # End the session and save transcript
        session = await voice_service.end_session(voice_session_id)
        if session:
            _save_transcript(session)
            voice_service.remove_session(voice_session_id)

        # Cancel Gemini task
        if not gemini_task.done():
            gemini_task.cancel()
            try:
                await gemini_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            await websocket.close()
        except Exception:
            pass


# ── Helper Functions ─────────────────────────────────────────────────────────

async def _get_voice_system_prompt(agent_name: str) -> str:
    """Get the voice system prompt.

    Priority:
    1. Per-agent voice_system_prompt from DB (set via API)
    2. voice-agent-system-prompt.md from agent container's working directory
    3. Auto-generated from agent template info (description + voice behaviour hints)
    """
    # 1. Check DB override
    prompt = db.get_voice_system_prompt(agent_name)
    if prompt:
        return prompt

    # 2. Read from agent container file
    container = get_agent_container(agent_name)
    if container:
        try:
            from services.docker_utils import container_exec_run
            result = await container_exec_run(
                container,
                "cat /home/developer/voice-agent-system-prompt.md",
                user="developer",
            )
            output = result.output.decode("utf-8").strip() if hasattr(result, 'output') else str(result).strip()
            if output and "No such file" not in output and len(output) > 10:
                return output
        except Exception as e:
            logger.debug(f"Could not read voice-agent-system-prompt.md from {agent_name}: {e}")

    # 3. Auto-generate from template info
    description = None
    if container:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"http://agent-{agent_name}:8000/api/template/info")
                if resp.status_code == 200:
                    info = resp.json()
                    description = info.get("description") or info.get("summary")
        except Exception:
            pass

    display_name = agent_name.replace("-", " ").title()
    lines = [f"You are {display_name}, an AI agent."]
    if description:
        lines.append(f"\n{description}")
    lines.append(
        "\n## Voice Behaviour\n"
        "You are in a voice conversation. Keep responses concise and natural for speech. "
        "No bullet points, markdown formatting, or code blocks — speak as you would in conversation. "
        "One idea at a time. Use the run_task tool when you need to look something up or take an action."
    )
    return "\n".join(lines)


def _get_voice_name(agent_name: str) -> str:
    """Get voice name for an agent (default: Kore)."""
    # For MVP, use a fixed default. Per-agent voice selection is Phase 3.
    return "Kore"


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


def _save_transcript(session) -> int:
    """Save voice transcript entries as ChatMessage rows."""
    saved = 0
    for entry in session.transcript:
        try:
            db.add_chat_message(
                session_id=session.chat_session_id,
                agent_name=session.agent_name,
                user_id=session.user_id,
                user_email=session.user_email,
                role=entry.role,
                content=entry.text,
                source="voice",
            )
            saved += 1
        except Exception as e:
            logger.error(f"Failed to save voice transcript entry: {e}")

    logger.info(f"Saved {saved} voice transcript messages for session {session.session_id}")
    return saved
