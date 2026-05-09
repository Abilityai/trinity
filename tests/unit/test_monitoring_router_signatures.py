"""
Regression tests for monitoring router call-site signatures (#682).

Bug: `routers/monitoring.py` was calling
`get_accessible_agents(current_user.email, all_agent_names)` after the
helper was refactored to `get_accessible_agents(current_user: User)`.
Admins skipped the call (early-branch), so the bug only surfaced for
non-admin users as a 500 TypeError on `GET /api/monitoring/status`.

These tests pin two things:

1. The canonical helper signature stays `(current_user: User) -> list`.
2. `get_fleet_status` invokes the helper with a single positional
   `User` arg and returns a 200-shape `FleetHealthStatus` for a
   non-admin caller — i.e. the historical TypeError can no longer fire.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Point the backend at an ephemeral SQLite file BEFORE any backend module
# imports — database.py tries to mkdir /data on import otherwise.
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_monitoring_router.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

def _find_backend_root() -> Path:
    """Locate the backend source tree across host and in-container layouts."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "src" / "backend",  # host
        Path("/app"),  # trinity-backend container
    ]
    env_override = os.environ.get("TRINITY_BACKEND_PATH")
    if env_override:
        candidates.insert(0, Path(env_override))
    for c in candidates:
        if (c / "routers" / "monitoring.py").exists():
            return c
    raise RuntimeError(
        "Cannot locate backend source tree (set TRINITY_BACKEND_PATH)"
    )


_BACKEND = _find_backend_root()
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _stub_passlib():
    """passlib import path resolution stub — bcrypt isn't needed here."""
    if "passlib" in sys.modules:
        return
    passlib = types.ModuleType("passlib")
    context = types.ModuleType("passlib.context")

    class _CryptContext:
        def __init__(self, **_):
            pass

        def hash(self, pw):
            return f"stub${pw}"

        def verify(self, pw, hashed):
            return hashed == f"stub${pw}"

    context.CryptContext = _CryptContext
    sys.modules["passlib"] = passlib
    sys.modules["passlib.context"] = context


_stub_passlib()


pytestmark = pytest.mark.unit


# ── Importlib-load routers/monitoring.py without dragging in routers/__init__ ─

