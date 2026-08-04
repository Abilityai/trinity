"""
Base classes for channel adapter abstraction.

ChannelAdapter: message processing interface (parse incoming, send outgoing)
NormalizedMessage: channel-agnostic incoming message
ChannelResponse: channel-agnostic outgoing response

Each channel (Slack, Telegram, etc.) implements ChannelAdapter.
Transport details (webhook vs socket vs polling) are handled separately
in adapters/transports/.
"""

from abc import ABC, abstractmethod
from typing import Optional, List
from pydantic import BaseModel


class FileAttachment(BaseModel):
    """File attached to an incoming message."""
    id: str                             # Channel-specific file ID
    name: str                           # Filename (e.g., "report.pdf")
    mimetype: str                       # MIME type (e.g., "application/pdf")
    size: int                           # File size in bytes
    url: str                            # Download URL (may require auth)


class NormalizedMessage(BaseModel):
    """Channel-agnostic incoming message."""
    sender_id: str                      # Channel-specific user ID
    text: str                           # Message content
    channel_id: str                     # Conversation/channel identifier
    thread_id: Optional[str] = None     # Thread ID (Slack thread_ts, Telegram reply_to)
    timestamp: str                      # ISO timestamp
    files: List[FileAttachment] = []    # Attached files
    metadata: dict = {}                 # Channel-specific extras (team_id, bot_token, etc.)


class OutboundFile(BaseModel):
    """File extracted from an agent response for outbound delivery."""
    filename: str                       # e.g., "response_1.csv"
    content: bytes                      # File bytes (UTF-8 encoded text content)
    language: str                       # Original code fence hint ("csv", "json", etc.)

    class Config:
        arbitrary_types_allowed = True


class ChannelResponse(BaseModel):
    """Channel-agnostic outgoing response."""
    text: str                           # Response content (may contain markdown)
    files: List[OutboundFile] = []      # Extracted files for outbound delivery
    metadata: dict = {}                 # Extra context (agent_name, cost, etc.)


