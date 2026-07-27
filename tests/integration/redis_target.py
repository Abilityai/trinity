"""Pure Redis-target helpers for the integration suite (#1775).

Separate from ``tests/integration/conftest.py`` on purpose: that conftest has a
deliberate import-time side effect (it resolves and applies the Redis target
before any test module is imported), so importing it from elsewhere — e.g. the
``tests/unit`` guard that unit-tests the masking — would re-point
``config.REDIS_URL`` in a session that never asked for it. Everything in this
module is pure: no I/O, no environment reads, no imports beyond stdlib.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

# THE one hardcoded local Redis endpoint for the integration suite (AC2). It
# matches the loopback publish in docker-compose.yml (`127.0.0.1:6379:6379`)
# used by the standard `./scripts/deploy/start.sh` dev stack. Any other target —
# sibling stack, CI on an ephemeral port — arrives as an exported REDIS_URL.
LOCAL_REDIS_ENDPOINT = "localhost:6379"
REDIS_ACL_USER = "backend"


def mask_redis_url(url: str | None) -> str:
    """Redact the password of a ``redis://`` URL, keeping everything diagnostic.

    These strings reach pytest output, CI logs and — since #1775 turns
    Redis-unreachable skips into failures — retained failure reports, in a
    PUBLIC repo. The host, port and exception text are what identify a
    misconfiguration; the password never is.

    EVERY parse step is inside the ``try``, not just ``urlsplit``. ``urlsplit``
    is lazy: it accepts ``redis://u:pw@host:notaport`` and only raises when
    ``.port`` is read. With that read outside the guard, this function raised on
    a malformed URL — and because callers pass the raw URL as an argument, the
    escaping ValueError made pytest render the caller's frame, printing the very
    password this function exists to hide. A masker must never be able to throw
    on the input it is most needed for.
    """
    if not url:
        return "<unset>"
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{parts.username or ''}:***@{host}{port}"
        return urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )
    except ValueError:
        # Credential-bearing but unparseable: say nothing beyond that.
        return "<unparseable REDIS_URL>"


def build_redis_url(password: str, endpoint: str = LOCAL_REDIS_ENDPOINT) -> str:
    """Build the backend-ACL Redis URL for a derived (non-caller-supplied) target."""
    return f"redis://{REDIS_ACL_USER}:{password}@{endpoint}"
