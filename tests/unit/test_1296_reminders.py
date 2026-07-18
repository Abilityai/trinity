"""Agent self-reminders — backend unit coverage (#1296).

Covers the migration/tables.py accessor guard, the three trigger constants, the
create-model XOR, the db layer (round-trip, tenant-scoped by-id, cancel CAS,
retention), the create-idempotency key derivation, the reminder_service bounds,
and the router self-gate / connector-reject / tenant-scope / idempotency edges.

Router endpoints are exercised by invoking the async functions directly (the
reports-test pattern) — ``name`` is the already-resolved ``AuthorizedAgent``
string and ``current_user`` a lightweight stand-in; no TestClient/auth stack.
The DB facade points at the conftest-migrated unit-test SQLite (agent_reminders
present after init_schema).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load():
    try:
        from fastapi import HTTPException  # noqa: F401
        import models  # noqa: F401
        from routers import reminders  # noqa: F401
        from services import reminder_service, idempotency_service  # noqa: F401
        from database import db  # noqa: F401
    except ImportError:
        pytest.skip("backend venv required")
    return None


def _user(agent_name=None, uid=1, email="u@example.com", connector_agent=None):
    return SimpleNamespace(
        id=uid,
        agent_name=agent_name,
        connector_agent=connector_agent,
        email=email,
        username="u",
    )


def _agent():
    """A unique agent name per test so idempotency rows never collide."""
    return f"rem-agent-{uuid.uuid4().hex[:8]}"


def _create(a, user, data, *, idem=None, src=None, keyid=None):
    """Invoke the create endpoint directly, passing the FastAPI-injected Header
    params explicitly (a direct call bypasses dependency resolution, so a bare
    ``Header(None)`` default would otherwise be a Header OBJECT, not None)."""
    from routers import reminders
    return asyncio.run(
        reminders.create_reminder_endpoint(
            data, name=a, current_user=user,
            idempotency_key=idem, x_source_agent=src, x_mcp_key_id=keyid,
        )
    )


def _list(a, user, status="pending"):
    from routers import reminders
    return asyncio.run(
        reminders.list_reminders_endpoint(name=a, current_user=user, status_filter=status)
    )


# ---------------------------------------------------------------------------
# Migration + tables.py accessor guard (the real guard schema-parity misses)
# ---------------------------------------------------------------------------

def test_agent_reminders_present_and_tables_py_columns_match():
    _load()
    from sqlalchemy import inspect, select
    from db.engine import get_engine
    from db.tables import agent_reminders

    eng = get_engine()
    insp = inspect(eng)
    assert insp.has_table("agent_reminders")
    db_cols = {c["name"] for c in insp.get_columns("agent_reminders")}
    tbl_cols = {c.name for c in agent_reminders.columns}
    assert db_cols == tbl_cols, f"tables.py drift: {tbl_cols ^ db_cols}"
    # Live select() of EVERY column — a mistyped/missing tables.py Column that
    # passes schema-parity (which never imports tables.py) detonates here.
    with eng.connect() as conn:
        for col in agent_reminders.columns:
            conn.execute(select(col).limit(1))


# ---------------------------------------------------------------------------
# The three triggered_by="reminder" constants (silent-failure trap)
# ---------------------------------------------------------------------------

def test_reminder_in_autonomous_triggers():
    _load()
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS
    assert "reminder" in _AUTONOMOUS_TRIGGERS


def test_reminder_in_valid_triggers():
    _load()
    from routers.executions import _VALID_TRIGGERS
    assert "reminder" in _VALID_TRIGGERS


def test_reminder_bucket_is_reminders():
    _load()
    from db.schedules import _bucket_for_trigger, _BUCKET_ORDER
    assert _bucket_for_trigger("reminder") == "Reminders"
    assert "Reminders" in _BUCKET_ORDER


# ---------------------------------------------------------------------------
# ReminderCreate — fire_at XOR delay_seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs,ok",
    [
        ({"message": "hi", "delay_seconds": 120}, True),
        ({"message": "hi", "fire_at": "2026-08-01T10:00:00Z"}, True),
        ({"message": "hi"}, False),                                    # neither
        ({"message": "hi", "delay_seconds": 120, "fire_at": "2026-08-01T10:00:00Z"}, False),  # both
        ({"message": "hi", "fire_at": "not-a-date"}, False),           # bad ISO
        ({"message": "hi", "delay_seconds": 0}, False),                # gt=0
        ({"message": "", "delay_seconds": 120}, False),                # empty message
    ],
)
def test_reminder_create_xor_validation(kwargs, ok):
    _load()
    from models import ReminderCreate
    from pydantic import ValidationError
    try:
        ReminderCreate(**kwargs)
        got = True
    except ValidationError:
        got = False
    assert got == ok


def test_raw_fire_spec():
    _load()
    from models import ReminderCreate
    assert ReminderCreate(message="x", delay_seconds=120).raw_fire_spec() == "delay_seconds=120"
    assert (
        ReminderCreate(message="x", fire_at="2026-08-01T10:00:00Z").raw_fire_spec()
        == "fire_at=2026-08-01T10:00:00Z"
    )


def test_derive_reminder_key_stable_and_raw():
    _load()
    from services import idempotency_service as idem
    # Stable for identical raw input.
    assert idem.derive_reminder_key("a", "m", "delay_seconds=120") == idem.derive_reminder_key(
        "a", "m", "delay_seconds=120"
    )
    # Distinct message / spec / agent → distinct key.
    assert idem.derive_reminder_key("a", "m", "delay_seconds=120") != idem.derive_reminder_key(
        "a", "m", "delay_seconds=121"
    )
    assert idem.derive_reminder_key("a", "m", "delay_seconds=120") != idem.derive_reminder_key(
        "b", "m", "delay_seconds=120"
    )


# ---------------------------------------------------------------------------
# DB layer — round-trip, tenant-scope, cancel CAS
# ---------------------------------------------------------------------------

def test_db_round_trip_and_tenant_scope():
    _load()
    from database import db
    a = _agent()
    r = db.insert_reminder(a, "ping", "2026-08-01T10:00:00.000000Z",
                           owner_id=5, created_by_email="x@y.z", source_agent_name=a)
    assert r["id"].startswith("rem_")
    assert r["status"] == "pending"
    assert db.count_pending_reminders(a) == 1
    assert [x["id"] for x in db.list_reminders(a)] == [r["id"]]
    # tenant-scoped by-id: another agent cannot read this id.
    assert db.get_reminder("someone-else", r["id"]) is None
    assert db.get_reminder(a, r["id"])["id"] == r["id"]


def test_cancel_cas_outcomes():
    _load()
    from database import db
    a = _agent()
    r = db.insert_reminder(a, "x", "2026-08-01T10:00:00.000000Z")
    assert db.cancel_reminder(a, r["id"]) == "cancelled"
    assert db.cancel_reminder(a, r["id"]) == "already_cancelled"     # idempotent
    assert db.cancel_reminder("other", r["id"]) == "not_found"       # tenant-scope
    assert db.cancel_reminder(a, "rem_nonexistent") == "not_found"
    assert db.count_pending_reminders(a) == 0


def test_cancel_conflict_on_non_pending():
    _load()
    from database import db
    from db.engine import get_engine
    from db.tables import agent_reminders
    from sqlalchemy import update
    a = _agent()
    r = db.insert_reminder(a, "x", "2026-08-01T10:00:00.000000Z")
    # Simulate the scheduler flipping it to firing.
    with get_engine().begin() as c:
        c.execute(update(agent_reminders).where(agent_reminders.c.id == r["id"]).values(status="firing"))
    assert db.cancel_reminder(a, r["id"]) == "conflict"


def test_allowed_tools_round_trip():
    _load()
    from database import db
    a = _agent()
    r = db.insert_reminder(a, "x", "2026-08-01T10:00:00.000000Z", allowed_tools=["Bash", "Read"])
    got = db.get_reminder(a, r["id"])
    assert got["allowed_tools"] == ["Bash", "Read"]


# ---------------------------------------------------------------------------
# Retention — terminal rows pruned, pending/firing preserved
# ---------------------------------------------------------------------------

def test_retention_prunes_terminal_preserves_pending():
    _load()
    from database import db
    from db.engine import get_engine
    from db.tables import agent_reminders
    from sqlalchemy import update
    a = _agent()
    old = "2020-01-01T00:00:00.000000Z"
    fired = db.insert_reminder(a, "fired", "2026-08-01T10:00:00.000000Z")
    cancelled = db.insert_reminder(a, "cancelled", "2026-08-01T10:00:00.000000Z")
    failed = db.insert_reminder(a, "failed", "2026-08-01T10:00:00.000000Z")
    pending = db.insert_reminder(a, "pending", "2026-08-01T10:00:00.000000Z")
    firing = db.insert_reminder(a, "firing", "2026-08-01T10:00:00.000000Z")
    with get_engine().begin() as c:
        c.execute(update(agent_reminders).where(agent_reminders.c.id == fired["id"]).values(status="fired", created_at=old))
        c.execute(update(agent_reminders).where(agent_reminders.c.id == cancelled["id"]).values(status="cancelled", created_at=old))
        c.execute(update(agent_reminders).where(agent_reminders.c.id == failed["id"]).values(status="failed", created_at=old))
        c.execute(update(agent_reminders).where(agent_reminders.c.id == pending["id"]).values(created_at=old))
        c.execute(update(agent_reminders).where(agent_reminders.c.id == firing["id"]).values(status="firing", created_at=old))
    # count candidates == 3 (the three terminal rows), pending/firing excluded.
    assert db.count_agent_reminders_candidates(90, 1000) >= 3
    pruned = db.prune_agent_reminders(90, 1000)
    assert pruned >= 3
    remaining = {x["status"] for x in db.list_reminders(a, status="all")}
    assert remaining <= {"pending", "firing"}
    assert db.get_reminder(a, pending["id"]) is not None
    assert db.get_reminder(a, firing["id"]) is not None
    assert db.get_reminder(a, fired["id"]) is None


# ---------------------------------------------------------------------------
# reminder_service bounds
# ---------------------------------------------------------------------------

def test_service_min_delay_rejected(monkeypatch):
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from fastapi import HTTPException
    a = _agent()
    with pytest.raises(HTTPException) as exc:
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=10),  # < 60s min
            owner_id=1, created_by_email="u@e.z",
        )
    assert exc.value.status_code == 400


def test_service_max_delay_rejected():
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from fastapi import HTTPException
    a = _agent()
    with pytest.raises(HTTPException) as exc:
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=99_999_999),  # > 30d
            owner_id=1, created_by_email="u@e.z",
        )
    assert exc.value.status_code == 400


def test_service_timeout_above_agent_cap_rejected():
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from fastapi import HTTPException
    a = _agent()
    # No agent_ownership row → default cap 3600; 5000 > cap → 400.
    with pytest.raises(HTTPException) as exc:
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=120, timeout_seconds=5000),
            owner_id=1, created_by_email="u@e.z",
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "reminder_timeout_exceeds_agent_cap"


def test_service_pending_cap_429(monkeypatch):
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from fastapi import HTTPException
    monkeypatch.setattr(reminder_service, "MAX_PENDING_REMINDERS_PER_AGENT", 2)
    a = _agent()
    for _ in range(2):
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=120),
            owner_id=1, created_by_email="u@e.z",
        )
    with pytest.raises(HTTPException) as exc:
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=120),
            owner_id=1, created_by_email="u@e.z",
        )
    assert exc.value.status_code == 429


def test_service_daily_cap_429(monkeypatch):
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from fastapi import HTTPException
    monkeypatch.setattr(reminder_service, "MAX_REMINDERS_PER_AGENT_PER_DAY", 3)
    a = _agent()
    for _ in range(3):
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=120),
            owner_id=1, created_by_email="u@e.z",
        )
    with pytest.raises(HTTPException) as exc:
        reminder_service.create_reminder(
            a, ReminderCreate(message="x", delay_seconds=120),
            owner_id=1, created_by_email="u@e.z",
        )
    assert exc.value.status_code == 429


def test_service_happy_path_inserts_pending():
    _load()
    from services import reminder_service
    from models import ReminderCreate
    from database import db
    a = _agent()
    r = reminder_service.create_reminder(
        a, ReminderCreate(message="hello", delay_seconds=300),
        owner_id=7, created_by_email="u@e.z", source_agent_name=a, source_mcp_key_id="k1",
    )
    assert r["status"] == "pending"
    assert r["owner_id"] == 7
    assert r["source_mcp_key_id"] == "k1"
    assert db.count_pending_reminders(a) == 1


# ---------------------------------------------------------------------------
# Router — self-gate (AC #5), connector reject, tenant-scope, idempotency edges
# ---------------------------------------------------------------------------

def test_router_self_gate_blocks_sibling():
    _load()
    from routers import reminders
    from models import ReminderCreate
    from fastapi import HTTPException
    data = ReminderCreate(message="x", delay_seconds=120)
    # Agent-scoped key bound to "other" against path "a1" → 403 (model the ATTACK).
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reminders.create_reminder_endpoint(
            data, name="a1", current_user=_user(agent_name="other")))
    assert exc.value.status_code == 403


def test_router_connector_key_rejected():
    _load()
    from routers import reminders
    from models import ReminderCreate
    from fastapi import HTTPException
    data = ReminderCreate(message="x", delay_seconds=120)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reminders.create_reminder_endpoint(
            data, name="a1", current_user=_user(connector_agent="a1")))
    assert exc.value.status_code == 403


def test_router_self_reminder_created():
    _load()
    from models import ReminderCreate, Reminder
    a = _agent()
    result = _create(a, _user(agent_name=a), ReminderCreate(message="own", delay_seconds=300))
    assert isinstance(result, Reminder)
    assert result.agent_name == a
    assert result.status == "pending"


def test_router_user_scoped_owner_created():
    _load()
    from models import ReminderCreate, Reminder
    a = _agent()
    result = _create(a, _user(agent_name=None), ReminderCreate(message="own", delay_seconds=300))
    assert isinstance(result, Reminder)


def test_router_idempotent_dup_replays():
    _load()
    from models import ReminderCreate
    from fastapi.responses import JSONResponse
    from database import db
    a = _agent()
    _create(a, _user(agent_name=a), ReminderCreate(message="same", delay_seconds=300))
    # Identical raw input → replay (no second row).
    second = _create(a, _user(agent_name=a), ReminderCreate(message="same", delay_seconds=300))
    assert isinstance(second, JSONResponse)
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert db.count_pending_reminders(a) == 1


def test_router_cancel_then_recreate_identical_is_fresh():
    _load()
    from models import ReminderCreate, Reminder
    from database import db
    a = _agent()
    first = _create(a, _user(agent_name=a), ReminderCreate(message="same", delay_seconds=300))
    # Cancel it, then recreate identical → a FRESH pending reminder (security F4).
    assert db.cancel_reminder(a, first.id) == "cancelled"
    again = _create(a, _user(agent_name=a), ReminderCreate(message="same", delay_seconds=300))
    assert isinstance(again, Reminder)
    assert again.id != first.id
    assert again.status == "pending"
    assert db.count_pending_reminders(a) == 1


def test_router_in_flight_dup_returns_409(monkeypatch):
    _load()
    from routers import reminders
    from models import ReminderCreate
    from services import idempotency_service
    from fastapi import HTTPException
    a = _agent()
    # Force the idempotency layer to report an in-flight replay.
    monkeypatch.setattr(
        idempotency_service, "begin",
        lambda scope, key: idempotency_service.IdempotencyDecision(
            enabled=True, replay=True, in_flight=True, scope=scope, key=key),
    )
    with pytest.raises(HTTPException) as exc:
        _create(a, _user(agent_name=a), ReminderCreate(message="x", delay_seconds=300))
    assert exc.value.status_code == 409


def test_router_cancel_outcomes():
    _load()
    from routers import reminders
    from models import ReminderCreate, Reminder
    from fastapi import HTTPException
    a = _agent()
    created = _create(a, _user(agent_name=a), ReminderCreate(message="c", delay_seconds=300))
    # cancel → 200 (returns the cancelled reminder)
    out = asyncio.run(reminders.cancel_reminder_endpoint(
        name=a, reminder_id=created.id, current_user=_user(agent_name=a)))
    assert isinstance(out, Reminder) and out.status == "cancelled"
    # cancel a foreign id → uniform 404 (tenant-scope, no oracle)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reminders.cancel_reminder_endpoint(
            name=a, reminder_id="rem_foreign", current_user=_user(agent_name=a)))
    assert exc.value.status_code == 404


def test_router_list_default_pending():
    _load()
    from models import ReminderCreate
    from database import db
    a = _agent()
    r = _create(a, _user(agent_name=a), ReminderCreate(message="l", delay_seconds=300))
    db.cancel_reminder(a, r.id)
    r2 = _create(a, _user(agent_name=a), ReminderCreate(message="l2", delay_seconds=300))
    # default status=pending → only the live one
    pending = _list(a, _user(agent_name=a))
    ids = {x.id for x in pending}
    assert r2.id in ids and r.id not in ids
    # status=all → both
    allr = _list(a, _user(agent_name=a), status="all")
    assert {r.id, r2.id} <= {x.id for x in allr}
