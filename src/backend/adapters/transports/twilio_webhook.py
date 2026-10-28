"""
Twilio webhook transport for WhatsApp (WHATSAPP-001).

Receives form-encoded POSTs at `POST /api/whatsapp/webhook/{webhook_secret}`
and validates the `X-Twilio-Signature` header using Twilio's `RequestValidator`
(HMAC-SHA1 over URL + alphabetically-sorted form params, keyed on AuthToken).

Signature validation handles Twilio's inclusion of empty-value params, which
a naive inline HMAC would drop — hence the dependency on `twilio>=9`.

URL reconstruction honors `X-Forwarded-Proto` (set by nginx) itself:
uvicorn runs with `--no-proxy-headers` (trinity-enterprise#873), so
`request.url` keeps the internal `http://` scheme and `_reconstruct_url`
upgrades it from the header for signature validation behind the Cloudflare
Tunnel + nginx stack.
"""

import asyncio
import logging
from typing import Optional

from fastapi import Request
from twilio.request_validator import RequestValidator

from adapters.transports.base import ChannelTransport
from database import db

logger = logging.getLogger(__name__)

# In-memory dedup ring for MessageSid (Twilio may retry on our 5xx or timeout).
# Bounded to keep memory flat; worst case a retry slips through and agent gets
# double-triggered — mitigated by `message_router`'s rate limiter.
_SEEN_MESSAGE_SIDS: "dict[str, float]" = {}
_SEEN_MAX = 2048


def _seen_recently(message_sid: str) -> bool:
    """Dedup check for Twilio retries. Returns True if this SID was seen before."""
    import time

    if not message_sid:
        return False

    now = time.time()
    if message_sid in _SEEN_MESSAGE_SIDS:
        return True

    # Evict oldest when at cap (simple LRU-ish by insert order on cpython dict)
    if len(_SEEN_MESSAGE_SIDS) >= _SEEN_MAX:
        # Drop oldest 10% in one pass
        drop_count = _SEEN_MAX // 10
        for key in list(_SEEN_MESSAGE_SIDS.keys())[:drop_count]:
            _SEEN_MESSAGE_SIDS.pop(key, None)

    _SEEN_MESSAGE_SIDS[message_sid] = now
    return False


def _reconstruct_url(request: Request) -> str:
    """
    Reconstruct the external URL that Twilio signed.

    Twilio signs using the exact URL it was configured with. Behind nginx,
    `request.url` may reflect the internal http:// URL — so we rebuild from
    the X-Forwarded-Proto + Host headers when present. uvicorn does not apply
    them (it runs with `--no-proxy-headers`, trinity-enterprise#873), so the
    header check below is what upgrades the scheme.
    """
    # Start from request.url; the scheme is the raw hop's until upgraded below.
    url = str(request.url)
    # If scheme is still http and X-Forwarded-Proto is https, upgrade.
    fwd_proto = request.headers.get("x-forwarded-proto", "").lower()
    if fwd_proto == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url


