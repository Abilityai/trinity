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
