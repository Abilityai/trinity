"""
Gated skills — the three wiring lines the #3208 validation found unpinned
(trinity#3274, item 4). Each test reaches the LINE, through the code that runs it:

1. `dependencies.get_current_user` stamps `is_event_loopback=bool(loopback)` on
   the EVT-001 loopback principal. Without it the loopback (which resolves to
   `admin`) is a person and could approve its own gated request.
2. `main._start_maintenance_services` calls `register_ending_observer()`.
   Without it an approval is recorded and nothing runs until the sweep.
3. `OperatorQueueSyncService._poll_cycle` awaits `skill_gate_service.sweep()`.
   Without it an ending no observer saw is never consumed.

Mutation-proved: deleting each line turns its test red.
"""
import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. dependencies.py — the loopback flag
# ---------------------------------------------------------------------------

def _resolve(token, *, method="POST", path="/api/agents/orch/task"):
    """`test_ent614_source_agent_attribution._resolve_principal`."""
    import dependencies as dep

    req = SimpleNamespace(method=method, scope={"path": path})
    with patch.object(dep, "db") as db:
        db.get_user_by_username.return_value = {
            "id": 1, "username": "admin", "email": "admin@example.com", "role": "admin",
            "suspended_at": None}
        return asyncio.run(dep.get_current_user(req, token))


def test_the_loopback_token_resolves_to_a_principal_that_is_never_a_person():
    from services import event_dispatch_service as eds
    from services.skill_gate_service import requester_from_principal

    user = _resolve(eds._get_internal_token("worker-a"))
    assert user.is_event_loopback is True
    requester = requester_from_principal(user)
    assert requester.is_person is False and requester.key == "event-loopback"


def test_an_ordinary_session_token_is_a_person():
    from datetime import datetime, timedelta

    from config import ALGORITHM, SECRET_KEY
    from jose import jwt
    from services.skill_gate_service import requester_from_principal

    tok = jwt.encode({"sub": "admin", "exp": datetime.utcnow() + timedelta(minutes=5)},
                     SECRET_KEY, algorithm=ALGORITHM)
    user = _resolve(tok, method="GET", path="/api/agents")
    assert user.is_event_loopback is False
    assert requester_from_principal(user).is_person is True


# ---------------------------------------------------------------------------
# 2. main.py — the ending observer is registered at startup
# ---------------------------------------------------------------------------

def _maintenance_phase():
    """`_start_maintenance_services` sliced out of `main.py` and compiled on
    its own: importing `main` builds the whole app (and needs the OTel
    exporter). Its globals are stub services plus the real `logger`."""
    source = (_BACKEND / "main.py").read_text(encoding="utf-8")
    fns = [n for n in ast.parse(source).body
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "_start_maintenance_services"]
    assert len(fns) == 1, "main.py no longer defines _start_maintenance_services"
    module = ast.Module(body=fns, type_ignores=[])
    started = []
    stub = lambda name: SimpleNamespace(start=lambda: started.append(name))  # noqa: E731
    namespace = {
        "logger": MagicMock(),
        "log_archive_service": stub("log_archive"),
        "audit_retention_service": stub("audit_retention"),
        "db_vacuum_service": stub("db_vacuum"),
        "db_backup_service": stub("db_backup"),
        "operator_queue_service": stub("operator_queue"),
    }
    exec(compile(module, str(_BACKEND / "main.py"), "exec"), namespace)
    return namespace["_start_maintenance_services"], started, namespace["logger"]


def test_startup_registers_the_skill_gate_ending_observer(monkeypatch):
    import services.ask_service as ask_service
    import services.skill_gate_service as sgs

    monkeypatch.setattr(ask_service, "_observers", [])
    phase, started, logger = _maintenance_phase()
    asyncio.run(phase())
    assert "operator_queue" in started                      # the slice really ran
    assert ask_service._observers == [sgs.on_ending]
    logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# 3. operator_queue_service.py — the poll cycle runs the sweep
# ---------------------------------------------------------------------------

def test_every_leader_poll_cycle_runs_the_skill_gate_sweep(monkeypatch):
    import services
    import services.operator_queue_service as oqs

    sweep = AsyncMock()
    fake_gate = SimpleNamespace(sweep=sweep)
    # `_poll_cycle` does `from services import skill_gate_service` at call time:
    # own both the package attribute and the `sys.modules` key (#1446 / #1595).
    monkeypatch.setattr(services, "skill_gate_service", fake_gate, raising=False)
    monkeypatch.setitem(sys.modules, "services.skill_gate_service", fake_gate)

    db = MagicMock()
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.mark_operator_queue_undelivered_for_stopped_agents.return_value = []
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "ask_service", SimpleNamespace(expire=lambda: SimpleNamespace(rows=[])))
    monkeypatch.setitem(sys.modules, "services.docker_service",
                        SimpleNamespace(agent_container_states=lambda: {}))
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(oqs, "_websocket_manager", ws)
    svc = oqs.OperatorQueueSyncService()
    svc._try_acquire_leadership = lambda: True
    svc._sync_agent = AsyncMock()

    asyncio.run(svc._poll_cycle())
    sweep.assert_awaited_once()
