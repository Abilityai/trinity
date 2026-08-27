"""
Pydantic models for the Trinity backend API.
"""
import os
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator, model_validator
from typing import Any, Dict, List, Literal, Optional, Union
from datetime import datetime
from enum import Enum

from utils.helpers import parse_iso_timestamp, to_utc_iso
from db_models import WebFileUpload  # noqa: F401 — re-exported for router imports


# Fork-to-own destination: "owner/name". Owner per GitHub rules (alphanumeric +
# inner hyphens); repo name word chars, dots, hyphens. Anchored so the value can
# be safely embedded in GitHub API paths and git URLs (trinity-enterprise#93).
_FORK_DESTINATION_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$"
)


# A GitHub PAT is sent as an `Authorization: Bearer <pat>` header
# (`services/github_service.py`) and embedded in a git remote URL. h11 rejects an
# illegal header value by ECHOING it, so a token carrying `\r` or `\n` — what a
# paste from a terminal or clipboard routinely picks up — surfaces the RAW token
# in the exception message, which then reaches an error response and the platform
# log (Vector-captured, operator-readable). Validating the charset HERE closes
# that at the boundary, once, for every consumer — rather than scrubbing each
# error handler downstream and hoping none is missed.
#
# `\x21-\x7E` is printable ASCII minus space: a superset of every GitHub PAT
# format (classic `ghp_*` and fine-grained `github_pat_*` are `[A-Za-z0-9_]`) and
# exactly the set that is safe in both a header value and a URL userinfo field.
# Deliberately permissive about WHICH printable characters, so a future token
# format is not rejected; strict about whitespace and control characters, which
# is where the leak lives.
_PAT_SAFE_RE = re.compile(r"^[\x21-\x7E]+$")


def _validate_pat_secret(v: SecretStr) -> SecretStr:
    """Strip surrounding whitespace and reject a header-unsafe GitHub token."""
    raw = v.get_secret_value().strip()
    if not raw:
        raise ValueError("github_pat must not be empty")
    if not _PAT_SAFE_RE.match(raw):
        # Never echo the value — that is the leak this guard exists to prevent.
        raise ValueError(
            "github_pat contains characters that are not valid in a GitHub "
            "token (whitespace, line breaks or control characters). Copy the "
            "token again without surrounding whitespace."
        )
    return SecretStr(raw)


class ForkToOwnRequest(BaseModel):
    """Fork-to-own creation parameters (trinity-enterprise#93).

    Copies the GitHub template into a repo the user owns before creating the
    agent from it. The PAT is the USER's token — it creates the destination
    repo, pushes the template copy, and becomes the agent's per-agent PAT
    (#347). SecretStr keeps it out of reprs/logs; unwrap exactly once at the
    crud boundary.
    """
    destination_repo: str = Field(
        ..., description="Destination repo as owner/name in the user's account or org"
    )
    github_pat: SecretStr = Field(
        ..., description="User's GitHub PAT — creates the repo and becomes the agent's git identity"
    )
    private: bool = Field(
        True, description="Destination repo visibility (private by default)"
    )

    @field_validator("destination_repo")
    @classmethod
    def _validate_destination(cls, v: str) -> str:
        v = v.strip()
        if ".." in v or not _FORK_DESTINATION_RE.match(v):
            raise ValueError(
                "destination_repo must be 'owner/name' (GitHub owner and repo "
                "name characters only)"
            )
        return v

    @field_validator("github_pat")
    @classmethod
    def _validate_pat(cls, v: SecretStr) -> SecretStr:
        return _validate_pat_secret(v)


class BindAgentRepoRequest(BaseModel):
    """Post-creation repo binding parameters (trinity-enterprise#109).

    Points a LIVE agent at a GitHub repo the user owns, creating it if needed,
    from the agent's *current workspace* — not from its template. Same three
    fields as :class:`ForkToOwnRequest` and the same destination validator, but
    a distinct model because the two are distinct API contracts (a create-time
    sub-object vs a standalone request body) and #654/Invariant #14 wants the
    contract visible here rather than aliased.

    ``github_pat`` is the USER's token: it creates/validates the destination,
    authenticates the in-container push, and is then persisted as the agent's
    per-agent PAT (#347). SecretStr keeps it out of reprs/logs; unwrap exactly
    once at the service boundary.
    """

    destination_repo: str = Field(
        ...,
        description="Destination repo as owner/name in the user's account or org",
    )
    github_pat: SecretStr = Field(
        ...,
        description=(
            "User's GitHub PAT — creates/authorizes the repo and becomes the "
            "agent's git identity"
        ),
    )
    private: bool = Field(
        True, description="Destination repo visibility (private by default)"
    )

    @field_validator("destination_repo")
    @classmethod
    def _validate_destination(cls, v: str) -> str:
        v = v.strip()
        if ".." in v or not _FORK_DESTINATION_RE.match(v):
            raise ValueError(
                "destination_repo must be 'owner/name' (GitHub owner and repo "
                "name characters only)"
            )
        return v

    @field_validator("github_pat")
    @classmethod
    def _validate_pat(cls, v: SecretStr) -> SecretStr:
        return _validate_pat_secret(v)


class BindAgentRepoResponse(BaseModel):
    """Result of a successful post-creation repo binding (trinity-enterprise#109).

    Carries no token and no repo URL containing one. ``previous_repo`` is
    echoed so the UI can state what the agent moved *from* without a second
    round-trip, and ``recreated`` tells the operator whether the container env
    was actually re-baked — the step that makes the rebind survive a restart.
    """

    success: bool = True
    agent_name: str
    github_repo: str
    previous_repo: str
    default_branch: str
    private: bool
    created_repo: bool
    reused_existing: bool
    recreated: bool
    repo_url: str
    message: str


class EphemeralConfig(BaseModel):
    """Ephemeral "ghost" agent budget (trinity-enterprise#69).

    At least one of ``max_executions`` / ``ttl_seconds`` is required. A TTL is
    ALWAYS stamped at creation (defaulting to the platform ceiling when only
    ``max_executions`` is given) so no ghost is immortal.
    """
    max_executions: Optional[int] = Field(
        None, ge=1, le=100,
        description="Discard after this many terminal executions (1-100)",
    )
    ttl_seconds: Optional[int] = Field(
        None, ge=60,
        description="Discard after this many seconds (60..platform ceiling, default ceiling 24h)",
    )

    @model_validator(mode="after")
    def _at_least_one_budget(self):
        if self.max_executions is None and self.ttl_seconds is None:
            raise ValueError(
                "ephemeral requires max_executions and/or ttl_seconds"
            )
        return self


class AgentConfig(BaseModel):
    """Configuration for creating a new agent."""
    name: str  # the immutable slug — every route/container/volume/key keys on it
    # ent#1640: optional human-facing display label set AT creation. Presentation
    # only; None → the agent renders under its slug (exactly today's behavior).
    # Same normalization + named validation as the post-creation PUT /label.
    display_label: Optional[str] = None
    # #2104: the free-text agent `type` taxonomy is retired — tags are the
    # classification mechanism. A `type=` kwarg from an older caller is
    # silently ignored (Pydantic default extra="ignore"), never an error.
    base_image: str = "trinity-agent-base:latest"
    resources: Optional[dict] = {"cpu": "2", "memory": "4g"}
    tools: Optional[List[str]] = ["filesystem", "web_search"]
    mcp_servers: Optional[List[str]] = []
    custom_instructions: Optional[str] = None
    port: Optional[int] = None  # SSH port (auto-assigned if None)
    template: Optional[str] = None  # Template to initialize agent from
    # GitHub-native agent support
    github_repo: Optional[str] = None  # GitHub repo (e.g., "Abilityai/agent-ruby")
    github_credential_id: Optional[str] = None  # Credential ID for GitHub PAT
    # GitHub source mode (unidirectional pull from a branch)
    source_branch: Optional[str] = "main"  # Branch to pull updates from
    source_mode: Optional[bool] = True  # True = track source branch (pull only), False = create working branch
    # Multi-runtime support
    runtime: Optional[str] = "claude-code"  # "claude-code" or "gemini-cli"
    runtime_model: Optional[str] = None  # Model override (e.g., "sonnet-4.5", "gemini-2.5-pro")
    # Security options
    full_capabilities: Optional[bool] = False  # True = Docker default caps (apt-get works), False = restricted (secure default)
    # Fork-to-own creation (trinity-enterprise#93): copy the github: template
    # into a user-owned repo first; the agent is created from that copy.
    fork_to_own: Optional[ForkToOwnRequest] = None
    # trinity-enterprise#15: explicit GitHub import intent. None → legacy
    # behavior (clone semantics; fork when a fork_to_own block is present).
    # "copy" is a backend-materialized snapshot: no git sync, no git-config
    # row, no GitHub env in the container.
    import_intent: Optional[Literal["fork", "copy", "clone"]] = None
    # Ephemeral "ghost" agent (trinity-enterprise#69): budgeted, volume-less,
    # hard-discarded at budget. Entitlement-gated at the creation path.
    ephemeral: Optional[EphemeralConfig] = None

    @field_validator("display_label")
    @classmethod
    def _normalize_display_label(cls, v: Optional[str]) -> Optional[str]:
        return normalize_display_label(v)


# ent#181/#1640 — the human-facing display label. Shared normalization + a
# NAMED validation error (not a generic 422 blob), used everywhere a label is
# accepted (set-after-creation AND at creation), so the policy lives in one place.
DISPLAY_LABEL_MAX_LEN = 120


def normalize_display_label(value: Optional[str]) -> Optional[str]:
    """Trim, empty→None (clear), reject control chars / line breaks, cap length.

    Uniqueness is deliberately NOT enforced — the slug (`agent_name`) already
    guarantees uniqueness; a display label is a presentation string that may
    legitimately repeat across agents (#1640). Raises ``ValueError`` with a
    specific message on bad input so callers surface a named error.
    """
    if value is None:
        return None
    # Normalize to NFC so visually-identical labels compare/store consistently.
    value = unicodedata.normalize("NFC", value).strip()
    if not value:
        return None  # blank clears the label → render under the slug again
    if len(value) > DISPLAY_LABEL_MAX_LEN:
        raise ValueError(
            f"display label must be at most {DISPLAY_LABEL_MAX_LEN} characters"
        )
    # A display name is a single line: reject control chars (category Cc, incl.
    # \n\t\r) and the Unicode line/paragraph separators.
    if any(unicodedata.category(ch) == "Cc" or ch in (" ", " ") for ch in value):
        raise ValueError("display label must not contain control characters or line breaks")
    return value


class AgentLabelUpdate(BaseModel):
    """PUT body — set or clear an agent's human-facing label (ent#181).

    `label=None` (or blank) clears it, and the agent renders under its slug
    again. Presentation only: the slug never moves, which is the entire point —
    a slug rename re-keys ~20 tables and strands the agent's volumes (#1664).

    #1821: `label` is REQUIRED-but-nullable, and unknown fields are rejected.
    Clearing is a legitimate operation expressed as an explicit null, but with
    an ignored-extras model and a `None` default it was also what you got from
    any body the server did not recognise — so `{"display_label": "..."}` (an
    easy mistake: `display_label` is the DB column and `display_name` the
    response field) returned 200 and silently wiped the label. Both an unknown
    field and an empty `{}` now 422 instead of destroying data.
    """
    model_config = ConfigDict(extra="forbid")

    label: Optional[str]

    @field_validator("label")
    @classmethod
    def _normalize_label(cls, v: Optional[str]) -> Optional[str]:
        return normalize_display_label(v)


class AgentStatus(BaseModel):
    """Status of an agent container. (#2104: no `type` — the taxonomy is retired.)"""
    name: str
    status: str
    port: int  # SSH port only - UI no longer exposed externally
    created: datetime
    resources: dict
    container_id: Optional[str] = None
    template: Optional[str] = None
    runtime: Optional[str] = "claude-code"  # "claude-code" or "gemini-cli"
    base_image_version: Optional[str] = None  # Version of trinity-agent-base image
    ephemeral: Optional[bool] = False  # trinity-enterprise#69: ghost agent (budgeted, hard-discarded)
    display_label: Optional[str] = None  # ent#181: human-facing name; None = render `name` (the slug)
    # trinity-enterprise#15: copy-intent provenance — {source_repo, source_branch,
    # head_sha, file_count}; set only on the create response of a snapshot import.
    import_snapshot: Optional[Dict[str, Any]] = None

    class Config:
        json_encoders = {
            # Use to_utc_iso to ensure 'Z' suffix for frontend compatibility
            datetime: lambda v: to_utc_iso(v) if v else None
        }


class AgentSubscriptionPressure(BaseModel):
    """One agent's subscription-pressure row for the Dashboard batch endpoint
    (#471). `auth_mode` reuses the `AgentAuthStatus` vocabulary verbatim
    ("subscription" | "api_key" | "not_configured") — never a third enum.
    An explicit response_model allow-list (ent#334): the payload is
    disclosure-bearing (subscription names to shared accessors, matching the
    per-agent `AgentAuthStatus` gate), so nothing extra may ride along."""
    agent_name: str
    auth_mode: str
    subscription_name: Optional[str] = None
    failure_events_24h: int = 0
    # #2352: the auth-kind slice of the total above. `failure_events_24h` cannot
    # tell a 429 from a rejected token, and since the display predicate was
    # narrowed to real 429s, a dead-token subscription is no longer
    # `rate_limited_now` — without this field the badge would just swap one
    # wrong word for another.
    auth_failures_24h: int = 0
    rate_limited_now: bool = False
    # #2352: the provider probe's own verdict — "ok" | "invalid_token" |
    # "rate_limited" | "error", or None when no snapshot exists at all. A
    # rejected token is the most actionable state this payload can carry and is
    # otherwise indistinguishable from "no provider data".
    token_status: Optional[str] = None
    utilization_5h_pct: Optional[float] = None  # provider-truth when fresh, else None
    headroom_source: str = "observed"           # "anthropic" | "observed"


class SubscriptionPressureResponse(BaseModel):
    """Batch payload for `GET /api/agents/subscription-pressure` (#471)."""
    agents: List[AgentSubscriptionPressure] = []


class HeadroomHistoryWindow(BaseModel):
    """One rolling-limit window inside a history bucket (ent#433).

    `utilization_pct` is nullable INDEPENDENTLY of `status`: a 429 reports
    `status='rate_limited'` with no figure. A consumer must render that as an
    outage, never as 0% — coercing the NULL inverts the single most important
    sample in the series.
    """
    utilization_pct: Optional[float] = None
    resets_at: Optional[str] = None
    status: Optional[str] = None


class HeadroomHistoryBucket(BaseModel):
    """The LAST probe in one time bucket (ent#433).

    `last`, not `max`: probes are demand-driven, so a max is biased by how often
    anyone looked; the 5h and 7d windows peak at different instants so a
    two-column max has no single owning row; and a max over `utilization_pct`
    silently drops rate-limited samples that carry no figure.

    BOTH timestamps are load-bearing and neither substitutes for the other:
    `bucket_start` is the logical slot (what makes a gap detectable — a real
    timestamp alone cannot distinguish sample jitter from a missing bucket), and
    `fetched_at` is when the provider was actually asked. Buckets with no sample
    are simply absent; consumers render gaps as gaps and never interpolate.
    """
    bucket_start: str
    fetched_at: str
    status: str
    samples: int = 0
    five_hour: HeadroomHistoryWindow = Field(default_factory=HeadroomHistoryWindow)
    seven_day: HeadroomHistoryWindow = Field(default_factory=HeadroomHistoryWindow)
    representative_claim: Optional[str] = None
    overage_status: Optional[str] = None
    unified_status: Optional[str] = None


class SubscriptionHeadroomHistory(BaseModel):
    """Windowed headroom series for one subscription (ent#433).

    `coverage_pct` states how much of the window was actually observed
    (buckets holding a sample ÷ buckets elapsed). It exists so a thin series
    REPORTS ITSELF as thin rather than rendering as a confident flat line —
    the structural form of the honest-gaps rule, and the reason a sparse chart
    must never become an argument for probing more often.
    """
    subscription_id: str
    window: str          # "24h" | "7d" | "30d"
    bucket: str          # "hour" | "day"
    buckets: List[HeadroomHistoryBucket] = []
    coverage_pct: float = 0.0


