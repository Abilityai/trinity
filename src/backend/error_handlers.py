"""
App-level exception handlers (trinity-enterprise#109).

Separate from ``main.py`` for two reasons: ``main`` drags in the lifespan, every
router, OpenTelemetry and a live DB/Redis, so a handler defined there cannot be
unit-tested without standing the whole platform up; and a handler is a policy
about the API's *output shape*, which is worth being able to read in one place.
"""

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from utils.credential_sanitizer import sanitize_text

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
