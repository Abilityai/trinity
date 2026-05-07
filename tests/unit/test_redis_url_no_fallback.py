"""Lint-style regression for #645 (#589 follow-up).

`config.py` raises at import if `REDIS_URL` lacks credentials (#589 / PR #643).
Three backend services bypassed that gate by reading the env var directly
with an unauthenticated localhost fallback:

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")

This test enforces the single-source-of-truth principle: every backend
service must route Redis URL resolution through `config.REDIS_URL`. New
services that grow a `redis://` literal default — or a fresh
`os.getenv("REDIS_URL", ...)` fallback — trip these assertions.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _backend_services_dir() -> Path:
    """Locate src/backend/services across host and in-container layouts."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "src" / "backend" / "services",
        Path("/app/services"),  # trinity-backend container
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, Path(env_override) / "services")
    for c in candidates:
        if c.is_dir():
            return c
    raise RuntimeError(
        "Cannot locate src/backend/services (set TRINITY_BACKEND_PATH)"
    )


_SERVICES = _backend_services_dir()
_PY_FILES = sorted(p for p in _SERVICES.rglob("*.py") if "__pycache__" not in p.parts)


def _grep(pattern: re.Pattern) -> list[tuple[Path, int, str]]:
    """Return (path, lineno, line) for every match across the services tree."""
    hits: list[tuple[Path, int, str]] = []
    for path in _PY_FILES:
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                hits.append((path, lineno, line.strip()))
    return hits


# Direct env reads of REDIS_URL are forbidden — config.py is the canonical gate.
_GETENV_REDIS_URL = re.compile(r"""os\.getenv\s*\(\s*['"]REDIS_URL['"]""")

# Unauthenticated localhost literals embedded as defaults are forbidden — they
# silently bypass #589's fail-fast guard. Constructors must default to None
# and resolve via config.REDIS_URL when called with no argument.
_UNAUTH_REDIS_LITERAL = re.compile(r"""['"]redis://redis:6379['"]""")


class TestNoOsGetenvRedisUrl:
    def test_no_os_getenv_redis_url_under_services(self):
        hits = _grep(_GETENV_REDIS_URL)
        assert not hits, (
            "os.getenv(\"REDIS_URL\", ...) must not appear under "
            "src/backend/services/. Import REDIS_URL from config instead "
            "(see #645). Offending lines:\n"
            + "\n".join(f"  {p}:{n}: {s}" for p, n, s in hits)
        )


class TestNoUnauthRedisLiteralDefault:
    def test_no_unauthenticated_redis_localhost_literal(self):
        hits = _grep(_UNAUTH_REDIS_LITERAL)
        assert not hits, (
            "'redis://redis:6379' literal must not appear under "
            "src/backend/services/. It silently bypasses #589's fail-fast "
            "credentials guard (see #645). Use Optional[str] = None + "
            "lazy import of config.REDIS_URL inside the constructor. "
            "Offending lines:\n"
            + "\n".join(f"  {p}:{n}: {s}" for p, n, s in hits)
        )