class User(BaseModel):
    """Authenticated user."""
    id: int
    username: str
    email: Optional[str] = None
    role: str = "user"
    # For agent-scoped MCP API keys, this is the agent name
    agent_name: Optional[str] = None
    # For connector-scoped MCP keys, the single agent this key may consume.
    # Set ⇒ the principal is consumption-only: it may read/chat ONLY this agent
    # and may NOT perform owner or role-gated operations, even though it
    # resolves to the owner user. Edition-agnostic enforcement primitive — the
    # key itself is minted by an entitled module (core-primitive + enterprise-
    # knob, same shape as users.suspended_at #995).
    connector_agent: Optional[str] = None
    # ent#163: True for a `portal_delegate` MCP key — a trusted issuer that may
    # exchange an asserted end-user email for a portal session, and NOTHING else
    # (fenced centrally in `dependencies.get_current_user`). Like every MCP key
    # it resolves to the key OWNER, so a consumer must branch on this flag
    # rather than on the resolved user: the whole point of the request is that
    # it concerns somebody other than the owner.
    portal_delegate: bool = False
    # #1854: the raw `mcp_api_keys.scope` this principal authenticated with, or
    # None on the JWT (interactive human) branch. `scope` is a free-text column
    # with NO CHECK constraint and already carries five live values
    # (user/agent/system/connector/portal_delegate), so the two flags above are a
    # DENYlist over an open space: `scope='system'` sets neither `agent_name`
    # (only for scope='agent') nor `connector_agent` (only for
    # scope='connector'), walks through both guards, and resolves to the key
    # OWNER carrying the owner's role. This field is what lets a guard be an
    # ALLOWlist ("is this a human?") instead — fail-closed against a sixth scope
    # a future PR invents. Also the missing audit dimension: without it a
    # credential-rotation row cannot distinguish "the owner from a browser" from
    # "the owner's leaked MCP key".
    mcp_scope: Optional[str] = None
    # #2323: WHICH credential this principal presented. `validate_mcp_api_key`
    # has always returned these; `get_current_user` dropped them, so an admin
    # action taken with a machine key audited byte-identically to the owner in a
    # browser — and `routers/a2a.py` read `mcp_key_id` off this model via
    # `getattr` in two places that have therefore never worked (one of them the
    # A2A idempotency SCOPE, where the fallback to username collapses two
    # agent-scoped keys of one owner into a shared `messageId` namespace).
    # None on the JWT branch, which is the honest "no credential" answer.
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None


class Token(BaseModel):
    """OAuth2 password-grant response for ``POST /token``.

    Two mutually exclusive shapes, and the routes serialize with
    ``response_model_exclude_none=True`` so each carries **only** its own
    fields (#2322):

    * **Grant issued** — ``access_token`` + ``token_type``. The 2FA fields are
      absent, which is what makes this docstring's old claim ("absent in
      OSS-only builds") true; before #2322 every successful login, OSS
      included, carried all four of them as ``null``.
    * **Second factor pending** (enterprise 2FA, #5) — ``mfa_required`` +
      ``challenge_token`` (+ the two flags), and **no** ``access_token`` and
      **no** ``token_type``. The caller completes the flow at
      ``/api/enterprise/2fa/login/*`` to obtain the real token.

    ``token_type`` is the load-bearing part of that second bullet and the
    reason it is ``Optional`` rather than a plain ``str`` default. A password
    grant that did not issue a session must not describe itself as a bearer
    grant: it made a refusal indistinguishable from a success for every
    client that reads the token field without checking it (our own MCP client
    and CLI both did), turning a login refusal into unexplained 401s much
    later. Both success paths set it explicitly, so the wire format of a real
    grant is unchanged.
    """
    access_token: Optional[str] = None
    token_type: Optional[str] = None
    mfa_required: Optional[bool] = None
    mfa_enrolled: Optional[bool] = None
    enrollment_required: Optional[bool] = None
    challenge_token: Optional[str] = None


class ChatMessageRequest(BaseModel):
    """Request model for chat messages."""
    message: str
    model: Optional[str] = None  # Model alias: sonnet, opus, haiku, or full model name


class ModelChangeRequest(BaseModel):
    """Request model for changing agent's model."""
    model: str  # Model alias: sonnet, opus, haiku, or full model name


class ParallelTaskRequest(BaseModel):
    """Request model for parallel task execution (stateless, no conversation context)."""
    message: str  # The task to execute (may include context prompt with history)
    model: Optional[str] = None  # Model override: sonnet, opus, haiku, or full model name
    allowed_tools: Optional[List[str]] = None  # Tool restrictions (--allowedTools)
    system_prompt: Optional[str] = None  # Additional instructions (--append-system-prompt)
    timeout_seconds: Optional[int] = None  # DEPRECATED (#1068, demotion PR 1): per-task override. Agent execution_timeout_seconds (#665) / schedule cap (#913) is authoritative; honored-but-clamped to the agent cap for now, to be removed after one release of soak. None = use agent's config.
    max_turns: Optional[int] = None  # Maximum agentic turns (--max-turns) for runaway prevention
    async_mode: Optional[bool] = False  # If true, return immediately with execution_id (fire-and-forget)
    save_to_session: Optional[bool] = False  # If true, persist messages to chat_sessions (for authenticated Chat tab)
    user_message: Optional[str] = None  # Original user message (without context), used when save_to_session=True
    create_new_session: Optional[bool] = False  # If true, close existing active sessions and create a new one
    chat_session_id: Optional[str] = None  # Explicit chat session ID to save messages to (for continuing existing sessions)
    resume_session_id: Optional[str] = None  # Claude Code session ID to resume (EXEC-023)
    inject_result: Optional[bool] = False  # If true and self-task, inject result as message in originating chat session (SELF-EXEC-001)
    files: Optional[List[WebFileUpload]] = None  # File attachments (#364)
    # ent#224: the CALLER's current execution id. When agent A delegates to B,
    # B inherits A's originating channel/thread from this row, so B's completion
    # can be reported back to the Slack thread the work actually came from.
    # Optional and fail-open — absent means "no channel context to inherit".
    parent_execution_id: Optional[str] = None


# ============================================================================
# Activity Stream Models
# ============================================================================

class ActivityType(str, Enum):
    """Types of activities that can be tracked."""
    # Chat activities
    CHAT_START = "chat_start"
    CHAT_END = "chat_end"
    TOOL_CALL = "tool_call"

    # Schedule activities
    SCHEDULE_START = "schedule_start"
    SCHEDULE_END = "schedule_end"

    # Collaboration activities
    AGENT_COLLABORATION = "agent_collaboration"

    # Self-execute activities (agent runs background task on itself during chat)
    SELF_TASK = "self_task"

    # Execution control activities
    EXECUTION_CANCELLED = "execution_cancelled"

    # Future activity types (not yet implemented)
    FILE_ACCESS = "file_access"
    MODEL_CHANGE = "model_change"
    CREDENTIAL_RELOAD = "credential_reload"
    GIT_SYNC = "git_sync"


class ActivityState(str, Enum):
    """State of an activity."""
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"  # #1332: user-cancelled terminal, distinct from FAILED


class ActivityCreate(BaseModel):
    """Request model for creating a new activity."""
    agent_name: str
    activity_type: ActivityType
    activity_state: ActivityState = ActivityState.STARTED
    parent_activity_id: Optional[str] = None
    user_id: Optional[int] = None
    triggered_by: str = "user"  # user, schedule, agent, system
    related_chat_message_id: Optional[str] = None
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None
    error: Optional[str] = None


class Activity(BaseModel):
    """Activity record from database."""
    id: str
    agent_name: str
    activity_type: str
    activity_state: str
    parent_activity_id: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    user_id: Optional[int] = None
    triggered_by: str
    related_chat_message_id: Optional[str] = None
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None
    error: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True


# ============================================================================
# Agent Reports (#918) — agent-published structured telemetry / domain reports
# ============================================================================

# Max serialized-JSON byte length of a report payload. Enforced at the router
# (oversize → HTTP 413) to bound SQLite growth and list-response weight.
# #1537: raised from 256 KiB. Measured on a live fleet before choosing: the
# reports in existence were 201 bytes on average, 683 at the largest — four
# orders of magnitude under the old cap. So the cap was never the thing agents
# hit; it was the thing that would reject the FIRST real tabular report (a lead
# list, a scan result) the moment one appeared. 5 MiB is a deliberate middle:
# comfortably past "thousands of rows" of ordinary tabular JSON, and still small
# enough that one row is a sane thing to hold in memory while rendering.
#
# The blob stays in one TEXT column. Off-row storage was considered and NOT
# built: with no payload anywhere near the old cap, a migration + a rows table
# would be a schema commitment made against a hypothetical. The paginated row
# reader below is what keeps a large payload off the wire; if real payloads ever
# approach this ceiling, THAT measurement is what should justify moving them
# off-row.
REPORT_PAYLOAD_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

# Rows returned per page by the tabular row reader (#1537). Bounds the response
# even when a payload carries tens of thousands of rows.
REPORT_ROWS_PAGE_DEFAULT = 100
REPORT_ROWS_PAGE_MAX = 1000

# Renderer hints the frontend understands; an unknown/absent hint falls back to
# the report_type prefix map, then the JSON viewer.
ReportDisplayHint = Literal["table", "kpi", "markdown", "timeline", "json"]

_REPORT_TYPE_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


def _validate_iso8601(value: Optional[str]) -> Optional[str]:
    """Reject a non-ISO-8601 period bound (None passes through)."""
    if value is None:
        return value
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ValueError("must be an ISO-8601 timestamp")
    return value


class ReportCreate(BaseModel):
    """Request body for an agent publishing a structured report (#918).

    The agent + author are resolved server-side from the MCP/JWT auth context —
    never from this body — so a report cannot be attributed to another agent.
    """
    report_type: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=300)
    payload: Dict  # byte-capped at the router (REPORT_PAYLOAD_MAX_BYTES → 413)
    display_hint: Optional[ReportDisplayHint] = None
    schema_version: int = Field(1, ge=1, le=1000)
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    # ent#365 — who the report is FOR. Absent = operator-only, which is what
    # every report published before this field meant. The address is checked at
    # the router against the agent's own roster: an agent may hand a report to
    # someone it already talks to, never to an arbitrary address.
    audience_email: Optional[str] = Field(None, max_length=320)
    # The turn the agent is publishing from. Used ONLY to resolve which
    # Workspace chat the deliverable card belongs in, server-side — the session
    # is never accepted from the agent, or a report could be posted into a
    # conversation the agent was never part of.
    execution_id: Optional[str] = Field(None, max_length=128)

    @field_validator("audience_email")
    @classmethod
    def _normalize_audience(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        # Shape only — reachability is the router's check, and it is the one
        # that matters. Rejecting the empty string here means "unaddressed" has
        # exactly one spelling (absent), so the audience column never holds ''.
        if not v:
            return None
        if "@" not in v or " " in v:
            raise ValueError("audience_email must be an email address")
        return v

    @field_validator("report_type")
    @classmethod
    def _check_report_type(cls, v: str) -> str:
        if not _REPORT_TYPE_RE.match(v):
            raise ValueError(
                "report_type must be namespaced lower_snake segments joined by "
                "'.', e.g. 'recon.weekly_summary'"
            )
        return v

    @field_validator("period_start", "period_end")
    @classmethod
    def _check_period_iso(cls, v: Optional[str]) -> Optional[str]:
        return _validate_iso8601(v)

    @model_validator(mode="after")
    def _check_period_order(self) -> "ReportCreate":
        if self.period_start and self.period_end and self.period_start > self.period_end:
            raise ValueError("period_start must be <= period_end")
        return self


class TelemetrySharingUpdate(BaseModel):
    """PUT body for Tier-2 opt-in fleet sharing consent (ent#12).

    ``enabled`` is the reversible, default-off consent. ``backfill_days`` (only
    meaningful on enable) is the disclosed history window included in the
    consent-time backfill share. Anonymized aggregates only — no PII.
    """
    enabled: bool
    backfill_days: Optional[int] = Field(None, ge=0, le=3650)


class OperatorIntakeUpdate(BaseModel):
    """PUT body for the operator-intake Settings surface (ent#463).

    Three intents share the endpoint, distinguished by ``enabled`` and the
    presence of contact fields:

    * **Opt-in and submit** — ``enabled=true`` with an ``email``: writes the
      durable consent flag and, on a fresh install, fires the at-most-once
      hosted intake POST via the existing service.
    * **Opt-in without submitting** — ``enabled=true`` on an install that has
      already submitted: records durable consent, no re-send (marker preserved).
    * **Opt-out** — ``enabled=false``: records durable decline, no rollback of a
      prior submission (the record was sent; contact support to request
      deletion).

    Field constraints mirror ``SetupPasswordRequest`` for the operator-profile
    fields so the two producers can't diverge on shape.
    """
    enabled: bool
    email: Optional[str] = Field(None, max_length=254)
    company: Optional[str] = Field(None, max_length=200)
    name: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    use_case: Optional[str] = Field(None, max_length=500)


class ProductEventCreate(BaseModel):
    """Request body for a local product-event beacon (ent#184).

    ``event_type`` is validated against a fixed allow-list at the router (unknown
    → 422) so the local table can't be spammed with arbitrary strings. Local-only,
    zero egress — one local row per accepted event.
    """
    event_type: str = Field(..., min_length=1, max_length=64)
    context: Optional[Dict] = None  # small optional metadata (byte-capped at the router)


class ReportSummary(BaseModel):
    """List-response model — metadata only, never carries ``payload`` (#918)."""
    id: str
    agent_name: str
    report_type: str
    title: str
    display_hint: Optional[str] = None
    schema_version: int = 1
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    created_at: str
    # ent#365: who the report was produced for (NULL = operator-facing). On the
    # access-controlled REST surfaces only — deliberately NOT on the `/ws`
    # broadcast, which is SCOPE_ALL and unfiltered (the #918 rule).
    addressed_to: Optional[str] = None

    class Config:
        from_attributes = True


class Report(ReportSummary):
    """Detail-response model — full row including the decoded ``payload``."""
    user_id: Optional[int] = None
    payload: Dict


class FleetReportStats(BaseModel):
    """Aggregate stat-card data for the fleet Reports view (#918)."""
    total: int
    by_type: Dict[str, int]
    agents: int


# ============================================================================
# Execution Queue Models (Parallel Execution Prevention)
# ============================================================================

class ExecutionSource(str, Enum):
    """Source of an execution request."""
    USER = "user"       # User chat via UI
    SCHEDULE = "schedule"  # Scheduled task
    AGENT = "agent"     # Agent-to-agent via MCP


class TaskExecutionStatus(str, Enum):
    """
    Canonical status values for task/schedule executions (RELIABILITY-005).

    State machine — allowed transitions and authorized writers:

        [create]  → QUEUED       writer: TaskExecutionService / BacklogService
        QUEUED    → RUNNING      writer: BacklogService (drain) / TaskExecutionService
        RUNNING   → SUCCESS      writer: TaskExecutionService (agent HTTP response — always wins)
        RUNNING   → FAILED       writer: TaskExecutionService / CleanupService (guarded: no overwrite of terminal)
        RUNNING   → CANCELLED    writer: terminate handler (guarded)
        RUNNING   → PENDING_RETRY writer: scheduler retry handler (#271)
        PENDING_RETRY → RUNNING  writer: scheduler retry dispatch
        any       → SKIPPED      writer: TaskExecutionService (capacity overflow path)

    CAS invariant (db/schedules.py update_execution_status): SUCCESS writes are
    unconditional; all other terminal writes are blocked if the row is already
    in a terminal state, preventing cleanup paths from overwriting a real completion.
    """
    QUEUED = "queued"          # Persisted async task waiting for a free slot (BACKLOG-001)
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    PENDING_RETRY = "pending_retry"  # Awaiting retry dispatch (#271)


class ActivityCloseOutcome(Enum):
    """Tri-state result of closing an activity (#1804).

    One boolean cannot answer both questions the callers ask. ``routers/
    internal.py`` needs "did this row exist" (it 404s); ``activity_service``
    needs "did anything change" (it broadcasts). Once the close is a lattice
    CAS (``db/activities.py::_close_predicate``), an idempotent no-op close is a
    *designed* outcome, so the two answers diverge routinely — hence three
    states, not two.

    Lives in ``models`` (not ``db/activities``) deliberately: it is a contract
    type shared by the db layer and the service layer, like ``ActivityState``
    beside it, and callers compare it by **identity**. ``models`` is the leaf
    everything imports from and nothing re-imports, so the enum object stays
    the same one across a test harness that evicts ``db.*`` from ``sys.modules``
    — otherwise two distinct enum classes exist and every ``is`` check silently
    goes False.
    """

    UPDATED = "updated"                # the CAS won — broadcast
    ALREADY_CLOSED = "already_closed"  # row exists, predicate refused — no clobber
    NOT_FOUND = "not_found"            # no such activity — 404 here, and only here


def activity_state_for_terminal(status) -> "ActivityState":
    """Map a terminal execution status to the activity state that closes its
    dispatch activity (#1332).

    SUCCESS → COMPLETED, CANCELLED → CANCELLED, everything else → FAILED.

    A cancelled execution must read as a distinct, non-failure terminal so
    activity-derived views (collaboration timeline, replay, needs-attention)
    don't collapse it into FAILED. ``status`` accepts a ``TaskExecutionStatus``
    or its bare string value (both compare equal — str-backed enum). The
    explicit ``else`` keeps a future status (e.g. PENDING_RETRY) deterministic.
    """
    if status == TaskExecutionStatus.SUCCESS:
        return ActivityState.COMPLETED
    if status == TaskExecutionStatus.CANCELLED:
        return ActivityState.CANCELLED
    return ActivityState.FAILED


class BusinessStatus(str, Enum):
    """
    Business validation status for task executions (VALIDATE-001).

    Separate from technical TaskExecutionStatus — an execution can complete
    successfully (technical status) but fail business validation.
    """
    PENDING_VALIDATION = "pending_validation"  # Execution completed, awaiting validation
    VALIDATED = "validated"                     # Validation passed
    FAILED_VALIDATION = "failed_validation"    # Validation found incomplete/incorrect work
    SKIPPED = "skipped"                        # Validation not configured for this schedule


class QueueItemStatus(str, Enum):
    """Status of an execution request in the in-memory/Redis execution queue."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class Execution(BaseModel):
    """
    Represents an execution request in the agent queue.

    Used to track and serialize requests for platform-level queuing.
    Only one execution can run per agent at a time.
    """
    id: str                                    # UUID
    agent_name: str
    source: ExecutionSource
    source_agent: Optional[str] = None         # If source == AGENT
    source_user_id: Optional[str] = None       # User who triggered
    source_user_email: Optional[str] = None    # User email for tracking
    message: str                               # The chat message
    queued_at: datetime
    started_at: Optional[datetime] = None
    status: QueueItemStatus = QueueItemStatus.QUEUED

    class Config:
        json_encoders = {
            # Use to_utc_iso to ensure 'Z' suffix for frontend compatibility
            datetime: lambda v: to_utc_iso(v) if v else None
        }


class QueueStatus(BaseModel):
    """Status of an agent's execution queue."""
    agent_name: str
    is_busy: bool
    current_execution: Optional[Execution] = None
    queue_length: int
    queued_executions: List[Execution] = []


# ============================================================================
# System Manifest Models (Recipe-based Multi-Agent Deployment)
# ============================================================================

# ent#126: cap on a manifest accepted over the wire OR read from the bundled
# catalog. Generous — the largest bundled manifest is ~2 KB — so it bounds abuse
# without constraining a real fleet definition.
MANIFEST_MAX_BYTES = 256 * 1024


class SystemAgentConfig(BaseModel):
    """Configuration for a single agent in a system manifest."""
    template: str  # e.g., "github:Org/repo" or "local:scout" (#1759: must resolve, or create 400s)
    resources: Optional[dict] = None  # {"cpu": "2", "memory": "4g"}
    folders: Optional[dict] = None  # {"expose": bool, "consume": bool}
    schedules: Optional[List[dict]] = None  # [{name, cron, message, ...}]
    tags: Optional[List[str]] = None  # Additional tags for this agent (ORG-001 Phase 4)


class SystemPermissions(BaseModel):
    """Permission configuration for system agents."""
    preset: Optional[str] = None  # "full-mesh", "orchestrator-workers", "none"
    explicit: Optional[Dict[str, List[str]]] = None  # {"orchestrator": ["worker1", "worker2"]}


class SystemViewConfig(BaseModel):
    """Configuration for auto-creating a System View on deploy (ORG-001 Phase 4)."""
    name: str  # Display name for the view
    icon: Optional[str] = None  # Emoji icon
    color: Optional[str] = None  # Hex color
    shared: bool = True  # Visible to all users?


class SystemManifest(BaseModel):
    """Parsed system manifest from YAML."""
    name: str
    description: Optional[str] = None
    prompt: Optional[str] = None
    agents: Dict[str, SystemAgentConfig]
    permissions: Optional[SystemPermissions] = None
    # ORG-001 Phase 4: Tags and System View support
    default_tags: Optional[List[str]] = None  # Applied to all agents in manifest
    system_view: Optional[SystemViewConfig] = None  # Auto-create System View on deploy
    # ent#126: top-level keys the parser does not recognise, recorded so
    # `validate_manifest` can WARN about them. Silently dropping them is how
    # `trinity_prompt:` (a typo for `prompt:`) sat in a shipped manifest doing
    # nothing. Warned, never rejected — rejecting would 400 manifests that
    # deploy today.
    unknown_keys: List[str] = []
    # #2373: the same record, one level down. `parse_manifest` reads five
    # per-agent keys and dropped the rest in silence, so `agents.x.credentials:`
    # / `skills:` / `display_label:` vanished with no warning while a top-level
    # typo already warned. {agent short name: [unknown keys]}
    unknown_agent_keys: Dict[str, List[str]] = {}


class SystemDeployRequest(BaseModel):
    """Request to deploy a system from YAML manifest."""
    # #1884: the raw YAML is parsed by `system_service.parse_manifest`, which
    # applies the size / expansion-cost / duplicate-key guards mirroring the MCP
    # pipeline reader (#919). Not validated here — a Pydantic field cannot see
    # YAML structure.
    manifest: str  # Raw YAML string
    dry_run: bool = False
    # trinity-enterprise#125: abort on the first agent-create failure (legacy
    # behavior) instead of the default best-effort continue-and-report.
    strict: bool = False


class SystemDeployFailure(BaseModel):
    """One agent that failed to create during a system deploy (trinity-enterprise#125)."""
    name: str  # Final (resolved) agent name
    short_name: str  # Short name from the manifest
    template: str
    reason: str  # Sanitized, truncated failure reason
    status_code: Optional[int] = None  # Original HTTP status when the failure was an HTTPException


class SystemSchedulePreview(BaseModel):
    """One schedule a manifest would create, as shown in the dry-run preview (ent#126).

    Mirrors what `create_schedules` would build, including its `.get()` defaults
    — `enabled` in particular defaults to True, which is why a manifest that
    merely lists a schedule starts autonomous executions on deploy.
    """
    agent: str  # Final (resolved) agent name
    short_name: str
    name: str
    cron: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str] = None


