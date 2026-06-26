"""
VoIP telephony service (VOIP-001, #1056 — Phase 1, outbound).

Orchestrates an outbound phone call: gate checks → abuse controls → stage a
Gemini voice session intent in Redis → place the Twilio call. The live audio
bridge runs later in `adapters/transports/twilio_media_stream.py` on whichever
worker Twilio's Media Streams socket actually connects to (cross-worker safety
— this service never calls `connect_and_stream`).

Two ids, never conflated (see requirements §39):
  - call_id      : chosen here, baked into the WSS URL + Redis intent key +
                   ticket binding (scope="voip:{call_id}").
  - vs_<id>      : the Gemini VoiceSession id, minted later inside the
                   unmodified `gemini_voice.create_session` at WS-connect.
"""

import json
import logging
import re
import secrets
from typing import Optional

from fastapi import HTTPException

from config import (
    GEMINI_API_KEY,
    REDIS_URL,
    VOIP_CALL_RATE_LIMIT,
    VOIP_CALL_RATE_WINDOW,
    VOIP_DEFAULT_DAILY_CALL_CAP,
    VOIP_ENABLED,
    VOIP_INTENT_TTL_SECONDS,
    VOIP_TICKET_TTL_SECONDS,
)
from database import db
from services import idempotency_service, rate_limiter
from services.ws_ticket_service import mint_ticket

logger = logging.getLogger(__name__)

_INTENT_KEY_PREFIX = "voip_intent:"
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def intent_key(call_id: str) -> str:
    return f"{_INTENT_KEY_PREFIX}{call_id}"


def normalize_e164(number: str) -> str:
    """Validate/normalize a destination number to bare E.164 (+digits).

    Strips an optional 'tel:' scheme and surrounding whitespace. Raises 400 on
    anything that isn't a plausible E.164 number — this is the first gate on
    PSTN spend, so be strict.
    """
    if not number:
        raise HTTPException(status_code=400, detail="to_number is required")
    candidate = number.strip()
    if candidate.lower().startswith("tel:"):
        candidate = candidate[4:].strip()
    candidate = candidate.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not _E164_RE.match(candidate):
        raise HTTPException(
            status_code=400,
            detail="to_number must be E.164, e.g. '+14155551234'",
        )
    return candidate


