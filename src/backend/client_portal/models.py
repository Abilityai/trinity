"""Pydantic models for the enterprise client-portal exposure config (#79)."""
from __future__ import annotations

from typing import Literal, Optional

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
    # #2159: the human-facing name the dashboard tiles render (ent#181/#1640).
    # Optional so an older payload degrades to slug-only rendering rather than
    # failing validation — the frontend falls back to `name`.
    display_label: Optional[str] = None
    owner: Optional[str] = None
    avatar_url: Optional[str] = None
    shared_at: Optional[str] = None
    voice_available: bool = False    # #78: portal voice (ElevenLabs key + agent voice set)
    # #2212 — whether the platform can TRANSCRIBE, i.e. exactly the `/stt` gate:
    # an ElevenLabs key resolves. Deliberately a SEPARATE bit from
    # `voice_available`: output additionally needs an effective voice to speak
    # WITH, input does not, so collapsing the two would either hide a working mic
    # or render a dead one. Fails CLOSED for the same reason `voice_available`
    # does — the bug it guards against is promising an affordance that cannot
    # work. The client uses it to prefer the server path (recorded audio → /stt,
    # which answers with real statuses and real messages) over the browser Web
    # Speech API, and to drop the mic entirely when neither path can work.
    stt_available: bool = False
    # #138 briefing — ships with the roster at sign-in so the new-chat screen
    # renders with zero extra fetches. Best-effort live data (a stopped/slow
    # agent yields None/[]). `playbooks` is the hint-card set (ent#380): the
    # exposed-playbook tier, else the template `use_cases` fallback.
    description: Optional[str] = None
    playbooks: list[PortalPlaybook] = Field(default_factory=list)
    # #2213 — the composer's `/` typeahead searches only what shipped, and
    # `playbooks` is bounded for the hint-card GRID (24, #2101). On an agent with
    # 33 client-visible skills that made the tail unreachable: typing the 27th
    # skill's name matched nothing, silently. These two fields separate the two
    # jobs.
    #
    # `searchable_playbooks` is the same client-visible set at a search-sized
    # bound, carrying title + starter_prompt only (no descriptions — that is what
    # keeps a 200-entry list small). `playbooks_total` is the count BEFORE either
    # bound, so the UI can say "N not shown" instead of rendering a short list
    # that looks complete. Both default empty/0, so an older payload degrades to
    # today's behaviour rather than failing validation.
    searchable_playbooks: list[PortalPlaybook] = Field(default_factory=list)
    playbooks_total: int = 0
    # #2196 — whether this agent can currently run. Roster MEMBERSHIP is a DB
    # fact (`agent_ownership` / `agent_sharing`); this is a Docker fact
    # PROJECTED onto the card, and is never a membership filter. A live
    # ownership row with no container is routine (#1747), so hiding those rows
    # would make "not shared with me" indistinguishable from "shared but
    # containerless" on the one surface a client has.
    #
    #   ready       container exists and runs        (renders as today)
    #   stopped     container exists, not running    (chip)
    #   unavailable no container at all — #2196      (chip)
    #   unknown     Docker could not be asked        (renders as today)
    #
    # ⚠️ The default is fail-OPEN, which is the OPPOSITE of `voice_available`
    # above and `PortalRoster.multi_agent_chat_available` below, and that
    # inversion is deliberate — do not "tidy" it into consistency. Those bits
    # fail closed because their bug is promising an affordance that cannot work.
    # This one's bug is the mirror image: denying a working agent, and — since
    # one unreadable Docker socket marks EVERY card at once — emptying a paying
    # customer's roster over an infrastructure fault. When Docker is unreadable
    # every card reads `unknown` and the roster renders exactly as it does today.
    availability: Literal["ready", "stopped", "unavailable", "unknown"] = "unknown"


class PortalTtsRequest(BaseModel):
    """A reply to speak in portal voice mode."""
    text: str = Field(min_length=1, max_length=8000)


class PortalRoster(BaseModel):
    """The client-facing roster: every agent the signed-in email may reach."""
    client_email: Optional[str] = None
    agents: list[PortalAgentCard]
    # #2128 — whether a chat may include MORE THAN ONE agent on this instance.
    # Named for the capability, never the module or the edition: this payload
    # goes to an operator's customer, who can neither buy a missing module nor
    # act on knowing it exists.
    # Defaults False so an older client, a partial payload or a failed read
    # never advertises an affordance that cannot work (the whole of this bug).
    multi_agent_chat_available: bool = False


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
    # ent#451: ask for a FRESH thread rather than the client's most recent one.
    # An absent `session_id` alone could not say this — it also means "I don't
    # know which thread", which is how New chat kept landing in the existing
    # conversation. Ignored when `session_id` names a thread: the id is a fact,
    # this is an intent. Defaults False so no existing caller changes behaviour.
    new_thread: bool = False


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
    # #2133: how long the client may wait before concluding it has lost track of
    # the turn. Sent by the server because the server owns the timeout; a
    # frontend constant would drift the next time it changes.
    wait_budget_seconds: Optional[int] = None


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


