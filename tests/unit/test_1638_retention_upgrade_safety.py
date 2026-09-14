"""#1638 — a lowered retention default must never destroy pre-existing data.

Background: #1065 lowered five retention defaults to the 5-day community floor
(#1039). `cleanup_service` resolves each window DB-row-first with the code
default as the fallback, and nothing writes those rows by default — so every
install that had never touched retention silently inherited the new floor, and
the startup sweep hard-DELETEd everything outside it seconds after the first
boot on the new version. A real instance lost 95.7% of `schedule_executions`
(~3 months) with a green /health and one INFO line.

The fix inverts the mechanism rather than patching the instance: the code
defaults stay WIDE (so an install with no row keeps its data, and every hole
fails safe), and the community floor is applied by SEEDING a fresh install's
rows — which only ever touches an empty database with nothing to lose.

These tests pin the properties that make that safe:
  * the seed only ever fires on a genuinely fresh DB
  * it never clobbers an operator's value, and is idempotent under a race
  * it never raises (init_database runs at import — raising = boot crash-loop)
  * `/ops/reset` cannot strand a retention window
  * the advertised defaults match the real ones
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

import database as _database
from services.settings_service import (
    COMMUNITY_FRESH_INSTALL_SEED,
    OPS_SETTINGS_DEFAULTS,
    RETENTION_OPS_KEYS,
)

# Load routers/settings.py in isolation so it does NOT trigger routers/__init__
# -> routers.agents -> services.agent_service, which another unit test pollutes
# under some pytest-random orderings. Mirrors test_retention_floor.py.
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load_isolated(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _BACKEND / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_RS = _load_isolated("retention_reset_isolated", "routers/settings.py")

pytestmark = pytest.mark.unit


def _bare_db() -> sqlite3.Connection:
    """A DB with just the two tables the seed touches, mirroring the moment the
    seed runs: after `init_schema`, before `_ensure_admin_user`."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            created_at TEXT
        );
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        );
        """
    )
    return conn


def _settings(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM system_settings").fetchall())


# ---------------------------------------------------------------------------
# The seed fires on a fresh install — and only there
# ---------------------------------------------------------------------------


def test_fresh_install_is_seeded_with_the_community_floor():
    conn = _bare_db()
    _database._seed_fresh_install_retention(conn.cursor(), conn)

    assert _settings(conn) == dict(COMMUNITY_FRESH_INSTALL_SEED)


def test_fresh_install_seed_leaves_agent_soft_delete_alone():
    """The recovery window is exempt (#1638): its expiry removes the agent's
    data volumes (#1581), so it must fall back to the wide 180-day default."""
    conn = _bare_db()
    _database._seed_fresh_install_retention(conn.cursor(), conn)

    assert "agent_soft_delete_retention_days" not in _settings(conn)
    assert OPS_SETTINGS_DEFAULTS["agent_soft_delete_retention_days"] == "180"


def test_existing_install_is_never_seeded():
    """THE regression test. An install with users owns data; writing the floor
    into it is what destroyed ~3 months of history on a real instance."""
    conn = _bare_db()
    conn.execute("INSERT INTO users (username) VALUES ('admin')")
    conn.commit()

    _database._seed_fresh_install_retention(conn.cursor(), conn)

    assert _settings(conn) == {}, (
        "an install that already has users must keep the wide code defaults"
    )


def test_existing_install_falls_back_to_wide_defaults():
    """The other half of the guarantee: not seeding is only safe because the
    fallback is wide. If both were narrow the install would still be pruned."""
    for key in RETENTION_OPS_KEYS:
        assert int(OPS_SETTINGS_DEFAULTS[key]) > 5


# ---------------------------------------------------------------------------
# Safety properties of the seed itself
# ---------------------------------------------------------------------------


def test_seed_is_idempotent():
    """Both migration locks fail open (migration_lock.py, alembic_runner.py), so
    two workers can run this concurrently. A non-idempotent write would raise →
    init_database raises → import fails → permanent boot crash-loop."""
    conn = _bare_db()
    cur = conn.cursor()

    _database._seed_fresh_install_retention(cur, conn)
    first = _settings(conn)
    _database._seed_fresh_install_retention(cur, conn)

    assert _settings(conn) == first


def test_seed_never_clobbers_an_operator_value():
    conn = _bare_db()
    conn.execute(
        "INSERT INTO system_settings (key, value, updated_at) "
        "VALUES ('execution_row_retention_days', '90', 'x')"
    )
    conn.commit()

    _database._seed_fresh_install_retention(conn.cursor(), conn)

    assert _settings(conn)["execution_row_retention_days"] == "90"


def test_seed_never_raises_on_a_broken_db():
    """`init_database` runs at import time, so an exception here does not fail a
    request — it prevents the process from starting, forever. A failed seed must
    degrade to 'install keeps wider retention', never to an outage."""
    conn = sqlite3.connect(":memory:")  # no tables at all

    _database._seed_fresh_install_retention(conn.cursor(), conn)  # must not raise


def test_fresh_install_detector_flips_on_first_user():
    conn = _bare_db()
    cur = conn.cursor()
    assert _database._is_fresh_install_sqlite(cur) is True

    conn.execute("INSERT INTO users (username) VALUES ('admin')")
    conn.commit()
    assert _database._is_fresh_install_sqlite(cur) is False


# ---------------------------------------------------------------------------
# The surfaces that could re-arm the bug
# ---------------------------------------------------------------------------


def test_ops_reset_cannot_strand_a_retention_window():
    """`POST /api/settings/ops/reset` deletes every OPS row it knows about. With
    retention included, a button labelled 'reset to defaults' silently changes
    how much operator data is kept — and pre-#1638 it re-armed mass deletion
    (delete the row -> fall back to the 5-day default -> purge on the next cycle).

    Drives the real handler and asserts on what it actually deleted, rather than
    grepping its source — the grep passes even if the skip is wired wrong.
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    admin = MagicMock()
    admin.role = "admin"
    admin.connector_agent = None  # #1310: not a connector principal
    admin.mcp_scope = None  # #2323: admin gate allowlists mcp_scope; absent fails CLOSED
    admin.agent_name = None  # ent#293: not an agent-scoped key

    fake_db = MagicMock()
    fake_db.delete_setting.return_value = True

    with patch.object(_RS, "db", fake_db):
        result = asyncio.run(
            _RS.reset_ops_settings(request=MagicMock(), current_user=admin)
        )

    deleted = {c.args[0] for c in fake_db.delete_setting.call_args_list}
    for key in RETENTION_OPS_KEYS:
        assert key not in deleted, f"reset must not delete {key} (#1638)"
        assert key in result["skipped"]
    # ...while still doing its actual job for non-retention settings.
    assert "ops_idle_timeout_minutes" in deleted


def test_descriptions_advertise_the_real_defaults():
    """#1638 factor 4: OPS_SETTINGS_DESCRIPTIONS still said 'default: 90' while
    the code deleted at 5. These strings are served to the admin UI by
    GET /api/settings/ops/config, so the drift told operators their data was
    kept 18x longer than it was. Mechanical drift — pin it."""
    import re

    from services.settings_service import OPS_SETTINGS_DESCRIPTIONS

    for key, description in OPS_SETTINGS_DESCRIPTIONS.items():
        match = re.search(r"default:\s*([\d.]+)", description)
        if not match:
            continue
        advertised = match.group(1)
        actual = OPS_SETTINGS_DEFAULTS[key]
        assert float(advertised) == float(actual), (
            f"{key}: description advertises default {advertised} but "
            f"OPS_SETTINGS_DEFAULTS is {actual}"
        )


def test_init_database_runs_at_import_not_in_the_lifespan():
    """The seed is only safe because it provably precedes the cleanup startup
    sweep — and it does so by CONSTRUCTION: `db = DatabaseManager()` is a
    module-level singleton whose __init__ calls init_database(), so migrations
    and the seed complete at import of `database`, before uvicorn builds the app.
    Moving init_database() into the lifespan would put it into an ordering
    contest with cleanup_service that it currently cannot lose."""
    import inspect

    src = inspect.getsource(_database)
    assert "\ndb = DatabaseManager()" in src
    assert "init_database()" in inspect.getsource(_database.DatabaseManager.__init__)
