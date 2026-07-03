"""
WhatsApp channel adapter via Twilio (WHATSAPP-001).

Twilio delivers inbound WhatsApp messages as form-encoded POSTs signed with
HMAC-SHA1. Outbound messages go via Twilio REST (`POST /Messages.json`) with
HTTP Basic auth (AccountSid:AuthToken).

Scope: Phase 1 MVP — direct messages only. Group chats are not supported by
Twilio's WhatsApp API (see issue #299). Phase 2 (#467) adds /login, /logout,
/whoami commands and wires the adapter into the unified cross-channel access
control system (#311).
"""

import base64
import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from database import db
from adapters.base import (
    ChannelAdapter,
    ChannelResponse,
    FileAttachment,
    NormalizedMessage,
    OutboundFile,
)
from services.agent_shared_files_service import create_share_from_bytes
from services.email_service import EmailService
from services.settings_service import get_public_chat_url

logger = logging.getLogger(__name__)

# Twilio's message body limit for WhatsApp
TWILIO_WHATSAPP_MAX_LENGTH = 1600

# Twilio REST API base (outbound send)
TWILIO_API_BASE = "https://api.twilio.com"

# SSRF allowlist for media downloads — only Twilio-hosted URLs permitted.
_TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES = (".twilio.com",)

# Pending-login TTL (matches verification-code expiry)
_PENDING_LOGIN_TTL = 600  # 10 minutes

# Outbound media (#1315): files are persisted to FILES-001 storage and handed to
# Twilio as a public `?sig=` MediaUrl, which Twilio fetches server-side within
# seconds of the send. A short TTL keeps the public link alive just long enough
# for that fetch (+ Twilio retries) — the cleanup service reaps expired shares,
# so the blob doesn't linger. NOT revoke-after-send: Twilio fetches the URL
# *asynchronously* after the POST 2xx, so revoking on the API ack would race it.
_OUTBOUND_MEDIA_EXPIRES_IN = 3600  # 1 hour

# Twilio WhatsApp outbound media byte caps (per Twilio docs): image/audio/video
# ≈5 MB, documents ≈16 MB. Outbound files today are text artifacts (documents,
# ≤500 KB each via message_router caps), so the document cap is the operative
# one; the media caps are defensive for future binary attachments.
_WA_MEDIA_CAP_BYTES = 5 * 1024 * 1024
_WA_DOC_CAP_BYTES = 16 * 1024 * 1024

# MIME types deliverable as WhatsApp *documents* beyond the broad `text/*` match.
_WA_DOC_MIME_ALLOWLIST = frozenset({
    "application/json",
    "application/xml",
    "application/pdf",
    "application/x-yaml",
    "application/yaml",
    "application/sql",
    "application/javascript",
    "application/x-ndjson",
})


def _outbound_media_cap_for_mime(mime: str) -> Optional[int]:
    """Return the WhatsApp outbound byte cap for a MIME type, or None if the type
    is not deliverable as media.

    image/audio/video → 5 MB; documents (``text/*`` + a small allowlist of
    structured types) → 16 MB. ``application/octet-stream`` / unknown is treated
    as undeliverable (Twilio would reject it) → caller falls back to a text link.
    """
    mime = (mime or "").lower().split(";")[0].strip()
    if not mime or mime == "application/octet-stream":
        return None
    top = mime.split("/")[0]
    if top in ("image", "audio", "video"):
        return _WA_MEDIA_CAP_BYTES
    if top == "text":
        return _WA_DOC_CAP_BYTES
    if mime in _WA_DOC_MIME_ALLOWLIST:
        return _WA_DOC_CAP_BYTES
    return None


def _get_pending_login_key(binding_id: int, user_id: str) -> str:
    """Redis key for pending WhatsApp login (scoped by binding + phone)."""
    return f"whatsapp_pending_login:{binding_id}:{user_id}"


def _get_pending_login(binding_id: int, user_id: str) -> Optional[str]:
    """Get pending login email from Redis."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            return r.get(_get_pending_login_key(binding_id, user_id))
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login lookup: {e}")
    return None


def _set_pending_login(binding_id: int, user_id: str, email: str) -> None:
    """Store pending login email in Redis with TTL."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            r.setex(_get_pending_login_key(binding_id, user_id), _PENDING_LOGIN_TTL, email)
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login store: {e}")


