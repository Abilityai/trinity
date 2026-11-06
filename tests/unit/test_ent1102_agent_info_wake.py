"""ent#1102 — an agent can read its own wake policy (`GET /api/agents/{name}/info`).

The receipt tells a caller whether THIS delegated run will be reported
(`wake` / `wake_reason`, `tools/chat.ts`). What it cannot say is why, or what
the standing policy is — and an agent that has just been told `wake: none,
wake_reason: opted_out` needs somewhere to look. `get_agent_info` is that
surface: `wake_on_ending: {effective, source}`, where `source` names the layer
that decided (`agent` / `legacy_resume` / `instance` / `unreadable`) so an
operator reading "why was I woken" gets the mechanism, not just the verdict.

The block is stamped on EVERY answer, including the stopped-agent and
degraded-template branches, because the policy is a platform fact that does not
depend on the container being up — which is exactly the state an owner is in
when they are wondering why nothing woke up.

Runs against a real SQLite file through the real mixin and the real resolver,
so the branch order of `wake_policy.resolve` is executed, not stubbed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """One SQLite file behind the seeding connection and the real engine."""
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
    conn.commit()

    try:
        import db.engine as engine_mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    engine_mod.dispose_engines()
    yield conn
    engine_mod.dispose_engines()
    conn.close()


def _seed(conn, agent_name: str, *, wake_on_ending=None, legacy=None) -> None:
    # `legacy=None`: the ent#329 column is tri-state since ent#1108 — NULL is
    # "never said" (what `register_agent_owner` writes), 0 is the owner's OFF.
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, "
        "operator_resume_enabled, wake_on_ending) VALUES (?, 1, '2026-01-01T00:00:00Z', ?, ?)",
        (agent_name, legacy, wake_on_ending),
    )
    conn.commit()


class _StoppedContainer:
    """The simplest real branch of the endpoint: no agent server is reachable."""

    status = "exited"
    labels = {"trinity.template": "github:Org/repo", "trinity.cpu": "1", "trinity.memory": "1g"}


@pytest.fixture
def info_endpoint(seeded_db, monkeypatch):
    """The real route handler, with Docker and the container reload stubbed."""
    try:
        from routers import agent_files
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"backend import unavailable: {exc}")

    monkeypatch.setattr(agent_files, "get_agent_container", lambda name: _StoppedContainer())

    async def _noop_reload(container):
        return None

    monkeypatch.setattr(agent_files, "container_reload", _noop_reload)
    return agent_files.get_agent_info_endpoint


async def _info(endpoint, agent_name: str) -> dict:
    return await endpoint(agent_name, request=None)


@pytest.mark.asyncio
async def test_the_answer_carries_the_effective_policy_and_the_layer_that_decided(
    info_endpoint, seeded_db
):
    _seed(seeded_db, "alpha")  # no opinion anywhere -> the instance default
    info = await _info(info_endpoint, "alpha")
    assert info["wake_on_ending"] == {"effective": True, "source": "instance"}


@pytest.mark.asyncio
async def test_an_owners_explicit_off_is_reported_as_the_agents_own_decision(
    info_endpoint, seeded_db
):
    """The case the receipt's `opted_out` reason points here to explain: the
    answer must name `agent`, not `instance`, or an owner reading it concludes
    the instance default is off and changes the wrong control."""
    _seed(seeded_db, "beta", wake_on_ending=0)
    info = await _info(info_endpoint, "beta")
    assert info["wake_on_ending"] == {"effective": False, "source": "agent"}


@pytest.mark.asyncio
async def test_the_pre_tristate_opt_in_is_named_as_legacy(info_endpoint, seeded_db):
    """ent#329's `operator_resume_enabled = 1` still means on (checkpoint A),
    and the surface says WHICH layer said so — so an owner can tell a decision
    they made by hand from one the instance made for them."""
    _seed(seeded_db, "gamma", legacy=1)
    info = await _info(info_endpoint, "gamma")
    assert info["wake_on_ending"] == {"effective": True, "source": "legacy_resume"}


@pytest.mark.asyncio
async def test_a_stopped_agent_still_reports_its_policy(info_endpoint, seeded_db):
    """The stub container is stopped, so this answer comes from the early
    label-only branch — and it is precisely the state an owner is in when
    nothing woke up, so the block cannot be stamped on the running path only."""
    _seed(seeded_db, "delta", wake_on_ending=1)
    info = await _info(info_endpoint, "delta")
    assert info["status"] == "stopped"
    assert info["wake_on_ending"] == {"effective": True, "source": "agent"}


@pytest.mark.asyncio
async def test_an_unreadable_policy_reads_off_and_says_so(info_endpoint, monkeypatch):
    """Fail-closed, and visibly: the resolver never guesses ON from an error,
    and the surface reports `unreadable` rather than a confident `false` that
    an operator would read as somebody's decision."""
    from services import wake_policy

    info = await _info(info_endpoint, "")  # an empty name is unreadable by construction
    assert info["wake_on_ending"] == {
        "effective": False,
        "source": wake_policy.SOURCE_UNREADABLE,
    }