def _load_monitoring_router():
    """Load routers/monitoring.py directly with scoped dependency stubs.

    Going through `from routers import monitoring` would import 50+
    unrelated routers via routers/__init__.py.

    Several test files collected alphabetically before this one permanently
    install minimal stubs in sys.modules at collection time:

    * test_inter_agent_timeout_unit.py: fake `dependencies` without require_admin.
    * test_start_agent_skip_inject.py: bare services.agent_service without get_accessible_agents.
    * test_inject_assigned_credentials.py: fastapi Mock, so @router.get() returns
      a Mock instead of the original function.

    We use patch.dict to install correct stubs for the duration of exec_module
    and recover real fastapi by briefly evicting the polluted entry.

    Type notes:
    - AuthorizedAgentByName is Annotated[str, Depends(func)] → use `str` so
      FastAPI treats it as a plain path parameter, not an unresolvable Pydantic type.
    - require_admin is a Depends callable → use a plain lambda.
    """
    _deps = types.ModuleType("dependencies")
    _deps.get_current_user = lambda: None
    _deps.require_admin = lambda: None
    _deps.AuthorizedAgentByName = str
    _deps.OwnedAgentByName = str
    _deps.get_authorized_agent = lambda: None

    _agent_svc = types.ModuleType("services.agent_service")
    _agent_svc.get_accessible_agents = MagicMock()

    _mon_svc = types.ModuleType("services.monitoring_service")
    _mon_svc.perform_health_check = MagicMock()
    _mon_svc.perform_fleet_health_check = MagicMock()
    _mon_svc.get_monitoring_service = MagicMock()
    _mon_svc.start_monitoring_service = MagicMock()
    _mon_svc.stop_monitoring_service = MagicMock()
    _mon_svc.DEFAULT_CONFIG = {}

    # test_inject_assigned_credentials.py permanently overwrites sys.modules['fastapi']
    # with a Mock at collection time (sys.modules.update), causing @router.get() to
    # return a Mock instead of the original function.  Evict the polluted entry briefly
    # so `import fastapi` reloads from disk; then restore the Mock for other tests.
    _saved_fastapi = sys.modules.pop("fastapi", None)
    import fastapi as _real_fastapi  # noqa: PLC0415
    if _saved_fastapi is not None:
        sys.modules["fastapi"] = _saved_fastapi

    path = _BACKEND / "routers" / "monitoring.py"
    spec = importlib.util.spec_from_file_location("routers.monitoring", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["routers.monitoring"] = mod
    with patch.dict(sys.modules, {
        "dependencies": _deps,
        "services.agent_service": _agent_svc,
        "services.monitoring_service": _mon_svc,
        "fastapi": _real_fastapi,
    }):
        spec.loader.exec_module(mod)
    return mod


monitoring_router = _load_monitoring_router()


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakeAgent:
    """Stand-in for the dataclass returned by list_all_agents_fast()."""

    def __init__(self, name: str, status: str = "running"):
        self.name = name
        self.status = status


def _make_user(role: str, *, username: str = "u", user_id: int = 1, email: str = "u@x.test"):
    """Construct a real db_models.User instance."""
    from db_models import User  # backend's pydantic User

    now = datetime.now(timezone.utc)
    return User(
        id=user_id,
        username=username,
        role=role,
        email=email,
        created_at=now,
        updated_at=now,
    )


def _run(coro):
    return asyncio.run(coro)


# ── Signature pin ────────────────────────────────────────────────────────────

class TestGetAccessibleAgentsSignature:
    """The helper must accept exactly one User argument.

    If this changes, every router call site needs auditing.
    """

    def test_helper_takes_single_user_param(self):
        # Load helpers.py DIRECTLY via importlib, bypassing services/agent_service/
        # __init__.py.  test_start_agent_skip_inject.py permanently registers
        # sys.modules['services.agent_service'] as a bare stub, so a normal
        # `from services.agent_service.helpers import …` picks up the stub's
        # MagicMock attribute rather than the real function.
        helpers_path = str(_BACKEND / "services" / "agent_service" / "helpers.py")
        _spec = importlib.util.spec_from_file_location(
            "agent_service_helpers_sigcheck", helpers_path
        )
        _hmod = importlib.util.module_from_spec(_spec)
        _docker_stub = types.SimpleNamespace(
            docker_client=MagicMock(),
            list_all_agents=MagicMock(),
            list_all_agents_fast=MagicMock(),
            get_agent_container=MagicMock(),
        )
        _settings_stub = types.SimpleNamespace(
            get_anthropic_api_key=lambda: "sk-test",
            get_github_pat=lambda: None,
            get_agent_full_capabilities=lambda: False,
            settings_service=MagicMock(),
        )
        with patch.dict(sys.modules, {
            "services.docker_service": _docker_stub,
            "services.docker_utils": types.SimpleNamespace(volume_get=MagicMock()),
            "services.settings_service": _settings_stub,
            "database": types.SimpleNamespace(db=MagicMock()),
        }):
            _spec.loader.exec_module(_hmod)

        get_accessible_agents = _hmod.get_accessible_agents
        sig = inspect.signature(get_accessible_agents)
        params = list(sig.parameters.values())
        assert len(params) == 1, (
            f"get_accessible_agents must take exactly one parameter, "
            f"got {[p.name for p in params]}"
        )
        assert params[0].name == "current_user"


# ── Behavioural regression for #682 ─────────────────────────────────────────

class TestGetFleetStatusNonAdmin:
    """`get_fleet_status` must work for non-admin users (#682)."""

    def test_non_admin_does_not_raise_type_error(self, monkeypatch):
        """The bug was: 500 TypeError on the non-admin branch."""
        # Stub list_all_agents_fast to return a fixed fleet.
        agents = [_FakeAgent("agent-a"), _FakeAgent("agent-b")]

        fake_docker = types.ModuleType("services.docker_service")
        fake_docker.list_all_agents_fast = MagicMock(return_value=agents)
        monkeypatch.setitem(sys.modules, "services.docker_service", fake_docker)

        # Spy on get_accessible_agents to verify call shape, return a subset.
        captured_calls = []

        def fake_get_accessible(*args, **kwargs):
            captured_calls.append((args, kwargs))
            return [{"name": "agent-a"}]  # alice owns just agent-a

        monkeypatch.setattr(monitoring_router, "get_accessible_agents", fake_get_accessible)

        # Stub db calls for the non-admin path.
        fake_db = MagicMock()
        fake_db.get_all_latest_health_checks = MagicMock(return_value={})
        fake_db.get_health_summary = MagicMock(
            return_value={"healthy": 0, "degraded": 0, "unhealthy": 0, "critical": 0, "unknown": 1}
        )
        monkeypatch.setattr(monitoring_router, "db", fake_db)

        # Stub the monitoring service.
        fake_service = MagicMock()
        fake_service.is_running = False
        monkeypatch.setattr(monitoring_router, "get_monitoring_service", lambda: fake_service)

        non_admin = _make_user("user", username="alice", email="alice@example.test")

        # Pre-fix this raised TypeError. Post-fix it returns FleetHealthStatus.
        result = _run(monitoring_router.get_fleet_status(current_user=non_admin))

        # 1. No exception, valid response shape.
        assert result.summary.total_agents == 1
        assert [a.name for a in result.agents] == ["agent-a"]

        # 2. Helper called exactly once with one positional arg, the User itself
        #    (not user.email and not a separate agent-list parameter).
        assert len(captured_calls) == 1
        args, kwargs = captured_calls[0]
        assert kwargs == {}
        assert len(args) == 1, (
            f"get_accessible_agents called with {len(args)} positional args; "
            f"the #682 regression was passing 2. Args: {args!r}"
        )
        assert args[0] is non_admin

    def test_admin_branch_skips_helper_and_keeps_full_fleet(self, monkeypatch):
        """Admins must not hit get_accessible_agents — they see everything."""
        agents = [_FakeAgent("agent-a"), _FakeAgent("agent-b")]

        fake_docker = types.ModuleType("services.docker_service")
        fake_docker.list_all_agents_fast = MagicMock(return_value=agents)
        monkeypatch.setitem(sys.modules, "services.docker_service", fake_docker)

        helper_calls = []

        def fake_get_accessible(*args, **kwargs):
            helper_calls.append((args, kwargs))
            return []

        monkeypatch.setattr(monitoring_router, "get_accessible_agents", fake_get_accessible)

        fake_db = MagicMock()
        fake_db.get_all_latest_health_checks = MagicMock(return_value={})
        fake_db.get_health_summary = MagicMock(
            return_value={"healthy": 0, "degraded": 0, "unhealthy": 0, "critical": 0, "unknown": 2}
        )
        monkeypatch.setattr(monitoring_router, "db", fake_db)

        fake_service = MagicMock()
        fake_service.is_running = True
        monkeypatch.setattr(monitoring_router, "get_monitoring_service", lambda: fake_service)

        # Admin path also touches services.agent_client.get_all_circuit_states.
        fake_client = types.ModuleType("services.agent_client")
        fake_client.get_all_circuit_states = MagicMock(return_value={})
        monkeypatch.setitem(sys.modules, "services.agent_client", fake_client)

        admin = _make_user("admin", username="admin")

        result = _run(monitoring_router.get_fleet_status(current_user=admin))

        assert helper_calls == [], "admins must bypass get_accessible_agents"
        assert {a.name for a in result.agents} == {"agent-a", "agent-b"}