def _clear_pending_login(binding_id: int, user_id: str) -> None:
    """Clear pending login from Redis."""
    from routers.auth import get_redis_client
    try:
        r = get_redis_client()
        if r:
            r.delete(_get_pending_login_key(binding_id, user_id))
    except Exception as e:
        logger.warning(f"Redis unavailable for pending login clear: {e}")


def _mask_phone(phone: str) -> str:
    """Mask a phone number for safe logging: 'whatsapp:+14155551234' → 'whatsapp:+141***1234'."""
    if not phone:
        return "<empty>"
    # Keep first 4 digits after '+' and last 4
    # Works for 'whatsapp:+14155551234' and '+14155551234'
    if "+" not in phone:
        return f"{phone[:4]}***"
    prefix, _, rest = phone.partition("+")
    if len(rest) <= 8:
        return f"{prefix}+***{rest[-2:]}"
    return f"{prefix}+{rest[:3]}***{rest[-4:]}"


def _is_twilio_media_url(url: str) -> bool:
    """Check if a URL is hosted on a Twilio domain (SSRF defense)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(host == s.lstrip(".") or host.endswith(s) for s in _TWILIO_MEDIA_ALLOWED_HOST_SUFFIXES)
    except Exception:
        return False


class WhatsAppAdapter(ChannelAdapter):
    """WhatsApp implementation of ChannelAdapter, backed by Twilio."""

    # =========================================================================
    # ChannelAdapter interface — identity & routing
    # =========================================================================

    @property
    def channel_type(self) -> str:
        return "whatsapp"

    def get_rate_key(self, message: NormalizedMessage) -> str:
        binding_id = message.metadata.get("binding_id", "unknown")
        return f"whatsapp:{binding_id}:{message.sender_id}"

    def get_session_identifier(self, message: NormalizedMessage) -> str:
        binding_id = message.metadata.get("binding_id", "unknown")
        # sender_id IS the WhatsApp phone (e.g. 'whatsapp:+14155551234')
        return f"{binding_id}:{message.sender_id}"

    def get_source_identifier(self, message: NormalizedMessage) -> str:
        return f"whatsapp:{message.sender_id}"

    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        """
        For WhatsApp, the "bot token" composite is AccountSid + AuthToken, used
        for HTTP Basic auth against Twilio. We return a 'sid:token' string here
        so `message_router` can pass it through response metadata unchanged —
        the actual auth header is constructed in `send_response`.
        """
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return None
        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            return None
        auth_token = db.get_whatsapp_auth_token(agent_name)
        if not auth_token:
            return None
        return f"{binding['account_sid']}:{auth_token}"

    # =========================================================================
    # ChannelAdapter interface — message processing
    # =========================================================================

    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Parse a Twilio WhatsApp form-encoded payload into a NormalizedMessage.

        Twilio fields of interest:
        - From            — sender, 'whatsapp:+E164'
        - To              — our bound from_number
        - Body            — message text
        - MessageSid      — globally unique per delivery attempt (dedup key)
        - ProfileName     — sender display name (optional)
        - NumMedia        — count of attached media
        - MediaUrl{N}     — Twilio-hosted media URL (requires Basic auth)
        - MediaContentType{N}
        """
        sender = raw_event.get("From", "").strip()
        if not sender:
            return None

        body = (raw_event.get("Body") or "").strip()
        wa_user_name = raw_event.get("ProfileName") or None
        message_sid = raw_event.get("MessageSid", "")

        files: List[FileAttachment] = []
        try:
            num_media = int(raw_event.get("NumMedia", "0") or "0")
        except ValueError:
            num_media = 0

        for i in range(num_media):
            media_url = raw_event.get(f"MediaUrl{i}") or ""
            if not media_url:
                continue
            # Defense-in-depth: reject non-Twilio URLs at parse time.
            if not _is_twilio_media_url(media_url):
                logger.warning(
                    "[WHATSAPP] Rejecting non-Twilio media URL at parse time: %s",
                    urlparse(media_url).hostname,
                )
                continue
            mimetype = raw_event.get(f"MediaContentType{i}") or "application/octet-stream"
            # Twilio doesn't provide filenames; synthesize one from index/mimetype.
            ext = mimetype.split("/")[-1].split(";")[0] or "bin"
            files.append(FileAttachment(
                id=f"{message_sid}-{i}",
                name=f"media_{i}.{ext}",
                mimetype=mimetype,
                size=0,  # Twilio doesn't send size in webhook; trust the router's post-download size check
                url=media_url,
            ))

        # No text and no media → nothing to process
        if not body and not files:
            return None

        if not body and files:
            body = "(media upload)"

        agent_name = raw_event.get("_agent_name", "")
        binding_id = raw_event.get("_binding_id")

        return NormalizedMessage(
            sender_id=sender,
            text=body,
            channel_id=sender,  # DMs: the "channel" is the sender's phone
            thread_id=message_sid,
            timestamp="",  # Twilio webhook doesn't include message timestamp directly
            files=files,
            metadata={
                "agent_name": agent_name,
                "binding_id": binding_id,
                "message_sid": message_sid,
                "from_number": raw_event.get("To", ""),
                "wa_user_name": wa_user_name,
                "is_group": False,
                "raw_event": raw_event,
            },
        )

    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None,
    ) -> None:
        """Send a WhatsApp message via Twilio REST API.

        Outbound files (``ChannelResponse.files``, #1315) are persisted to
        FILES-001 storage and delivered as Twilio ``MediaUrl`` attachments — one
        message per file (WhatsApp permits a single media per message). Text is
        always sent *first* so the reply survives even if every media send fails;
        files that can't be delivered as media (no public URL / unsupported MIME /
        oversized) degrade to a download link appended to the text body.
        """
        composite = response.metadata.get("bot_token") or ""
        agent_name = response.metadata.get("agent_name", "")

        account_sid, _, auth_token = composite.partition(":")
        if not account_sid or not auth_token:
            logger.error(
                "[WHATSAPP] Missing Twilio credentials in response metadata for agent=%s",
                agent_name,
            )
            return

        binding = db.get_whatsapp_binding(agent_name) if agent_name else None
        if not binding:
            logger.error("[WHATSAPP] No binding for agent=%s when sending response", agent_name)
            return

        # Persist + classify outbound files (per-file isolated; never raises).
        media_urls, fallback_links = self._prepare_outbound_media(agent_name, response.files)

        # Voice-out (epic #24 / trinity-enterprise#56): if the agent has spoken
        # replies enabled, deliver the reply text as a WhatsApp voice note (audio
        # media) instead of text. Strictly additive — any failure returns False
        # and the reply falls through to the text path below. Real file
        # attachments + download links are still delivered regardless.
        agent_text = response.text or ""
        voice_sent = False
        if agent_text.strip():
            voice_sent = await self._maybe_send_voice(
                account_sid, auth_token, binding, channel_id, agent_text, agent_name
            )

        # Build the text body: convert the agent's markdown FIRST, then append any
        # download links verbatim. Links must NOT pass through markdown conversion
        # — a `__` in a `?sig=` token (base64url alphabet) would be mis-rendered as
        # *bold* and corrupt the URL.
        body_parts: List[str] = []
        if agent_text.strip() and not voice_sent:
            body_parts.append(self._markdown_to_whatsapp(agent_text))
        if fallback_links:
            body_parts.append("\n".join(f"📎 {name}: {url}" for name, url in fallback_links))
        body = "\n\n".join(body_parts).strip()

        if not body and not media_urls:
            return

        if body:
            for chunk in self._split_message(body):
                await self._send_message(
                    account_sid=account_sid,
                    auth_token=auth_token,
                    from_number=binding["from_number"],
                    messaging_service_sid=binding.get("messaging_service_sid"),
                    to_number=channel_id,
                    body=chunk,
                )

        # One media message per file — WhatsApp allows only a single media per message.
        for media_url in media_urls:
            result = await self._send_message(
                account_sid=account_sid,
                auth_token=auth_token,
                from_number=binding["from_number"],
                messaging_service_sid=binding.get("messaging_service_sid"),
                to_number=channel_id,
                body="",
                media_url=media_url,
            )
            if result is None:
                logger.warning(
                    "[WHATSAPP] outbound media message rejected by Twilio (to=%s) — "
                    "likely outside the 24h session window (63016) or unsupported media",
                    _mask_phone(channel_id),
                )

    def _plain_for_tts(self, text: str) -> str:
        """Strip markdown markup so the spoken reply doesn't read out asterisks,
        backticks, or raw link URLs."""
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [label](url) -> label
        t = re.sub(r"[*_`#>~]", "", t)
        return t.strip()

    async def _maybe_send_voice(
        self,
        account_sid: str,
        auth_token: str,
        binding: dict,
        to_number: str,
        text: str,
        agent_name: str,
    ) -> bool:
        """Try to deliver ``text`` as a WhatsApp voice note (epic #24 / #56).

        Returns True only when a voice note was actually sent. Any miss — feature
        off, no voice id, TTS unavailable, over the cost cap, provider/transcode
        error, hosting failure, or a rejected Twilio media send — returns False so
        the caller falls back to text. Never raises.
        """
        try:
            cfg = db.get_tts_config(agent_name)
        except Exception as e:
            logger.warning("[WHATSAPP] voice-out config lookup failed for %s: %s", agent_name, e)
            return False
        if not cfg.get("enabled") or not cfg.get("voice_id"):
            return False

        import services.tts_service as tts_service

        ogg = await tts_service.synthesize_voice_note(self._plain_for_tts(text), cfg["voice_id"])
        if not ogg:
            return False

        # Host the transient voice note for Twilio to fetch. Voice-out has its own
        # feature gate, so this bypasses the file-sharing toggle (MIME/quota/disk
        # protections still apply). Isolated — any failure → text fallback.
        try:
            share = create_share_from_bytes(
                agent_name,
                ogg,
                display_name="voice.ogg",
                expires_in=_OUTBOUND_MEDIA_EXPIRES_IN,
                created_by=agent_name,
                require_sharing_enabled=False,
            )
        except Exception as e:
            logger.warning("[WHATSAPP] voice note hosting failed for %s: %s", agent_name, e)
            return False
        url = share.get("url") or ""
        if not url:
            return False

        result = await self._send_message(
            account_sid=account_sid,
            auth_token=auth_token,
            from_number=binding["from_number"],
            messaging_service_sid=binding.get("messaging_service_sid"),
            to_number=to_number,
            body="",
            media_url=url,
        )
        return result is not None

    def _prepare_outbound_media(
        self, agent_name: str, files: List["OutboundFile"]
    ) -> tuple:
        """Persist each outbound file to FILES-001 storage and classify it for
        delivery.

        Returns ``(media_urls, fallback_links)`` where ``media_urls`` are absolute
        HTTPS ``?sig=`` URLs to send as Twilio ``MediaUrl`` and ``fallback_links``
        are ``(filename, url)`` pairs to append to the text body (no public URL /
        unsupported MIME / oversized).

        Per-file isolation: a rejected or failed file (quota/disk/MIME-blocked,
        ``HTTPException``, or any error) is logged and skipped — it never aborts
        the text reply or its sibling files (#1315).
        """
        media_urls: List[str] = []
        fallback_links: List[tuple] = []
        if not files:
            return media_urls, fallback_links

        # Outbound media reuses FILES-001 public storage — gate on the same
        # per-agent toggle. Off → text-only delivery (placeholder text remains).
        if not db.get_file_sharing_enabled(agent_name):
            logger.warning(
                "[WHATSAPP] file sharing disabled for agent=%s; %d outbound file(s) not delivered as media",
                agent_name, len(files),
            )
            return media_urls, fallback_links

        for f in files:
            try:
                share = create_share_from_bytes(
                    agent_name,
                    f.content,
                    display_name=f.filename,
                    expires_in=_OUTBOUND_MEDIA_EXPIRES_IN,
                    created_by=agent_name,
                )
            except HTTPException as e:
                logger.warning(
                    "[WHATSAPP] outbound file '%s' rejected (%s); skipping",
                    f.filename, getattr(e, "detail", e),
                )
                continue
            except Exception as e:
                logger.warning(
                    "[WHATSAPP] outbound file '%s' persist failed: %s; skipping",
                    f.filename, e,
                )
                continue

            url = share.get("url") or ""
            mime = share.get("mime_type") or "application/octet-stream"
            size = share.get("size_bytes") or 0

            # Twilio fetches the MediaUrl from the public internet — a relative or
            # non-HTTPS URL (public_chat_url unset) is unreachable. Degrade to a link.
            if not url.lower().startswith("https://"):
                logger.warning(
                    "[WHATSAPP] public_chat_url not configured for HTTPS fetch; "
                    "delivering '%s' as a text link instead of media",
                    f.filename,
                )
                fallback_links.append((f.filename, url))
                continue

            cap = _outbound_media_cap_for_mime(mime)
            if cap is None:
                logger.warning(
                    "[WHATSAPP] unsupported outbound media type '%s' for '%s'; delivering as text link",
                    mime, f.filename,
                )
                fallback_links.append((f.filename, url))
                continue
            if size > cap:
                logger.warning(
                    "[WHATSAPP] outbound file '%s' (%d bytes, %s) exceeds WhatsApp cap %d; delivering as text link",
                    f.filename, size, mime, cap,
                )
                fallback_links.append((f.filename, url))
                continue

            media_urls.append(url)

        return media_urls, fallback_links

    def format_response(self, text: str) -> str:
        """Expose markdown conversion for non-send code paths (e.g. proactive)."""
        return self._markdown_to_whatsapp(text)

    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        return message.metadata.get("agent_name") or None

    # =========================================================================
    # Unified access control (Issue #311) — minimal Phase 1 plumbing
    # =========================================================================

    async def resolve_verified_email(
        self, message: NormalizedMessage
    ) -> Optional[str]:
        """Look up verified email for this WhatsApp user (#311 Phase 2)."""
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return None
        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            return None
        return db.get_whatsapp_verified_email(binding["id"], message.sender_id)

    async def prompt_auth(
        self,
        message: NormalizedMessage,
        agent_name: str,
        bot_token: Optional[str] = None,
    ) -> None:
        """Send a WhatsApp-native prompt when the agent requires a verified email."""
        text = (
            "🔒 This agent requires a verified email.\n\n"
            "Send */login your@email.com* and I'll email you a 6-digit code. "
            "Then reply with */login 123456* to complete verification."
        )
        await self.send_response(
            message.channel_id,
            ChannelResponse(
                text=text,
                metadata={"bot_token": bot_token, "agent_name": agent_name},
            ),
            thread_id=message.thread_id,
        )

    # =========================================================================
    # Bot commands (#467 Phase 2)
    # =========================================================================

    def is_command(self, message: NormalizedMessage) -> bool:
        """Detect WhatsApp bot commands (/login, /logout, /whoami)."""
        return message.text.startswith("/")

    async def handle_command(self, message: NormalizedMessage) -> Optional[str]:
        """Dispatch /login, /logout, /whoami. Returns reply text or None."""
        text = message.text.strip()

        if text == "/login" or text.startswith("/login "):
            return await self._handle_login_command(message, text)

        if text == "/logout":
            return await self._handle_logout_command(message)

        if text == "/whoami":
            email = await self.resolve_verified_email(message)
            if email:
                return f"You are verified as `{email}`."
            return "You are not verified. Send */login your@email.com* to verify."

        return None

    async def _handle_login_command(
        self, message: NormalizedMessage, text: str
    ) -> Optional[str]:
        """Handle /login {email} (request code) and /login {code} (verify)."""
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return "Login is unavailable for this chat."

        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            return "Login is unavailable for this chat."

        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return (
                "Usage:\n"
                "*/login your@email.com* — request a verification code\n"
                "*/login 123456* — confirm the code I emailed you"
            )

        arg = parts[1].strip()

        # 6-digit code path
        if arg.isdigit() and len(arg) == 6:
            pending_email = _get_pending_login(binding["id"], message.sender_id)
            if not pending_email:
                return (
                    "I don't have a pending login for you. Send "
                    "*/login your@email.com* first."
                )
            result = db.verify_login_code(pending_email, arg)
            if not result:
                return "❌ Invalid or expired code. Try again or request a new one."
            db.set_whatsapp_verified_email(binding["id"], message.sender_id, pending_email)
            _clear_pending_login(binding["id"], message.sender_id)

            # Run the same access gate as message_router so the user learns
            # immediately whether they're in or in the approval queue — avoids
            # the two-step "you can chat" → "access pending" UX surprise.
            policy = db.get_access_policy(agent_name)
            if db.email_has_agent_access(agent_name, pending_email) or policy.get("open_access"):
                return (
                    f"✅ Verified! You're now signed in as `{pending_email}`.\n"
                    "You can chat normally now."
                )

            try:
                db.upsert_access_request(agent_name, pending_email, "whatsapp")
            except Exception as e:
                logger.error(
                    f"Failed to upsert access_request for {pending_email} on agent={agent_name}: {e}"
                )
            return (
                f"✅ Verified as `{pending_email}`.\n"
                "🔒 Your access request is pending approval — "
                "I'll let you know once the agent owner responds."
            )

        # Email path
        email = arg.lower()
        if "@" not in email or " " in email or len(email) > 254:
            return "That doesn't look like an email address. Try */login you@example.com*."

        try:
            code_data = db.create_login_code(email, expiry_minutes=10)
        except Exception as e:
            logger.error(f"Failed to create login code for {email}: {e}")
            return "Couldn't create a verification code. Please try again later."

        try:
            email_service = EmailService()
            sent = await email_service.send_verification_code(email, code_data["code"])
        except Exception as e:
            logger.error(f"Failed to send verification email to {email}: {e}")
            sent = False

        _set_pending_login(binding["id"], message.sender_id, email)

        if not sent:
            return (
                f"⚠️ I couldn't send the email to `{email}`. "
                "Ask the agent owner to check email delivery."
            )
        return (
            f"📧 Sent a 6-digit code to `{email}`.\n"
            "Reply with */login 123456* to finish verification."
        )

    async def _handle_logout_command(self, message: NormalizedMessage) -> str:
        agent_name = message.metadata.get("agent_name")
        if not agent_name:
            return "Logout is unavailable for this chat."
        binding = db.get_whatsapp_binding(agent_name)
        if not binding:
            return "Logout is unavailable for this chat."
        db.clear_whatsapp_verified_email(binding["id"], message.sender_id)
        _clear_pending_login(binding["id"], message.sender_id)
        return "👋 Logged out. Send */login your@email.com* to sign in again."

    # =========================================================================
    # File download — Twilio-hosted media with HTTP Basic auth
    # =========================================================================

    async def download_file(
        self, file: FileAttachment, message: NormalizedMessage
    ) -> Optional[bytes]:
        """
        Fetch Twilio-hosted media. The URL requires Basic auth with
        AccountSid:AuthToken.
        """
        if not _is_twilio_media_url(file.url):
            logger.warning(
                "[WHATSAPP] Refusing to download non-Twilio media URL (host=%s)",
                urlparse(file.url).hostname,
            )
            return None

        agent_name = message.metadata.get("agent_name", "")
        binding = db.get_whatsapp_binding(agent_name) if agent_name else None
        auth_token = db.get_whatsapp_auth_token(agent_name) if agent_name else None
        if not binding or not auth_token:
            logger.error("[WHATSAPP] No credentials to download media for agent=%s", agent_name)
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                resp = await client.get(
                    file.url,
                    auth=(binding["account_sid"], auth_token),
                )
                # Twilio sometimes 302-redirects to a signed S3 URL for large media.
                # Follow only if the redirect target is still Twilio-hosted.
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not _is_twilio_media_url(location):
                        logger.warning(
                            "[WHATSAPP] Refusing off-domain media redirect to host=%s",
                            urlparse(location).hostname,
                        )
                        return None
                    # Follow once, without auth (signed URL carries its own creds)
                    resp = await client.get(location)

                if resp.status_code != 200:
                    logger.error(
                        "[WHATSAPP] Media download failed (status=%d, file=%s)",
                        resp.status_code, file.name,
                    )
                    return None
                return resp.content
        except httpx.TimeoutException:
            logger.error("[WHATSAPP] Timeout downloading media %s", file.name)
            return None
        except Exception as e:
            logger.error("[WHATSAPP] Error downloading media %s: %s", file.name, e)
            return None

    # =========================================================================
    # Twilio REST helpers
    # =========================================================================

    @staticmethod
    async def _send_message(
        account_sid: str,
        auth_token: str,
        from_number: str,
        messaging_service_sid: Optional[str],
        to_number: str,
        body: str,
        media_url: Optional[str] = None,
    ) -> Optional[dict]:
        """POST a single message to Twilio's Messages endpoint.

        Prefers MessagingServiceSid if configured (handles sender selection
        server-side); falls back to explicit From. When ``media_url`` is set the
        message carries a WhatsApp attachment (Twilio fetches the URL
        server-side); ``Body`` is omitted when empty so a media-only message is
        valid (#1315).
        """
        url = f"{TWILIO_API_BASE}/2010-04-01/Accounts/{account_sid}/Messages.json"
        data = {"To": to_number}
        if body:
            data["Body"] = body
        if media_url:
            data["MediaUrl"] = media_url
        if messaging_service_sid:
            data["MessagingServiceSid"] = messaging_service_sid
        else:
            data["From"] = from_number

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, data=data, auth=(account_sid, auth_token))
                if resp.status_code >= 400:
                    # Twilio error codes we care about:
                    #   63016 — message outside the 24-hour window (need template)
                    #   21408 — permission to send to this region not enabled
                    #   21211 — 'To' number is not a valid WhatsApp number
                    body_masked = resp.text[:500] if resp.text else ""
                    logger.error(
                        "[WHATSAPP] Twilio send failed (status=%d, to=%s): %s",
                        resp.status_code, _mask_phone(to_number), body_masked,
                    )
                    return None
                return resp.json()
        except Exception as e:
            logger.error("[WHATSAPP] Twilio send error (to=%s): %s", _mask_phone(to_number), e)
            return None

    @staticmethod
    def _markdown_to_whatsapp(text: str) -> str:
        """Convert standard markdown to WhatsApp's native syntax.

        WhatsApp supports: *bold*, _italic_, ~strike~, `mono`, ```code```.
        It does not render headings or markdown links. Conversions:
        - **bold** / __bold__ → *bold*
        - *italic* / _italic_ → _italic_  (italic already matches; star→underscore)
        - ~~strike~~ → ~strike~
        - `mono` and ```code``` → unchanged (WhatsApp parses them natively)
        - # / ## / ### headers → bold line (*Header*)
        - [text](url) → text (url)

        Conversion order matters: bold runs before italic so that **x** isn't
        mis-parsed as two *italic* tokens.
        """
        if not text:
            return text
        try:
            # Strip headers (# Title, ## Section, ### Sub) → bold line
            text = re.sub(r'^[ \t]*#{1,6}[ \t]+(.+?)\s*$', r'*\1*', text, flags=re.MULTILINE)
            # Markdown links → "text (url)"
            text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', text)
            # Bold: **x** or __x__ → *x*  (must come before italic)
            text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
            text = re.sub(r'__(.+?)__', r'*\1*', text)
            # Italic `*x*` is left intact: WhatsApp renders single-asterisk as
            # bold, so agent-intended italic must use `_x_` (which already
            # matches WA syntax and passes through unchanged). A Phase 2
            # compromise — see feature-flows/whatsapp-integration.md.
            # Strikethrough: ~~x~~ → ~x~
            text = re.sub(r'~~(.+?)~~', r'~\1~', text)
            return text
        except Exception as e:
            logger.debug("[WHATSAPP] markdown conversion failed, passing through: %s", e)
            return text

    @staticmethod
    def _split_message(text: str) -> List[str]:
        """Split text into chunks respecting Twilio's 1600-char WhatsApp limit."""
        if len(text) <= TWILIO_WHATSAPP_MAX_LENGTH:
            return [text]
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= TWILIO_WHATSAPP_MAX_LENGTH:
                chunks.append(remaining)
                break
            split_at = TWILIO_WHATSAPP_MAX_LENGTH
            for sep in ("\n\n", "\n", ". ", " "):
                idx = remaining.rfind(sep, 0, TWILIO_WHATSAPP_MAX_LENGTH)
                if idx > TWILIO_WHATSAPP_MAX_LENGTH // 2:
                    split_at = idx + len(sep)
                    break
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        return chunks
