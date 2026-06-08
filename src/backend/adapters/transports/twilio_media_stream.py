"""
Twilio Media Streams transport for VoIP telephony (VOIP-001, #1056 — Phase 1).

Bridges a Twilio Programmable Voice Media Streams WebSocket into the existing,
**unmodified** Gemini Live voice service. A phone call is just a different audio
transport feeding the same `VoiceSession` queues:

    Twilio (μ-law 8kHz)  ──ulaw2lin→ratecv(8k→16k)──►  voice_service.send_audio(PCM16 16k)
    Twilio (μ-law 8kHz)  ◄──lin2ulaw◄ratecv(24k→8k)──  on_audio_out(PCM16 24k)

All codec work lives here (Invariant: gemini_voice.py is untouched). Per-direction
`audioop.ratecv` state is carried across chunks (no per-chunk reset → no boundary
clicks). Outbound audio is re-chunked to 160-byte/20ms μ-law frames and paced so
Gemini's native barge-in stays effective — when the caller speaks while the agent
is mid-utterance we send Twilio a `clear` event and drop the local buffer.

Two ids, never conflated:
  - call_id : in the WSS URL + ticket binding + Redis intent key (chosen by the trigger).
  - vs_<id> : the Gemini VoiceSession id, minted here at connect time.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from adapters.transports.voip_audio import (
    FRAME_BYTES,
    pcm24k_to_ulaw8k,
    pop_frames,
    ulaw8k_to_pcm16k,
)
from config import REDIS_URL, VOIP_MAX_CALL_DURATION
from database import db
from services.gemini_voice import voice_service
from services.voip_service import voip_service, intent_key
from services.ws_ticket_service import consume_ticket

logger = logging.getLogger(__name__)

_FRAME_INTERVAL = 0.02       # 20ms pacing
_START_FRAME_TIMEOUT = 10.0  # seconds to wait for Twilio's first `start` frame
_START_FRAME_MAX_READS = 20  # frames to skim for `start` before giving up

# Strong refs for fire-and-forget post-call processing tasks (avoid GC).
_BG_TASKS: set = set()

_redis = None


async def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


class _CallBridge:
    """Per-connection audio bridge state. One instance per live call."""

    def __init__(self, websocket: WebSocket, vs_session_id: str, call_id: str, intent: dict):
        self._ws = websocket
        self._vs_id = vs_session_id
        self._call_id = call_id
        self._intent = intent
        self._stream_sid: Optional[str] = None
        self._in_state = None       # ratecv state: inbound 8k→16k
        self._out_state = None      # ratecv state: outbound 24k→8k
        self._out_buffer = bytearray()  # pending μ-law bytes awaiting framing
        self._out_lock = asyncio.Lock()
        self._agent_speaking = False
        self._closed = False

    # --- Gemini → Twilio (outbound) -----------------------------------------

    async def on_audio_out(self, pcm24: bytes):
        """Gemini emits PCM16 24kHz; convert to μ-law 8kHz and buffer for paced send."""
        if not pcm24:
            return
        # Direct 24k→8k decimation (exact 3:1); stateful so chunk boundaries don't click.
        mulaw, self._out_state = pcm24k_to_ulaw8k(pcm24, self._out_state)
        async with self._out_lock:
            self._out_buffer.extend(mulaw)

    async def on_transcript(self, role: str, text: str):
        """Barge-in: if the caller speaks while the agent is mid-utterance, flush
        Twilio's buffer (`clear`) and drop our local buffer so buffered agent
        audio doesn't talk over the caller."""
        if role == "user" and text and text.strip() and self._agent_speaking:
            async with self._out_lock:
                self._out_buffer.clear()
            await self._send_clear()

    async def on_status(self, state: str):
        if state == "speaking":
            self._agent_speaking = True
        elif state in ("listening", "ended"):
            self._agent_speaking = False

    async def sender_loop(self):
        """Drain the μ-law buffer into 160-byte frames on a ~20ms cadence."""
        next_t = time.monotonic()
        while not self._closed:
            next_t += _FRAME_INTERVAL
            frame = None
            async with self._out_lock:
                if len(self._out_buffer) >= FRAME_BYTES:
                    frame = bytes(self._out_buffer[:FRAME_BYTES])
                    del self._out_buffer[:FRAME_BYTES]
            if frame is not None and self._stream_sid:
                await self._send_media(frame)
            delay = next_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Fell behind — reset the schedule rather than burst-catch-up.
                next_t = time.monotonic()

    async def _send_media(self, mulaw_frame: bytes):
        try:
            await self._ws.send_json({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": base64.b64encode(mulaw_frame).decode("ascii")},
            })
        except Exception:
            self._closed = True

    async def _send_clear(self):
        if not self._stream_sid:
            return
        try:
            await self._ws.send_json({"event": "clear", "streamSid": self._stream_sid})
        except Exception:
            self._closed = True

    # --- Twilio → Gemini (inbound) ------------------------------------------

    async def twilio_recv_loop(self):
        """Read Twilio JSON frames; forward caller audio into the Gemini queue."""
        while not self._closed:
            raw = await self._ws.receive_text()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            event = data.get("event")
            if event == "start":
                self._stream_sid = (data.get("start") or {}).get("streamSid") or data.get("streamSid")
            elif event == "media":
                if not self._stream_sid:
                    self._stream_sid = data.get("streamSid")
                payload = (data.get("media") or {}).get("payload")
                if not payload:
                    continue
                try:
                    mulaw = base64.b64decode(payload)
                    pcm16, self._in_state = ulaw8k_to_pcm16k(mulaw, self._in_state)
                    await voice_service.send_audio(self._vs_id, pcm16)
                except Exception as e:
                    logger.debug("VoIP inbound frame dropped (call=%s): %s", self._call_id, e)
            elif event == "stop":
                break
            # connected / mark / dtmf — ignore


