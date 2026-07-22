"""An agent with no container is still deletable (#1747).

`DELETE /api/agents/{name}` looked up the Docker container first and treated its
absence as "agent not found". But identity lives in `agent_ownership`, not
Docker, so an agent with a live row and no container was simultaneously:

* **invisible** — `GET /api/agents` is Docker-as-truth (Invariant #11);
* **undeletable** — 404 "Agent not found";
* **holding its own name** — re-creating returned "Agent already exists";

escapable only by calling `POST /start` on an agent you cannot see.

Agents reach that state routinely: the #834 Phase 1c recovery flow leaves them
there *by design* (metadata-only, "operator runs POST /start"), as does any
`docker prune` / daemon reset, or a crash between the row write and container
creation.

The ephemeral branch directly above the guard already handled the same "live row,
no container" residue — its comment says a half-discarded ghost "must be
force-discardable, never 404". Ordinary agents simply never got that treatment.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

AGENTS_ROUTER = Path(__file__).resolve().parents[2] / "src" / "backend" / "routers" / "agents.py"


def _delete_endpoint_source() -> str:
    """Source of the delete endpoint only — the same 3-line container guard
    appears in four endpoints in this module, so a file-wide string search would
    silently pass on the wrong one."""
    tree = ast.parse(AGENTS_ROUTER.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_agent_endpoint":
            return ast.get_source_segment(AGENTS_ROUTER.read_text(), node)
    raise AssertionError("delete_agent_endpoint not found")


def test_existence_is_decided_by_the_ownership_row_not_docker():
    src = _delete_endpoint_source()
    assert "not db.is_agent_live(agent_name)" in src, (
        "delete must decide existence from agent_ownership — gating on the "
        "container alone traps any agent whose container is missing (#1747)"
    )
    # The bare form is the bug.
    assert "if not container:\n        raise HTTPException(status_code=404" not in src


def test_container_teardown_is_conditional():
    """`container_stop(None)` would raise into the generic except and log a
    misleading error; the delete must simply skip teardown when there is nothing
    to tear down."""
    src = _delete_endpoint_source()
    stop_at = src.index("await container_stop(")
    preceding = src[:stop_at]
    assert "if container:" in preceding, (
        "container stop/remove must be guarded by `if container:`"
    )


def test_unknown_agent_still_404s():
    """The guard must not turn a genuinely unknown name into a 200 — that would
    trade one bug for a worse one."""
    src = _delete_endpoint_source()
    assert 'raise HTTPException(status_code=404, detail="Agent not found")' in src
    # 404 requires BOTH: no container AND no live row.
    assert "if not container and not db.is_agent_live(agent_name):" in src


def test_ephemeral_branch_still_precedes_the_container_lookup():
    """#69 relies on the ephemeral discard running BEFORE the container lookup so
    a half-discarded ghost is force-discardable. The #1747 change must not have
    reordered that."""
    src = _delete_endpoint_source()
    assert src.index("discard_ephemeral_agent") < src.index("get_agent_container("), (
        "the ephemeral discard branch must stay above the container lookup (#69)"
    )


# --- the primitive the guard relies on ---------------------------------------

@pytest.fixture()
def live_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-1747.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.engine import get_engine
    # mcp_api_keys is not needed by this branch's delete_agent_ownership, but
    # #1745 makes it deactivate the agent's keys in the same transaction — create
    # it so this fixture holds before and after that lands.
    from db.tables import metadata as m, agent_ownership, users, mcp_api_keys
    m.create_all(get_engine(), tables=[agent_ownership, users, mcp_api_keys])
    from sqlalchemy import insert
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(id=1, username="alice", role="admin",
                                          created_at="t", updated_at="t"))
    yield str(db_file)


def test_is_agent_live_distinguishes_the_three_states(live_db):
    """The guard's whole correctness rests on this predicate: a live agent is
    deletable, an already-deleted one and an unknown one both 404."""
    from database import db
    from sqlalchemy import insert, text
    from db.engine import get_engine
    from db.tables import agent_ownership

    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name="alive", owner_id=1, created_at="t"))
        conn.execute(insert(agent_ownership).values(
            agent_name="already-deleted", owner_id=1, created_at="t", deleted_at="t"))

    assert db.is_agent_live("alive") is True             # deletable without a container
    assert db.is_agent_live("already-deleted") is False  # 404 — it IS deleted
    assert db.is_agent_live("never-existed") is False    # 404 — unknown


def test_delete_of_a_containerless_agent_soft_deletes_the_row(live_db):
    """The soft-delete itself never needed a container — it keys off the name."""
    from database import db
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_ownership

    with get_engine().begin() as conn:
        conn.execute(insert(agent_ownership).values(
            agent_name="containerless", owner_id=1, created_at="t"))

    assert db.delete_agent_ownership("containerless") is True
    assert db.is_agent_live("containerless") is False
    # The name stays reserved — that is the normal soft-delete policy for EVERY
    # delete (#834), not something this fix changes.
    assert db.is_agent_name_reserved("containerless") is True
