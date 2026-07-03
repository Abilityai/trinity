"""
Slack integration service (SLACK-001).

Provides:
- Slack request signature verification
- Slack API interactions (chat.postMessage, users.info)
- OAuth token exchange
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx

from config import (
    FRONTEND_URL,
    SECRET_KEY
)
from services.settings_service import (
    get_slack_signing_secret,
    get_slack_client_id,
    get_slack_client_secret,
    get_public_chat_url,
)

logger = logging.getLogger(__name__)


class SlackService:
    """Service for Slack API interactions."""

    SLACK_API_BASE = "https://slack.com/api"
    SLACK_OAUTH_URL = "https://slack.com/oauth/v2/authorize"

    def __init__(self):
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy-initialize async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # =========================================================================
    # Request Verification
    # =========================================================================

    def verify_slack_signature(
        self,
        timestamp: str,
        body: bytes,
        signature: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify that a request came from Slack using the signing secret.

        Returns (is_valid, error_reason).
        """
        signing_secret = get_slack_signing_secret()
        if not signing_secret:
            return False, "Slack Signing Secret not configured"

        # Reject requests older than 5 minutes (prevent replay attacks)
        try:
            request_timestamp = int(timestamp)
            if abs(time.time() - request_timestamp) > 60 * 5:
                return False, "Request timestamp too old"
        except (ValueError, TypeError):
            return False, "Invalid timestamp"

        # Compute expected signature
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected_signature = 'v0=' + hmac.new(
            signing_secret.encode('utf-8'),
            sig_basestring.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        # Constant-time comparison
        if not hmac.compare_digest(expected_signature, signature):
            return False, "Invalid signature"

        return True, None

    # =========================================================================
    # OAuth Flow
    # =========================================================================

    def get_oauth_url(self, state: str) -> str:
        """Generate Slack OAuth URL for workspace installation."""
        public_chat_url = get_public_chat_url()
        if not public_chat_url:
            raise ValueError("PUBLIC_CHAT_URL not configured")

        redirect_uri = f"{public_chat_url}/api/public/slack/oauth/callback"

        params = {
            "client_id": get_slack_client_id(),
            "scope": "im:history,im:read,im:write,chat:write,chat:write.customize,users:read,users:read.email,app_mentions:read,channels:history,channels:read,channels:join,channels:manage,reactions:write,files:read,files:write",
            "redirect_uri": redirect_uri,
            "state": state
        }

        return f"{self.SLACK_OAUTH_URL}?{urlencode(params)}"

    async def exchange_oauth_code(self, code: str) -> Tuple[bool, dict]:
        """
        Exchange OAuth authorization code for access token.

        Returns (success, result_dict).
        result_dict contains either the token info or error info.
        """
        public_chat_url = get_public_chat_url()
        if not public_chat_url:
            return False, {"error": "PUBLIC_CHAT_URL not configured"}

        redirect_uri = f"{public_chat_url}/api/public/slack/oauth/callback"

        try:
            response = await self.client.post(
                f"{self.SLACK_API_BASE}/oauth.v2.access",
                data={
                    "client_id": get_slack_client_id(),
                    "client_secret": get_slack_client_secret(),
                    "code": code,
                    "redirect_uri": redirect_uri
                }
            )
            data = response.json()

            if not data.get("ok"):
                logger.error(f"Slack OAuth error: {data.get('error')}")
                return False, {"error": data.get("error", "unknown_error")}

            return True, {
                "access_token": data.get("access_token"),
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "bot_user_id": data.get("bot_user_id"),
                "scope": data.get("scope")
            }

        except Exception as e:
            logger.error(f"Slack OAuth exchange failed: {e}")
            return False, {"error": str(e)}

    def encode_oauth_state(self, link_id: str, agent_name: str, user_id: str, source: str = "agent") -> str:
        """Encode OAuth state as a signed token.

        Args:
            source: "agent" (per-agent flow) or "platform" (Settings install)
        """
        import base64
        import json

        state_data = {
            "link_id": link_id,
            "agent_name": agent_name,
            "user_id": user_id,
            "source": source,
            "timestamp": int(time.time())
        }
        state_json = json.dumps(state_data, separators=(',', ':'))

        # Sign the state
        signature = hmac.new(
            SECRET_KEY.encode(),
            state_json.encode(),
            hashlib.sha256
        ).hexdigest()[:16]

        # Combine and base64 encode
        combined = f"{state_json}:{signature}"
        return base64.urlsafe_b64encode(combined.encode()).decode()

    def decode_oauth_state(self, state: str) -> Tuple[bool, Optional[dict]]:
        """Decode and verify OAuth state token."""
        import base64

        try:
            decoded = base64.urlsafe_b64decode(state.encode()).decode()
            state_json, signature = decoded.rsplit(':', 1)

            # Verify signature
            expected_signature = hmac.new(
                SECRET_KEY.encode(),
                state_json.encode(),
                hashlib.sha256
            ).hexdigest()[:16]

            if not hmac.compare_digest(expected_signature, signature):
                return False, None

            state_data = json.loads(state_json)

            # Check timestamp (15 minute expiry)
            if time.time() - state_data.get("timestamp", 0) > 15 * 60:
                return False, None

            return True, state_data

        except Exception as e:
            logger.error(f"Failed to decode OAuth state: {e}")
            return False, None

    # =========================================================================
    # Slack API Interactions
    # =========================================================================

    async def send_message(
        self,
        bot_token: str,
        channel: str,
        text: str,
        username: Optional[str] = None,
        icon_url: Optional[str] = None,
        thread_ts: Optional[str] = None,
        blocks: Optional[list] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Send a message to a Slack channel/DM.

        Supports chat:write.customize for per-message agent identity:
        - username: Override display name (requires chat:write.customize scope)
        - icon_url: Override avatar (requires chat:write.customize scope)
        - thread_ts: Reply in thread
        - blocks: Block Kit formatted content

        Returns (success, error_message).
        """
        try:
            payload = {
                "channel": channel,
                "text": text,
            }
            if username:
                payload["username"] = username
            if icon_url:
                payload["icon_url"] = icon_url
            if thread_ts:
                payload["thread_ts"] = thread_ts
            if blocks:
                payload["blocks"] = blocks

            response = await self.client.post(
                f"{self.SLACK_API_BASE}/chat.postMessage",
                headers={"Authorization": f"Bearer {bot_token}"},
                json=payload,
            )
            data = response.json()

            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                logger.error(f"Slack chat.postMessage failed: {error}")
                return False, error

            return True, None

        except Exception as e:
            logger.error(f"Failed to send Slack message: {e}")
            return False, str(e)

    async def post_webhook(
        self,
        webhook_url: str,
        text: str,
        blocks: Optional[list] = None,
        timeout_seconds: float = 5.0,
    ) -> Tuple[bool, Optional[str]]:
        """Post to a Slack incoming webhook URL.

        Used by the canary alert sink (CANARY-001 Phase 1). Distinct from
        `send_message` which uses an OAuth bot token: webhooks are
        URL-as-credential, single-channel, fire-and-forget. Slack's
        response is HTTP 200 with body "ok", or non-200 with an error
        string — we surface the body verbatim so the admin Test button
        can show "invalid_token" / "channel_not_found" verbatim.

        Short timeout (5s default) so a hung webhook can't stall the
        canary cycle. Caller still wraps in its own try/except — this
        already returns `(False, error)` on httpx errors.
        """
        if not webhook_url:
            return False, "webhook_url not configured"
        payload: dict = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            response = await self.client.post(
                webhook_url,
                json=payload,
                timeout=timeout_seconds,
            )
            if response.status_code != 200:
                # Slack returns body like "invalid_token" / "no_service" for
                # webhook errors; preserve verbatim for the admin Test UI.
                body = (response.text or "").strip() or f"HTTP {response.status_code}"
                logger.warning(f"Slack webhook post failed: {body}")
                return False, body
            return True, None
        except httpx.TimeoutException:
            return False, f"timeout after {timeout_seconds}s"
        except Exception as e:
            # Don't echo `str(e)` — httpx exceptions typically embed the
            # request URL in their message, and for canary alerts that URL
            # IS the credential. Log + return only the exception class.
            err = type(e).__name__
            logger.error("Slack webhook post error: %s", err)
            return False, f"{err}: webhook unreachable"

    async def get_user_email(
        self,
        bot_token: str,
        user_id: str
    ) -> Optional[str]:
        """
        Get a user's email from their Slack profile.

        Requires users:read.email scope.
        Returns None if not available.
        """
        try:
            response = await self.client.get(
                f"{self.SLACK_API_BASE}/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": user_id}
            )
            data = response.json()

            if not data.get("ok"):
                logger.warning(f"Failed to get Slack user info: {data.get('error')}")
                return None

            user = data.get("user", {})
            profile = user.get("profile", {})
            return profile.get("email")

        except Exception as e:
            logger.error(f"Failed to get Slack user email: {e}")
            return None

    async def get_user_info(
        self,
        bot_token: str,
        user_id: str
    ) -> Optional[dict]:
        """
        Fetch a user's display identity (#350).

        Returns ``{"real_name", "display_name", "name"}`` (any may be empty),
        or None on error. Requires ``users:read`` scope. Best-effort — the
        caller degrades to the bare user ID when this returns None.
        """
        try:
            response = await self.client.get(
                f"{self.SLACK_API_BASE}/users.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"user": user_id}
            )
            data = response.json()
            if not data.get("ok"):
                logger.warning(f"Failed to get Slack user info: {data.get('error')}")
                return None
            user = data.get("user", {})
            profile = user.get("profile", {})
            return {
                "real_name": profile.get("real_name") or user.get("real_name"),
                "display_name": profile.get("display_name"),
                "name": user.get("name"),
            }
        except Exception as e:
            logger.error(f"Failed to get Slack user info: {e}")
            return None

    async def get_channel_info(
        self,
        bot_token: str,
        channel_id: str
    ) -> Optional[dict]:
        """
        Fetch a channel's name (#350).

        Returns ``{"name", "is_private"}`` or None on error. Requires
        ``channels:read`` / ``groups:read`` scope. Best-effort.
        """
        try:
            response = await self.client.get(
                f"{self.SLACK_API_BASE}/conversations.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"channel": channel_id}
            )
            data = response.json()
            if not data.get("ok"):
                logger.warning(f"Failed to get Slack channel info: {data.get('error')}")
                return None
            channel = data.get("channel", {})
            return {
                "name": channel.get("name"),
                "is_private": channel.get("is_private", False),
            }
        except Exception as e:
            logger.error(f"Failed to get Slack channel info: {e}")
            return None

    async def open_dm_channel(
        self,
        bot_token: str,
        user_id: str
    ) -> Optional[str]:
        """
        Open a DM channel with a user.

        Returns the channel ID.
        """
        try:
            response = await self.client.post(
                f"{self.SLACK_API_BASE}/conversations.open",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"users": user_id}
            )
            data = response.json()

            if not data.get("ok"):
                logger.error(f"Failed to open DM channel: {data.get('error')}")
                return None

            return data.get("channel", {}).get("id")

        except Exception as e:
            logger.error(f"Failed to open Slack DM channel: {e}")
            return None

    async def get_user_by_email(
        self,
        bot_token: str,
        email: str
    ) -> Optional[dict]:
        """
        Look up a Slack user by email address (for proactive messaging #321).

        Returns user info dict with 'id', 'name', 'real_name', or None if not found.
        Requires users:read.email scope on the bot token.
        """
        try:
            response = await self.client.get(
                f"{self.SLACK_API_BASE}/users.lookupByEmail",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"email": email.lower()}
            )
            data = response.json()

            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                if error == "users_not_found":
                    logger.debug(f"Slack user not found for email: {email}")
                else:
                    logger.warning(f"Failed to lookup Slack user by email: {error}")
                return None

            user = data.get("user", {})
            return {
                "id": user.get("id"),
                "name": user.get("name"),
                "real_name": user.get("real_name"),
                "profile": user.get("profile", {}),
            }

        except Exception as e:
            logger.error(f"Failed to lookup Slack user by email: {e}")
            return None

    async def add_reaction(
        self,
        bot_token: str,
        channel: str,
        timestamp: str,
        emoji: str = "hourglass_flowing_sand",
    ) -> bool:
        """Add a reaction emoji to a message. Returns True on success."""
        try:
            response = await self.client.post(
                f"{self.SLACK_API_BASE}/reactions.add",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"channel": channel, "timestamp": timestamp, "name": emoji}
            )
            data = response.json()
            if not data.get("ok") and data.get("error") != "already_reacted":
                logger.warning(f"Failed to add reaction: {data.get('error')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Failed to add reaction: {e}")
            return False

    async def remove_reaction(
        self,
        bot_token: str,
        channel: str,
        timestamp: str,
        emoji: str = "hourglass_flowing_sand",
    ) -> bool:
        """Remove a reaction emoji from a message. Returns True on success."""
        try:
            response = await self.client.post(
                f"{self.SLACK_API_BASE}/reactions.remove",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"channel": channel, "timestamp": timestamp, "name": emoji}
            )
            data = response.json()
            if not data.get("ok") and data.get("error") != "no_reaction":
                logger.warning(f"Failed to remove reaction: {data.get('error')}")
                return False
            return True
        except Exception as e:
            logger.warning(f"Failed to remove reaction: {e}")
            return False

    async def create_channel(
        self,
        bot_token: str,
        channel_name: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Create a Slack channel with the given name.

        Returns (success, channel_id, error).
        If channel already exists, joins it instead.
        """
        try:
            # Try to create
            response = await self.client.post(
                f"{self.SLACK_API_BASE}/conversations.create",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={"name": channel_name, "is_private": False}
            )
            data = response.json()

            if data.get("ok"):
                channel_id = data["channel"]["id"]
                logger.info(f"Created Slack channel #{channel_name} ({channel_id})")
                return True, channel_id, None

            error = data.get("error", "unknown_error")

            # Channel already exists — find and join it
            if error == "name_taken":
                join_ok, channel_id, join_err = await self._find_and_join_channel(
                    bot_token, channel_name
                )
                if join_ok:
                    return True, channel_id, None
                return False, None, join_err

            logger.error(f"Failed to create Slack channel: {error}")
            return False, None, error

        except Exception as e:
            logger.error(f"Failed to create Slack channel: {e}")
            return False, None, str(e)

    async def _find_and_join_channel(
        self,
        bot_token: str,
        channel_name: str
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Find a channel by name and join it."""
        try:
            # List channels to find the ID
            response = await self.client.get(
                f"{self.SLACK_API_BASE}/conversations.list",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"types": "public_channel", "limit": 200}
            )
            data = response.json()

            if not data.get("ok"):
                return False, None, data.get("error")

            for channel in data.get("channels", []):
                if channel.get("name") == channel_name:
                    channel_id = channel["id"]
                    # Join the channel
                    join_resp = await self.client.post(
                        f"{self.SLACK_API_BASE}/conversations.join",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        json={"channel": channel_id}
                    )
                    join_data = join_resp.json()
                    if join_data.get("ok"):
                        logger.info(f"Joined existing Slack channel #{channel_name} ({channel_id})")
                        return True, channel_id, None
                    return False, None, join_data.get("error")

            return False, None, "channel_not_found"

        except Exception as e:
            return False, None, str(e)

    def get_oauth_callback_redirect(
        self,
        agent_name: str,
        success: bool = True,
        error: Optional[str] = None,
        source: str = "agent"
    ) -> str:
        """Get the redirect URL after OAuth completion."""
        base_url = FRONTEND_URL
        if not base_url:
            logger.error("FRONTEND_URL not configured — OAuth redirect will fail. Set FRONTEND_URL in .env")
            base_url = "http://localhost"
        if source == "platform":
            if success:
                return f"{base_url}/settings?slack=installed"
            else:
                return f"{base_url}/settings?slack=error&reason={error or 'unknown'}"
        if success:
            return f"{base_url}/agents/{agent_name}?tab=sharing&slack=connected"
        else:
            return f"{base_url}/agents/{agent_name}?tab=sharing&slack=error&reason={error or 'unknown'}"


    async def upload_file(
        self,
        bot_token: str,
        channel: str,
        filename: str,
        content: bytes,
        thread_ts: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """
        Upload a file to Slack using the V2 Upload API.

        Three-step process:
        1. files.getUploadURLExternal → presigned URL + file_id
        2. POST content to presigned URL
        3. files.completeUploadExternal → finalize and share to channel/thread

        Returns (success, error_message).
        """
        try:
            # Step 1: Get upload URL
            resp = await self.client.get(
                f"{self.SLACK_API_BASE}/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                params={"filename": filename, "length": len(content)},
            )
            data = resp.json()
            if not data.get("ok"):
                error = data.get("error", "unknown_error")
                logger.error(f"Slack files.getUploadURLExternal failed: {error}")
                return False, error

            upload_url = data["upload_url"]
            file_id = data["file_id"]

            # Step 2: Upload content to presigned URL
            upload_resp = await self.client.post(
                upload_url,
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            if upload_resp.status_code != 200:
                error = f"Upload PUT failed: HTTP {upload_resp.status_code}"
                logger.error(f"Slack file upload failed: {error}")
                return False, error

            # Step 3: Complete upload and share to channel/thread
            complete_payload = {
                "files": [{"id": file_id, "title": filename}],
                "channel_id": channel,
            }
            if thread_ts:
                complete_payload["thread_ts"] = thread_ts

            complete_resp = await self.client.post(
                f"{self.SLACK_API_BASE}/files.completeUploadExternal",
                headers={
                    "Authorization": f"Bearer {bot_token}",
                    "Content-Type": "application/json",
                },
                json=complete_payload,
            )
            complete_data = complete_resp.json()
            if not complete_data.get("ok"):
                error = complete_data.get("error", "unknown_error")
                logger.error(f"Slack files.completeUploadExternal failed: {error}")
                return False, error

            logger.info(f"Uploaded {filename} ({len(content)} bytes) to Slack channel {channel}")
            return True, None

        except Exception as e:
            logger.error(f"Failed to upload file to Slack: {e}")
            return False, str(e)

    async def download_file(self, bot_token: str, url: str, max_size: int = 10 * 1024 * 1024) -> Optional[bytes]:
        """
        Download a file from Slack using the bot token.

        Args:
            bot_token: Slack bot token for authorization
            url: url_private_download from Slack file object
            max_size: Maximum file size in bytes (default 10MB)

        Returns:
            File bytes, or None on failure.
        """
        try:
            response = await self.client.get(
                url,
                headers={"Authorization": f"Bearer {bot_token}"},
                follow_redirects=True,
            )
            if response.status_code != 200:
                logger.error(f"Slack file download failed: HTTP {response.status_code}")
                return None
            if len(response.content) > max_size:
                logger.warning(f"Slack file too large ({len(response.content)} bytes, max {max_size})")
                return None
            return response.content
        except Exception as e:
            logger.error(f"Slack file download error: {e}")
            return None


# Singleton instance
slack_service = SlackService()