class PortalAllSessionsItem(PortalSessionSummary):
    """One thread in the cross-agent sidebar list — the per-agent summary plus the
    one field a flat list cannot infer from its position (#2198)."""
    agent_name: str


class PortalAllSessions(BaseModel):
    """Every thread the caller has, across every agent on their roster (#2198).

    Deliberately NO cap and NO `total`. Today's per-agent route is unbounded and
    the sidebar calls it once per rostered agent, so this ships the same row
    volume in one request — a cap would be a new behaviour, and it would collide
    with the starred-chat pinning guarantee in requirements §5.10 (a pure recency
    LIMIT can drop a starred-but-old thread out of the pinned section). That is a
    real product question and it deserves its own issue, not a side effect of a
    request-count fix.
    """
    sessions: list[PortalAllSessionsItem]


class PortalAgentAsk(BaseModel):
    """One thing the agent is waiting on a person for (ent#360).

    Agent-authored `approval`/`question` items only. `context` is deliberately
    absent — free-form agent JSON, and a known credential-leak surface.
    """
    id: str
    type: str
    priority: Optional[str] = None
    title: Optional[str] = None
    question: Optional[str] = None
    options: Optional[list] = None
    created_at: Optional[str] = None


class PortalAgentWork(BaseModel):
    """One execution: shape, plus the name of the schedule behind it.

    No message, response, cost or model — the viewer may be an external client,
    and AC #7 excludes costs outright. `schedule_name` (#2161) is the one label
    that crosses deliberately: never the schedule's own `message`, which is a
    prompt. It is truncated at the service, and treated as untrusted — a
    schedule can be created by an agent-scoped key, so its name is not
    necessarily human-written.
    """
    id: Optional[str] = None
    status: Optional[str] = None
    triggered_by: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    schedule_name: Optional[str] = None


class PortalFirstTry(BaseModel):
    """Successes that needed no retry, over the window. `rate` is None with no
    terminal executions — a fresh agent has no first-try rate, and 0% would read
    as "it fails every time"."""
    terminal: int = 0
    first_try: int = 0
    rate: Optional[float] = None


class PortalAgentStats(BaseModel):
    window: str
    window_hours: int
    total_executions: int = 0
    success_rate: Optional[float] = None
    first_try: PortalFirstTry = Field(default_factory=PortalFirstTry)
    timeline: list = Field(default_factory=list)
    by_type: list = Field(default_factory=list)
    # Canonical stack order for the chart, straight from the analytics accessor
    # (#2161) — so the portal never re-derives an ordering that could drift from
    # the operator surface's.
    buckets: list[str] = Field(default_factory=list)
    unavailable: bool = False


class PortalAgentHealth(BaseModel):
    status: str = "unknown"
    checked_at: Optional[str] = None


class PortalAgentHeader(BaseModel):
    name: str
    description: Optional[str] = None
    avatar_url: Optional[str] = None
    owner: Optional[str] = None
    health: PortalAgentHealth = Field(default_factory=PortalAgentHealth)
    # #2196 — a projection of the roster card's field, NOT a second Docker read,
    # and a SECOND FACT beside `health` rather than a replacement for it. The two
    # differ in freshness by construction: `health` is the last persisted
    # `agent_health_checks` row (stale by design, and `unknown` on most installs
    # because monitoring is default-OFF), while this is read at request time.
    # One widget carrying both freshness semantics would tell the viewer neither.
    # Same fail-open default and rationale as `PortalAgentCard.availability`.
    availability: Literal["ready", "stopped", "unavailable", "unknown"] = "unknown"
    last_active: Optional[str] = None


class PortalAgentPage(BaseModel):
    """The Workspace agent page (ent#360) — one call, because the page is one
    screen and five round trips would render it in pieces."""
    agent_name: str
    header: PortalAgentHeader
    capabilities: list[PortalPlaybook] = Field(default_factory=list)
    stats: PortalAgentStats
    asks: list[PortalAgentAsk] = Field(default_factory=list)
    recent_work: list[PortalAgentWork] = Field(default_factory=list)
    # ent#366 — raw up/down counts (never a percentage). `unavailable` keeps an
    # unread tally from rendering as a real zero.
    ratings: dict = Field(default_factory=lambda: {"up": 0, "down": 0, "total": 0, "unavailable": False})


