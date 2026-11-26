"""A response computed under a narrowed view says so (trinity-enterprise#978).

While a shared person's or a visitor's turn runs on an agent, every request
made with the agent's key reads executions narrowed to that person
(`dependencies.acting_turn_user_id`): the platform cannot tell which process
sent a request, so it fails closed. The cost is accepted (operator ruling
2026-11-23), but it was silent: the owner's own concurrent run read an empty
list, or "not found" for a finished run, with nothing to say why.

The seam that decides the narrowing calls `note(code)`, and
`ReadNarrowedMiddleware` puts the first code of the request on the response as
`X-Trinity-Read-Narrowed`. One mechanism, so a reader that narrows through the
seam is marked without remembering anything: a list, a detail read, a 404, a
stream.

Three properties are the contract; `tests/unit/test_ent978_narrowed_read_marker.py`
pins each:

1. **Principal-derived, never row-derived.** The code is noted where the VIEW
   is decided, before any row is looked up, so a run that exists and one that
   does not carry the same header (Invariant #8). A handler that narrows must
   compute its scope before its row lookup.
2. **Codes only.** The header carries one of `turn_tier_service.READ_NARROWED_CODES`:
   no person, no run id, no count. A value of any other shape is not sent.
3. **Per request.** The holder lives in a `ContextVar` set by the middleware
   and is MUTATED downstream, never re-set: the threadpool and the
   `BaseHTTPMiddleware` layers each run the handler in a copied context, which
   shares the holder object but would lose a new binding. Outside a request
   `note` does nothing.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Dict, Optional

logger = logging.getLogger(__name__)

HEADER = "X-Trinity-Read-Narrowed"
_HEADER_BYTES = HEADER.lower().encode("latin-1")
_CODE_SHAPE = re.compile(r"^[a-z_]{1,64}$")

_holder: ContextVar[Optional[Dict[str, str]]] = ContextVar("read_narrowing", default=None)


def note(code: Optional[str]) -> None:
    """Record that this request's answer is narrowed, and why. The first code
    of a request sticks, so a request that reads its scope several times sends
    one stable value."""
    holder = _holder.get()
    if holder is None or not isinstance(code, str) or not _CODE_SHAPE.match(code):
        return
    holder.setdefault("code", code)


def noted() -> Optional[str]:
    """The code recorded for the current request, if any."""
    holder = _holder.get()
    return holder.get("code") if holder else None


class ReadNarrowedMiddleware:
    """Pure ASGI: adds the header when the request's handler noted a code.

    Not a `BaseHTTPMiddleware`: the holder has to be installed in the context
    the rest of the stack is called from, and the header has to land on every
    kind of response (an `HTTPException`, a streamed body)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        holder: Dict[str, str] = {}
        token = _holder.set(holder)

        async def send_marked(message):
            if message.get("type") == "http.response.start":
                # The header is the platform's alone: a value a handler (or an
                # upstream it forwards) set is dropped, with or without a code.
                headers = [h for h in message.get("headers", []) if h[0].lower() != _HEADER_BYTES]
                code = holder.get("code")
                if code:
                    headers.append((_HEADER_BYTES, code.encode("latin-1")))
                    logger.info("[ent#978] read narrowed: code=%s %s %s",
                                code, scope.get("method"), scope.get("path"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_marked)
        finally:
            _holder.reset(token)
