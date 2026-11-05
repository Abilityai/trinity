"""ent#1102 — the operator-resume switch becomes tri-state, and one resolver reads it.

ent#329 shipped a boolean `operator_resume_enabled` with `DEFAULT 0`, which is
fine while the default is off and impossible once it is on: "nobody has ever
touched this" and "the owner said off" are the same byte, so flipping the
default either silences the fleet that never answered or overrides the owners
who already said no. ent#1102's `mode` ('default' | 'on' | 'off') is that third
state, and `wake_policy.resolve` is the only reader of either column.

Two halves, and the second is the one that would otherwise rot: the WRITE
accepting both bodies, and every place that GATES a wake reading the resolver
rather than the old column. A dispatcher that honours the tri-state while the
pre-filter in front of it still reads `operator_resume_enabled` declines to
even schedule the work, so the resolver is never consulted — the switch would
look wired and do nothing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE agent_ownership (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_name TEXT UNIQUE NOT NULL,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            operator_resume_enabled INTEGER DEFAULT 0,
            wake_on_ending INTEGER,
            deleted_at TEXT
        )
        """
    )
    # `updated_at` is part of the real table (db/tables.py), so the column
    # has to be here for the instance-default read to SUCCEED and answer
    # "no row": a read that raises now resolves OFF (fail-closed), which
    # would make this fixture, not the policy, decide the verdict.
    conn.execute(
        "CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
        "VALUES ('alpha', 1, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    try:
        import db.engine as engine_mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


@pytest.fixture
def routes(seeded_db, monkeypatch):
    try:
        from routers import agents as agents_router
        from services import platform_audit_service as audit_mod
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"backend import unavailable: {exc}")

    async def _noop_log(*args, **kwargs):
        return None

    monkeypatch.setattr(audit_mod.platform_audit_service, "log", _noop_log)
    return agents_router


class _Person:
    """A JWT human: what `require_person` + `reject_agent_principal` expect."""

    id = 1
    username = "owner"
    email = "owner@example.com"
    role = "admin"
    agent_name = None
    mcp_scope = None


def _stored(conn, agent_name="alpha"):
    row = conn.execute(
        "SELECT operator_resume_enabled, wake_on_ending FROM agent_ownership "
        "WHERE agent_name = ?",
        (agent_name,),
    ).fetchone()
    return {"operator_resume_enabled": row[0], "wake_on_ending": row[1]}


# ---------------------------------------------------------------------------
# The body: three modes, the legacy boolean, and a named refusal
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode,expected", [("default", None), ("on", True), ("off", False)])
def test_each_mode_names_the_tristate_value_it_writes(mode, expected):
    from models import OperatorResumeUpdate

    assert OperatorResumeUpdate(mode=mode).wake_override is expected


@pytest.mark.parametrize("enabled,expected", [(True, True), (False, False)])
def test_the_legacy_boolean_also_records_an_opinion(enabled, expected):
    """A person who moved the ent#329 switch has spoken, so `enabled: false`
    must land in the tri-state too — otherwise the instance default (ON) keeps
    overriding the owner who just turned the wake off."""
    from models import OperatorResumeUpdate

    assert OperatorResumeUpdate(enabled=enabled).wake_override is expected


@pytest.mark.parametrize("payload", [{}, {"mode": "on", "enabled": True}])
def test_neither_or_both_is_a_named_refusal(payload):
    from models import OperatorResumeUpdate

    with pytest.raises(ValidationError) as exc:
        OperatorResumeUpdate(**payload)
    assert "exactly one" in str(exc.value)


def test_an_unknown_mode_is_refused_rather_than_silently_ignored():
    """FastAPI renders this as a 422 naming the field — Product Quality Bar's
    named validation, not a 500 and not a quiet fallback to the default."""
    from models import OperatorResumeUpdate

    with pytest.raises(ValidationError) as exc:
        OperatorResumeUpdate(mode="sometimes")
    assert "mode" in str(exc.value)


# ---------------------------------------------------------------------------
# The route: what it writes and what it answers with
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mode_off_is_stored_as_an_explicit_no(routes, seeded_db):
    from models import OperatorResumeUpdate

    out = await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(mode="off"), current_user=_Person()
    )
    assert _stored(seeded_db)["wake_on_ending"] == 0
    assert out["mode"] == "off"
    assert out["effective"] is False
    assert out["source"] == "agent"


@pytest.mark.asyncio
async def test_mode_default_clears_the_override_back_to_the_instance(routes, seeded_db):
    from models import OperatorResumeUpdate

    await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(mode="off"), current_user=_Person()
    )
    out = await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(mode="default"), current_user=_Person()
    )
    assert _stored(seeded_db)["wake_on_ending"] is None
    assert out["mode"] == "default"
    assert out["effective"] is True
    assert out["source"] == "instance"


@pytest.mark.asyncio
async def test_a_legacy_body_writes_both_columns(routes, seeded_db):
    """The boolean column still has readers, so it is kept in step; the
    tri-state is what the resolver consults."""
    from models import OperatorResumeUpdate

    await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(enabled=False), current_user=_Person()
    )
    assert _stored(seeded_db) == {"operator_resume_enabled": 0, "wake_on_ending": 0}

    await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(enabled=True), current_user=_Person()
    )
    assert _stored(seeded_db) == {"operator_resume_enabled": 1, "wake_on_ending": 1}


@pytest.mark.asyncio
async def test_an_unknown_agent_is_a_404_not_a_silent_success(routes, seeded_db):
    from fastapi import HTTPException
    from models import OperatorResumeUpdate

    with pytest.raises(HTTPException) as exc:
        await routes.set_operator_resume_endpoint(
            "nope", OperatorResumeUpdate(mode="on"), current_user=_Person()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_read_reports_the_mode_and_what_it_resolves_to(routes, seeded_db):
    out = await routes.get_operator_resume_endpoint("alpha", current_user=_Person())
    assert out["mode"] == "default"
    assert out["effective"] is True
    assert out["source"] == "instance"
    # ent#329's own field is unchanged for every existing client.
    assert out["enabled"] is False


# ---------------------------------------------------------------------------
# The gates: every wake decision reads the resolver, not the old column
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_tristate_on_wakes_an_agent_whose_legacy_column_is_zero(routes, seeded_db):
    """The regression this file exists for. `operator_resume_enabled` stays 0
    (nobody wrote it), so any gate still reading that column declines — and the
    dispatcher behind it never gets asked."""
    from models import OperatorResumeUpdate
    from services import ask_service, operator_resume_service, wake_policy
    from client_portal.asks import service as portal_asks

    await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(mode="on"), current_user=_Person()
    )
    assert _stored(seeded_db) == {"operator_resume_enabled": 0, "wake_on_ending": 1}
    assert wake_policy.resolve("alpha").enabled is True

    for module in (operator_resume_service, ask_service, portal_asks):
        src = Path(module.__file__).read_text(encoding="utf-8")
        assert "db.get_operator_resume_enabled(agent_name)" not in src, (
            f"{module.__name__} still gates a wake on the ent#329 column, which "
            "a tri-state 'on' never writes"
        )


@pytest.mark.asyncio
async def test_an_explicit_off_is_honoured_over_the_instance_default(routes, seeded_db):
    from models import OperatorResumeUpdate
    from services import wake_policy

    await routes.set_operator_resume_endpoint(
        "alpha", OperatorResumeUpdate(mode="off"), current_user=_Person()
    )
    verdict = wake_policy.resolve("alpha")
    assert verdict.enabled is False
    assert verdict.source == "agent"
    assert verdict.reason == "wake_disabled_by_agent"
