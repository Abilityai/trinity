"""
Tests for X-Forwarded-For rate limit bypass fix (issue #181 / pentest 3.2.4).

Covers:
- SMOKE: Per-token secondary rate limit is configured

The resolver's unit cases (trusted-proxy matching, forwarded-header walk) moved
to `tests/unit/test_ent873_client_ip_resolver.py` with the resolver itself
(`utils/client_ip.py`, trinity-enterprise#873) — this root file was never
collected by the per-PR unit job.

Run with: pytest tests/test_ip_rate_limit_fix.py -v
"""

import os
import sys
import importlib
import types
import pytest
import httpx
from unittest.mock import MagicMock, patch

BASE_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helpers to load the router module without a full FastAPI app
# ---------------------------------------------------------------------------

def _load_public_router(monkeypatch):
    """Import the public router and return its module.

    Uses ``monkeypatch`` for every ``sys.modules`` mutation so that pytest
    automatically restores the original module state after each test.  Without
    this, the minimal fastapi stub (which lacks ``status``, ``security``, etc.)
    would leak into subsequent test files and cause ImportErrors in any module
    that does ``from fastapi import status``.
    """
    backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "backend"))
    if backend_path not in sys.path:
        sys.path.insert(0, backend_path)

    # ------------------------------------------------------------------
    # Stub heavy dependencies so the module loads without a running app.
    #
    # IMPORTANT: "routers" must be stubbed as an empty package BEFORE the
    # first `import routers.public`.  Without this, Python evaluates
    # routers/__init__.py which eagerly imports routers.agents → fastapi,
    # which is not installed in the test venv.
    # ------------------------------------------------------------------
    if "routers" not in sys.modules:
        routers_pkg = types.ModuleType("routers")
        routers_pkg.__path__ = [os.path.join(backend_path, "routers")]
        routers_pkg.__package__ = "routers"
        monkeypatch.setitem(sys.modules, "routers", routers_pkg)

    # Stub fastapi. routers/public.py only needs APIRouter, Depends,
    # HTTPException, and Request at module load time.  We stub `dependencies`
    # directly (below) so the transitive chain that needs fastapi.status and
    # fastapi.security never runs.
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_mod.APIRouter = MagicMock(return_value=MagicMock())
    fastapi_mod.HTTPException = Exception
    fastapi_mod.Request = MagicMock()
    fastapi_mod.Depends = MagicMock()
    # `routers/public.py` now imports `services.rate_limiter`, which does
    # `from fastapi import HTTPException, status` at module load. The stub had
    # no `status`, so all 14 tests here errored at setup with "cannot import
    # name 'status' from 'fastapi'". The real constants module is a leaf
    # (plain ints), so hand it through rather than mocking status codes.
    from starlette import status as _status
    fastapi_mod.status = _status
    fastapi_mod.exceptions = types.ModuleType("fastapi.exceptions")
    fastapi_mod.routing = types.ModuleType("fastapi.routing")
    monkeypatch.setitem(sys.modules, "fastapi", fastapi_mod)
    monkeypatch.setitem(sys.modules, "fastapi.exceptions", fastapi_mod.exceptions)

    fr_mod = types.ModuleType("fastapi.responses")
    fr_mod.StreamingResponse = MagicMock()
    monkeypatch.setitem(sys.modules, "fastapi.responses", fr_mod)

    # Stub application modules that routers/public.py imports directly.
    # `database` and `dependencies` must be unconditional (not setdefault)
    # because other test files may have installed incompatible stubs.
    monkeypatch.setitem(sys.modules, "database", MagicMock())
    # `unsafe=True` on `dependencies` is LOAD-BEARING, not tidy-up (#2241).
    # MagicMock raises AttributeError for any attribute whose name starts with
    # `assert` — a typo guard against `mock.assert_called_once` — and the #1310
    # imperative auth-guard family is named exactly that: `assert_admin`,
    # `assert_agent_access`, `assert_agent_owner`, `assert_owns_or_admin`,
    # `assert_owns`. `routers/public.py` does `from dependencies import
    # get_current_user, assert_owns`, so a bare MagicMock() here fails at IMPORT
    # time with "'assert_owns' is not a valid assertion", taking all 14 tests in
    # this file down as setup errors before one of them runs. `unsafe=True` exists
    # precisely to disable that prefix check.
    monkeypatch.setitem(sys.modules, "dependencies", MagicMock(unsafe=True))
    monkeypatch.setitem(sys.modules, "models", MagicMock())

    stubs = {
        "routers.auth": MagicMock(
            check_login_rate_limit=MagicMock(),
            record_login_attempt=MagicMock(),
        ),
        "services.docker_service": MagicMock(),
        "services.email_service": MagicMock(),
        "services.task_execution_service": MagicMock(),
        "services.platform_prompt_service": MagicMock(),
        "services.settings_service": MagicMock(),
        "services.upload_service": MagicMock(),
    }
    for name, stub in stubs.items():
        # Use unconditional setitem (not setdefault) so that module-level stubs
        # installed by other test files at import time don't shadow our stubs.
        # monkeypatch restores the original value after each test.
        monkeypatch.setitem(sys.modules, name, stub)

    # Force a fresh load of routers.public every call so module-level state
    # is reset cleanly.
    monkeypatch.delitem(sys.modules, "routers.public", raising=False)
    import routers.public as pub  # noqa: E402
    return pub


# ---------------------------------------------------------------------------
# SMOKE: Per-token rate limit enforced via API
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_headers():
    password = os.getenv("TRINITY_TEST_PASSWORD", "password")
    resp = httpx.post(f"{BASE_URL}/api/token", data={"username": "admin", "password": password})
    if resp.status_code != 200:
        pytest.skip("Could not authenticate")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestPerTokenRateLimit:
    """Verify the per-token secondary rate limit constant is configured."""

    @pytest.fixture(autouse=True)
    def load_module(self, monkeypatch):
        self.pub = _load_public_router(monkeypatch)

    def test_max_messages_per_token_constant_exists(self):
        assert hasattr(self.pub, "MAX_CHAT_MESSAGES_PER_TOKEN")
        assert self.pub.MAX_CHAT_MESSAGES_PER_TOKEN > 0

    def test_rate_limit_constants_reasonable(self):
        """Per-token limit must be >= per-IP limit (token covers all IPs)."""
        assert self.pub.MAX_CHAT_MESSAGES_PER_TOKEN >= self.pub.MAX_CHAT_MESSAGES_PER_IP
