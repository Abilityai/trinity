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


def _user(agent_name=None, uid=1, email="u@example.com", connector_agent=None, mcp_scope=None):
    return SimpleNamespace(
        id=uid,
        agent_name=agent_name,
        connector_agent=connector_agent,
        mcp_scope=mcp_scope,
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
            idempotency_key=idem, x_source_agent=src,
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
    # #1693 split db/schedules.py into a package; the private bucket helpers
    # live in the analytics slice (the package __init__ re-exports only the
    # public facade surface).
    from db.schedules.analytics import _bucket_for_trigger, _BUCKET_ORDER
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


# ---------------------------------------------------------------------------
# #1831 — fire_at validation delegates to parse_iso_timestamp
#
# The validator used to normalize with ``v.replace("Z", "+00:00")`` (EVERY 'Z')
# while ``parse_iso_timestamp`` strips only a TRAILING 'Z'. A mid-string 'Z'
# therefore passed validation and then raised ValueError out of the UNGUARDED
# ``reminder_service._resolve_fire_at`` — HTTP 500 on an agent-supplied value.
# ---------------------------------------------------------------------------

# The measured divergence class on the production interpreter (py3.13): each of
# these was confirmed a live 500 through the real code before the fix. The
# parser raises on all seven under BOTH py3.13 and py3.14, so the list is
# interpreter-portable.
_1831_MID_STRING_Z = [
    "2026-01-15 10:30Z:00",
    "2026-01-15 10Z:30:00",
    "2026-01-15T10:30:00Z.5",
    "2026-01-15T10:30Z:00",
    "2026-01-15T10:30Z:00.5",
    "2026-01-15T10Z:30",
    "2026-01-15T10Z:30:00",
]

# The DELIBERATE loosening: an uppercase 'Z' used as the date/time SEPARATOR.
# ``fromisoformat`` (3.11+) accepts any single character there, so lowercase 'z'
# and 'X' already worked; uppercase 'Z' was rejected only as an artifact of the
# ``replace`` bug mangling it into "2026-01-15+00:0010:30:00".
_1831_Z_SEPARATOR = [
    "2026-01-15Z10:30",
    "2026-01-15Z10:30:00",
    "2026-01-15Z10:30:00Z",
    "2026-01-15Z10:30:00+00:00",
    "2026-01-15Z10:30:00-05:00",
    "2026-01-15Z10:30:00.123456",
    "2026-01-15Z10:30:00.123456Z",
]

# Shapes accepted before AND after the fix — the no-regression anchor.
_1831_CANONICAL = [
    "2026-08-01T10:00:00Z",
    "2026-08-01T10:00:00+00:00",
    "2026-08-01T10:00:00",
    "2026-08-01 10:00:00",
    "2026-08-01T10:00:00.123456Z",
    "2026-08-01T13:00:00+03:00",
    "2026-08-01",
]

_1831_GARBAGE = ["not-a-date", "", "2026-13-45T99:99:99", "Z", "2026-01-15T"]


def _1831_validator_accepts(s) -> bool:
    from models import ReminderCreate
    from pydantic import ValidationError
    try:
        ReminderCreate(message="x", fire_at=s)
        return True
    except ValidationError:
        return False


@pytest.mark.parametrize("s", _1831_MID_STRING_Z)
def test_1831_mid_string_z_fire_at_rejected(s):
    """T1 — the reported bug. Each of these seven produced HTTP 500 before the
    fix (validator ACCEPT -> parser RAISE -> ValueError escapes the router's
    ``except HTTPException``). They must now be rejected at validation."""
    _load()
    assert not _1831_validator_accepts(s), f"{s!r} must be rejected, not 500 later"


@pytest.mark.parametrize("s", _1831_CANONICAL + _1831_Z_SEPARATOR)
def test_1831_validator_does_not_canonicalize_fire_at(s):
    """T2 — the Invariant #18 guard. The create-idempotency key hashes
    ``raw_fire_spec()``, so a validator that "helpfully" returned a normalized
    value would fork the key and DOUBLE-CREATE on a client retry. ``fire_at``
    must round-trip byte-identically."""
    _load()
    from models import ReminderCreate
    d = ReminderCreate(message="x", fire_at=s)
    assert d.fire_at == s
    assert d.raw_fire_spec() == f"fire_at={s}"


@pytest.mark.parametrize(
    "s", _1831_MID_STRING_Z + _1831_Z_SEPARATOR + _1831_CANONICAL + _1831_GARBAGE
)
def test_1831_validator_implies_parser(s):
    """T3 — P7 restated discretely and Hypothesis-free: validator_accepts(s)
    implies parser_accepts(s). Survives if the property suite is ever
    deselected. ONE-DIRECTIONAL on purpose — asserting the converse would forbid
    the validator from ever becoming legitimately stricter than the parser."""
    _load()
    from utils.helpers import parse_iso_timestamp
    if _1831_validator_accepts(s):
        parse_iso_timestamp(s)  # must not raise


@pytest.mark.parametrize("s", _1831_Z_SEPARATOR)
def test_1831_uppercase_z_separator_now_accepted(s):
    """T4 — pins the DELIBERATE loosening so a future reader does not "fix" it
    back. These parse to a single unambiguous instant and remain subject to the
    service's min/max-window bounds."""
    _load()
    from models import ReminderCreate
    from utils.helpers import parse_iso_timestamp
    assert ReminderCreate(message="x", fire_at=s).fire_at == s
    assert parse_iso_timestamp(s) is not None


def test_1831_fire_at_none_cannot_reach_the_parser(monkeypatch):
    """T5 — the XOR guard is the ONLY thing between ``fire_at=None`` and an
    ``AttributeError`` 500 in ``parse_iso_timestamp(None)``. Pin that it is
    load-bearing: None alone is rejected, and the ``delay_seconds`` branch
    returns before the parser is ever consulted."""
    _load()
    from datetime import datetime, timezone
    from models import ReminderCreate
    from pydantic import ValidationError
    from services import reminder_service

    with pytest.raises(ValidationError):
        ReminderCreate(message="x", fire_at=None)   # neither supplied
    with pytest.raises(ValidationError):
        ReminderCreate(message="x")                  # neither supplied

    data = ReminderCreate(message="x", fire_at=None, delay_seconds=120)
    assert data.fire_at is None

    def _never(_value):
        raise AssertionError("parse_iso_timestamp reached on the delay_seconds branch")

    monkeypatch.setattr(reminder_service, "parse_iso_timestamp", _never)
    resolved = reminder_service._resolve_fire_at(data)
    assert isinstance(resolved, datetime) and resolved.tzinfo is not None
    assert resolved > datetime.now(timezone.utc)


def test_1831_malformed_fire_at_never_claims_an_idempotency_key(monkeypatch):
    """T6 — the fix's second-order value, pinned so a later refactor cannot move
    validation back into the service. A malformed ``fire_at`` must be rejected
    at BODY BINDING, i.e. before ``idempotency_service.begin()``. Pre-#1831 the
    escaping ``ValueError`` skipped the router's ``except HTTPException:
    fail(idem)`` rescue and stranded an ``in_flight`` claim, turning every
    identical retry into a 409 for 24h (attempt 1 -> 500, attempts 2-3 -> 409)."""
    _load()
    from models import ReminderCreate
    from pydantic import ValidationError
    from services import idempotency_service

    a = _agent()
    payload = {"message": "wedge", "fire_at": "2026-08-01T10Z:30:00"}
    real_begin = idempotency_service.begin
    claimed = []

    def _spy(scope, key):
        claimed.append((scope, key))
        return real_begin(scope, key)

    monkeypatch.setattr(idempotency_service, "begin", _spy)

    for _ in range(3):
        # Exactly what FastAPI does: bind the body, THEN run the path function.
        with pytest.raises(ValidationError):
            _create(a, _user(agent_name=a), ReminderCreate(**payload))

    assert claimed == [], "begin() must not run for a body that never binds"

    # And the key that WOULD have been claimed is still free — a corrected retry
    # proceeds instead of hitting a stranded-claim 409.
    scope = idempotency_service.make_agent_scope(a)
    key = idempotency_service.derive_reminder_key(
        a, payload["message"], f"fire_at={payload['fire_at']}"
    )
    decision = real_begin(scope, key)
    assert not decision.replay
    idempotency_service.fail(decision)


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
