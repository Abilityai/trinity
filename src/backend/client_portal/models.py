"""Pydantic models for the enterprise client-portal exposure config (#79)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PortalExposureConfig(BaseModel):
    """Effective portal exposure config + the resolved base URL in use."""
    exposure_mode: str                       # "public" | "private"
    portal_base_url: Optional[str] = None    # explicit override (None = unset → fallback)
    resolved_base_url: str                   # what portal URL-generation actually uses
    public_chat_url_fallback: Optional[str] = None  # the fallback source, for operator clarity


class PortalExposureUpdate(BaseModel):
    """Partial update — only provided fields change.

    ``portal_base_url = ""`` explicitly clears the override (revert to the
    ``public_chat_url`` fallback); ``None`` leaves it unchanged.
    """
    exposure_mode: Optional[str] = None
    portal_base_url: Optional[str] = None


class PortalPlaybook(BaseModel):
    """A capability hint card for the new-chat briefing (#138, ent#380).

    Usually a client-visible playbook — the subset an operator exposed via the
    connector allow-list (∩ user_invocable). When an agent exposes none, the
    same shape carries its template-declared ``use_cases`` ("What You Can Ask")
    instead, so the chat surface renders one hint set either way.
    ``starter_prompt`` pre-fills the composer — it never auto-runs; the client
    completes/edits and sends it.
    """
    title: str
    description: Optional[str] = None
    starter_prompt: str


class PortalAgentCard(BaseModel):
    """One agent on the client's "My Agents" roster."""
    name: str
    owner: Optional[str] = None
    avatar_url: Optional[str] = None
    shared_at: Optional[str] = None
    voice_available: bool = False    # #78: portal voice (ElevenLabs key + agent voice set)
    # #138 briefing — ships with the roster at sign-in so the new-chat screen
    # renders with zero extra fetches. Best-effort live data (a stopped/slow
    # agent yields None/[]). `playbooks` is the hint-card set (ent#380): the
    # exposed-playbook tier, else the template `use_cases` fallback.
    description: Optional[str] = None
    playbooks: list[PortalPlaybook] = Field(default_factory=list)


class PortalTtsRequest(BaseModel):
    """A reply to speak in portal voice mode."""
    text: str = Field(min_length=1, max_length=8000)


class PortalRoster(BaseModel):
    """The client-facing roster: every agent the signed-in email may reach."""
    client_email: Optional[str] = None
    agents: list[PortalAgentCard]


class PortalAuthRequest(BaseModel):
    """Step 1 of portal sign-in: request a 6-digit code for an email."""
    email: str


class PortalAuthVerify(BaseModel):
    """Step 2: verify the code and receive a portal session token."""
    email: str
    code: str


class PortalSession(BaseModel):
    """A minted portal session — a verified email, not a platform account."""
    token: str
    email: str


class PortalExchangeRequest(BaseModel):
    """ent#163 — a trusted issuer asserting WHICH of its end users this is for.

    The issuer has already authenticated this person on their own side; Trinity
    is the agent backend, not the identity provider.
    """
    email: str


class PortalExchangeResponse(BaseModel):
    """A delegated portal session. `expires_in` is seconds, so the issuer can
    cache one token per end user instead of exchanging on every request."""
    token: str
    email: str
    expires_in: int


class PortalChatRequest(BaseModel):
    """A client's chat turn to a rostered agent. ``session_id`` targets a specific
    thread; when omitted the turn lands in the client's most-recent session (or a
    new one if they've never chatted with this agent)."""
    message: str = Field(min_length=1, max_length=8000)
    session_id: Optional[str] = None


class PortalChatResponse(BaseModel):
    """The agent's reply to a portal chat turn. ``session_id`` echoes the thread
    the turn landed in, so a client that sent none learns which session was used."""
    response: str
    cost: Optional[float] = None
    session_id: Optional[str] = None


class PortalTurnStarted(BaseModel):
    """A turn that has been dispatched but not finished (ent#286).

    The 202 answer from the streaming route. ``execution_id`` is what the
    client subscribes to for live tool activity; ``session_id`` echoes the
    thread so a client that sent none can adopt it immediately rather than
    waiting for the turn to end.
    """
    execution_id: str
    session_id: Optional[str] = None


class PortalSessionSummary(BaseModel):
    """One conversation thread in a client↔agent history list."""
    id: str
    title: Optional[str] = None
    created_at: Optional[str] = None
    last_message_at: Optional[str] = None
    message_count: int = 0


class PortalSessions(BaseModel):
    """A client's conversation threads with a rostered agent (most-recent first)."""
    agent_name: str
    sessions: list[PortalSessionSummary]


class PortalSearchResult(BaseModel):
    """One matching conversation from a cross-chat search."""
    agent_name: str
    session_id: str
    title: Optional[str] = None
    snippet: Optional[str] = None            # excerpt around the match (or title)
    last_message_at: Optional[str] = None


class PortalSearchResults(BaseModel):
    """Cross-agent search over a client's conversations (newest-active first)."""
    query: str
    results: list[PortalSearchResult]


class PortalDocument(BaseModel):
    """A file a rostered agent has shared, downloadable by the client."""
    id: str
    filename: str
    size_bytes: int
    mime_type: Optional[str] = None
    download_url: str
    created_at: Optional[str] = None


class PortalDocuments(BaseModel):
    """The documents a rostered agent has made available to the client."""
    agent_name: str
    documents: list[PortalDocument]


class PortalUpload(BaseModel):
    """Result of a client → agent file upload (landed in the agent inbox)."""
    filename: str
    size_bytes: int
    path: str


class PortalUploadItem(BaseModel):
    """A file the client has previously uploaded to the agent (their inbox)."""
    filename: str
    size_bytes: int
    uploaded_at: Optional[str] = None


class PortalUploads(BaseModel):
    """The files a client has sent to an agent — so they can review what they sent."""
    agent_name: str
    uploads: list[PortalUploadItem]


class PortalHistoryMessage(BaseModel):
    """One persisted turn in a client↔agent conversation."""
    role: str                       # 'user' | 'assistant'
    content: str
    cost: Optional[float] = None
    created_at: Optional[str] = None


class PortalHistory(BaseModel):
    """A client's persisted conversation with a rostered agent (oldest-first).
    ``session_id`` is the thread these messages belong to (None for an empty or
    never-started conversation)."""
    agent_name: str
    session_id: Optional[str] = None
    messages: list[PortalHistoryMessage]
    # ent#286: set when a turn is running on this thread RIGHT NOW. A client
    # that reloaded mid-turn subscribes to this id to reattach to the live
    # stream instead of showing a thread that looks finished.
    in_flight_execution_id: Optional[str] = None


# --- Operator controls over a signed-in client (ent#281) ----------------------

class PortalClientState(BaseModel):
    """One client of an agent, with the operator controls' current state.

    Deliberately carries no `active_sessions` count: portal sessions are
    stateless JWTs with no server-side store, so a count would be a guess. What
    is knowable is honest here — when they were last active, and whether the
    durable block is on.
    """
    email: str
    shared_at: Optional[str] = None
    last_active: Optional[str] = None
    message_count: int = 0
    blocked: bool = False
    blocked_at: Optional[str] = None
    blocked_by_email: Optional[str] = None
    block_reason: Optional[str] = None
    # Absent when Redis is unavailable — the same condition under which a
    # log-out would not have landed. Not a claim that no revoke ever happened.
    sessions_revoked_at: Optional[str] = None


class PortalClientRoster(BaseModel):
    agent_name: str
    clients: list[PortalClientState]


class PortalBlockRequest(BaseModel):
    """Optional operator note recorded with the block (why they were barred)."""
    reason: Optional[str] = None


class PortalLogoutResult(BaseModel):
    """``revoked=False`` means the cutoff did NOT land (Redis down) and the
    client's session is still live — reported rather than hidden behind a 200."""
    email: str
    revoked: bool


class PortalBlockResult(BaseModel):
    email: str
    blocked: bool
    # False ⇒ the durable block is in place but live sessions were not killed;
    # the client stays signed in until their token expires.
    sessions_revoked: bool = False
    reason: Optional[str] = None


class PortalUnblockResult(BaseModel):
    email: str
    blocked: bool
    # False ⇒ there was nothing to lift; the call changed nothing.
    was_blocked: bool
