"""
App-level exception handlers (trinity-enterprise#109).

Separate from ``main.py`` for two reasons: ``main`` drags in the lifespan, every
router, OpenTelemetry and a live DB/Redis, so a handler defined there cannot be
unit-tested without standing the whole platform up; and a handler is a policy
about the API's *output shape*, which is worth being able to read in one place.
"""

import inspect
import logging
import time
from collections import OrderedDict

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from utils.credential_sanitizer import sanitize_text

logger = logging.getLogger(__name__)

# Keys removed from every validation-error entry before it is returned.
# `input` is the rejected value; `ctx` can carry it too, depending on the
# error type.
_VALUE_BEARING_KEYS = ("input", "ctx")


async def validation_error_without_input(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 bodies must not echo the value that failed validation.

    Pydantic v2 records the rejected value in ``errors()[i]["input"]`` and
    FastAPI's default handler passes ``exc.errors()`` through verbatim. For a
    ``SecretStr`` field that means a failed validation returns the SECRET in the
    response body — and, wherever a 422 is logged, in the platform log.

    This is load-bearing for the ent#109 PAT charset guard
    (``models._validate_pat_secret``), which rejects a GitHub token carrying
    ``\\r``/``\\n`` precisely because such a token makes h11 echo it verbatim in
    a ``LocalProtocolError``. Without this handler that guard would *relocate*
    the leak from a 500 into a 422 rather than closing it.

    ``input`` is dropped for EVERY field rather than only for names that look
    sensitive. A name-matching allowlist is the "new producer missing from the
    consumer's list" class — the next ``SecretStr`` field added would have to
    remember to be named correctly to be protected, and the one that forgets
    fails silently. The value also carries no information the caller lacks:
    they just sent it. ``type``, ``loc`` and ``msg`` are what a client needs to
    fix its request, and they are preserved, so the 422 stays actionable.
    """
    safe = []
    for err in exc.errors():
        entry = {k: v for k, v in err.items() if k not in _VALUE_BEARING_KEYS}
        if isinstance(entry.get("msg"), str):
            # Belt: a validator's own message should not quote the value it
            # rejected, but the generic redactor costs nothing and covers the
            # ones written before this rule existed.
            entry["msg"] = sanitize_text(entry["msg"])
        safe.append(entry)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(safe)})


async def inter_agent_depth_exceeded(request: Request, exc) -> JSONResponse:
    """The named 403 for a chain-depth refusal (#2806, #2973).

    Same body and header as ``routers/chat.py::_raise_depth_exceeded_403``, for
    the routes that let ``InterAgentDepthExceeded`` propagate (loop start,
    schedule trigger, event emit). Each of those calls the guard after its
    access dependency has answered, so the 403 discloses nothing about whether
    a target exists (Invariant #8).
    """
    # Lazy: chat_execution_service pulls in the dispatch stack.
    from services.chat_execution_service import ERROR_CODE_HEADER

    detail = exc.detail()
    return JSONResponse(
        status_code=403,
        content={"detail": detail},
        headers={ERROR_CODE_HEADER: detail["error"]},
    )


async def skill_gate_error(request: Request, exc) -> JSONResponse:
    """A request that named a gated skill and was not dispatched
    (trinity-enterprise#751).

    `SkillApprovalRequired` → **202** with the pending body at the top level: the
    request was accepted for a decision, not run, so a client must never read it
    as an empty reply. `SkillGateRefused` → its own status with the body under
    `detail`, like any other refusal. Both carry the code on
    `X-Trinity-Error-Code`. The gate runs after each route's access dependency,
    so neither discloses whether an agent exists (Invariant #8).
    """
    from services.chat_execution_service import ERROR_CODE_HEADER
    from services.skill_gate_errors import SkillApprovalRequired

    detail = exc.detail()
    headers = {ERROR_CODE_HEADER: exc.code}
    if isinstance(exc, SkillApprovalRequired):
        return JSONResponse(status_code=202, content=detail, headers=headers)
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=headers)


async def secret_setting_value_error(request: Request, exc) -> JSONResponse:
    """The named 422 for a credential setting whose VALUE cannot be stored
    (#3325) — e.g. an unpaired UTF-16 surrogate in a pasted API key.

    The message is value-free by construction (``SecretSettingValueError`` names
    the key only and is raised ``from None``), so it is safe to return as is.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# --- trinity-enterprise#719: a refused human-only attempt is audited ----------

# At most one row per principal, method and route inside this window, in each
# worker process. An agent retrying a human-only route in a loop would otherwise
# add one synchronous insert per attempt to an append-only table kept for a year.
# Only a written row uses the window: a write that fails releases its key.
_REFUSAL_WINDOW_S = 60.0
# The row's three values whose length the request chooses: the concrete path,
# the agent name in it and the request id (`X-Request-ID`). Each is cut to this
# many characters, so no request can make those three fields any longer.
_CALLER_SIZED_MAX = 256
# The most (principal, method, route) keys held at once. An entry leaves only
# when its window ends, never early: forgetting one would let that principal
# write again inside its window. A key needs an authenticated principal and a
# matched route, so the set is bounded by the credentials refused in the last
# minute; if it is ever full, nothing new is recorded until entries expire, and
# that is logged once per window.
_REFUSAL_WINDOW_MAX = 65536
_recent_refusals: "OrderedDict[tuple, float]" = OrderedDict()
_window_full_logged_at: "float | None" = None
_clock = time.monotonic


def _route_template(request: Request) -> str:
    """The route the request matched (`/api/agents/{agent_name}/autonomy`), so
    one principal walking many agent names is still one key per route."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.scope.get("path") or ""


def _capped(value):
    """A caller-sized value cut to `_CALLER_SIZED_MAX` characters."""
    return value[:_CALLER_SIZED_MAX] if isinstance(value, str) else value


def _path_agent(request: Request):
    """The agent the route names, if any: `{agent_name}` anywhere, and `{name}`
    only as the segment after `/agents/` (`/api/agents/{name}/...`,
    `/api/nevermined/agents/{name}/config`); elsewhere a `{name}` is not an
    agent's."""
    params = request.scope.get("path_params") or {}
    if params.get("agent_name"):
        return params["agent_name"]
    if "/agents/{name}" in _route_template(request):
        return params.get("name")
    return None


def _refusal_actor(user) -> dict:
    """Who the row is filed against.

    The audit resolver ranks `actor_user` first and would record the OWNER for a
    key that resolves to them, so only a person is filed as `actor_user` (the
    person's own `user` key, refused on an INTERACTIVE route). An agent key is
    filed as its agent and the system key as `trinity-system`
    (`acting_agent_name`); a loopback as the agent it vouches for; any other key
    by its scope and key id. A principal that names neither an agent nor a key
    (an unvouched loopback) gets the audit resolver's platform actor, never a
    person. The owner rides as `actor_email`.
    """
    from dependencies import acting_agent_name, is_person_principal

    if is_person_principal(user) and not getattr(user, "vouched_source_agent", None):
        return {"actor_user": user}
    keyed = {
        "actor_email": getattr(user, "email", None),
        "mcp_key_id": getattr(user, "mcp_key_id", None),
        "mcp_key_name": getattr(user, "mcp_key_name", None),
        "mcp_scope": getattr(user, "mcp_scope", None),
    }
    agent = (
        acting_agent_name(user)
        or getattr(user, "agent_name", None)
        or getattr(user, "vouched_source_agent", None)
    )
    if agent:
        keyed["actor_agent_name"] = agent
    return keyed


def _first_in_window(key: tuple) -> bool:
    """True when `key` has no row inside the window and there is room to hold
    it; records it if so. Fails toward writing nothing: a full window suppresses
    a new key rather than forgetting an unexpired one."""
    global _window_full_logged_at
    now = _clock()
    while _recent_refusals:
        oldest_key, oldest_at = next(iter(_recent_refusals.items()))
        if now - oldest_at < _REFUSAL_WINDOW_S:
            break
        del _recent_refusals[oldest_key]
    if key in _recent_refusals:
        return False
    if len(_recent_refusals) >= _REFUSAL_WINDOW_MAX:
        if (
            _window_full_logged_at is None
            or now - _window_full_logged_at >= _REFUSAL_WINDOW_S
        ):
            _window_full_logged_at = now
            logger.warning(
                "human-only refusal audit window is full (%d keys); refusals "
                "are not recorded until entries expire",
                len(_recent_refusals),
            )
        return False
    _recent_refusals[key] = now
    return True


async def _record_human_only_refusal(request: Request, exc) -> None:
    from services.platform_audit_service import AuditEventType, platform_audit_service

    user = exc.principal
    route = _route_template(request)
    key = (
        getattr(user, "mcp_scope", None),
        getattr(user, "mcp_key_id", None),
        getattr(user, "agent_name", None),
        getattr(user, "vouched_source_agent", None),
        getattr(user, "id", None),
        request.method,
        route,
    )
    if not _first_in_window(key):
        return
    event_id = None
    try:
        # The details carry the method, the route and the rule: never the body,
        # the query or a header value.
        details = {"method": request.method, "route": route, "rule": exc.rule}
        if isinstance(exc.detail, dict) and exc.detail.get("code"):
            details["code"] = exc.detail["code"]
        if getattr(user, "is_event_loopback", False) is True or getattr(
            user, "vouched_source_agent", None
        ):
            details["loopback"] = True
        target = _capped(_path_agent(request))
        event_id = await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action="human_only_refused",
            source="api",
            **_refusal_actor(user),
            # the client address the app resolved, as on every audit row
            # (behind a trusted proxy, the client it saw; trinity-enterprise#873)
            actor_ip=request.client.host if request.client else None,
            target_type="agent" if target else None,
            target_id=target,
            # the routed path, never request.url.path (#3108)
            endpoint=_capped(request.scope.get("path")),
            request_id=_capped(getattr(request.state, "request_id", None)),
            details=details,
        )
    finally:
        # `log` returns None for a write it could not make. A row that was not
        # written does not use the window, so the next refusal tries again.
        if event_id is None:
            _recent_refusals.pop(key, None)


def _http_exception_answer(request: Request, exc):
    """The handler that answers every other `HTTPException`: the first one
    registered for a class above `HumanOnlyRefused` in its MRO, read from the
    table the app is dispatching with (`scope["starlette.exception_handlers"]`).
    FastAPI registers `http_exception_handler` for Starlette's `HTTPException`.

    A handler registered for the status code 403 would answer before this
    module is reached (Starlette checks status handlers first), and the refusal
    would go unrecorded; `test_ent719_human_only_refusal_audit.py` fails if the
    app ever registers one."""
    registered = request.scope.get("starlette.exception_handlers")
    handlers = registered[0] if registered else {}
    for cls in type(exc).__mro__[1:]:
        if cls in handlers:
            return handlers[cls]
    return http_exception_handler


async def human_only_refused(request: Request, exc) -> Response:
    """Record a refused human-only attempt once, then answer exactly as before
    (trinity-enterprise#719).

    `HumanOnlyRefused` is raised by the human-only primitives in
    `dependencies.py` (`require_person`, `assert_person`, `require_interactive`,
    `reject_non_interactive_principal`, `reject_non_person_principal`). The row
    is an `authorization` / `human_only_refused` audit entry naming who was
    refused, with the method, the route and the rule. It is best-effort and
    bounded: an audit failure is logged and never turns the 403 into a 500, a
    repeat inside the window writes nothing, and a failed write does not use the
    window. A site that already wrote its own row for the refusal marks it
    `audited`, and nothing more is written.

    The response comes from the handler the app registers for `HTTPException`
    (FastAPI's own `http_exception_handler` unless something replaced it), so
    the status, body and headers are what they were before the type existed.
    """
    if not getattr(exc, "audited", False) and request.scope.get("type") == "http":
        try:
            await _record_human_only_refusal(request, exc)
        except Exception:  # noqa: BLE001 — never turn the refusal into a 500
            logger.warning(
                "human-only refusal audit failed: rule=%s",
                getattr(exc, "rule", None),
                exc_info=True,
            )
    result = _http_exception_answer(request, exc)(request, exc)
    if inspect.isawaitable(result):
        result = await result
    return result