class TwilioWebhookTransport(ChannelTransport):
    """Twilio webhooks for WhatsApp inbound messages (HTTP POST + HMAC-SHA1)."""

    async def start(self) -> None:
        self._running = True
        logger.info("Twilio WhatsApp webhook transport ready")

    async def stop(self) -> None:
        self._running = False

    async def handle_webhook(self, request: Request, webhook_secret: str) -> dict:
        """
        Validate and process an incoming Twilio WhatsApp webhook.

        Always returns 200 with empty TwiML on non-error paths to prevent
        Twilio retries (we accept the request; we just don't process it).
        Returns {"ok": False, "status": 403} on signature mismatch so the
        caller can emit an HTTP 403.
        """
        # 1. Route webhook_secret → binding
        binding = db.get_whatsapp_binding_by_webhook_secret(webhook_secret)
        if not binding:
            # Don't leak binding existence — return 200 regardless.
            logger.warning("[WHATSAPP] Webhook with unknown secret (masked)")
            return {"ok": True}

        # 2. Read form-encoded body
        try:
            form = await request.form()
            params = {k: v for k, v in form.items()}
        except Exception as e:
            logger.error("[WHATSAPP] Failed to parse webhook form body: %s", e)
            return {"ok": True}  # Return 200 so Twilio won't retry

        # 3. Verify signature via Twilio's RequestValidator
        auth_token = db.get_whatsapp_auth_token(binding["agent_name"])
        if not auth_token:
            logger.error(
                "[WHATSAPP] AuthToken decrypt failed for agent=%s — dropping webhook",
                binding["agent_name"],
            )
            return {"ok": True}

        validator = RequestValidator(auth_token)
        signature = request.headers.get("x-twilio-signature", "")
        url = _reconstruct_url(request)

        if not validator.validate(url, params, signature):
            logger.warning(
                "[WHATSAPP] Signature verification failed for agent=%s",
                binding["agent_name"],
            )
            return {"ok": False, "status": 403}

        # 4. Dedup by MessageSid (Twilio retries on our 5xx / timeout)
        message_sid = params.get("MessageSid", "")
        if _seen_recently(message_sid):
            logger.debug(
                "[WHATSAPP] Skipping duplicate MessageSid for agent=%s",
                binding["agent_name"],
            )
            return {"ok": True}

        # 5. Inject routing metadata for the adapter
        raw_event = dict(params)
        raw_event["_binding_id"] = binding["id"]
        raw_event["_agent_name"] = binding["agent_name"]

        # 6. Process asynchronously — return 200 immediately
        asyncio.create_task(self._process_update(raw_event, binding))

        return {"ok": True}

    async def _process_update(self, raw_event: dict, binding: dict) -> None:
        """Route parsed event through adapter → router pipeline.

        Commands (`/login`, `/logout`, `/whoami`) are handled inline by the
        adapter before the standard router pipeline runs — this mirrors the
        Telegram webhook pattern so verification state changes don't pass
        through the access gate that's gating on them.
        """
        try:
            body = (raw_event.get("Body") or "").strip()
            if body.startswith("/"):
                normalized = self.adapter.parse_message(raw_event)
                if normalized and self.adapter.is_command(normalized):
                    command_response = await self.adapter.handle_command(normalized)
                    if command_response:
                        from adapters.base import ChannelResponse
                        bot_token = self.adapter.get_bot_token(normalized)
                        if bot_token:
                            await self.adapter.send_response(
                                normalized.channel_id,
                                ChannelResponse(
                                    text=command_response,
                                    metadata={
                                        "bot_token": bot_token,
                                        "agent_name": binding["agent_name"],
                                    },
                                ),
                                thread_id=normalized.thread_id,
                            )
                        return

            await self.on_event(raw_event)
        except Exception as e:
            logger.error("[WHATSAPP] Update processing error: %s", e, exc_info=True)


def compute_webhook_url(public_url: str, webhook_secret: str) -> str:
    """
    Build the public-facing webhook URL for Twilio configuration.

    Example:
      public_url='https://public.example.com'
      webhook_secret='abc123...'
      → 'https://public.example.com/api/whatsapp/webhook/abc123...'
    """
    base = (public_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/api/whatsapp/webhook/{webhook_secret}"


def backfill_webhook_urls(public_url: str) -> None:
    """
    Sync `whatsapp_bindings.webhook_url` to match the current `public_chat_url`.

    Called from settings back-fill hook. Unlike Telegram (which calls an
    external API), Twilio webhook URLs are pasted by the user into the Twilio
    Console — so this function only refreshes the DB so the UI shows the
    current URL the user should paste.
    """
    try:
        bindings = db.get_all_whatsapp_bindings()
    except Exception as e:
        logger.warning("[WHATSAPP] Webhook URL backfill skipped: %s", e)
        return

    for binding in bindings:
        agent_name = binding.get("agent_name", "<unknown>")
        try:
            url = compute_webhook_url(public_url, binding["webhook_secret"])
            db.update_whatsapp_webhook_url(agent_name, url)
        except Exception as e:
            logger.warning(
                "[WHATSAPP] Webhook URL backfill failed for agent=%s: %s",
                agent_name, e,
            )