def _parse_start_frame(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract (ticket, stream_sid) from a Twilio Media Streams `start` event.

    Twilio surfaces `<Parameter>` children under `start.customParameters` and
    the stream id under `start.streamSid` (top-level `streamSid` as a fallback).
    Either field may be absent; the caller validates the ticket.
    """
    start = data.get("start") or {}
    ticket = (start.get("customParameters") or {}).get("ticket")
    stream_sid = start.get("streamSid") or data.get("streamSid")
    return ticket, stream_sid


async def _read_until_start(websocket: WebSocket) -> Optional[dict]:
    """Read frames until Twilio's `start` event (skipping `connected`).

    Bounded by a timeout and a max-read cap so a client that completes the
    handshake but never sends a `start` frame can't pin a worker. Returns the
    parsed `start` frame dict, or None on timeout/disconnect/early-stop.
    """
    for _ in range(_START_FRAME_MAX_READS):
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=_START_FRAME_TIMEOUT
            )
        except (asyncio.TimeoutError, WebSocketDisconnect):
            return None
        except Exception:  # noqa: BLE001 — any transport error ends the wait
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        event = data.get("event")
        if event == "start":
            return data
        if event == "stop":
            return None
        # connected / mark / other — keep skimming
    return None


async def handle_media_stream(websocket: WebSocket, call_id: str, ticket: Optional[str]):
    """Authenticate, consume the staged intent, and run the live bridge.

    Runs on whichever worker Twilio's Media Streams socket connects to — the
    Gemini session is created HERE, not at trigger time (cross-worker safety).

    Twilio Media Streams does **not** forward the `<Stream url>` query string,
    so the auth ticket arrives as a `<Parameter>` in the first `start` frame —
    readable only AFTER the handshake completes. We therefore `accept()` first,
    then authenticate (#1073). A query-string ticket is still honored as a
    fallback for non-Twilio / diagnostic clients.
    """
    # 1. Accept the transport first — the ticket lives in the `start` frame for
    #    Twilio, and rejecting before accept() is what surfaced as the 403 that
    #    dropped every call on answer (#1073).
    await websocket.accept()

    # 2. Resolve the call-bound, single-use ticket (Twilio can't send a JWT).
    #    Query string wins when present (non-Twilio/diagnostic clients);
    #    otherwise read it from the first Twilio `start` frame's customParameters
    #    and capture the streamSid from that same frame.
    prelude_stream_sid: Optional[str] = None
    if not ticket:
        start_data = await _read_until_start(websocket)
        if start_data is None:
            await websocket.close(code=4001, reason="No start frame / ticket")
            return
        ticket, prelude_stream_sid = _parse_start_frame(start_data)

    info = consume_ticket(ticket) if ticket else None
    if not info or info.get("scope") != f"voip:{call_id}":
        await websocket.close(code=4001, reason="Invalid or missing ticket")
        return

    # 3. Consume the staged intent exactly once (GETDEL) — a double Twilio
    #    connect for the same call finds nothing and is rejected.
    r = await _get_redis()
    try:
        raw = await r.getdel(intent_key(call_id))
    except Exception as e:
        logger.warning("VoIP intent GETDEL failed (call=%s): %s", call_id, e)
        raw = None
    if not raw:
        await websocket.close(code=4004, reason="Call intent not found")
        return
    intent = json.loads(raw)

    # 4. Create the Gemini session on THIS worker (only after auth succeeds, so
    #    a rejected connection never orphans a session).
    session = None
    vs_id = None
    bridge = None
    gemini_task = sender_task = recv_task = None
    try:
        session = await voice_service.create_session(
            agent_name=intent["agent_name"],
            chat_session_id=intent["chat_session_id"],
            user_id=intent["user_id"],
            user_email=intent["user_email"],
            system_prompt=intent["system_prompt"],
            voice_name=intent.get("voice_name", "Kore"),
            # Phone calls use the VoIP cap (default 10min), not the 5-min voice cap.
            max_duration=VOIP_MAX_CALL_DURATION,
        )
        vs_id = session.session_id
        try:
            db.update_voip_call_status(call_id, "connected")
        except Exception:
            pass

        bridge = _CallBridge(websocket, vs_id, call_id, intent)
        # The Twilio `start` frame was already consumed during auth; carry its
        # streamSid over so the sender can emit media before the next frame.
        if prelude_stream_sid:
            bridge._stream_sid = prelude_stream_sid
        gemini_task = asyncio.create_task(
            voice_service.connect_and_stream(
                vs_id,
                on_audio_out=bridge.on_audio_out,
                on_transcript=bridge.on_transcript,
                on_status=bridge.on_status,
            )
        )
        sender_task = asyncio.create_task(bridge.sender_loop())
        recv_task = asyncio.create_task(bridge.twilio_recv_loop())

        # Tear down as soon as EITHER the Twilio leg ends (stop/hangup) OR the
        # Gemini session ends — otherwise a dead Gemini leaves the PSTN call up
        # and billing (eng review R5).
        await asyncio.wait({recv_task, gemini_task}, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        logger.info("VoIP WebSocket disconnected: call=%s", call_id)
    finally:
        if bridge is not None:
            bridge._closed = True
        if vs_id is not None:
            await voice_service.end_session(vs_id)
        live_tasks = [t for t in (sender_task, recv_task, gemini_task) if t is not None]
        for t in live_tasks:
            if not t.done():
                t.cancel()
        if live_tasks:
            await asyncio.gather(*live_tasks, return_exceptions=True)
        if vs_id is not None:
            await _finalize(call_id, session, intent, r)
            await voice_service.remove_session(vs_id)
        try:
            await websocket.close()
        except Exception:
            pass


async def _finalize(call_id: str, session, intent: dict, r):
    """Persist the transcript once (SETNX guard) and dispatch post-call processing."""
    try:
        won = await r.set(f"voip_saved:{call_id}", "1", nx=True, ex=3600)
    except Exception:
        won = True  # fail-open: better to risk a rare double-save than skip it
    if not won:
        return

    from routers.voice import _save_transcript
    try:
        _save_transcript(session)
    except Exception as e:
        logger.error("VoIP transcript save failed (call=%s): %s", call_id, e)
    try:
        db.update_voip_call_status(call_id, "completed")
    except Exception:
        pass

    # Post-call processing (default ON): hand the transcript to the MAIN agent.
    if intent.get("process_transcript", True):
        task = asyncio.create_task(
            voip_service.process_call_transcript(
                agent_name=intent["agent_name"],
                chat_session_id=intent.get("chat_session_id"),
                to_number=intent.get("to_number", "the user"),
                transcript=getattr(session, "transcript", []),
                initiator_user_id=intent.get("user_id"),
                initiator_email=intent.get("user_email"),
            )
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