class BundledManifestSummary(BaseModel):
    """One manifest shipped in `config/manifests/`, as listed in the catalog (ent#126)."""
    id: str  # Filename stem — the {manifest_id} path parameter
    filename: str
    # None when the file could not be parsed; the card still renders, with `reason`.
    name: Optional[str] = None
    description: Optional[str] = None
    agent_count: int = 0
    templates: List[str] = []
    schedule_count: int = 0
    # True when the manifest carries a top-level `prompt:`, which OVERWRITES the
    # platform-wide trinity_prompt for every agent. The UI gates deploy on an
    # explicit acknowledgement for this.
    sets_prompt: bool = False
    permissions_preset: Optional[str] = None
    # parse + validate + the same side-effect-free template/resource preflight the
    # dry-run uses. Named `valid` only because all three ran; a parse-only check
    # would be `parseable`.
    valid: bool = False
    reason: Optional[str] = None  # Why it is not valid (short, human-readable)
    # At least one agent this manifest would create already exists, so deploying
    # produces `_N`-suffixed duplicates.
    already_deployed: bool = False


class BundledManifestDetail(BundledManifestSummary):
    """A bundled manifest plus its raw YAML, for loading into the editor (ent#126)."""
    manifest: str  # Raw YAML text


class SystemDeployResponse(BaseModel):
    """Response from system deployment."""
    # "deployed" (all created) | "partial" (some failed) | "failed" (none created)
    # | "valid" (dry_run, will deploy) | "invalid" (dry_run, blockers in `failed`,
    # #1841) — trinity-enterprise#125
    status: str
    system_name: str
    agents_created: List[str]  # Final agent names created
    agents_to_create: Optional[List[dict]] = None  # For dry_run: [{name, template}]
    prompt_updated: bool
    permissions_configured: int = 0
    schedules_created: int = 0
    tags_configured: int = 0  # ORG-001 Phase 4: Number of tags applied
    system_view_created: Optional[str] = None  # ORG-001 Phase 4: View ID if created
    warnings: List[str] = []
    failed: List[SystemDeployFailure] = []  # trinity-enterprise#125: per-agent create failures
    # ent#126 dry-run preview fields (None on a real deploy). Computed by the
    # SAME pure resolvers the writers consume, so the preview cannot drift from
    # what deploy actually does.
    #
    # `permission_edges` is {source: targets} rather than the resolver's ordered
    # pair list: write ORDER does not matter for display, and the sources are
    # unique by construction (each branch writes any given agent at most once —
    # explicit's clear phase and set phase are disjoint), so collapsing to a dict
    # is lossless for the set of writes.
    permission_edges: Optional[Dict[str, List[str]]] = None
    schedules_preview: Optional[List[SystemSchedulePreview]] = None
    # `system_view_created` is None both when a view was never requested AND when
    # creating it failed (create_system_view swallows the exception), so a caller
    # cannot tell "no view wanted" from "view lost". This disambiguates it: the
    # frontend needs it to choose its post-deploy navigation, and a requested view
    # that failed also appends a warning.
    system_view_requested: bool = False


# ============================================================================
# Local Agent Deployment Models
# ============================================================================

class CredentialImportResult(BaseModel):
    """Result of importing a single credential."""
    status: str  # "created", "reused", "renamed"
    name: str
    original: Optional[str] = None  # Original name if renamed


class VersioningInfo(BaseModel):
    """Versioning information for local agent deployment."""
    base_name: str
    previous_version: Optional[str] = None
    previous_version_stopped: bool = False
    new_version: str


class DeployLocalRequest(BaseModel):
    """Request to deploy a local agent."""
    archive: str  # Base64-encoded tar.gz
    name: Optional[str] = None  # Override name from template.yaml
    credentials: Optional[Dict[str, str]] = None  # Optional credentials to inject {KEY: value}


# Maximum credentials allowed per deploy-local request
MAX_DEPLOY_CREDENTIALS = 100


class DeployLocalResponse(BaseModel):
    """Response from local agent deployment."""
    status: str  # "success" or "error"
    agent: Optional[AgentStatus] = None
    versioning: Optional[VersioningInfo] = None
    credentials_imported: Optional[Dict[str, str]] = None  # Files found in archive
    credentials_injected: Optional[int] = None  # Count of credentials injected
    warnings: List[str] = []  # Advisory deploy-time warnings (e.g. MCP credential gaps)
    error: Optional[str] = None
    code: Optional[str] = None  # Error code for machine-readable errors


# ============================================================================
# Credential Injection Models (CRED-002: Simplified Credential System)
# ============================================================================

class CredentialInjectRequest(BaseModel):
    """Request to inject credential files directly into an agent."""
    files: Dict[str, str] = {}       # text files: {".env": "KEY=value\n...", ".mcp.json": "{}"}
    files_b64: Dict[str, str] = {}   # binary files: {path: base64(content)} (#11 — .p12/.pfx/DER)


class CredentialInjectResponse(BaseModel):
    """Response from credential injection."""
    status: str  # "success"
    files_written: List[str]
    message: str


class CredentialExportResponse(BaseModel):
    """Response from exporting credentials to encrypted file."""
    status: str  # "success"
    encrypted_file: str  # Path to .credentials.enc
    files_exported: int


class CredentialImportResponse(BaseModel):
    """Response from importing credentials from encrypted file."""
    status: str  # "success"
    files_imported: List[str]
    message: str


class InternalDecryptInjectRequest(BaseModel):
    """Request for internal decrypt-and-inject (startup.sh)."""
    agent_name: str

# ============================================================================
# Guided Credential Setup (trinity-enterprise#127)
# ============================================================================

class CredentialRequirement(BaseModel):
    """One credential variable an agent needs, with its live status.

    RENDERING CONTRACT — binding, and enforced by component tests rather than by
    this model, because it is about the DOM the fields land in:

    * `title`, `description`, `source`, `default` and `errors[]` are
      author-controlled text that reaches an operator. Interpolate them as TEXT
      only. Do NOT route them through `utils/markdown.js`: for these fields
      markdown is a WIDENING, since it hands the template author an arbitrary
      `[label](url)` surface immediately beside a credential input, which defeats
      the point of having one validated `setup_url`. `v-html` stays banned
      (H-005).
    * The anchor text for `setup_url` MUST be `setup_url_display_host`, never
      `title`. `<a href="https://evil.tld">OpenAI API keys</a>` recreates the
      userinfo attack in pure HTML with no validator in the way — and
      `_setup_url_error` rejects `user@host` precisely to stop label/destination
      divergence.
    * `setup_url_display_host is None` means the host could not be verified:
      render the URL as INERT TEXT, not a link.
    * `secret is True` (the default) MUST mask the input.
    * `format` is an OPEN vocabulary — never map an unrecognised value onto a DOM
      attribute (`type`, `pattern`) without a client-side allowlist.
    """

    name: str
    title: str
    description: Optional[str] = None
    # Tri-state: True | False | "unknown". `"unknown"` means the template author
    # never opted in (a bare `- FOO`), and rendering it as required cries wolf on
    # every legacy template. It never counts toward `summary.blocking`.
    required: Union[bool, str] = "unknown"
    # Fail-safe default: mask until an author says otherwise.
    secret: bool = True
    format: Optional[str] = None
    # A PLACEHOLDER, never a prefilled value, and suppressed entirely by the UI
    # unless `secret is False`. Nothing enforces the schema's "NEVER put a real
    # credential here", so an author — or a prompt-injected agent rewriting its
    # own template.yaml — could set `default: "sk-attacker-controlled"`; prefilling
    # it turns author YAML into a one-click credential write, and `secret: true`
    # would MASK the field, making it less likely the operator reads what they
    # submit. `default` exists for `"./Brain"`-style paths, i.e. the
    # `secret: false` case.
    default: Optional[str] = None
    source: Optional[str] = None
    # True for a name observed in `.mcp.json.template` / `.env.example` but never
    # declared (`state: "declaration_incomplete"`). Advisory rows are never
    # required and never blocking.
    advisory: bool = False
    status: str  # "set" | "missing" | "unknown"
    setup_url: Optional[str] = None
    setup_url_display_host: Optional[str] = None
    setup_url_registrable: Optional[str] = None
    setup_url_verified: bool = False


class CredentialRequirementsSummary(BaseModel):
    """Server-computed counts, so a badge and its headline cannot drift."""

    total: int = 0
    set: int = 0
    missing: int = 0
    unknown: int = 0
    # `required is True AND status == "missing"`. Excludes `required == "unknown"`
    # and every advisory row by construction.
    blocking: int = 0
    # Platform-injected variables are dropped from `requirements` (they are never
    # the operator's to set) but counted here, so the exclusion stays visible.
    platform_injected_excluded: int = 0
    advisory: int = 0


class CredentialRequirementsResponse(BaseModel):
    """Per-agent credential checklist (trinity-enterprise#127)."""

    agent_name: str
    # `degraded` DOMINATES `no_credentials_required` unconditionally — a degraded
    # lookup and a genuinely credential-free agent produce an identical empty
    # requirement set, and "Ready" is the one state nobody investigates.
    state: str  # ok | no_credentials_required | declaration_incomplete | degraded
    requirements_source: str  # live_workspace | catalog | none
    status_source: str  # live | unavailable
    degraded_reason: Optional[str] = None
    requirements: List[CredentialRequirement] = []
    summary: CredentialRequirementsSummary = CredentialRequirementsSummary()
    # The normalizer's sanitized messages: non-printables stripped and
    # length-capped, but NOT HTML-escaped. Text-interpolate only.
    errors: List[str] = []




# ============================================================================
# GitHub PAT Propagation Models (#211)
# ============================================================================

class AgentPropagationStatus(BaseModel):
    """Per-agent result when propagating the global GitHub PAT."""
    agent_name: str
    # "updated", "skipped_per_agent_pat", "skipped_no_pat", "failed"
    status: str
    error: Optional[str] = None
    # #1967: whether the LIVE git remote was re-templated. The `.env` write only
    # takes effect on the next restart, so this is the field that says whether
    # fetch/push work *now*. Optional with a None default: it is genuinely
    # unknown on the pre-skip and failure paths, and "unknown" must stay
    # distinguishable from "attempted and did not happen".
    remote_updated: Optional[bool] = None


class GithubPatPropagationResult(BaseModel):
    """Aggregate result of a GitHub PAT propagation run."""
    total_running: int
    updated: List[str]
    skipped: List[AgentPropagationStatus]
    failed: List[AgentPropagationStatus]
    # #1967: how many of `updated` also got their live remote re-templated.
    # `updated` alone overstates the fix — an agent whose `.env` was rewritten
    # but whose remote was not is still authenticating with the revoked token
    # until it restarts, which is the silent failure this issue reports.
    remotes_updated: int = 0


# =============================================================================
# Outbound File Sharing (FILES-001)
# =============================================================================

