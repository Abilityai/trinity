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
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class ChatAdmission(NamedTuple):
    """Result of the chat admission gate (#1026 slice 1) when a request is
    cleared to proceed. Carries the values the rest of the endpoint needs."""
    idem: object
    execution_id: str
    capacity_result: object
    capacity: object
    queue_result: str
    chat_timeout: int


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