class ChannelAdapter(ABC):
    """
    Message processing interface — transport-agnostic.

    Each channel implements this to handle:
    - Parsing raw events into NormalizedMessage
    - Sending responses back through the channel
    - Resolving which agent handles the message

    Channel-specific concerns (verification, rich formatting, identity overrides)
    live on the concrete adapter, not here.
    """

    @property
    @abstractmethod
    def channel_type(self) -> str:
        """Channel identifier string, e.g. 'slack', 'telegram'."""

    @abstractmethod
    def get_rate_key(self, message: NormalizedMessage) -> str:
        """Build a rate-limit key unique to this sender on this channel."""

    @abstractmethod
    def get_session_identifier(self, message: NormalizedMessage) -> str:
        """Build a session identifier for conversation persistence."""

    @abstractmethod
    def get_source_identifier(self, message: NormalizedMessage) -> str:
        """Build a source identifier for audit/execution tracking."""

    @abstractmethod
    def get_bot_token(self, message: NormalizedMessage) -> Optional[str]:
        """Get the bot/app token needed to send responses for this message."""

    @abstractmethod
    def parse_message(self, raw_event: dict) -> Optional[NormalizedMessage]:
        """
        Extract NormalizedMessage from a raw channel event.

        Returns None to skip the event (bot messages, unsupported types, etc.)
        """

    def format_response(self, text: str) -> str:
        """
        Convert standard markdown to the channel's native format.

        Agent responses are always standard markdown. Each channel has its own
        text format (Slack mrkdwn, Telegram HTML, Discord markdown, etc.).
        Override in concrete adapters to apply channel-specific conversion.

        Default: passthrough (returns text unchanged).

        NOTE: If formatting needs grow beyond simple text conversion (e.g.
        structured Block Kit, interactive elements, platform-specific widgets),
        consider extracting formatters into a separate abstraction layer
        (e.g. a FormatterRegistry or per-channel Formatter classes) rather
        than overloading this method.
        """
        return text

    @abstractmethod
    async def send_response(
        self,
        channel_id: str,
        response: ChannelResponse,
        thread_id: Optional[str] = None
    ) -> None:
        """Deliver a response back to the channel."""

    @abstractmethod
    async def get_agent_name(self, message: NormalizedMessage) -> Optional[str]:
        """
        Resolve which Trinity agent should handle this message.

        Returns agent name, or None if no agent is configured for this channel/user.
        """

    async def enrich_message(self, message: NormalizedMessage) -> None:
        """
        Enrich a parsed message with async-fetched sender/channel identity (#350).

        ``parse_message`` is synchronous and, on some channels (Slack), the raw
        event carries only opaque IDs — resolving a display name / channel name
        needs an async API call. The router calls this once after resolving the
        agent, so an adapter can populate ``message.metadata`` in place with:
          - ``channel_name`` — human channel name (drives the ``[Channel: #x]``
            context prefix; presence gates that prefix, so DMs stay clean)
          - ``sender_display_name`` / ``sender_username`` — attribution

        Default: no-op — channels whose event already carries identity
        (Telegram) need nothing here. Override in concrete adapters. Must be
        best-effort (never raise): the router still handles the message if
        enrichment fails.
        """
        return None

    # -------------------------------------------------------------------------
    # In-flight progress indicator seam (ent#264): start / progress / resolve
    # -------------------------------------------------------------------------
    # Capability declaration for the router's per-turn progress driver. The
    # router arms a driver task only when `progress_threshold_seconds` is set,
    # so channels without a progress implementation behave byte-identically
    # (no task created, no extra calls). All per-turn indicator state rides
    # `NormalizedMessage.metadata` — adapters are long-lived singletons
    # handling concurrent turns, so turn state must never live on the adapter.
    progress_threshold_seconds: Optional[float] = None  # None ⇒ driver never armed
    progress_interval_seconds: float = 60.0             # tick cadence past threshold

    async def indicate_processing(self, message: NormalizedMessage) -> None:
        """
        Show a processing indicator to the user.

        Called when the agent starts working on a message.
        Each channel implements this differently:
        - Slack: add ⏳ reaction to the user's message
        - Telegram: send typing action + 👀 reaction ack on the triggering
          message (ent#264 — gated by the per-binding toggle and, in groups,
          on @mention/reply triggers or `all` trigger mode); stashes the
          per-turn indicator config on ``message.metadata``
        - Discord: trigger typing indicator

        Must be best-effort: a raise here would abort the turn, so concrete
        implementations wrap their whole body (the router also wraps the call
        as a second layer). Default: no-op. Override in concrete adapters.
        """
        pass

    async def indicate_progress(
        self, message: NormalizedMessage, elapsed_seconds: float
    ) -> None:
        """
        Show in-flight progress for a long-running turn (ent#264).

        Called by the router's progress driver on each tick once the run has
        crossed ``progress_threshold_seconds`` (never called when that is
        None). The adapter owns what "progress" looks like — Telegram sends an
        elapsed-time placeholder message on the first tick and then edits it
        in place with a fresh elapsed time.

        Must be fail-soft (never raise past its own boundary, except letting
        CancelledError propagate) and must keep all per-turn state on
        ``message.metadata``.

        Default: no-op. Override in concrete adapters.
        """
        return None

    async def indicate_done(self, message: NormalizedMessage) -> None:
        """
        Remove the processing indicator / show completion.

        Called when the agent finishes (success or error). The router resolves
        the ent#264 progress driver (cancelled and awaited dead) BEFORE this
        hook runs, so an implementation may safely tear down anything
        ``indicate_progress`` created.
        - Slack: remove ⏳, add ✅
        - Telegram: clear the 👀 reaction; delete the elapsed-time placeholder
          (fallback: edit it to a short neutral terminal line) — ent#264

        Default: no-op. Override in concrete adapters.
        """
        pass

    async def handle_verification(self, message: NormalizedMessage) -> bool:
        """
        Verify the sender is authorized to use the agent.

        Called before processing. Return True to proceed, False to stop.
        Channels that don't need verification should leave this as-is.

        Default: always verified. Override in concrete adapters.
        """
        return True

    async def resolve_verified_email(
        self, message: NormalizedMessage
    ) -> Optional[str]:
        """
        Translate the channel-native identity into a verified email, if known.

        Unified cross-channel access control (Issue #311). Each channel is
        responsible only for producing a verified email — everything else
        (sharing checks, access requests, memory injection) runs off that key.

        Returns the email as a lowercase string, or None when the sender has
        not yet proven an email (the router will then prompt via
        ``prompt_auth`` if the agent requires email).

        Default: None. Override in concrete adapters.
        """
        return None

    async def prompt_auth(
        self,
        message: NormalizedMessage,
        agent_name: str,
        bot_token: Optional[str] = None,
    ) -> None:
        """
        Ask the sender to prove an email (channel-specific).

        Called by the router when the agent requires a verified email and
        the adapter couldn't resolve one. Default sends a generic text reply
        with instructions; channels can override for richer UX (e.g. Telegram
        ``/login`` hint, Slack DM, etc.).
        """
        text = (
            "This agent requires a verified email to chat.\n"
            "Send `/login your@email.com` to start verification."
        )
        await self.send_response(
            message.channel_id,
            ChannelResponse(
                text=text,
                metadata={"bot_token": bot_token, "agent_name": agent_name},
            ),
            thread_id=message.thread_id,
        )

    async def download_file(self, file: "FileAttachment", message: NormalizedMessage) -> Optional[bytes]:
        """
        Download a file attachment's bytes.

        Each channel implements its own download logic (auth headers, etc.).
        Returns file bytes, or None on failure.

        Default: not implemented. Override in concrete adapters.
        """
        return None

    async def on_response_sent(
        self,
        message: NormalizedMessage,
        agent_name: str,
    ) -> None:
        """
        Called after a response is successfully sent.

        Adapters can use this to track state, e.g.:
        - Slack: register active thread for reply-without-mention
        - Telegram: no-op

        Default: no-op. Override in concrete adapters.
        """
        pass

    async def record_inbound_activity(
        self,
        message: NormalizedMessage,
        agent_name: str,
    ) -> None:
        """
        Record that an external client sent this agent a message (#1533).

        Feeds the Sharing-tab client roster (``services/client_roster_service``),
        which reports a per-client ``message_count`` and ``last_active``. The
        router calls this once per *delivered* direct message, after the access
        gate and never for group messages — so it counts conversation turns, not
        gate-rejected attempts.

        ``agent_name`` is passed explicitly rather than read from
        ``message.metadata``: the router resolves it, but does not write it back
        onto the message.

        Default: no-op — channels with no per-client roster source (Slack, VoIP)
        need nothing here. Override in concrete adapters. Must be best-effort:
        the router swallows and logs any exception, and the message is still
        processed.
        """
        return None

    # =========================================================================
    # Group Authentication (group_auth_mode support)
    # =========================================================================

    async def is_group_verified(
        self,
        message: NormalizedMessage,
        agent_name: str,
    ) -> bool:
        """
        Check if the group chat has at least one verified member.

        Called when group_auth_mode == "any_verified". Channels that don't
        support groups should return True (allow all).

        Default: True. Override in concrete adapters.
        """
        return True

    async def set_group_verified(
        self,
        message: NormalizedMessage,
        agent_name: str,
        email: str,
    ) -> None:
        """
        Mark the group as verified by the given email.

        Called when a verified user sends the first message to an unverified group.
        The email becomes the "group verifier" — subsequent messages from any
        group member are allowed.

        Default: no-op. Override in concrete adapters.
        """
        pass

    async def prompt_group_auth(
        self,
        message: NormalizedMessage,
        agent_name: str,
        bot_token: Optional[str] = None,
    ) -> None:
        """
        Prompt for group verification (channel-specific).

        Called when group_auth_mode == "any_verified" and no one in the group
        has verified yet. Default sends a generic text reply; channels can
        override for better UX.
        """
        text = (
            "This agent requires at least one verified member in the group.\n"
            "Send `/login your@email.com` to verify your email."
        )
        await self.send_response(
            message.channel_id,
            ChannelResponse(
                text=text,
                metadata={"bot_token": bot_token, "agent_name": agent_name},
            ),
            thread_id=message.thread_id,
        )
