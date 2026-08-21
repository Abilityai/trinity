"""Rename follows the SAME registry as delete (#1819).

The reported symptom was that renaming an agent stranded its Session-tab
history. The cause was structural: `cascade_delete` consumed
`db.agent_cleanup.AGENT_REFS`, while `rename_agent` kept its own hand-written
sequence of `update()` calls — two lists answering one question. Every table
added since only ever joined one of them, so by the time this was filed the
rename list had fallen **23 tables** behind the registry: sessions and session
messages (reported), plus reminders, loops, notifications, events, operator
queue, sync state, compatibility results, per-user memory, and the Telegram /
WhatsApp / VoIP / Slack channel bindings.

`test_agent_cleanup_parity.py` already guards schema ↔ registry. This guards
registry ↔ **rename behaviour**, which is the half that was missing:

  * behavioural — seed one row per registered table under the old name, rename,
    and assert NOTHING is left behind. This fails on the pre-fix code.
  * structural — `rename_agent` must not grow a private table list again.

The behavioural test drives the real `rename_agent` against the real schema, so
it cannot pass by agreeing with a mock about which tables exist.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_TESTS = Path(__file__).resolve().parent.parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

OLD = "rename-probe-old"
NEW = "rename-probe-new"


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """Real production schema on a throwaway SQLite file."""
    try:
        from db.engine import dispose_engines
        from db_harness import bootstrap_schema
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "trinity.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dispose_engines()
    bootstrap_schema()
    yield
    dispose_engines()


def _columns(conn, table: str) -> dict:
    from sqlalchemy import text

    rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    # name -> (type, notnull, default, pk)
    return {r[1]: (r[2] or "", r[3], r[4], r[5]) for r in rows}


def _placeholder(coltype: str, name: str) -> object:
    t = (coltype or "").upper()
    if "INT" in t:
        return 1
    if "REAL" in t or "FLOA" in t or "DOUB" in t or "NUMERIC" in t:
        return 1.0
    # A timestamp-ish TEXT column with NOT NULL is common; any string works
    # since nothing here parses it.
    return f"seed-{name}"


_FILTER_RE = re.compile(r"(\w+)\s*=\s*'([^']*)'")


def _filter_values(extra_filter: str | None) -> dict:
    """The column values a ref's `extra_filter` requires a row to carry.

    A filtered ref only re-keys rows its predicate selects, so a seeded row that
    does not satisfy the predicate is skipped by design — and would then be
    reported as a strand that is really this seeder's fault. Parsing the filter
    keeps the harness correct for ANY filtered ref, instead of one hard-coded
    column per ref (the `scope` special case this replaces, and the `kind` /
    `sender_kind` pair ent#443 added for the polymorphic room columns).
    """
    return dict(_FILTER_RE.findall(extra_filter or ""))


def _seed_row(conn, table: str, agent_column: str, extra_filter: str | None = None) -> bool:
    """Insert one minimally-valid row for `table` keyed to OLD.

    Returns False when the table cannot be seeded generically (e.g. a required
    column this helper cannot synthesize); such tables are reported so the
    coverage claim stays honest rather than silently shrinking.
    """
    from sqlalchemy import text

    cols = _columns(conn, table)
    if agent_column not in cols:
        return False

    required = _filter_values(extra_filter)
    values = {}
    for name, (coltype, notnull, default, pk) in cols.items():
        if name == agent_column:
            values[name] = OLD
        elif name in required:
            # Satisfy the ref's own predicate — see `_filter_values`. Applies to
            # the scope-filtered key refs and to ent#443's kind-scoped room refs;
            # `test_scope_filtered_keys_narrow_deliberately` covers the rows the
            # filters intentionally leave alone.
            values[name] = required[name]
        elif pk and "INT" in (coltype or "").upper():
            continue  # let AUTOINCREMENT assign
        elif notnull and default is None:
            values[name] = _placeholder(coltype, name)
    collist = ", ".join(values)
    binds = ", ".join(f":{c}" for c in values)
    try:
        conn.execute(text(f"INSERT INTO {table} ({collist}) VALUES ({binds})"), values)
        return True
    except Exception:
        return False


def test_rename_rekeys_every_registered_table(sqlite_db):
    """Seed one row per AGENT_REFS entry, rename, assert none stay behind."""
    from sqlalchemy import text

    from db.agent_cleanup import AGENT_REFS
    from db.engine import get_engine
    from db.agent_settings.metadata import MetadataMixin

    seeded: list[tuple[str, str]] = []
    unseedable: list[str] = []
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, role, created_at, updated_at) "
                "VALUES (1, 'admin', 'admin', 'now', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES (:n, 1, 'now')"
            ),
            {"n": OLD},
        )
        for ref in AGENT_REFS:
            if _seed_row(conn, ref.table, ref.column, ref.extra_filter):
                seeded.append((ref.table, ref.column))
            else:
                unseedable.append(f"{ref.table}.{ref.column}")

    # The guard is only as good as its coverage — say what it could not seed.
    assert len(seeded) >= 25, (
        f"only seeded {len(seeded)} registry tables; coverage too thin to trust. "
        f"unseedable={unseedable}"
    )

    assert MetadataMixin().rename_agent(OLD, NEW) is True

    stranded = []
    with get_engine().begin() as conn:
        for table, column in seeded:
            left = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :n"), {"n": OLD}
            ).scalar()
            moved = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :n"), {"n": NEW}
            ).scalar()
            if left or not moved:
                stranded.append(f"{table}.{column} (old={left}, new={moved})")

    assert not stranded, (
        "rename left rows behind — these tables are in AGENT_REFS but were not "
        f"re-keyed:\n  " + "\n  ".join(stranded)
    )


def test_session_history_specifically_follows_the_rename(sqlite_db):
    """The reported symptom, pinned on its own.

    A generic sweep can drift (a table that stops being seedable quietly leaves
    the assertion), so the two tables from the bug report get an explicit test
    with realistic rows.
    """
    from sqlalchemy import text

    from db.engine import get_engine
    from db.agent_settings.metadata import MetadataMixin

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, role, created_at, updated_at) "
                "VALUES (1, 'admin', 'admin', 'now', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES (:n, 1, 'now')"
            ),
            {"n": OLD},
        )
        conn.execute(
            text(
                "INSERT INTO agent_sessions (id, agent_name, user_id, user_email, "
                "started_at, last_message_at) VALUES ('s1', :n, 1, 'a@b.c', 'now', 'now')"
            ),
            {"n": OLD},
        )
        for i in range(4):
            conn.execute(
                text(
                    "INSERT INTO agent_session_messages (id, session_id, agent_name, "
                    "user_id, user_email, role, content, timestamp) "
                    "VALUES (:i, 's1', :n, 1, 'a@b.c', 'user', 'hi', 'now')"
                ),
                {"i": f"m{i}", "n": OLD},
            )

    assert MetadataMixin().rename_agent(OLD, NEW) is True

    with get_engine().begin() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_sessions WHERE agent_name = :n"), {"n": NEW}
        ).scalar() == 1
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_session_messages WHERE agent_name = :n"),
            {"n": NEW},
        ).scalar() == 4
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_sessions WHERE agent_name = :n"), {"n": OLD}
        ).scalar() == 0


def test_rename_does_not_maintain_its_own_table_list():
    """Structural guard: the defect was two lists, so forbid the second one.

    A future edit that re-adds `update(<some_table>)` inside `rename_agent`
    recreates exactly the drift this issue is about — the behavioural test above
    would still pass for the tables that edit happens to include.
    """
    source = (_BACKEND / "db" / "agent_settings" / "metadata.py").read_text()
    start = source.index("def rename_agent")
    end = source.index("\n    def ", start + 10)
    body = source[start:end]

    assert "cascade_rename(" in body, "rename_agent must derive its tables from AGENT_REFS"

    # `agent_ownership` is legitimately updated here (the row being renamed, plus
    # the #1664 volume_base_name pin); everything else belongs to the registry.
    hand_written = {
        t for t in re.findall(r"update\(([a-z_]+)\)", body) if t != "agent_ownership"
    }
    assert not hand_written, (
        "rename_agent re-grew a private table list: "
        f"{sorted(hand_written)} — add them to AGENT_REFS instead"
    )


def test_scope_filtered_keys_narrow_deliberately(sqlite_db):
    """A behaviour change worth stating, not discovering later.

    The old hand-written rename re-keyed EVERY `mcp_api_keys` row carrying the
    agent's name. The registry declares two scope-filtered refs
    (`scope='agent'`, `scope='connector'`), so those two follow the rename and a
    row with any other scope does not.

    That is deliberate: `cascade_delete` already used exactly these filters, so
    honouring them in rename is what makes the two paths one rule. A user- or
    system-scoped key is not per-agent — the column is provenance, not
    ownership. Pinning it here so the narrowing stays a decision.
    """
    from sqlalchemy import text

    from db.engine import get_engine
    from db.agent_settings.metadata import MetadataMixin

    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, role, created_at, updated_at) "
                "VALUES (1, 'admin', 'admin', 'now', 'now')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
                "VALUES (:n, 1, 'now')"
            ),
            {"n": OLD},
        )
        for key_id, scope in (("k-agent", "agent"), ("k-conn", "connector"), ("k-user", "user")):
            conn.execute(
                text(
                    "INSERT INTO mcp_api_keys (id, name, key_prefix, key_hash, "
                    "created_at, user_id, agent_name, scope) "
                    "VALUES (:i, :i, 'tk_', :i, 'now', 1, :n, :s)"
                ),
                {"i": key_id, "n": OLD, "s": scope},
            )

    assert MetadataMixin().rename_agent(OLD, NEW) is True

    with get_engine().begin() as conn:
        moved = {
            row[0]
            for row in conn.execute(
                text("SELECT id FROM mcp_api_keys WHERE agent_name = :n"), {"n": NEW}
            ).all()
        }
    assert moved == {"k-agent", "k-conn"}, (
        "agent- and connector-scoped keys must follow the rename (a leaked "
        "connector snippet must not reach a future same-name agent)"
    )