class ShareFileRequest(BaseModel):
    """Body for POST /api/internal/agent-files/share (internal, agent-server path)."""
    agent_name: str = Field(..., max_length=128)
    filename: str = Field(..., min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    expires_in: Optional[int] = None
    # NOTE: `one_time` is deferred — the schema retains the columns
    # so we can re-enable it later without a migration.


class ShareFileMcpRequest(BaseModel):
    """Body for POST /api/agents/{agent_name}/shared-files (MCP path).

    The agent_name lives in the URL, so the body only needs the
    per-share parameters.
    """
    filename: str = Field(..., min_length=1, max_length=255)
    display_name: Optional[str] = Field(default=None, max_length=255)
    expires_in: Optional[int] = None
    # Effect-scoped idempotency (#1084): a re-run of the same turn sharing the
    # same file replays the original signed URL instead of minting a new token.
    execution_id: Optional[str] = Field(default=None, max_length=200)
    dedup_label: str = Field(default="", max_length=200)


class ShareFileResponse(BaseModel):
    """Response payload for a successful share."""
    file_id: str
    url: str
    expires_at: str
    size_bytes: int
    mime_type: Optional[str] = None


class SharedFileInfo(BaseModel):
    """One row in the owner's file-sharing panel."""
    file_id: str
    filename: str
    size_bytes: int
    mime_type: Optional[str] = None
    url: str
    created_at: str
    expires_at: str
    download_count: int
    last_downloaded_at: Optional[str] = None


class SharedFilesList(BaseModel):
    """Response for GET /api/agents/{name}/shared-files."""
    agent_name: str
    files: List[SharedFileInfo]
    total_bytes: int
    quota_bytes: int


class ClientRosterEntry(BaseModel):
    """One external channel client in the Sharing-tab roster (#20).

    An outside person (no Trinity account) who has messaged the agent through a
    channel. `identity` is the channel-native handle (Telegram @username or
    numeric id, WhatsApp phone). `display_name`/`verified_email` are null when
    unknown; `last_active` is null for a row that has never recorded activity.
    Channel-extensible: Slack/VoIP slot in without a contract change.
    """
    channel: str
    identity: str
    display_name: Optional[str] = None
    verified_email: Optional[str] = None
    message_count: int = 0
    last_active: Optional[str] = None


class AgentDataImportResponse(BaseModel):
    """Response for POST /api/agents/{name}/data/import (#1169).

    `restored`/`skipped` come straight from the agent-server restore
    primitive (`restore_from_tar`); `skipped` entries fell outside the
    `data/**` allowlist or tripped a path-traversal guard.
    """
    agent_name: str
    restored: List[str]
    skipped: List[str]
    bytes_received: int


class AgentDefaultResourcesUpdate(BaseModel):
    """Body for PUT /api/settings/agent-defaults/resources (RES-001)."""
    cpu: Optional[str] = None
    memory: Optional[str] = None


class AgentDefaultAccessPolicyUpdate(BaseModel):
    """Body for PUT /api/settings/agent-defaults/access-policy (#1129)."""
    require_email: Optional[bool] = None


class SkillsLibraryAutomationUpdate(BaseModel):
    """Body for PUT /api/settings/skills-library (trinity-enterprise#236).

    Partial update — every field optional, an omitted field is left untouched,
    so toggling one flag can never silently reset the interval. The interval's
    range (300–86400) is enforced in the router so an out-of-range value returns
    a descriptive 400 rather than a generic 422.
    """
    auto_sync_enabled: Optional[bool] = None
    auto_sync_interval_seconds: Optional[int] = None
    auto_reinject_enabled: Optional[bool] = None


class MaxParallelTasksCeilingUpdate(BaseModel):
    """Body for PUT /api/settings/max-parallel-tasks-ceiling (#506).

    Range (1–32) is enforced in the router so an out-of-range value returns a
    400 with a descriptive message rather than a generic 422.
    """
    value: int


class ProactiveRateLimitsUpdate(BaseModel):
    """Body for PUT /api/settings/proactive-rate-limits (#1609).

    All optional — only provided caps change. Each is an int per hour; ``0`` =
    unlimited. Range ([0, MAX]) is enforced in the router with a named 422.
    """
    slack_proactive_per_channel: Optional[int] = None
    slack_proactive_per_agent: Optional[int] = None
    telegram_proactive_per_group: Optional[int] = None
    telegram_proactive_per_agent: Optional[int] = None
    proactive_dm_per_recipient: Optional[int] = None


class AgentCapacityUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/capacity (CAPACITY-001, #506)."""
    max_parallel_tasks: int


class BrainOrbSettingsUpdate(BaseModel):
    """Body for PUT /api/settings/brain-orb (trinity-enterprise#85).

    Partial update: only non-None booleans are written. `clear` lists flag
    names ("enabled" / "voice_enabled" / "write_enabled") whose stored
    override should be deleted, reverting that flag to its env/default value
    — without it the BRAIN_ORB_* env var is silently dead once a DB row
    exists. A flag may not appear in both a boolean field and `clear` (400).
    """
    enabled: Optional[bool] = None
    voice_enabled: Optional[bool] = None
    write_enabled: Optional[bool] = None
    clear: Optional[List[str]] = None


class ElevenLabsSettingsUpdate(BaseModel):
    """Body for PUT /api/settings/elevenlabs (trinity-enterprise#117).

    Partial update. ``api_key`` sets the ElevenLabs key (stored AES-256-GCM
    encrypted; never echoed back). ``default_voice_id`` sets the platform default
    voice the agent-level config falls back to. ``clear`` lists which to remove:
    "api_key" (revert to env/unavailable) and/or "default_voice_id". A field may
    not be both set and cleared (400).
    """
    api_key: Optional[str] = None
    default_voice_id: Optional[str] = None
    clear: Optional[List[str]] = None


# Max length for the public/channel custom-instructions fragment (#1205).
PUBLIC_CHANNEL_PROMPT_MAX_LEN = 4000


class PublicChannelPrompt(BaseModel):
    """Per-agent custom instructions for public & channel chats (#1205).

    Response for GET, and the stored value echoed back by PUT. `null`/empty
    means unset — a strict no-op for the agent's behavior.
    """
    public_channel_system_prompt: Optional[str] = None


class PublicChannelPromptUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/public-prompt (#1205)."""
    public_channel_system_prompt: Optional[str] = Field(
        default=None, max_length=PUBLIC_CHANNEL_PROMPT_MAX_LEN
    )


# ---------------------------------------------------------------------------
# Fleet Executions (EXEC-022 / Issue #18)
# ---------------------------------------------------------------------------

class FleetExecutionSummary(BaseModel):
    """Lightweight execution row for the Unified Executions Dashboard list.

    Excludes large fields (response, tool_calls, execution_log).
    error_summary is a 200-char truncation for failed-row one-liners.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    message: str
    triggered_by: str
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    error_summary: Optional[str] = None
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    model_used: Optional[str] = None
    fan_out_id: Optional[str] = None
    business_status: Optional[str] = None
    validation_execution_id: Optional[str] = None
    queued_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        json_encoders = {datetime: lambda v: to_utc_iso(v) if v else None}


class FleetExecutionStats(BaseModel):
    """Aggregate stats for the Unified Executions Dashboard stat cards."""
    total: int
    success_count: int
    failed_count: int
    running_count: int
    queued_count: int
    total_cost: float
    success_rate: float
    hours: int  # 0 = all-time
    # #1743: the slice of the above that belongs to a DELETED agent (soft-deleted
    # or purged). Execution rows outlive their agent deliberately — cost is
    # billing truth and a soft-deleted agent is recoverable — but the per-agent
    # surfaces render only live agents, so these totals would otherwise exceed
    # the sum of the tiles by an amount nothing on screen explains. Defaults keep
    # the field additive for any client that predates it.
    deleted_agent_count: int = 0
    deleted_agent_cost: float = 0.0


class ExecutionTimelineSplit(BaseModel):
    """One trigger's share of a time bucket (ent#96).

    `failed` rides alongside `total` deliberately: a stack drawn from totals
    alone hides failures inside the columns, which is exactly what the tile
    reading this must not do.
    """
    total: int
    failed: int


class ExecutionTimelineBucket(BaseModel):
    """One bucket of `GET /api/executions/timeline` (ent#326)."""
    bucket: str
    total: int
    success: int
    failed: int
    cost: float
    # Context-window occupancy summed over the bucket — NOT "tokens consumed".
    # `schedule_executions` has no token columns (`output_tokens` lives only on
    # `chat_messages`, which covers chat turns rather than fleet executions), so
    # the field is named for the quantity it actually holds. A tile labelling
    # this as tokens would be exactly the mislabel ent#326 warns against.
    context_used: int
    # ent#96: present only for `split=trigger`, and then on EVERY bucket —
    # a gap-filled empty hour carries `{}`, so a chart never has to tell
    # "no runs" apart from "field missing".
    by_trigger: Optional[Dict[str, ExecutionTimelineSplit]] = None


class ExecutionTimeline(BaseModel):
    """Bucketed fleet execution rollups for the grid's data tiles (ent#326).

    The time-series sibling of `FleetExecutionStats`: same table, same access
    model, buckets instead of scalars. `hour`/`day` series are gap-filled so a
    chart renders a real zero rather than skipping the interval (#1107).
    """
    group_by: str
    hours: int
    gap_filled: bool
    buckets: List[ExecutionTimelineBucket]
    # ent#96: echoed back so a caller can tell a split response from a plain
    # one without inspecting the buckets.
    split: Optional[str] = None
    # The stack/legend order for `by_trigger`, served by the backend so the
    # tile, its legend and the #1107 Overview chart cannot order the same
    # buckets differently. Includes every catalog bucket, not only the ones
    # present in this window, so the colours a reader learns stay put as
    # traffic comes and goes.
    trigger_order: Optional[List[str]] = None


class EvaluationCreate(BaseModel):
    """Body for a manual/admin evaluation write (ent#206). The graded agent is
    the path `{name}`; the caller is fenced human-admin-only at the router, so
    a graded agent can never write its own grade."""
    execution_id: Optional[str] = None
    archetype: Optional[str] = Field(default=None, max_length=40)
    completion: Optional[bool] = None
    quality: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    checks: Optional[dict] = None
    judge: Optional[dict] = None


class EvaluationResponse(BaseModel):
    id: str
    agent_name: str
    execution_id: Optional[str] = None
    archetype: Optional[str] = None
    completion: Optional[bool] = None
    quality: Optional[float] = None
    checks: Optional[Any] = None
    judge: Optional[Any] = None
    evaluator: str
    created_at: str
    # ent#366 — a one-click Workspace rating carries what was rated and, for a
    # negative one, the person's words. `comment_withheld` says the text exists
    # but was not shown to THIS caller (the rated agent), so a reader can tell
    # "no comment" from "not yours to read".
    target_kind: Optional[str] = None
    target_id: Optional[str] = None
    comment: Optional[str] = None
    comment_withheld: bool = False
    updated_at: Optional[str] = None


class CircuitBreakerConfigUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/circuit-breaker (RELIABILITY-007, #526).

    Per-agent opt-in for the dispatch breaker. Gated again by the global
    DISPATCH_BREAKER_ENABLED master switch — both must be on to engage.
    """
    enabled: bool


class OperatorResumeUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/operator-resume (ent#329).

    Per-agent opt-in: when enabled, answering one of this agent's parked
    operator-queue items dispatches a turn so the agent acts on the answer
    instead of waiting for a next tick it may never have.

    Per-agent rather than per-item deliberately — a dispatch spends money, and an
    agent-declared per-item flag would let the agent make answering costly for
    whoever answers, including an external Workspace client (ent#430 AC #3).
    """
    enabled: bool


class McpExposedUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/mcp-exposed (#846).

    Per-agent opt-in. When enabled, the Trinity MCP server dynamically registers
    a dedicated ``chat_with_<slug>`` tool for the agent. Execution still runs the
    same access gate — this only publishes a surface.
    """
    enabled: bool


# ---------------------------------------------------------------------------
# Per-agent MCP connector (ent#46; OSS-core since #118)
# ---------------------------------------------------------------------------

class ConnectorConfigUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/connector.

    ``exposed_playbooks=None`` leaves the allow-list unchanged; an explicit list
    sets it; ``expose_all_playbooks=True`` resets it to "all user_invocable".
    """
    enabled: Optional[bool] = None
    exposed_playbooks: Optional[List[str]] = None
    expose_all_playbooks: Optional[bool] = None


class ConnectorClientSnippet(BaseModel):
    """A copy-paste-ready connector config for one AI client."""
    client: str
    label: str
    format: str            # 'shell' | 'json'
    content: str           # the literal block to copy (key pre-embedded)
    note: Optional[str] = None


class ConnectorPlaybook(BaseModel):
    """A playbook exposed by the connector as an MCP tool."""
    name: str
    description: Optional[str] = None
    argument_hint: Optional[str] = None
    automation: Optional[str] = None


class ConnectorStatus(BaseModel):
    """Response for GET .../connector (owner view)."""
    agent_name: str
    enabled: bool = False
    exposed_playbooks: Optional[List[str]] = None
    has_key: bool = False
    key_prefix: Optional[str] = None
    mcp_url: Optional[str] = None
    snippets: List[ConnectorClientSnippet] = Field(default_factory=list)
    # #848 inline email auth. When the platform flag is on, an owner can share
    # the agent WITHOUT minting a key: the collaborator connects keyless and
    # signs in by email. `inline_auth_available` mirrors the flag;
    # `keyless_snippets` is the no-key config (empty when the flag is off).
    inline_auth_available: bool = False
    keyless_snippets: List[ConnectorClientSnippet] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConnectorKeySecret(BaseModel):
    """Response when minting/regenerating the key — secret returned once."""
    agent_name: str
    api_key: str
    key_prefix: str
    mcp_url: Optional[str] = None
    snippets: List[ConnectorClientSnippet] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MCP inline email auth (#848) — /api/internal/mcp-auth/*
#
# The MCP server calls these with X-Internal-Secret on behalf of a keyless
# session. The internal secret authenticates the CALLER, never the action: every
# body carries the asserted ``email`` and the backend re-gates each call on that
# email's own access. Nothing here ever returns a credential.
# ---------------------------------------------------------------------------

class McpInlineLoginRequest(BaseModel):
    """Body for POST /api/internal/mcp-auth/request."""
    email: str = Field(..., min_length=3, max_length=254)
    session_id: Optional[str] = Field(default=None, max_length=200)


class McpInlineLoginVerify(BaseModel):
    """Body for POST /api/internal/mcp-auth/verify."""
    email: str = Field(..., min_length=3, max_length=254)
    code: str = Field(..., min_length=1, max_length=32)
    session_id: Optional[str] = Field(default=None, max_length=200)


class McpInlineAgent(BaseModel):
    """One agent a verified email may reach through the connector surface."""
    name: str
    description: Optional[str] = None


class McpInlineVerifyResponse(BaseModel):
    """Response for a SUCCESSFUL verify. Carries no credential of any kind —
    the MCP session binding is the credential and it lives only in the MCP
    server's memory (§7.6: session, not a minted key)."""
    verified: bool = True
    username: Optional[str] = None
    agents: List[McpInlineAgent] = Field(default_factory=list)


class McpInlinePlaybooksRequest(BaseModel):
    """Body for POST /api/internal/mcp-auth/playbooks."""
    email: str = Field(..., min_length=3, max_length=254)
    agent: str = Field(..., min_length=1, max_length=100)


class McpInlineChatRequest(BaseModel):
    """Body for POST /api/internal/mcp-auth/chat."""
    email: str = Field(..., min_length=3, max_length=254)
    agent: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=100000)
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


class McpInlineChatResponse(BaseModel):
    """Response for POST /api/internal/mcp-auth/chat."""
    agent: str
    response: str
    execution_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Agent MCP key — detection, self-heal & rotation (#1854)
# ---------------------------------------------------------------------------

class AgentMcpKeyStatus(BaseModel):
    """Response for GET /api/agents/{name}/mcp-key (owner view).

    Metadata only — the agent-key plaintext is unrecoverable by design (only its
    SHA-256 hash is stored) and has never been exposed over HTTP.
    """
    agent_name: str
    exists: bool = False
    key_id: Optional[str] = None
    key_prefix: Optional[str] = None
    scope: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    usage_count: int = 0
    # missing | env_absent | env_mismatch | never_used | stale | active | exempt
    health: str = "missing"
    health_detail: Optional[str] = None
    rotatable: bool = True


class AgentMcpKeyVerifyEntry(BaseModel):
    """One `.mcp.json` server entry as the CONTAINER reports it.

    Carries no secret: the bearer token is hashed inside the container and only
    the resolved key's public metadata (scope / prefix / bound agent) appears
    here.
    """
    server_name: str
    # ok | foreign_user_key | foreign_ops_key | foreign_agent_key | unknown_key
    verdict: str
    key_scope: Optional[str] = None
    key_prefix: Optional[str] = None
    key_agent_name: Optional[str] = None


class AgentMcpKeyVerifyResult(BaseModel):
    """Response for POST /api/agents/{name}/mcp-key/verify — container truth."""
    agent_name: str
    # ok | foreign_user_key | foreign_ops_key | foreign_agent_key | unknown_key | not_configured
    # | shadow_entry | unavailable
    verdict: str
    message: Optional[str] = None
    entries: List[AgentMcpKeyVerifyEntry] = Field(default_factory=list)


class AgentMcpKeyRegenerateResult(BaseModel):
    """Response for POST /api/agents/{name}/mcp-key/regenerate.

    Deliberately carries **no plaintext**. Nobody outside the container has any
    use for an agent key — it is minted, baked into ``Config.Env``, and read by
    the agent — so returning it would add a credential-exfiltration primitive on
    an owner-reachable route and buy nothing.
    """
    agent_name: str
    key_id: str
    key_prefix: str
    delivery: str            # recreated | db_only
    superseded_deleted: int = 0
    children_repointed: int = 0
    message: Optional[str] = None


class VoiceRepliesUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/voice-replies (epic #24 / #25; v2 ent#117).

    Partial update. Agent-level fields (from the Settings surface): ``enabled`` +
    ``voice_id`` — when both are present they set the agent-level capability
    (voice id is an ElevenLabs voice id; may be omitted to fall back to the platform
    default). ``channels`` (from the channel panels) is a partial map of
    ``{telegram|slack|whatsapp: bool}`` per-channel voice-allowed flags. At least
    one of ``enabled`` / ``channels`` should be provided.
    """
    enabled: Optional[bool] = None
    voice_id: Optional[str] = None
    channels: Optional[Dict[str, bool]] = None

    @field_validator("voice_id")
    @classmethod
    def _strip_voice_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, v: Optional[Dict[str, bool]]) -> Optional[Dict[str, bool]]:
        if v is None:
            return None
        allowed = {"telegram", "slack", "whatsapp"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown channel(s): {', '.join(sorted(unknown))}")
        return v


class VoiceReplyRequest(BaseModel):
    """Body for POST /api/agents/{name}/voice-reply (send_voice_reply MCP tool, ent#117).

    ``text`` is spoken as a voice note on the channel the ``execution_id`` came from.
    ``dedup_label`` lets an agent intentionally send two voice notes in one turn.
    """
    text: str = Field(min_length=1, max_length=4096)
    execution_id: str = Field(min_length=1)
    dedup_label: str = ""


class PublicChannelModelUpdate(BaseModel):
    """Body for PUT /api/agents/{name}/public-channel-model (#894).

    ``model`` is the Claude model id to use for public-facing channels (public
    link, Slack/Telegram/WhatsApp, x402). ``None`` or empty string clears the
    override so the agent inherits the platform default.
    """
    model: Optional[str] = None


class ExecutionResultEnvelope(BaseModel):
    """Body for POST /api/agents/{name}/executions/{id}/result (#1083).

    The typed terminal an agent POSTs back after a fire-and-forget turn. The
    backend does NOT re-classify — ``status``/``error_code`` are authoritative
    and flow straight into ``TaskExecutionService.apply_result``. ``metadata``
    carries the same shape the synchronous ``/api/task`` response does (cost_usd,
    context_window, token counts, compact_events, session_id).

    ``status`` is free-form ``str`` (not an enum) for forward-compatibility: the
    backend maps ``success``→SUCCESS, ``cancelled``→CANCELLED (#679), and every
    other value (incl. an unknown future status) →FAILED.

    Field caps bound abuse from a buggy/compromised agent while staying well
    above a legitimate large transcript (the sync path already accepts these):
    enforced in the router after parse so the failure is a clean 413, not a
    Pydantic 422 (the agent's retry logic special-cases status codes).
    """
    status: str = Field(..., description="'success', 'failed', or 'cancelled'")
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    terminal_reason: Optional[str] = None  # completed|max_duration|stall_no_output|auth|empty_result|cancelled
    metadata: Optional[Dict] = None
    execution_log: Optional[List] = None
    session_id: Optional[str] = None
    execution_time_ms: Optional[int] = None


# =============================================================================
# Pull / work-stealing coordination (#1081 Phase 1 — DARK)
# =============================================================================

# The pinned typed terminal-reason taxonomy (MESSAGE_ENVELOPE_SCHEMA.md §4):
# the TaskExecutionErrorCode code-enum values plus the two contract additions
# (OOM, MAX_TURNS). Lower-case, matching the code enum's string values.
_PULL_ERROR_CODES = frozenset({
    "timeout", "capacity", "auth", "billing", "agent_error", "network",
    "circuit_open", "reconciled", "lease_expired", "oom", "max_turns",
})
# reply.status value set (MESSAGE_ENVELOPE_SCHEMA §2.4/§4; `cancelled` per the
# live #1083 3-way map — OPEN-1).
_PULL_RESULT_STATUSES = frozenset({"success", "failed", "cancelled"})


class PullTaskResultRequest(BaseModel):
    """Body for ``POST /api/internal/tasks/{id}/result`` (#1081 Phase 1, DARK).

    The ``reply`` payload per ``MESSAGE_ENVELOPE_SCHEMA.md`` §3.3 (which cites
    §2.4) plus the ``claim_token`` the worker received from the §3.1 claim
    response. The token is validated INSIDE the atomic CAS terminal write
    (``db/schedules.py`` ``update_execution_status(claim_token=…)``) — a
    stale / duplicate / wrong-token POST can never clobber a terminal row
    (#1082 status-as-projection). ``status`` + ``error_code`` are the pinned
    typed taxonomy (§4); unknown values are rejected at the boundary (422).
    """
    claim_token: str = Field(..., min_length=1, description="Token from the §3.1 claim response")
    status: str = Field(..., description="'success' | 'failed' | 'cancelled' (§2.4/§4)")
    content: Optional[str] = Field(None, description="Result text (§2.4 reply.content)")
    error_code: Optional[str] = Field(None, description="Typed failure class (§4); required on failed")
    cost: Optional[float] = None
    tokens: Optional[int] = None
    session_id: Optional[str] = None
    execution_log: Optional[List] = None
    metadata: Optional[Dict] = None

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in _PULL_RESULT_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_PULL_RESULT_STATUSES)} "
                "(MESSAGE_ENVELOPE_SCHEMA §2.4/§4)"
            )
        return v

    @field_validator("error_code")
    @classmethod
    def _check_error_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        norm = v.strip().lower()
        if norm not in _PULL_ERROR_CODES:
            raise ValueError(
                f"error_code must be one of {sorted(_PULL_ERROR_CODES)} "
                "(MESSAGE_ENVELOPE_SCHEMA §4)"
            )
        return norm


# =============================================================================
# Soft-Delete Admin Recovery (#834 Phase 1c)
# =============================================================================

class SoftDeletedAgent(BaseModel):
    """Response item for GET /api/admin/soft-deleted/agents."""
    agent_name: str
    owner_id: int
    created_at: str
    deleted_at: str
    # When the retention sweep would hard-purge this row (None when
    # the retention setting is 0 = disabled).
    purge_eta: Optional[str]


class SoftDeletedSchedule(BaseModel):
    """Response item for GET /api/admin/soft-deleted/schedules."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    owner_id: int
    enabled: bool
    deleted_at: str
    purge_eta: Optional[str]


# =============================================================================
# Schedule Analytics (#868)
# =============================================================================
#
# Per-schedule distributions over `schedule_executions`. Per-agent rollup
# and per-chat-session analytics deferred to #18 and a follow-up issue
# respectively — see #868 issue body "Out of Scope" section for the
# decision context.


class DurationPercentiles(BaseModel):
    """Duration percentiles in milliseconds. All null when the schedule
    has fewer than 1 successful execution in the window."""
    p50: Optional[int] = None
    p95: Optional[int] = None
    p99: Optional[int] = None


class CostTotals(BaseModel):
    """Cost totals in USD for the analytics window."""
    total: float = 0.0


class ToolCallEntry(BaseModel):
    """One row of the top-N tool-call distribution."""
    name: str
    total_duration_ms: int


class ToolCallSummary(BaseModel):
    """Tool-call distribution weighted by total wall time per tool.

    Top-N is intentionally weighted by `sum(duration_ms)` rather than
    raw count — raw count is dominated by `Read` / `Bash` on every
    agent and has low signal-to-noise. Locked by /autoplan strategy
    finding #6.
    """
    top: List[ToolCallEntry] = []
    total_calls: int = 0


class TimelineEntry(BaseModel):
    """One UTC-day bucket on the analytics timeline. Zero-filled for
    days that had no executions (Python-side gap fill) so chart
    libraries render a continuous x-axis."""
    date: str
    success: int
    failed: int
    cost: float


class ScheduleAnalyticsResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/schedules/{schedule_id}/analytics.

    `sampled` reports whether the percentile / tool-call pool was
    capped (currently 5000 newest success rows). Counts and timeline
    are always unsampled. UTC day boundaries.
    """
    window_hours: int
    total_executions: int
    success_count: int
    failed_count: int
    cancelled_count: int
    success_rate: float
    duration_ms: DurationPercentiles
    cost: CostTotals
    tool_calls: ToolCallSummary
    timeline: List[TimelineEntry]
    sampled: bool = False
    sample_size: int = 0


# ---------------------------------------------------------------------------
# Agent-scoped Overview analytics (#1107) — generalises the #868 per-schedule
# analytics to agent scope with a `triggered_by` type breakdown. Backs the
# Agent Detail "Overview" trend charts.
# ---------------------------------------------------------------------------


class DurationStats(BaseModel):
    """Overall duration stats for the window (milliseconds). `avg` is the
    SQL mean over the *full* success rowset; `p95` is computed over the
    newest capped pool. Both null when the agent has no successful runs
    with a duration in the window."""
    avg: Optional[int] = None
    p95: Optional[int] = None


class AgentTypeTotal(BaseModel):
    """Per-bucket execution total for the window. `bucket` is a user-facing
    grouping of the raw `triggered_by` values (Chat/Tasks, MCP, Channels,
    Public, Scheduled, Agent-to-agent, Voice, Other)."""
    bucket: str
    total: int


class AgentAnalyticsTimelinePoint(BaseModel):
    """One UTC-day bucket for the Overview charts. `success_rate`,
    `duration_avg_ms`, and `context_avg` are null on days with no
    qualifying rows so the chart renders a gap rather than a false zero.
    `by_type` maps present buckets → that day's count (drives the stacked
    bars)."""
    date: str
    total: int
    success: int
    failed: int
    success_rate: Optional[float] = None
    duration_avg_ms: Optional[int] = None
    context_avg: Optional[int] = None
    by_type: Dict[str, int] = {}


class AgentAnalyticsResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/analytics (#1107).

    Deterministic, DB-sourced agent activity over a rolling window.
    `by_type` groups raw `triggered_by` into user-facing buckets (with an
    "Other" catch-all so a new trigger type never silently vanishes);
    `buckets` is the ordered legend / stack order for the chart.
    `success_rate` is terminal-based (success / (success + failed)).
    `sampled` reports whether the p95 pool was capped — `avg` is always
    full-set, never sampled. UTC day boundaries.
    """
    window_hours: int
    total_executions: int
    success_count: int
    failed_count: int
    success_rate: float
    duration_ms: DurationStats
    context_avg: Optional[int] = None
    by_type: List[AgentTypeTotal] = []
    buckets: List[str] = []
    timeline: List[AgentAnalyticsTimelinePoint] = []
    sampled: bool = False
    sample_size: int = 0


class ScheduleSummaryRow(BaseModel):
    """One per-schedule performance rollup (#1115).

    `success_rate` is terminal-based (success / (success + failed [incl.
    `error`])) and `None` when there were zero terminal runs in the window —
    the UI renders `—`, not a false 0%. `avg_duration_ms` / `context_avg` are
    `None` when nothing measurable ran. A zero-run schedule still appears
    (all counts 0, rates `None`).
    """
    schedule_id: str
    name: str
    command: str = ""
    cron_expression: str
    enabled: bool
    total_executions: int
    success_count: int
    failed_count: int
    cancelled_count: int
    success_rate: Optional[float] = None
    avg_duration_ms: Optional[int] = None
    cost_total: float
    context_avg: Optional[int] = None
    tool_call_total: int
    last_run_at: Optional[str] = None
    last_run_status: Optional[str] = None


class AgentSchedulesSummaryResponse(BaseModel):
    """Response envelope for GET /api/agents/{name}/schedules/analytics-summary (#1115).

    One compact rollup row per non-deleted schedule for the window — consumed
    by BOTH the Overview "Schedules performance" section and the Schedules-tab
    inline stats from a single call (no N per-schedule round-trips).
    `tool_calls_sampled` flags when the agent-wide tool-call parse pool was
    capped. UTC window via `iso_cutoff`.
    """
    window_hours: int
    schedule_count: int
    tool_calls_sampled: bool = False
    schedules: List[ScheduleSummaryRow] = []


# =============================================================================
# Agent Compatibility Validation (#668)
# =============================================================================

class CompatibilityCheck(BaseModel):
    """Result of a single compatibility check (one row from the spec catalog).

    `status` is the check outcome: "pass" (compliant), "fail" (issue found), or
    "skipped" (not evaluated — e.g. AI checks with no API key, or a check that
    doesn't apply to this agent's runtime). `severity` is the catalog severity
    (hard | soft | info); AI-evaluated checks are capped at SOFT since their
    verdict is non-deterministic (HARD is reserved for deterministic STATIC
    checks). `detail` carries safe, redacted specifics (line numbers, patterns)
    — never a secret value.
    """
    check_id: str  # "F-001", "S-003", "C-002", ...
    category: str  # human-readable category name
    severity: str  # "hard" | "soft" | "info"
    type: str  # "static" | "ai"
    status: str  # "pass" | "fail" | "skipped"
    message: str
    auto_fixable: bool = False
    explanation: Optional[str] = None  # AI rationale / extra context (markdown)
    confidence: Optional[float] = None  # AI confidence 0..1 (None for STATIC)
    detail: Optional[Dict] = None  # redacted specifics (location, pattern)
    skip_reason: Optional[str] = None  # why a check was skipped


class CompatibilityReport(BaseModel):
    """Aggregate compatibility report for one agent (#668).

    `overall_status`: "compatible" (no hard/soft failures), "issues" (≥1
    hard/soft failure), or "unavailable" (couldn't read the workspace — e.g.
    agent stopped, collector failure). `container_running` distinguishes the
    degraded-stopped case from a genuine clean result. `ai_ran_at` is the
    timestamp of the last AI evaluation (None if never run) so the UI can show
    staleness and a re-run affordance.
    """
    agent_name: str
    container_running: bool
    overall_status: str  # "compatible" | "issues" | "unavailable"
    runtime: Optional[str] = None  # agent runtime (claude | gemini | codex)
    checks: List[CompatibilityCheck] = []
    hard_count: int = 0
    soft_count: int = 0
    info_count: int = 0
    ai_ran_at: Optional[str] = None
    static_ran_at: Optional[str] = None
    message: Optional[str] = None  # human note for the unavailable case


class CompatibilityFixRequest(BaseModel):
    """Request to auto-fix a single correctable compatibility check (#668)."""
    check_id: str


class CompatibilityFixResponse(BaseModel):
    """Result of an auto-fix attempt (#668).

    `uncommitted` is always true on success: the fix edits the in-container
    `.gitignore` only — committing/pushing is the agent's own git-sync job, so
    the change is not yet on GitHub until the next sync.
    """
    check_id: str
    fixed: bool
    message: str
    uncommitted: bool = True


# =============================================================================
# Router-relocated request/response models (#654, INV-14)
# Each section below was moved verbatim from its router so Pydantic models
# live in one place (Architectural Invariant #14). One exception remains in
# routers/canary.py (RunCycleRequest) — see test_models_centralized.py.
# =============================================================================


# =============================================================================
# Agent Files Models (routers/agent_files.py)
# =============================================================================


class FileUpdateRequest(BaseModel):
    """Request body for file updates."""
    content: str


class CreateFolderRequest(BaseModel):
    """Request body for folder creation."""
    path: str


# =============================================================================
# Agent Rename Models (routers/agent_rename.py)
# =============================================================================


class RenameAgentRequest(BaseModel):
    """Request body for agent rename."""
    new_name: str


# =============================================================================
# Agent Ssh Models (routers/agent_ssh.py)
# =============================================================================


class SshAccessRequest(BaseModel):
    """Request body for SSH access (key-based only; #1615 removed password auth)."""
    ttl_hours: float = 4.0
    auth_method: str = "key"  # only "key" is supported (password auth removed, #1615)
    public_key: Optional[str] = None  # Required — client-supplied OpenSSH public key


# =============================================================================
# Agents Models (routers/agents.py)
# =============================================================================


class HeartbeatPayload(BaseModel):
    """Lightweight liveness payload POSTed by the agent every ~5s."""
    memory_mb: Optional[float] = None
    active_executions: Optional[int] = None
    uptime_s: Optional[float] = None


# =============================================================================
# Audit Log Models (routers/audit_log.py)
# =============================================================================


class AuditLogEntry(BaseModel):
    """Single audit log row as returned to API clients."""

    id: int
    event_id: str
    event_type: str
    event_action: str
    actor_type: str
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    actor_ip: Optional[str] = None
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    mcp_scope: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: str
    details: Optional[dict] = None
    request_id: Optional[str] = None
    source: str
    endpoint: Optional[str] = None
    previous_hash: Optional[str] = None
    entry_hash: Optional[str] = None
    created_at: Optional[str] = None


class AuditLogListResponse(BaseModel):
    """Paginated list response."""

    entries: List[AuditLogEntry]
    total: int
    limit: int
    offset: int


class AuditLogStatsResponse(BaseModel):
    """Aggregate counts."""

    total: int
    by_event_type: dict = Field(default_factory=dict)
    by_actor_type: dict = Field(default_factory=dict)


class AuditHeatmapCell(BaseModel):
    """Single populated bucket in the 7×24 dow×hour heatmap."""

    dow: int = Field(..., ge=0, le=6, description="Weekday (0=Sunday)")
    hour: int = Field(..., ge=0, le=23, description="Hour 0–23 UTC")
    count: int = Field(..., ge=0)


class AuditHeatmapResponse(BaseModel):
    """Sparse 7×24 dow×hour heatmap. Zero-count cells omitted."""

    cells: List[AuditHeatmapCell]
    total: int
    max_count: int


class AuditCalendarDay(BaseModel):
    """Single populated day in the calendar heatmap."""

    date: str = Field(..., description="UTC date, ISO 'YYYY-MM-DD'")
    count: int = Field(..., ge=0)


class AuditCalendarResponse(BaseModel):
    """Sparse per-day calendar heatmap (GitHub-style). Quiet days omitted."""

    days: List[AuditCalendarDay]
    total: int
    max_count: int


class AuditVerifyResponse(BaseModel):
    """Hash chain verification result."""

    # TRI-STATE (#1984). True = verified intact, False = mismatch/tampering,
    # None = UNVERIFIABLE (nothing in the range carried a hash). It was a plain
    # `bool`, so "no integrity data exists" was indistinguishable from
    # "verified" — and that is the default state of every install which never
    # enabled hashing.
    valid: Optional[bool] = None
    # verified | verified_partial | tampered | unverifiable | empty_range
    status: str = "unverifiable"
    checked: int = 0
    # Entries skipped for carrying no hash. Non-zero alongside
    # `verified_partial` marks the permanent unhashed prefix of a chain that
    # was enabled midway.
    skipped_unhashed: int = 0
    total_in_range: int = 0
    hash_chain_enabled: bool = False
    first_invalid_id: Optional[int] = None


# =============================================================================
# Avatar Models (routers/avatar.py)
# =============================================================================


class AvatarGenerateRequest(BaseModel):
    identity_prompt: str


# =============================================================================
# Canary Models (routers/canary.py)
# =============================================================================


class CanaryViolation(BaseModel):
    """Single canary_violations row as returned to API clients."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict = Field(default_factory=dict)
    signal_query: Optional[str] = None
    created_at: Optional[str] = None


class CanaryViolationListResponse(BaseModel):
    """Paginated list response."""

    violations: List[CanaryViolation]
    total: int
    limit: int
    offset: int


class CanaryStatsResponse(BaseModel):
    """Aggregate violation counts for dashboard tiles."""

    total: int
    by_invariant: dict = Field(default_factory=dict)
    by_severity: dict = Field(default_factory=dict)


class CycleViolation(BaseModel):
    """One violation persisted during a run-cycle call."""

    id: int
    invariant_id: str
    tier: str
    severity: str
    snapshot_time: str
    observed_state: dict
    signal_query: Optional[str] = None


class CycleTransition(BaseModel):
    """A green→red transition this cycle **delivered an alert for**.

    `CanaryService` delivered exactly one Slack webhook message per entry,
    mapping severity to the message styling. Surfaced here so the run-cycle
    response mirrors what the service actually sent.

    Since #1897 an entry means *notified*, not merely *detected*: a
    transition whose webhook POST was rejected appears in
    `RunCycleResponse.undelivered_invariant_ids` instead and is retried on
    a later cycle while the invariant stays red. Conversely an entry here
    may be a retry that finally landed, whose flip was detected on an
    earlier cycle.
    """

    invariant_id: str
    severity: str
    violations_in_cycle: int
    previous_violation_at: Optional[str] = Field(
        None,
        description=(
            "snapshot_time of the most recent prior violation for this "
            "invariant; null if the invariant has never violated before."
        ),
    )


class RunCycleResponse(BaseModel):
    """Result of one canary cycle."""

    snapshot_time: str
    cycle_duration_ms: int
    # Invariants this cycle attempted (= the request's `invariants` filter,
    # or all registered ids if unfiltered). Whether each one *fired* is
    # surfaced via `violations` and `transitions`. Sources that were down
    # this cycle are listed in `sources_unavailable` — invariants that
    # depend on them returned no violations regardless of state.
    checks_run: List[str]
    sources_unavailable: List[str]
    violations: List[CycleViolation]
    transitions: List[CycleTransition]
    undelivered_invariant_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Invariants this cycle tried to alert on and could not deliver — "
            "a rejected webhook, a raised emit, or a retry held off by the "
            "per-interval floor (#1897). Disjoint from `transitions`, which "
            "since #1897 lists only what was actually sent; without this "
            "field a webhook outage would render as zero transitions, which "
            "is indistinguishable from a green cycle. Each entry is retried "
            "on a later cycle while its invariant stays red."
        ),
    )


class CanaryStatusResponse(BaseModel):
    """Run-state of the canary harness (#2217) — `GET /api/canary/status`.

    Answers the "is the harness actually running" question that a disabled
    canary (zero violations, byte-identical to a clean fleet) otherwise hides.
    Plain fields only — no canary-lib symbols — so this lives in models.py
    rather than following the `RunCycleRequest` in-router exception (Inv #14).
    """

    enabled: bool = Field(
        ...,
        description="CANARY_ENABLED == '1'. Answers the disabled case; "
        "default-OFF is the normal state for most installs.",
    )
    status: str = Field(
        ...,
        description=(
            "Derived liveness: 'disabled' (never an alarm, Redis never read), "
            "'unknown' (enabled but no readable cursor — fail-open, never an "
            "alarm), 'stale' (enabled, cursor older than stale_after_seconds — "
            "the incident case), or 'healthy'."
        ),
    )
    last_cycle_at: Optional[str] = Field(
        None,
        description=(
            "ISO-Z from the shared canary:last_cycle_at cursor — the "
            "collection-START instant of the last cycle the leader completed "
            "(lags real completion by up to one cycle's duration). null = no "
            "cycle recorded yet / cursor unreadable / disabled."
        ),
    )
    seconds_since_last_cycle: Optional[int] = Field(
        None,
        description="Age of last_cycle_at, clamped max(0, int(age)) so clock "
        "skew never yields a negative int; null when unknown/disabled.",
    )
    interval_seconds: int = Field(
        ..., description="Scheduled cycle interval (CANARY_INTERVAL_SECONDS, 300 default)."
    )
    stale_after_seconds: int = Field(
        ...,
        description=(
            "The staleness bound: _max_failover_seconds + _MAX_CYCLE_LEASE_SECONDS "
            "(≈1680s at defaults) — provably above both the leader-failover window "
            "and a maxed-out-but-healthy cycle. Explicit + testable."
        ),
    )
    alert_sink_configured: bool = Field(
        ...,
        description=(
            "Whether CANARY_SLACK_WEBHOOK_URL is set. A cycling canary with no "
            "sink persists violations but pushes nothing — surfaced separately "
            "from `status` (liveness and can-it-alert are orthogonal facts)."
        ),
    )
    redis_available: Optional[bool] = Field(
        None,
        description="Whether the cursor read succeeded (distinguishes Redis "
        "down from just-booted); null when not consulted (disabled).",
    )


# =============================================================================
# Event Subscriptions Models (routers/event_subscriptions.py)
# =============================================================================


class EmitEventRequest(BaseModel):
    """Request body for emitting an event."""
    event_type: str  # Namespaced event type (e.g., "prediction.resolved")
    payload: Optional[dict] = None  # Structured data


# =============================================================================
# Fan Out Models (routers/fan_out.py)
# =============================================================================


TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


MAX_TASKS = 50


MAX_CONCURRENCY = 10


class FanOutTask(BaseModel):
    """A single task in a fan-out request."""
    id: str
    message: str = Field(..., min_length=1, max_length=100_000)

    @field_validator("id")
    @classmethod
    def validate_task_id(cls, v: str) -> str:
        if not TASK_ID_RE.match(v):
            raise ValueError(
                f"Task ID must be 1-64 alphanumeric characters, hyphens, or underscores: '{v}'"
            )
        return v


class FanOutRequest(BaseModel):
    """Request model for fan-out parallel task execution."""
    tasks: List[FanOutTask]
    agent: str = "self"
    # Optional overall fan-out deadline. When None, no outer deadline is
    # applied — each sub-task is still bounded by the target agent's
    # configured execution_timeout_seconds (TIMEOUT-001).
    timeout_seconds: Optional[int] = None
    max_concurrency: int = 3
    policy: str = "best-effort"
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("tasks")
    @classmethod
    def validate_tasks(cls, v: List[FanOutTask]) -> List[FanOutTask]:
        if len(v) == 0:
            raise ValueError("At least one task is required")
        if len(v) > MAX_TASKS:
            raise ValueError(f"Maximum {MAX_TASKS} tasks per fan-out")
        # Check for duplicate IDs
        ids = [t.id for t in v]
        if len(ids) != len(set(ids)):
            dupes = [i for i in ids if ids.count(i) > 1]
            raise ValueError(f"Duplicate task IDs: {set(dupes)}")
        return v

    @field_validator("max_concurrency")
    @classmethod
    def validate_concurrency(cls, v: int) -> int:
        if v < 1 or v > MAX_CONCURRENCY:
            raise ValueError(f"max_concurrency must be between 1 and {MAX_CONCURRENCY}")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, v: Optional[int]) -> Optional[int]:
        if v is None:
            return v
        if v < 10 or v > 3600:
            raise ValueError("timeout_seconds must be between 10 and 3600")
        return v

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, v: str) -> str:
        if v != "best-effort":
            raise ValueError("Only 'best-effort' policy is supported")
        return v


class FanOutTaskResponse(BaseModel):
    """Result of a single fan-out subtask."""
    id: str
    status: str
    response: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    execution_id: Optional[str] = None
    cost: Optional[float] = None
    context_used: Optional[int] = None
    duration_ms: Optional[int] = None


class FanOutResponse(BaseModel):
    """Aggregated fan-out result."""
    fan_out_id: str
    status: str
    total: int
    completed: int
    failed: int
    results: List[FanOutTaskResponse]


# =============================================================================
# Git Models (routers/git.py)
# =============================================================================


class GitSyncRequest(BaseModel):
    """Request body for git sync operation."""
    message: Optional[str] = None  # Custom commit message
    paths: Optional[List[str]] = None  # Specific paths to sync
    strategy: Optional[str] = "normal"  # "normal", "pull_first", "force_push"


class GitPullRequest(BaseModel):
    """Request body for git pull operation."""
    strategy: Optional[str] = "clean"  # "clean", "stash_reapply", "force_reset"


class GitInitializeRequest(BaseModel):
    """Request body for git initialization."""
    repo_owner: str  # GitHub username or organization
    repo_name: str  # Repository name
    create_repo: bool = True  # Whether to create the repository if it doesn't exist
    private: bool = True  # Whether the new repository should be private
    description: Optional[str] = None  # Repository description


class GitHubPATRequest(BaseModel):
    """Request body for setting agent GitHub PAT."""
    pat: str


class AutoSyncToggle(BaseModel):
    enabled: bool


class FreezeSchedulesToggle(BaseModel):
    enabled: bool


# =============================================================================
# Image Generation Models (routers/image_generation.py)
# =============================================================================


class ImageGenerateRequest(BaseModel):
    prompt: str
    use_case: Optional[str] = "general"
    aspect_ratio: Optional[str] = "1:1"
    refine_prompt: Optional[bool] = True


# =============================================================================
# Internal Models (routers/internal.py)
# =============================================================================


class ActivityTrackRequest(BaseModel):
    """Request model for tracking activity start."""
    agent_name: str
    activity_type: str  # e.g., "schedule_start"
    user_id: Optional[int] = None
    triggered_by: str = "schedule"  # schedule, manual, user, agent, system
    related_execution_id: Optional[str] = None
    details: Optional[Dict] = None


class ActivityCompleteRequest(BaseModel):
    """Request model for completing an activity."""
    status: str = ActivityState.COMPLETED  # ActivityState: completed, failed, cancelled
    details: Optional[Dict] = None
    error: Optional[str] = None


class InternalTaskExecutionRequest(BaseModel):
    """Request model for internal task execution via TaskExecutionService."""
    agent_name: str
    message: str
    triggered_by: str = "schedule"
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None  # TIMEOUT-001: None = use agent's config (default 15 min)
    allowed_tools: Optional[List[str]] = None
    execution_id: Optional[str] = None
    async_mode: bool = False
    # #171: optional schedule metadata surfaced in the agent's execution context block.
    schedule_name: Optional[str] = None
    schedule_cron: Optional[str] = None
    schedule_next_run: Optional[str] = None
    attempt: Optional[int] = None


class ValidateExecutionRequest(BaseModel):
    """Request model for triggering execution validation."""
    execution_id: str
    agent_name: str
    schedule_id: str
    original_message: str
    execution_response: str
    custom_prompt: Optional[str] = None
    timeout_seconds: int = 120


class InternalAuditRequest(BaseModel):
    """Request model for audit log entries from MCP server."""
    event_type: str          # AuditEventType value
    event_action: str        # e.g. "tool_call"
    source: str = "mcp"      # Always "mcp" for MCP server calls
    # MCP auth context
    mcp_key_id: Optional[str] = None
    mcp_key_name: Optional[str] = None
    mcp_scope: Optional[str] = None
    actor_agent_name: Optional[str] = None
    # #848: a keyless inline-auth caller has no key and no agent — the verified
    # email is its only identity. Without this field Pydantic would silently
    # DROP it (extra='ignore' by default), leaving an unattributable audit row
    # and no error to notice.
    actor_email: Optional[str] = None
    # Target
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    # Request correlation (#905): lets an MCP `mcp_operation` row be joined to
    # the backend `git_operation`/etc. row it triggered, when the MCP tool
    # forwards the same X-Request-ID it sends on the proxied backend call.
    request_id: Optional[str] = None
    # Details
    details: Optional[Dict] = None


# =============================================================================
# Logs Models (routers/logs.py)
# =============================================================================


class RetentionConfig(BaseModel):
    """Retention configuration."""
    retention_days: int = Field(..., ge=1, le=3650, description="Days to retain logs")
    archive_enabled: bool = Field(..., description="Whether archival is enabled")
    cleanup_hour: int = Field(..., ge=0, le=23, description="Hour (UTC) to run nightly archival")


class ArchiveRequest(BaseModel):
    """Manual archive request."""
    retention_days: Optional[int] = Field(None, ge=1, le=3650, description="Override retention days")
    delete_after_archive: bool = Field(True, description="Delete originals after archiving")


# =============================================================================
# Loops Models (routers/loops.py)
# =============================================================================


MAX_RUNS_LIMIT = 100


MAX_MESSAGE_LEN = 100_000


MAX_DELAY_SECONDS = 3600


MAX_TIMEOUT_PER_RUN = 7200


MAX_STOP_SIGNAL_LEN = 200


MAX_DURATION_SECONDS = 604_800  # 7 days — hard ceiling on the wall-clock deadline


MAX_CONSECUTIVE_FAILURES_LIMIT = 100  # #1167 — cap on the continue-mode circuit breaker


class StartLoopRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    max_runs: int = Field(..., ge=1, le=MAX_RUNS_LIMIT)
    stop_signal: Optional[str] = Field(default=None, max_length=MAX_STOP_SIGNAL_LEN)
    delay_seconds: int = Field(default=0, ge=0, le=MAX_DELAY_SECONDS)
    timeout_per_run: Optional[int] = Field(default=None, ge=10, le=MAX_TIMEOUT_PER_RUN)
    # #1156: optional loop-level wall-clock deadline. NULL = unbounded
    # (max_runs is still the hard stop). Lower bound vs the per-run timeout
    # is validated in the endpoint (needs the agent's configured timeout).
    max_duration_seconds: Optional[int] = Field(default=None, ge=1, le=MAX_DURATION_SECONDS)
    # #1155: optional per-loop USD cost budget. NULL = no limit (max_runs is
    # still the hard stop). Enforced as an iteration-boundary gate — the loop
    # stops before the next run once accumulated cost meets/exceeds the budget
    # (stop_reason='budget_exhausted'). The current run always finishes, so one
    # run (including the first) can overshoot. No upper cap — allow sub-cent.
    max_cost_usd: Optional[float] = Field(default=None, gt=0)
    # #1157: doom-loop detection. Stop the loop after K consecutive runs whose
    # response fingerprint (SHA-256 of normalized text) is identical. 0 disables;
    # default 3. 1 is nonsensical ("repeated identical" needs ≥2) → rejected.
    no_progress_threshold: Optional[int] = Field(default=3, ge=0)
    # #1167: failure policy. 'abort' (default) = fail-fast, backward compatible;
    # 'continue' tolerates failed iterations up to max_consecutive_failures.
    on_failure: Literal["abort", "continue"] = "abort"
    max_consecutive_failures: int = Field(
        default=3, ge=1, le=MAX_CONSECUTIVE_FAILURES_LIMIT
    )
    model: Optional[str] = None
    allowed_tools: Optional[List[str]] = None

    @field_validator("stop_signal")
    @classmethod
    def _normalize_stop_signal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None  # empty after strip → fixed mode

    @field_validator("no_progress_threshold")
    @classmethod
    def _validate_no_progress_threshold(cls, v: Optional[int]) -> Optional[int]:
        if v == 1:
            raise ValueError(
                "no_progress_threshold must be 0 (disabled) or >= 2; "
                "1 would stop after the first success"
            )
        return v


class StartLoopResponse(BaseModel):
    loop_id: str
    status: str
    agent_name: str
    max_runs: int
    on_failure: str = "abort"  # #1167


class LoopRunResponse(BaseModel):
    run_number: int
    execution_id: Optional[str] = None
    status: str
    response_preview: Optional[str] = None
    cost: Optional[float] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None


class LoopStatusResponse(BaseModel):
    loop_id: str
    agent_name: str
    status: str
    max_runs: int
    runs_completed: int
    failed_runs: int = 0  # #1167
    on_failure: str = "abort"  # #1167
    max_consecutive_failures: int = 3  # #1167
    stop_reason: Optional[str] = None
    last_response: Optional[str] = None
    error: Optional[str] = None
    runs: List[LoopRunResponse]
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # #1156: wall-clock deadline (NULL = unbounded) + elapsed since started_at
    # (frozen at completed_at once terminal). Both NULL before the loop runs.
    max_duration_seconds: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    # #1155: cost budget (NULL = unbounded) + total_cost computed on read as
    # the sum of agent_loop_runs.cost (NULL→0). total_cost is always a float,
    # 0.0 for a zero-run loop.
    max_cost_usd: Optional[float] = None
    total_cost: float = 0.0
    # #1157: no-progress threshold (NULL = disabled / legacy loop).
    no_progress_threshold: Optional[int] = None


class StopLoopResponse(BaseModel):
    loop_id: str
    status: str  # "stopping" | "already_done"


# =============================================================================
# Agent Self-Reminders (#1296) — routers/reminders.py
# =============================================================================

# Env-tunable abuse bounds. Read once at import; the router enforces the stateful
# ones (pending cap, daily cap, resolved-window) with real DB counts, while the
# field caps below are the hard Pydantic ceilings (422 before any DB work).
REMINDER_MESSAGE_MAX_CHARS = int(os.getenv("REMINDER_MESSAGE_MAX_CHARS", "4000"))
# Min delay ≥ the 60s scheduler reload interval so a reminder can be armed before
# it is due (a near-min reminder fires ~one interval late, never dropped).
REMINDER_MIN_DELAY_SECONDS = int(os.getenv("REMINDER_MIN_DELAY_SECONDS", "60"))
# Max delay 30 days — deliberately < the 180-day soft-delete name reservation, so
# a pending reminder can never outlive the reuse of its agent's name.
REMINDER_MAX_DELAY_SECONDS = int(os.getenv("REMINDER_MAX_DELAY_SECONDS", "2592000"))
# Concurrency cap on pending reminders per agent (429 on a real pending count).
MAX_PENDING_REMINDERS_PER_AGENT = int(os.getenv("MAX_PENDING_REMINDERS_PER_AGENT", "25"))
# Durable rolling-24h create cap — the non-fail-open backstop against
# self-perpetuation (a reminder can itself call set_reminder). 429.
MAX_REMINDERS_PER_AGENT_PER_DAY = int(os.getenv("MAX_REMINDERS_PER_AGENT_PER_DAY", "100"))


class ReminderCreate(BaseModel):
    """Request body for an agent scheduling a one-shot self-reminder (#1296).

    Exactly one of ``fire_at`` (absolute ISO-8601) or ``delay_seconds``
    (relative) must be supplied — the XOR is validated below. The resolved
    fire instant's min/max window and the timeout clamp are enforced at the
    router against the agent's config (they need the live agent cap).
    """
    message: str = Field(..., min_length=1, max_length=REMINDER_MESSAGE_MAX_CHARS)
    fire_at: Optional[str] = Field(
        default=None,
        description="Absolute ISO-8601 time to fire (UTC recommended). XOR delay_seconds.",
    )
    delay_seconds: Optional[int] = Field(
        default=None,
        gt=0,
        description="Relative delay in seconds from now. XOR fire_at.",
    )
    model: Optional[str] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=10)
    allowed_tools: Optional[List[str]] = None

    @field_validator("fire_at")
    @classmethod
    def _check_fire_at_iso(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Delegate to the SAME parser reminder_service._resolve_fire_at uses, so
        # "the validator accepted it" implies "the parser can parse it" BY
        # CONSTRUCTION (#1831). A local re-implementation diverged: this used
        # v.replace("Z", "+00:00") (EVERY Z) against the parser's trailing-Z-only
        # strip, so a mid-string Z passed here and then raised ValueError out of
        # the unguarded service call — HTTP 500. Do not reintroduce a local
        # normalization to match _validate_iso8601 above: that value is never
        # re-parsed, so it has no parser to agree with. This one does.
        try:
            parse_iso_timestamp(v)
        except (ValueError, AttributeError):
            raise ValueError("fire_at must be an ISO-8601 timestamp")
        # Return v UNCHANGED — never canonicalized. The create-idempotency key
        # hashes raw_fire_spec() == f"fire_at={self.fire_at}" (Invariant #18), so
        # rewriting it here forks the key and double-creates on a client retry.
        return v

    @model_validator(mode="after")
    def _check_fire_spec_xor(self) -> "ReminderCreate":
        has_fire_at = self.fire_at is not None
        has_delay = self.delay_seconds is not None
        if has_fire_at == has_delay:
            raise ValueError(
                "Provide exactly one of fire_at (absolute) or delay_seconds (relative)."
            )
        return self

    def raw_fire_spec(self) -> str:
        """The literal fire spec as supplied — the create-idempotency key hashes
        this (NOT the resolved instant), so a ``delay_seconds`` client-retry
        dedupes instead of resolving ``now+delay`` to a fresh key each call."""
        if self.delay_seconds is not None:
            return f"delay_seconds={self.delay_seconds}"
        return f"fire_at={self.fire_at}"


class ReminderSummary(BaseModel):
    """List-response model for a reminder (#1296)."""
    id: str
    agent_name: str
    message: str
    fire_at: str
    status: str  # pending | firing | fired | cancelled | failed
    created_at: str
    fired_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    # #1806: derived at read time (NOT a column) — true when this reminder is
    # still live but its agent has autonomy off, so the scheduler will not arm
    # it. Without this a held reminder is indistinguishable from a healthy one:
    # `pending` with a fire_at that quietly slides into the past.
    autonomy_hold: bool = False

    class Config:
        from_attributes = True


class Reminder(ReminderSummary):
    """Detail-response model — full reminder row (#1296)."""
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    execution_id: Optional[str] = None
    fire_attempts: int = 0
    error: Optional[str] = None


# =============================================================================
# Messages Models (routers/messages.py)
# =============================================================================


class SendMessageRequest(BaseModel):
    """Request to send a proactive message to a user."""
    recipient_email: EmailStr = Field(
        ...,
        description="Verified email of the recipient. Must be in agent_sharing with allow_proactive=1."
    )
    text: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Message content (max 4096 characters)"
    )
    channel: Literal["auto", "telegram", "slack", "web"] = Field(
        default="auto",
        description="Target channel. 'auto' tries channels in order: telegram -> slack -> web"
    )
    reply_to_thread: bool = Field(
        default=False,
        description="Continue in last thread if one exists (channel-dependent)"
    )
    execution_id: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "The execution this send belongs to (effect-scoped idempotency, #1084). "
            "A re-delivery of the same turn dedupes to one send per (recipient, channel). "
            "Fail-open when absent."
        ),
    )
    dedup_label: str = Field(
        default="",
        max_length=200,
        description=(
            "Optional discriminator (#1084) to intentionally send two distinct "
            "messages to the same recipient in one turn. Default → at-most-one."
        ),
    )


class SendMessageResponse(BaseModel):
    """Response from sending a proactive message."""
    success: bool
    channel: str
    message_id: Optional[str] = None
    error: Optional[str] = None


class ProactiveShareUpdate(BaseModel):
    """Request to update allow_proactive flag for a share."""
    email: EmailStr
    allow_proactive: bool


class ProactiveSharesResponse(BaseModel):
    """List of emails with proactive messaging enabled."""
    agent_name: str
    emails: list[str]


# =============================================================================
# Notifications Models (routers/notifications.py)
# =============================================================================


class DismissAllRequest(BaseModel):
    """Body for bulk-dismissing notifications (#1017)."""
    agent_name: Optional[str] = None


# =============================================================================
# Operator Queue Models (routers/operator_queue.py)
# =============================================================================


class OperatorResponse(BaseModel):
    """Body for responding to a queue item."""
    response: str
    response_text: Optional[str] = None


class BulkCancelRequest(BaseModel):
    """Body for bulk-cancelling pending queue items (#1017).

    The client sends the ids it actually rendered, so a sync-loop race can
    never cancel items the operator never saw.
    """
    ids: List[str] = Field(..., min_length=1, max_length=500)


class ClearResolvedRequest(BaseModel):
    """Body for clearing the Resolved tab (#1017)."""
    agent_name: Optional[str] = None


# =============================================================================
# Paid Models (routers/paid.py)
# =============================================================================


class PaidChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


# =============================================================================
# Public Models (routers/public.py)
# =============================================================================


class PublicChatHistoryResponse(BaseModel):
    """Response model for chat history endpoint."""
    messages: List[dict]
    session_id: str
    message_count: int


class ClearSessionResponse(BaseModel):
    """Response model for clear session endpoint."""
    cleared: bool
    new_session_id: Optional[str] = None


# =============================================================================
# Public Memory Models (routers/public_memory.py)
# =============================================================================


class WriteUserMemoryRequest(BaseModel):
    execution_id: str = Field(..., min_length=1, max_length=200)
    memory_text: str = Field(..., max_length=8000)


# =============================================================================
# Schedules Models (routers/schedules.py)
# =============================================================================


class ScheduleUpdateRequest(BaseModel):
    """Request model for updating a schedule."""
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    message: Optional[str] = None
    enabled: Optional[bool] = None
    timezone: Optional[str] = None
    description: Optional[str] = None
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Retry configuration (RETRY-001)
    max_retries: Optional[int] = None
    retry_delay_seconds: Optional[int] = None
    # Validation configuration (VALIDATE-001)
    validation_enabled: Optional[bool] = None
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: Optional[int] = None


class ScheduleResponse(BaseModel):
    """Response model for schedule data."""
    id: str
    agent_name: str
    name: str
    cron_expression: str
    message: str
    enabled: bool
    timezone: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime]
    next_run_at: Optional[datetime]
    # #913: null means "inherit from agent_ownership.execution_timeout_seconds".
    timeout_seconds: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    model: Optional[str] = None  # Model override (MODEL-001)
    # Validation configuration (VALIDATE-001)
    validation_enabled: bool = False
    validation_prompt: Optional[str] = None
    validation_timeout_seconds: int = 120

    class Config:
        from_attributes = True


class ExecutionSummary(BaseModel):
    """Lightweight execution response for list views - excludes large text fields.

    Used by GET /api/agents/{name}/executions for fast list loading.
    Full details available via GET /api/agents/{name}/executions/{id}.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    triggered_by: str
    # Observability fields (small)
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    # Origin tracking (small) - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume (small) - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection (small) - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage (small) - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking (small) - VALIDATE-001
    business_status: Optional[str] = None  # pending_validation, validated, failed_validation, skipped
    validation_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B) - small JSON list
    compact_metadata: Optional[str] = None

    # EXCLUDED (large fields - fetch via /executions/{id}):
    # - response: Optional[str]      # Full response text
    # - error: Optional[str]         # Full error text
    # - tool_calls: Optional[str]    # JSON array of tool calls
    # - execution_log: Optional[str] # Full Claude Code transcript

    class Config:
        from_attributes = True


class ExecutionResponse(BaseModel):
    """Full response model for execution data - includes all fields.

    Used by GET /api/agents/{name}/executions/{id} for single execution details.
    """
    id: str
    schedule_id: str
    agent_name: str
    status: str
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]
    message: str
    response: Optional[str]
    error: Optional[str]
    triggered_by: str
    # Observability fields
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None  # Full Claude Code execution transcript (JSON)
    # Origin tracking - AUDIT-001
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    # Session resume - EXEC-023
    claude_session_id: Optional[str] = None
    # Model selection - MODEL-001
    model_used: Optional[str] = None
    # Fan-out linkage - FANOUT-001
    fan_out_id: Optional[str] = None
    # Validation tracking - VALIDATE-001
    business_status: Optional[str] = None
    validated_at: Optional[datetime] = None
    validation_execution_id: Optional[str] = None
    validates_execution_id: Optional[str] = None
    # Auto-compact observability (Bundle B)
    compact_metadata: Optional[str] = None

    class Config:
        from_attributes = True


class WebhookStatusResponse(BaseModel):
    """Webhook configuration for a schedule.

    ent#77: `auth_enabled`/`has_secret` describe the optional HMAC signature
    layer. `signing_secret` is populated **only** in the response to a
    mint/rotate call — the plaintext secret is shown exactly once and never
    returned by GET or persisted in the clear.
    """
    schedule_id: str
    has_token: bool
    webhook_enabled: bool
    webhook_url: Optional[str] = None
    auth_enabled: bool = False
    has_secret: bool = False
    signing_secret: Optional[str] = None
    signature_header: Optional[str] = None  # header name to send the signature in


# =============================================================================
# Sessions Models (routers/sessions.py)
# =============================================================================


class CreateSessionRequest(BaseModel):
    """Optional body for POST /session. All fields optional."""

    subscription_id: Optional[str] = None


class SessionMessageRequest(BaseModel):
    """Body for the turn endpoint."""

    message: str = Field(..., min_length=1)
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    # File attachments — same shape as ParallelTaskRequest.files (#364).
    # Images become vision blocks for the model; non-images are written
    # into the agent workspace and a "[File uploaded by X]: name (size)
    # saved to path" line is appended to the prompt so the agent can
    # `Read` them. (Phase 5.2 file-upload parity with Chat.)
    files: Optional[list] = None


# =============================================================================
# Settings Models (routers/settings.py)
# =============================================================================


class ApiKeyUpdate(BaseModel):
    """Request body for updating an API key."""
    api_key: str


class ApiKeyTest(BaseModel):
    """Request body for testing an API key."""
    api_key: str


class OpsSettingsUpdate(BaseModel):
    """Request body for updating ops settings."""
    settings: Dict[str, str]


class RetentionAcknowledge(BaseModel):
    """Approve one over-threshold retention prune (#1644).

    `window_days` is not advisory — the endpoint rejects (409) unless it matches
    the window actually in force, so an ack always names the deletion it authorizes.
    """
    key: str = Field(..., min_length=1, max_length=100)
    window_days: int = Field(..., ge=0, le=3650)



class SlackSettingsUpdate(BaseModel):
    """Request body for updating Slack settings."""
    client_id: str = None
    client_secret: str = None
    signing_secret: str = None


class SlackConnectRequest(BaseModel):
    """Request body for connecting Slack transport."""
    app_token: Optional[str] = None  # xapp-... for Socket Mode
    transport_mode: Optional[str] = None  # "socket" or "webhook"


class GitHubTemplateEntry(BaseModel):
    """A single GitHub template entry."""
    github_repo: str
    display_name: str = ""
    description: str = ""


class GitHubTemplatesUpdate(BaseModel):
    """Request body for updating GitHub templates."""
    templates: List[GitHubTemplateEntry]


class TemplateRegistryUpdate(BaseModel):
    """PUT body for the remote template registry (TMPL-002, ent#14).

    Partial update: an omitted field is left untouched (the
    `/api/settings/skills-library` shape), so an admin can flip the toggle
    without re-typing the URL. `url` is validated by
    `utils.url_validation.validate_template_registry_url` at the route — SSRF
    gating needs DNS resolution, which does not belong in a Pydantic model.
    """
    url: Optional[str] = None
    enabled: Optional[bool] = None


class McpUrlUpdate(BaseModel):
    """Request body for updating the MCP server URL."""
    url: str


class AgentQuotaUpdate(BaseModel):
    """Request body for updating per-role agent quotas."""
    max_agents_creator: Optional[str] = None
    max_agents_operator: Optional[str] = None
    max_agents_user: Optional[str] = None


# =============================================================================
# Setup Models (routers/setup.py)
# =============================================================================


class SetAdminPasswordRequest(BaseModel):
    """Request body for creating the admin account at first-time setup.

    `email` is **required** (trinity-enterprise#49): it becomes the admin's
    sign-in identity (login with email + password instead of the fixed 'admin')
    and is the contact used for the optional operator intake. The remaining
    operator-profile fields (company/name/role/use_case) stay optional and are
    only forwarded to the hosted intake endpoint when `consent_updates` is true.
    """
    password: str = Field(..., max_length=128)
    confirm_password: str = Field(..., max_length=128)
    # Required admin email — sign-in identity. Shape validated in the handler so
    # a typo / blank value yields a clean 400 (a missing field yields a 422).
    email: str = Field(..., max_length=254)
    # Optional operator profile — all skippable; setup completes without them.
    company: Optional[str] = Field(None, max_length=200)
    name: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=200)
    use_case: Optional[str] = Field(None, max_length=500)
    # Affirmative, opt-in consent to occasionally receive security & product
    # updates. ONLY when true is anything submitted to the hosted intake.
    consent_updates: bool = False


# =============================================================================
# Sharing Models (routers/sharing.py)
# =============================================================================


class AccessPolicy(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessPolicyUpdate(BaseModel):
    require_email: bool
    open_access: bool
    group_auth_mode: str = "none"  # 'none' or 'any_verified'


class AccessRequest(BaseModel):
    id: str
    agent_name: str
    email: str
    channel: str | None = None
    requested_at: str
    status: str


class AccessRequestDecision(BaseModel):
    approve: bool


# =============================================================================
# Slack Models (routers/slack.py)
# =============================================================================


class SlackEventResponse(BaseModel):
    """Response to Slack events (always return 200)."""
    ok: bool = True
    challenge: Optional[str] = None


# =============================================================================
# Telegram Models (routers/telegram.py)
# =============================================================================


class TelegramWebhookResponse(BaseModel):
    ok: bool = True


class TelegramBindingResponse(BaseModel):
    agent_name: str
    bot_username: Optional[str] = None
    bot_id: Optional[str] = None
    webhook_url: Optional[str] = None
    bot_link: Optional[str] = None
    configured: bool = False
    group_count: int = 0
    warning: Optional[str] = None
    # ent#264: in-progress indicator toggle (default ON); None when unconfigured.
    progress_indicator_enabled: Optional[bool] = None


class TelegramConfigureRequest(BaseModel):
    bot_token: str


class TelegramTestRequest(BaseModel):
    chat_id: Optional[str] = None
    message: str = "Hello from Trinity! Your Telegram bot is configured correctly."


class TelegramGroupConfigResponse(BaseModel):
    id: int
    chat_id: str
    chat_title: Optional[str] = None
    chat_type: str = "group"
    trigger_mode: str = "mention"
    welcome_enabled: bool = False
    welcome_text: Optional[str] = None
    is_active: bool = True
    # ent#265: per-group consent for completion reports (default allow; the
    # model IS the field allowlist for the GET's `Response(**row)` build).
    allow_proactive: bool = True


class TelegramGroupConfigUpdateRequest(BaseModel):
    trigger_mode: Optional[str] = None
    welcome_enabled: Optional[bool] = None
    welcome_text: Optional[str] = None
    # ent#265: human-only arm — the router calls reject_agent_principal when set.
    allow_proactive: Optional[bool] = None


class TelegramGroupMessageRequest(BaseModel):
    """Request model for proactive group messaging (Issue #349)."""
    message: str


class TelegramProgressIndicatorRequest(BaseModel):
    """ent#264 — toggle the in-progress status indicator on a Telegram binding."""
    enabled: bool


class SlackChannelProactiveRequest(BaseModel):
    """ent#223 — toggle per-channel proactive consent on a Slack channel binding."""
    allow_proactive: bool


class SlackChannelMessageRequest(BaseModel):
    """Request model for proactive Slack channel messaging (#350)."""
    message: str
    thread_ts: Optional[str] = None  # optionally reply in an existing thread


# =============================================================================
# Users Models (routers/users.py)
# =============================================================================


class UserRoleUpdate(BaseModel):
    role: str


class UpdateMyEmailRequest(BaseModel):
    email: str


# =============================================================================
# Voice Models (routers/voice.py)
# =============================================================================


class VoiceStartRequest(BaseModel):
    session_id: Optional[str] = None  # Existing chat session to continue
    voice_name: Optional[str] = None  # Gemini voice name (e.g. "Kore", "Puck")
    workspace_mode: bool = False       # Enable canvas panel tools


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


# =============================================================================
# Voip Models (routers/voip.py)
# =============================================================================


class VoipConfigureRequest(BaseModel):
    account_sid: str
    auth_token: str
    from_number: str
    daily_call_cap: Optional[int] = None


class VoipBindingResponse(BaseModel):
    agent_name: str
    configured: bool
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    daily_call_cap: Optional[int] = None
    display_name: Optional[str] = None
    enabled: Optional[bool] = None


class VoipCallRequest(BaseModel):
    to_number: str
    context: Optional[str] = None
    process_transcript: bool = True
    # Effect-scoped idempotency (#1084): a re-delivery of the same turn replays
    # the original call instead of placing a second PSTN call. Fail-open absent.
    execution_id: Optional[str] = Field(default=None, max_length=200)
    dedup_label: str = Field(default="", max_length=200)


class VoipEnabledRequest(BaseModel):
    enabled: bool


# =============================================================================
# Webhooks Models (routers/webhooks.py)
# =============================================================================


CONTEXT_MAX_CHARS = 4000


class WebhookTriggerRequest(BaseModel):
    """Optional body for a webhook trigger call."""
    context: Optional[str] = Field(
        default=None,
        description="Additional context appended to the schedule message.",
        max_length=CONTEXT_MAX_CHARS,
    )
    metadata: Optional[dict] = Field(
        default=None,
        description="Arbitrary key/value metadata stored on the execution record.",
    )


# =============================================================================
# Whatsapp Models (routers/whatsapp.py)
# =============================================================================


class WhatsAppBindingResponse(BaseModel):
    agent_name: str
    configured: bool = False
    account_sid: Optional[str] = None
    from_number: Optional[str] = None
    messaging_service_sid: Optional[str] = None
    display_name: Optional[str] = None
    is_sandbox: bool = False
    webhook_url: Optional[str] = None
    warning: Optional[str] = None


class WhatsAppConfigureRequest(BaseModel):
    account_sid: str
    auth_token: str
    from_number: str
    messaging_service_sid: Optional[str] = None


class WhatsAppTestRequest(BaseModel):
    to_number: Optional[str] = None
    message: str = "Hello from Trinity! Your WhatsApp integration is configured correctly."


# ============================================================================
# Skill Sources (ent#237 — multi-source skills library)
# ============================================================================

class SkillSourceCreate(BaseModel):
    """Register a skills repo as a source.

    `is_default` is absent by design: the bundled community source is seeded at
    fresh install and there can be only one, so an API caller never sets the
    flag. Its trust posture (tag-pinned, ours to bump) is not a property a
    caller should be able to claim for an arbitrary repo.
    """
    name: str = Field(..., min_length=1, max_length=100)
    url: str = Field(..., min_length=1, max_length=500)
    ref: str = Field("main", min_length=1, max_length=200)
    # 'branch' tracks a moving head; 'tag' pins and REFUSES a moved tag. An
    # operator syncing a repo whose write access they do not fully control
    # should pin (ent#237 AC#5).
    ref_type: Literal["branch", "tag"] = "branch"
    enabled: bool = True

    @field_validator("name", "ref")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        if any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("must not contain control characters")
        return v


class SkillSourceUpdate(BaseModel):
    """Patch a source. Every field optional; omitted fields are untouched.

    `is_default` is not patchable — promoting a custom source would change its
    trust posture without changing where it points (enforced in the db layer
    too, so an added field here cannot silently start working).
    """
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    url: Optional[str] = Field(None, min_length=1, max_length=500)
    ref: Optional[str] = Field(None, min_length=1, max_length=200)
    ref_type: Optional[Literal["branch", "tag"]] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(None, ge=1, le=10000)


class SkillsLibrarySourceStatus(BaseModel):
    """One source's entry in the PUBLIC library-status projection (ent#334).

    Every field the service emits per source EXCEPT `url`. Repo URLs are
    admin-sensitive by ent#237's own classification — that is why
    `GET /skills/sources` is `require_admin` + `reject_agent_principal` — but
    `GET /skills/library/status` is open to any authenticated user (and to
    agent-scoped keys, deliberately: the per-agent Skills tab and the MCP tool
    both read it). Emitting the URL there handed the admin-gated value to
    exactly the callers the gate excludes.
    """
    id: str
    name: str
    ref: Optional[str] = None
    ref_type: Optional[str] = None
    is_default: bool = False
    enabled: bool = True
    priority: Optional[int] = None
    cloned: bool = False
    # ent#332: resolved layout root; null until the source has been cloned.
    skills_root: Optional[str] = None
    layout_conflict: bool = False
    last_sync: Optional[str] = None
    last_sync_status: Optional[str] = None
    commit_sha: Optional[str] = None
    skill_count: int = 0
    # `last_error` is deliberately NOT here (ent#334, found by /cso).
    #
    # It carries git's own failure text, which routinely echoes the remote
    # URL — and the URL the clone path uses is `_authenticated_url`'s, with a
    # PAT spliced in. `skill_source_clone.redact()` scrubs it on the way in,
    # but that scrubber under-matches a double-`@` authority (ent#347), which
    # is exactly the shape `_authenticated_url` produces when the stored URL
    # ALREADY carries userinfo — and that combination reliably fails auth, so
    # the failing branch is the guaranteed one.
    #
    # Dropping the field here costs nothing: its only consumer is
    # `SkillSourcesPanel.vue`, which reads the admin-gated
    # `GET /api/skills/sources` (raw dict, field intact). So the operator who
    # needs the error still sees it, and a caller who should not see repo
    # URLs cannot reach one through an error string. Do not add it back to
    # this projection to "help" a non-admin surface debug a sync.


class SkillsLibraryStatus(BaseModel):
    """PUBLIC projection of `skill_service.get_library_status()` (ent#334).

    **This model exists to be an allow-list, and that is the whole point** —
    do not replace it with the raw dict, and do not add fields to it
    reflexively. FastAPI serialises through an explicitly-constructed model, so
    anything the service starts returning that is not named here is invisible
    over REST until someone deliberately adds it. Fail-closed: the next
    sensitive field the service grows leaks nothing by default. Same idiom, and
    same reason, as `response_model=List[SkillInfo]` on `GET /skills/library`.

    Omitted on purpose: the flat `url` and the per-source `url`. Those are the
    admin-sensitive value — ent#237 classes source repo URLs that way, which is
    why `GET /skills/sources` is `require_admin` + `reject_agent_principal` —
    and this route is open to every authenticated caller including agent-scoped
    keys.

    `branch` and `commit_sha` are deliberately KEPT. They are a ref name and a
    commit hash, not credentials and not a repo identity, and the Library
    header renders both. Dropping them was considered and cut: it would have
    been a second, unrelated behaviour change riding a security fix. If a
    reason to drop them appears later it belongs in its own issue.

    `configured` and `cloned` are load-bearing, not decoration: the frontend
    stores derive their empty-state discriminator from them
    (`stores/skillsLibrary.js` → `emptyReason === 'not_cloned'`,
    `stores/skills.js` gates on `configured`). Dropping either turns a
    configured-but-unsynced library into a wrong "no library" empty state.
    """
    configured: bool = False
    cloned: bool = False
    sources: List[SkillsLibrarySourceStatus] = Field(default_factory=list)
    source_count: int = 0
    enabled_source_count: int = 0
    skill_count: int = 0
    multi_file_count: int = 0
    shadowed_count: int = 0
    last_sync: Optional[str] = None
    last_sync_status: Optional[str] = None
    last_sync_error: Optional[str] = None
    # Legacy flat fields (first source in resolution order). Not URLs.
    branch: Optional[str] = None
    commit_sha: Optional[str] = None


class SkillAssignmentAgent(BaseModel):
    """One agent holding a skill, as it leaves `GET /api/skills/assignments`.

    An allow-list for the same reason as `SkillsLibraryStatus` above (ent#334):
    the db layer selects from `agent_ownership`, whose columns include
    subscription ids, resource limits and encrypted-credential pointers. Naming
    the two fields that may leave makes the next column added to that table
    fail-closed instead of shipping by default.

    `display_label` is the ent#181 human-facing name; NULL means the UI renders
    the slug. It discloses nothing new — `GET /api/agents` already serves it for
    exactly the set this endpoint is scoped to.
    """
    name: str
    display_label: Optional[str] = None


class SkillAssignmentsResponse(BaseModel):
    """Fleet-wide skill→agents map for the Library's Skills tab (ent#384).

    `scope` is not decoration. An empty `assignments` map is ambiguous on its
    own — it means either "nobody holds anything" or "you can see no agents" —
    and a UI that guesses tells a `role=user` with no agents that a skill held
    by forty agents has no holders. `all` (admin, unfiltered) vs `accessible`
    (owned ∪ shared) lets the client word the zero honestly: "no agents yet"
    only under `all`, "none of your agents" under `accessible` — which is true
    whether the caller has zero agents or zero assignments among them, so no
    count of the accessible set is needed and none is sent.
    """
    assignments: Dict[str, List[SkillAssignmentAgent]] = Field(default_factory=dict)
    scope: str = "accessible"
    # ent#386 — the agents this caller may assign TO, which is a strictly
    # different set from the holders above: holders are owned ∪ shared, while
    # the skill write routes are owner-or-admin. A shared agent therefore shows
    # as a holder and is correctly absent here. Server-computed rather than
    # derived client-side, because deriving it client-side means a second copy
    # of an authorization predicate, free to drift from the one the write route
    # enforces. Ghosts excluded, exactly as in `assignments`.
    assignable_agents: List[SkillAssignmentAgent] = Field(default_factory=list)


# ============================================================================
# Outbound A2A calls (#736)
# ============================================================================

class A2ACallRequest(BaseModel):
    """Body for `POST /api/agents/{name}/a2a/call` (#736).

    Note what is NOT here: a URL. The target is an `endpoint` **reference** — an
    id or the operator-facing name of a pre-registered endpoint — resolved
    server-side. The issue's filed AC asked for `agent_card_url`, and it is
    rejected: an agent's parameters are LLM-generated and prompt-injectable, so
    a URL parameter would turn any document the agent reads into a lever on a
    credentialed server-side request from inside the platform network. See
    requirements mcp.md §32.5 FR-1.

    There is likewise no `stream` field. A parameter that is accepted and
    silently does not stream is a lie in a schema agents read (§32.5 FR-6).
    """
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        ..., min_length=1, max_length=200,
        description="Id or name of a pre-registered outbound A2A endpoint.",
    )
    message: str = Field(..., min_length=1, max_length=100_000)
    dedup_label: str = Field(
        ..., min_length=1, max_length=200,
        description=(
            "A distinct label per call within this turn. REQUIRED: the effect "
            "guard keys on the endpoint + conversation ids, never the message "
            "body, so without a distinct label a second question to the same "
            "endpoint in one execution would replay the FIRST answer."
        ),
    )
    context_id: Optional[str] = Field(default=None, max_length=200)
    task_id: Optional[str] = Field(default=None, max_length=200)
    execution_id: Optional[str] = Field(default=None, max_length=200)


class A2ATaskRequest(BaseModel):
    """Body for `POST /api/agents/{name}/a2a/task` — poll a remote task (#736)."""
    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(..., min_length=1, max_length=200)
    task_id: str = Field(..., min_length=1, max_length=200)


class A2ACallResponse(BaseModel):
    """Allowlisted outbound-call result (#736).

    An allowlist, not a filtered dump: the remote controls its response, so the
    fields that reach the calling agent are enumerated here and nothing else
    crosses — never the raw body, never request headers, never the resolved URL.
    """
    success: bool = True
    state: str
    text: Optional[str] = None
    task_id: Optional[str] = None
    context_id: Optional[str] = None
    truncated: bool = False
    protocol_version: str = "0.3"
    endpoint: str
    replayed: bool = False


class A2AOutboundEndpointUpsert(BaseModel):
    """Body for the admin OSS endpoint registry (#736 §32.5 FR-2).

    `credentials` is WRITE-ONLY: it is accepted here and never returned by any
    read. Omitting it on an update leaves an existing secret in place (so an
    operator can repoint or rename without re-typing something they may not
    have); `clear_credentials` removes it.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=1, max_length=2048)
    credentials: Optional[SecretStr] = Field(default=None)
    clear_credentials: bool = False

    @field_validator("credentials")
    @classmethod
    def _validate_credential(cls, v: Optional[SecretStr]) -> Optional[SecretStr]:
        """Reject a header-unsafe credential — the same guard, for the same
        reason, as `_validate_pat_secret` (ent#109).

        This value becomes an `Authorization: Bearer …` header on the outbound
        POST, and h11 rejects an illegal header value by **echoing it**. A
        credential carrying a stray line break — the routine paste artifact —
        would therefore reappear inside the transport error the calling agent
        reads and the backend logs. `error_handlers.validation_error_without_input`
        strips Pydantic's `input` from every 422, so refusing here does not move
        the leak into the rejection.
        """
        if v is None:
            return None
        raw = v.get_secret_value().strip()
        if not raw:
            return None
        if not _PAT_SAFE_RE.match(raw):
            # Never echo the value — that is the leak this guard prevents.
            raise ValueError(
                "credentials contains characters that are not valid in an HTTP "
                "header (whitespace, line breaks or control characters). Paste "
                "the token again without surrounding whitespace."
            )
        return SecretStr(raw)


class FirstRunState(BaseModel):
    """First-run state for the front-desk surface (ent#319, epic ent#54).

    `first_run` stays true while every agent the caller can see is one Trinity
    seeded on their behalf — seeding (ent#124) made "zero agents" permanently
    false, so the surface it used to gate needed a predicate that survives it.
    `demo_agent` is the seeded agent the "Show me" door opens, or None when the
    install seeded nothing (seeding disabled) and there is nothing to show yet.
    """
    first_run: bool
    seeded_agents: List[str] = []
    own_agent_count: int = 0
    demo_agent: Optional[str] = None
