"""
Domain signals shared across the chat/task service split (#1483).

A dependency-free leaf module: the chat services return / raise these instead of
touching FastAPI, and the thin router (``routers/chat.py``) maps them 1:1 to
HTTP. This keeps Invariant #1 ("services hold no HTTP") intact — the split moved
business logic out of the router, and the router keeps only the domain→HTTP
translation.

  * ``ChatAdmission`` / ``ChatExecutionContext`` — the handoff NamedTuples the
    ``/chat`` slices pass forward (relocated verbatim from ``routers/chat.py``;
    they are ``NamedTuple``, not Pydantic ``BaseModel``, so Invariant #14 does
    not apply).
  * ``ChatAdmissionReplay`` — an idempotent-replay outcome: the admission gate
    matched a prior request. The router builds the 200 ``JSONResponse`` (with the
    ``X-Idempotent-Replay`` header) or the 409 in-flight error.
  * ``ChatDispatchError`` — an HTTP-free error carrying the exact
    (status_code, detail, headers) the router raises as an ``HTTPException``.
    The service builds the domain payload (an error dict/string); the status code
    is a mapping hint, not a FastAPI import.
  * ``InterAgentDepthExceeded`` — an agent-to-agent hop past the configured
    chain depth (#2806). Each router maps it explicitly to a named 403.
  * ``AdmissionRefused`` — a registered admission policy stopped the turn
    before anything was claimed or acquired (trinity-enterprise#1059,
    ``services/admission_policy``). The routers map it to a named 429.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple, Optional

from services.execution_envelope import TaskExecutionErrorCode


def chat_trigger(x_source_agent: Optional[str], x_via_mcp: Optional[str]) -> str:
    """``triggered_by`` of a ``POST /chat`` turn (#96, #3127): ``agent`` for
    agent-to-agent, ``mcp`` for a user's MCP call, ``chat`` otherwise. Admission
    (pull routing) and the execution row read the same value."""
    if x_source_agent:
        return "agent"
    if x_via_mcp:
        return "mcp"
    return "chat"


class ChatAdmission(NamedTuple):
    """Result of the chat admission gate (#1026 slice 1) when a request is
    cleared to proceed. Carries the values the rest of the endpoint needs."""
    idem: object
    execution_id: str
    capacity_result: object
    capacity: object
    queue_result: str
    chat_timeout: int
    # #2806: the depth to stamp on the child row — None for a non-agent
    # principal (a root). Last, with a default, so the NamedTuple stays
    # positionally compatible.
    chain_depth: Optional[int] = None
    # trinity-enterprise#752: the skill gate's decision (a
    # `skill_gate_service.GateDecision`), recorded by the row's setup once the
    # execution the agent will receive exists — never under this admission's
    # capacity-slot id. Last, with a default, for the same reason.
    gate: Optional[object] = None


class ChatExecutionContext(NamedTuple):
    """Execution-setup handoff for chat_with_agent (#1026 slice 2). Carries the
    records/ids the downstream execute+finalize body consumes. As with
    ChatAdmission (slice 1), every field here is referenced downstream — leaving
    one out strands a local and NameErrors the admitted path."""
    execution: object
    task_execution_id: object
    triggered_by: str
    subscription_id: object
    collaboration_activity_id: object
    chat_activity_id: object
    session: object
    is_queued: bool
    # trinity-enterprise#752: a self-approved turn of a gated skill runs in its
    # own session, never the agent's shared one (the next caller would resume
    # it with the skill loaded). Last, with a default.
    isolated_session: bool = False


class ChatAdmissionReplay(NamedTuple):
    """Idempotent-replay signal (RELIABILITY-006, #525). The router turns this
    into the 200 replay ``JSONResponse`` (``snapshot`` or a default body) or, when
    ``in_flight``, the 409 ``request_in_progress`` error."""
    execution_id: str
    in_flight: bool
    snapshot: object  # dict | None (None ⇒ router supplies the endpoint's default)


class ChatDispatchError(Exception):
    """HTTP-free domain error the thin router maps 1:1 to an ``HTTPException``.

    Carries the *already-computed* domain payload (``detail`` — a str or dict) plus
    the status code + optional headers so the router does a single
    ``raise HTTPException(status_code=e.status_code, detail=e.detail, headers=e.headers)``.
    No ``fastapi`` import in the service layer.
    """

    def __init__(self, status_code: int, detail, headers: Optional[dict] = None):
        super().__init__(detail if isinstance(detail, str) else str(detail))
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


# The one spelling, shared with the X-Trinity-Error-Code vocabulary.
INTER_AGENT_DEPTH_EXCEEDED = TaskExecutionErrorCode.INTER_AGENT_DEPTH_EXCEEDED.value


class InterAgentDepthExceeded(Exception):
    """An agent-to-agent hop past ``inter_agent_max_chain_depth`` (#2806).

    Raised by ``dispatch_admission_service.enforce_inter_agent_depth`` before any
    idempotency claim, capacity slot or execution row exists. Not retryable:
    the depth only falls when the caller's own running turns finish, so the
    message tells the calling model to stop rather than retry or re-route.
    """

    def __init__(self, caller: Optional[str], target: str, depth: int, max_depth: int):
        super().__init__(
            f"{INTER_AGENT_DEPTH_EXCEEDED}: {caller} -> {target} at depth {depth} > {max_depth}"
        )
        self.caller = caller
        self.target = target
        self.depth = depth
        self.max_depth = max_depth

    def detail(self) -> dict:
        return {
            "error": INTER_AGENT_DEPTH_EXCEEDED,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "caller": self.caller,
            "target": self.target,
            "message": (
                f"Inter-agent chain depth limit reached ({self.depth} > {self.max_depth}). "
                "Do not retry this call or route it through another agent; finish your "
                "turn with what you have and report back to your caller."
            ),
        }


class AdmissionRefused(Exception):
    """A registered admission policy refused the turn (trinity-enterprise#1059).

    Raised by ``admission_policy.enforce`` at the admission seams, before any
    idempotency claim, capacity slot or (on ``/chat`` and ``/task``) execution
    row exists, so the same request goes through unchanged once the condition
    clears. ``code`` names the reason on ``X-Trinity-Error-Code``; ``message``
    is the plain-words sentence a person or a calling agent reads; ``frees_at``
    (aware UTC, optional) is when the policy expects to admit again and becomes
    ``Retry-After``. Not retryable by default: re-sending before ``frees_at``
    meets the same refusal.
    """

    def __init__(self, code: str, agent_name: str, message: str, *,
                 frees_at: Optional[datetime] = None, retryable: bool = False):
        super().__init__(f"{code}: {agent_name}: {message}")
        self.code = code
        self.agent_name = agent_name
        self.message = message
        self.frees_at = frees_at
        self.retryable = retryable

    def retry_after_seconds(self, now: Optional[datetime] = None) -> Optional[int]:
        """Whole seconds until ``frees_at`` (never negative), or None if unknown."""
        if self.frees_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        frees_at = self.frees_at
        if frees_at.tzinfo is None:
            frees_at = frees_at.replace(tzinfo=timezone.utc)
        return max(0, int((frees_at - now).total_seconds()))

    def detail(self) -> dict:
        frees_at = self.frees_at
        if frees_at is not None:
            if frees_at.tzinfo is None:
                frees_at = frees_at.replace(tzinfo=timezone.utc)
            frees_at = frees_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "error": self.code,
            "agent": self.agent_name,
            "message": self.message,
            "frees_at": frees_at,
            "retryable": self.retryable,
        }


# The codes a terminal row can carry that mean "an admission policy stopped this
# turn before it ran". A SKIPPED row with one of these is a refusal wherever it
# is read back, not a success-shaped empty reply.
ADMISSION_REFUSAL_CODES = (TaskExecutionErrorCode.USAGE_SHARE.value,)


def admission_refusal_of(result, agent_name: str) -> Optional[AdmissionRefused]:
    """The admission-policy refusal a terminal result carries, or None.

    Reads the in-memory ``admission_refusal`` the gate attached when the turn was
    refused inside this process, and otherwise rebuilds it from the row's shape
    — ``status == skipped`` with an ``error_code`` in ``ADMISSION_REFUSAL_CODES``
    — which is what a turn refused on another worker (the backlog drain, a
    long-poll that read the row back) comes back as. ``frees_at`` is not on the
    row, so a rebuilt refusal has none; the message itself says when.

    Every ``dispatch_and_await_terminal`` caller that answers a person must ask
    this before branching on ``failed``/``cancelled`` (trinity-enterprise#1059
    review C2): a refusal is not a failure and not a success.
    """
    refusal = getattr(result, "admission_refusal", None)
    if isinstance(refusal, AdmissionRefused):
        return refusal
    status = getattr(result, "status", None)
    status = str(getattr(status, "value", status) or "").strip().lower()
    code = getattr(result, "error_code", None)
    code = getattr(code, "value", code)
    if status == "skipped" and code in ADMISSION_REFUSAL_CODES:
        return AdmissionRefused(code, agent_name, (getattr(result, "error", None) or "").strip())
    return None