class PortalAgentReport(BaseModel):
    """Report metadata for the agent page's Reports tab (#918 surface reused)."""
    id: str
    report_type: Optional[str] = None
    title: Optional[str] = None
    display_hint: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    created_at: Optional[str] = None


class PortalAgentReports(BaseModel):
    agent_name: str
    reports: list[PortalAgentReport] = Field(default_factory=list)


class PortalChatStateEntry(BaseModel):
    """One chat's per-viewer state (ent#359). ``kind`` is ``thread`` (a portal
    session) or ``room`` (a multi-agent room) — two independent id spaces, so
    both fields are needed to address a chat."""
    kind: str
    id: str
    starred: bool = False
    unread: int = 0


class PortalChatState(BaseModel):
    """The signed-in viewer's star + unread state across every chat (ent#359).

    One call rather than a field on each list: threads and rooms are served by
    different endpoints (and live in different repos), and the sidebar has to
    order them together.
    """
    chats: list[PortalChatStateEntry] = []


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


class PortalRatingRequest(BaseModel):
    """One click on a message or a deliverable (ent#366).

    `comment` is optional and only meaningful on a negative rating — it is the
    box that opens under a thumbs-down. Capped here as well as at the service,
    because this is the boundary a client writes to.
    """
    target_kind: str                  # 'message' | 'deliverable'
    target_id: str = Field(..., max_length=128)
    rating: str                       # 'up' | 'down'
    comment: Optional[str] = Field(None, max_length=2000)


class PortalRatingResult(BaseModel):
    """What was recorded, and whether the words went anywhere further."""
    target_kind: str
    target_id: str
    rating: str
    comment_recorded: bool
    rated_at: Optional[str] = None
    # ent#366 AC #6: absent the capture-feedback skill the rating still records
    # and this says so, so the UI can thank the person honestly instead of
    # implying a follow-up that will not happen.
    capture_feedback: Optional[str] = None   # 'dispatched' | 'already_dispatched' | 'skill_not_installed' | None


class PortalHistoryMessage(BaseModel):
    """One persisted turn in a client↔agent conversation."""
    # ent#366: the row's own id, so a thumb has something to point at. Optional
    # because a message composed client-side during a live turn has no row yet.
    id: Optional[str] = None
    role: str                       # 'user' | 'assistant'
    content: str
    cost: Optional[float] = None
    created_at: Optional[str] = None
    # The caller's OWN rating of this message, if any — never anyone else's.
    # Present so a reload shows the thumb the person already gave.
    my_rating: Optional[str] = None  # 'up' | 'down' | None


class PortalTurnOutcome(BaseModel):
    """Why the last turn on this thread ended badly (#2320).

    A turn that fails before or at start persists no assistant message and
    clears its in-flight marker, so the client sees a thread that looks idle and
    reports "we've lost track of this turn" — for a turn the backend diagnosed
    precisely. This is that diagnosis, in the only form a client may receive it.

    ``message`` is client-safe by construction: every producer is a
    ``ClientPortalError`` detail already authored as client copy, and the one
    uncategorised path substitutes a fixed sentence. The raw
    ``schedule_executions.error`` text is never carried here.

    ``retryable`` is decided at the raise site and answers exactly one question:
    is re-sending guaranteed to be wrong? It is True only where nothing reached
    the agent, so nothing was billed — the case the #2120/#2133 no-Retry rule
    was never about.
    """
    execution_id: str
    category: str          # see PORTAL_FAILURE_CATEGORIES in service.py
    message: str
    retryable: bool = False


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
    # #2214: how long the reattaching client may honestly keep waiting for that
    # turn — the in-flight marker's REMAINING TTL in seconds, measured at this
    # read (the budget was fixed at dispatch, so a fresh full budget here would
    # over-wait by the turn's elapsed time). None when nothing is in flight or
    # the TTL is unreadable; the client then falls back. Optional and additive —
    # declared here because the route's `response_model` strips undeclared
    # fields, so without this line the budget would silently never leave the
    # server.
    in_flight_wait_budget_seconds: Optional[int] = None
    # #2320: the last turn's failure, when it failed. Present only while the
    # record lives (15 min) and only for a turn that ended badly — a thread
    # whose last turn answered carries None. Declared here for the same reason
    # the budget above is: an undeclared key is stripped by `response_model` and
    # never reaches the client.
    last_turn_outcome: Optional[PortalTurnOutcome] = None


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