class VoipService:
    """Outbound-call orchestration + post-call transcript dispatch."""

    def __init__(self):
        self._redis = None  # lazy async client

    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._redis

    def is_available(self) -> bool:
        """Feature-flag gate (default OFF). Mirrors voice_available shape."""
        return VOIP_ENABLED and bool(GEMINI_API_KEY)

    # =========================================================================
    # Stream URL + TwiML
    # =========================================================================

    @staticmethod
    def _wss_base(public_url: str) -> str:
        """Turn the configured public HTTP(S) base into a ws(s):// origin."""
        base = public_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        # Bare host — assume TLS at the edge (Cloudflare Tunnel).
        return "wss://" + base

    def build_stream_twiml(self, call_id: str, ticket: str, public_url: str) -> str:
        """`<Connect><Stream>` TwiML pointing Twilio at our Media Streams WS.

        The auth ticket is passed as a Media Streams `<Parameter>` (Twilio
        surfaces these under the `start` event's `customParameters`), NOT a URL
        query string — Twilio does **not** forward query params on the
        `<Stream url>` WebSocket, so a query-string ticket arrives as `None` and
        the handshake 403s on answer (#1073). `call_id` stays in the path so the
        ticket scope (`voip:{call_id}`) can still be verified.
        """
        from xml.sax.saxutils import quoteattr
        wss_url = f"{self._wss_base(public_url)}/api/voip/voice/{call_id}"
        # quoteattr returns a value WITH surrounding quotes and proper escaping.
        # <Stream> now has a child so it cannot be self-closing.
        return (
            f"<Response><Connect>"
            f"<Stream url={quoteattr(wss_url)}>"
            f"<Parameter name=\"ticket\" value={quoteattr(ticket)}/>"
            f"</Stream>"
            f"</Connect></Response>"
        )

    # =========================================================================
    # Outbound trigger
    # =========================================================================

    async def place_outbound_call(
        self,
        agent_name: str,
        to_number: str,
        initiator_user_id: int,
        initiator_email: str,
        public_url: str,
        context: Optional[str] = None,
        process_transcript: bool = True,
        execution_id: Optional[str] = None,
        dedup_label: str = "",
    ) -> dict:
        """Gate, stage, and dial. Returns {call_id, status, to_number}.

        Raises HTTPException for every gate failure (FastAPI translates to the
        right status). On a Twilio dial failure the staged intent is cleaned up
        and a 502 is raised so the caller's idempotency key is released (the
        router treats a raised exception as a failed attempt).

        Effect-scoped idempotency (#1084): the actual dial is wrapped in the
        effect guard keyed on the RESOLVED dial target + Twilio account, scoped
        to ``execution_id``. When the turn it ran in is re-delivered (pull-mode
        at-least-once), the same number within the same execution replays the
        original {call_id, status, ...} instead of placing a second PSTN call.
        The router's boundary Idempotency-Key gate stays the OUTER layer.
        Fail-open when ``execution_id`` is absent/invalid (old image).
        """
        if not self.is_available():
            raise HTTPException(status_code=404, detail="VoIP is not enabled")

        binding = db.get_voip_binding(agent_name)
        if not binding or not binding.get("enabled"):
            raise HTTPException(
                status_code=400,
                detail="No active Twilio voice binding configured for this agent",
            )

        dest = normalize_e164(to_number)
        if not public_url:
            raise HTTPException(
                status_code=400,
                detail="public_chat_url is not configured; cannot build the Media Streams URL",
            )

        async with idempotency_service.effect_guard(
            "voip_call",
            {"to": dest, "account": binding.get("account_sid")},
            execution_id=execution_id,
            agent_name=agent_name,
            dedup_label=dedup_label,
        ) as guard:
            if guard.replay:
                return guard.snapshot or {"call_id": None, "status": "ringing", "to_number": dest}
            result = await self._place_call_inner(
                agent_name=agent_name,
                dest=dest,
                binding=binding,
                initiator_user_id=initiator_user_id,
                initiator_email=initiator_email,
                public_url=public_url,
                context=context,
                process_transcript=process_transcript,
            )
            guard.snapshot = result
            return result

    async def _place_call_inner(
        self,
        *,
        agent_name: str,
        dest: str,
        binding: dict,
        initiator_user_id: int,
        initiator_email: str,
        public_url: str,
        context: Optional[str],
        process_transcript: bool,
    ) -> dict:
        """Abuse controls → stage Gemini intent → dial Twilio. Wrapped by
        place_outbound_call's effect guard (#1084): a success here is recorded
        for replay; a raised HTTPException releases the claim for retry."""
        # --- Abuse controls (bounds PSTN spend) ---------------------------------
        # Frequency: sliding window per (owner, destination).
        rate_limiter.enforce(
            key=f"voip:call:{initiator_user_id}:{dest}",
            limit=VOIP_CALL_RATE_LIMIT,
            window_seconds=VOIP_CALL_RATE_WINDOW,
            detail="Too many call attempts to this number.",
        )
        # Durable daily cap per agent (survives Redis outages).
        cap = binding.get("daily_call_cap") or VOIP_DEFAULT_DAILY_CALL_CAP
        if db.count_voip_calls_since(agent_name, hours=24) >= cap:
            raise HTTPException(
                status_code=429,
                detail=f"Daily call cap reached for this agent ({cap}/day).",
            )

        auth_token = db.get_voip_auth_token(agent_name)
        if not auth_token:
            raise HTTPException(status_code=500, detail="Could not decrypt Twilio voice credentials")

        # --- Build session context ---------------------------------------------
        call_id = f"voip_{secrets.token_urlsafe(24)}"  # ≥128-bit routing token
        chat_session = db.get_or_create_chat_session(
            agent_name=agent_name,
            user_id=initiator_user_id,
            user_email=initiator_email,
        )
        system_prompt = await self._build_call_system_prompt(agent_name, dest, context)

        # --- Mint call-bound WSS ticket + stage intent (consumed once at WS) -----
        ticket = mint_ticket(
            subject=str(initiator_user_id),
            scope=f"voip:{call_id}",
            ttl_seconds=VOIP_TICKET_TTL_SECONDS,
        )
        intent = {
            "call_id": call_id,
            "agent_name": agent_name,
            "chat_session_id": chat_session.id,
            "user_id": initiator_user_id,
            "user_email": initiator_email,
            "system_prompt": system_prompt,
            # Persisted per-agent voice (#28); falls back to 'Kore' inside the
            # accessor. Always set here so twilio_media_stream's intent.get(...,
            # "Kore") fallback is dead code, not a silent override (reviewer C2).
            "voice_name": db.get_voice_name(agent_name),
            "to_number": dest,
            "process_transcript": bool(process_transcript),
        }
        r = await self._get_redis()
        await r.setex(intent_key(call_id), VOIP_INTENT_TTL_SECONDS, json.dumps(intent))

        db.create_voip_call_log(
            call_id=call_id,
            agent_name=agent_name,
            to_number=dest,
            chat_session_id=chat_session.id,
            initiated_by_user_id=initiator_user_id,
            initiated_by_email=initiator_email,
        )

        # --- Dial. The TwiML needs the ticket+call_id, so we stage-then-dial and
        #     clean up the staged intent if Twilio rejects the call. ------------
        twiml = self.build_stream_twiml(call_id, ticket, public_url)
        try:
            from twilio.rest import Client

            client = Client(binding["account_sid"], auth_token)
            twilio_call = await self._dial(client, dest, binding["from_number"], twiml)
        except Exception as e:  # noqa: BLE001 — clean up before re-raising
            await r.delete(intent_key(call_id))
            db.update_voip_call_status(call_id, "failed", error=str(e)[:300])
            detail = "Twilio rejected the call"
            try:
                from twilio.base.exceptions import TwilioRestException as _TRE
                if isinstance(e, _TRE):
                    detail = f"Twilio error: {e.msg}"
            except Exception:
                pass
            logger.error("VoIP dial failed for agent=%s call_id=%s: %s", agent_name, call_id, e)
            raise HTTPException(status_code=502, detail=detail)

        db.update_voip_call_status(call_id, "ringing", twilio_call_sid=getattr(twilio_call, "sid", None))
        logger.info(
            "VoIP outbound placed: agent=%s call_id=%s twilio_sid=%s",
            agent_name, call_id, getattr(twilio_call, "sid", None),
        )
        return {
            "call_id": call_id,
            "status": "ringing",
            "to_number": dest,
            "twilio_call_sid": getattr(twilio_call, "sid", None),
            "chat_session_id": chat_session.id,
        }

    async def _dial(self, client, to_number: str, from_number: str, twiml: str):
        """Run the blocking Twilio SDK call off the event loop."""
        import asyncio
        return await asyncio.to_thread(
            client.calls.create, to=to_number, from_=from_number, twiml=twiml
        )

    async def _build_call_system_prompt(
        self, agent_name: str, to_number: str, context: Optional[str]
    ) -> str:
        """Reuse the voice router's 3-tier prompt resolution, then add call framing."""
        try:
            from routers.voice import _get_voice_system_prompt
            base = await _get_voice_system_prompt(agent_name)
        except Exception as e:
            logger.debug("Falling back to minimal voice prompt for %s: %s", agent_name, e)
            base = (
                f"You are {agent_name.replace('-', ' ').title()}, an AI agent on a "
                "phone call. Keep responses concise and natural for speech."
            )
        framing = (
            f"\n\n## Phone Call\nYou placed an outbound phone call to {to_number}. "
            "Greet the person, state who you are and why you're calling, and keep "
            "turns short and conversational. Use the run_task tool when you need to "
            "look something up or take an action."
        )
        if context:
            framing += f"\n\n## Call purpose\n{context[:2000]}"
        return base + framing

    # =========================================================================
    # Post-call transcript processing (default ON)
    # =========================================================================

    async def process_call_transcript(
        self,
        agent_name: str,
        chat_session_id: Optional[str],
        to_number: str,
        transcript: list,
        initiator_user_id: Optional[int],
        initiator_email: Optional[str],
    ) -> None:
        """Dispatch the full call transcript to the MAIN agent as one task.

        Lets the real agent (with its skills/memory/MCP) digest the call and
        take follow-up actions — the call becomes a real input, not just a log.
        Fire-and-forget via the standard execution path; never raises.
        """
        lines = []
        for entry in transcript or []:
            role = getattr(entry, "role", None) or (entry.get("role") if isinstance(entry, dict) else "")
            text = getattr(entry, "text", None) or (entry.get("text") if isinstance(entry, dict) else "")
            if not text:
                continue
            who = "You" if role == "assistant" else "Caller"
            lines.append(f"{who}: {text}")
        if not lines:
            return  # empty transcript (no-answer / instant hangup) — nothing to process

        transcript_text = "\n".join(lines)
        message = (
            f"A phone call you placed to {to_number} just ended. Below is the full "
            "transcript. Review it and take any appropriate follow-up — update your "
            "memory, create tasks, send messages, etc. If nothing is needed, briefly "
            f"note the outcome.\n\n## Call transcript\n{transcript_text}"
        )
        try:
            from services.task_execution_service import get_task_execution_service
            svc = get_task_execution_service()
            await svc.execute_task(
                agent_name=agent_name,
                message=message,
                triggered_by="voip",
                source_user_id=initiator_user_id,
                source_user_email=initiator_email,
            )
            logger.info("VoIP post-call processing dispatched for agent=%s", agent_name)
        except Exception as e:  # noqa: BLE001 — best-effort; a failed digest must not crash teardown
            logger.error("VoIP post-call processing failed for agent=%s: %s", agent_name, e)


# Singleton
voip_service = VoipService()
