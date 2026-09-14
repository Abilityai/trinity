"""#2085 — every install writes explicit retention rows, not just fresh ones.

Background: #1645 closed #1638 by reverting `OPS_SETTINGS_DEFAULTS` to the wide
historical values and applying the #1039 community floor through explicit
`system_settings` rows seeded on FRESH installs only. That left the other half
undone: an install that has ever *upgraded* rather than been created fresh has
no rows at all, so `cleanup_service` resolves every window at prune time from a
dict that ships inside the backend image and is replaced on every rebuild.

The safety of those installs rested entirely on a code comment telling people
not to lower those numbers. #1638 is what happens when that comment is missed:
a lowered value hard-DELETEs existing data seconds after the next boot, with no
error and a green /health.

`_seed_retention_windows` closes it by writing the value ALREADY IN FORCE for
any window with no row. After it runs once, no install anywhere resolves a
retention window from the image.

These tests pin the properties that make that safe:
  * it is behaviourally inert — it writes what the prune already used
  * it covers EVERY retention window, so none is left resolving from the image
  * it runs AFTER the fresh-install seed, so it cannot overwrite the #1039 floor
  * it never clobbers an operator value, is idempotent, and never raises
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

import database as _database
from services.settings_service import (
    COMMUNITY_FRESH_INSTALL_SEED,
    OPS_SETTINGS_DEFAULTS,
    RETENTION_OPS_KEYS,
)

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _bare_db() -> sqlite3.Connection:
    """The moment the seed runs: after `init_schema`, before `_ensure_admin_user`."""
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


def _existing_install() -> sqlite3.Connection:
    """A DB that predates this boot — the population #2085 exists for."""
    conn = _bare_db()
    conn.execute("INSERT INTO users (username) VALUES ('admin')")
    conn.commit()
    return conn


def _settings(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM system_settings").fetchall())


# ---------------------------------------------------------------------------
# The core guarantee
# ---------------------------------------------------------------------------


def test_existing_install_gets_a_row_for_every_retention_window():
    """THE point of #2085. An upgraded install has no rows, so its policy lives
    in the image. After the seed it lives in its own database."""
    conn = _existing_install()

    _database._seed_retention_windows(conn.cursor(), conn)

    stored = _settings(conn)
    assert set(stored) == set(RETENTION_OPS_KEYS), (
        "every retention window must get a row — any key left out still "
        "resolves from OPS_SETTINGS_DEFAULTS at prune time"
    )


def test_the_seed_is_behaviourally_inert():
    """It writes the number already in force, so nothing prunes differently the
    day it runs. This is what makes it safe to ship without a migration note."""
    conn = _existing_install()

    _database._seed_retention_windows(conn.cursor(), conn)

    for key, value in _settings(conn).items():
        assert value == OPS_SETTINGS_DEFAULTS[key], (
            f"{key} seeded {value!r} but the prune-time fallback was "
            f"{OPS_SETTINGS_DEFAULTS[key]!r} — that is a behaviour change"
        )


def test_seed_runs_on_a_fresh_install_too():
    """#2085 is not conditional on install age. A fresh install gets the four
    community-floor rows from the #1638 seed and the remaining windows here."""
    conn = _bare_db()

    _database._seed_retention_windows(conn.cursor(), conn)

    assert set(_settings(conn)) == set(RETENTION_OPS_KEYS)


# ---------------------------------------------------------------------------
# Ordering — the one way this change can destroy something
# ---------------------------------------------------------------------------


def test_community_floor_survives_when_both_seeds_run_in_order():
    """Both writers use INSERT OR IGNORE, so the FIRST one to reach a key wins.

    Run in the real order the #1039 floor (5) survives on the four overlapping
    keys. Run backwards, a fresh install would silently get 30/90/7/30 instead
    — the community floor deleted by the change meant to protect retention.
    """
    conn = _bare_db()
    cur = conn.cursor()

    _database._seed_fresh_install_retention(cur, conn)
    _database._seed_retention_windows(cur, conn)

    stored = _settings(conn)
    for key, floor in COMMUNITY_FRESH_INSTALL_SEED.items():
        assert stored[key] == floor, (
            f"{key} is {stored[key]!r}, expected the #1039 floor {floor!r} — "
            "_seed_retention_windows must run AFTER _seed_fresh_install_retention"
        )
    # ...and the non-overlapping windows still got their wide defaults.
    for key in set(RETENTION_OPS_KEYS) - set(COMMUNITY_FRESH_INSTALL_SEED):
        assert stored[key] == OPS_SETTINGS_DEFAULTS[key]


@pytest.mark.parametrize(
    "fresh_call,all_call",
    [
        ("_seed_fresh_install_retention(cursor, conn)", "_seed_retention_windows(cursor, conn)"),
        ("_seed_fresh_install_retention_engine()", "_seed_retention_windows_engine()"),
    ],
)
def test_init_database_calls_the_fresh_seed_first(fresh_call, all_call):
    """Source-order guard, on BOTH backends.

    The failure this prevents is silent: a fresh install would come up with the
    wide defaults instead of the community floor, no error anywhere, and the
    only evidence would be four `system_settings` rows holding the wrong number
    forever. Cheap to assert, expensive to notice.
    """
    source = (_BACKEND / "database.py").read_text()
    fresh_at = source.find(fresh_call)
    all_at = source.find(all_call)

    assert fresh_at != -1, f"{fresh_call} not found in init_database"
    assert all_at != -1, f"{all_call} not found in init_database"
    assert fresh_at < all_at, (
        f"{all_call} must run AFTER {fresh_call} — INSERT OR IGNORE means the "
        "first writer wins, so the reverse order overwrites the #1039 floor"
    )


# ---------------------------------------------------------------------------
# Safety properties shared with the #1638 seed
# ---------------------------------------------------------------------------


def test_seed_never_clobbers_an_operator_value():
    conn = _existing_install()
    conn.execute(
        "INSERT INTO system_settings (key, value, updated_at) "
        "VALUES ('execution_row_retention_days', '365', 'x')"
    )
    conn.commit()

    _database._seed_retention_windows(conn.cursor(), conn)

    assert _settings(conn)["execution_row_retention_days"] == "365"


def test_seed_is_idempotent():
    """Both migration locks fail open, so two workers can race this."""
    conn = _existing_install()
    cur = conn.cursor()

    _database._seed_retention_windows(cur, conn)
    first = _settings(conn)
    _database._seed_retention_windows(cur, conn)

    assert _settings(conn) == first


def test_seed_never_raises_on_a_broken_db():
    """`init_database` runs at import, so raising here is a permanent boot
    crash-loop, not a failed request. A failed seed must degrade to 'this
    install keeps resolving from the code defaults' — i.e. today's behaviour."""
    conn = sqlite3.connect(":memory:")  # no tables at all

    _database._seed_retention_windows(conn.cursor(), conn)  # must not raise


# ---------------------------------------------------------------------------
# Couplings this change creates
# ---------------------------------------------------------------------------


def test_backup_window_default_agrees_with_its_own_reader():
    """`backup_retention_days` is the one window with a private reader
    (`effective_backup_retention_days`, whose coercion is inverted, #2216). It
    falls back to its OWN module constant, not to OPS_SETTINGS_DEFAULTS — so
    before #2085 the two could drift with no visible effect. Seeding makes the
    OPS value the one that lands in the database, so they must agree."""
    from services.db_backup_service import DEFAULT_RETENTION_DAYS, RETENTION_KEY

    assert OPS_SETTINGS_DEFAULTS[RETENTION_KEY] == str(DEFAULT_RETENTION_DAYS)


def test_every_seeded_value_is_a_valid_ops_setting():
    """The seed writes through neither validated route, so it must not be able
    to introduce a value `PUT /api/settings/ops/config` would reject."""
    from config import validate_ops_setting

    for key in RETENTION_OPS_KEYS:
        # raises on an invalid value
        validate_ops_setting(key, OPS_SETTINGS_DEFAULTS[key])


def test_prune_still_falls_back_to_the_code_defaults():
    """The seed is only worth having while an unseeded window would resolve
    from the image. If `cleanup_service` ever stops using OPS_SETTINGS_DEFAULTS
    as its prune-time fallback, re-read #2085 before trusting this seed."""
    cleanup = (_BACKEND / "services" / "cleanup_service.py").read_text()
    assert "OPS_SETTINGS_DEFAULTS" in cleanup, (
        "the prune-time fallback moved — the exposure #2085 closes may have "
        "changed shape; re-derive the seed against the new resolution"
    )


# ---------------------------------------------------------------------------
# The seed must actually RUN — no in-process test can see this
# ---------------------------------------------------------------------------


def test_seed_runs_at_real_import_time(tmp_path):
    """The regression test for the bug this feature shipped with first.

    `init_database()` is called from `DatabaseManager.__init__`, i.e. while
    `database.py` is still executing its own module body — `database.db` does
    not exist yet. So ANY import of `services.settings_service` from inside the
    seed raises `ImportError: cannot import name 'db' from partially
    initialized module`, and because the seed is fail-safe by contract it
    SWALLOWS that and prints a warning nobody reads. The feature is then dead
    on every boot, forever, with a full green unit suite — every test above
    passes, because they call the function after `database` finished importing.

    `_seed_fresh_install_retention` documents this trap in a comment three
    lines from where the #2085 seed was written, and it was still walked into.
    The only test that can catch it is one that pays for a real import.
    """
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "TRINITY_DB_PATH": str(tmp_path / "probe.db"),
        "REDIS_URL": "redis://u:p@localhost:6379",
        "SECRET_KEY": "x" * 32,
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import database"],
        cwd=str(_BACKEND), env=env, capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout + proc.stderr

    assert "WARNING: [#2085]" not in out, (
        "the seed hit its fail-safe path at import time and silently did "
        f"nothing — the feature is dead on every boot:\n{out}"
    )
    assert "[#2085] Seeded" in out, (
        f"the seed never ran during a real import:\n{out}"
    )


def test_seed_writes_every_window_at_real_import_time(tmp_path):
    """The other half: prove the rows land, and that the #1638 floor won the
    four keys the two seeds share (ordering, observed end-to-end rather than
    by reading the source)."""
    import os
    import subprocess
    import sys

    db_file = tmp_path / "probe.db"
    env = {
        **os.environ,
        "TRINITY_DB_PATH": str(db_file),
        "REDIS_URL": "redis://u:p@localhost:6379",
        "SECRET_KEY": "x" * 32,
    }
    subprocess.run(
        [sys.executable, "-c", "import database"],
        cwd=str(_BACKEND), env=env, capture_output=True, text=True, timeout=180,
    )

    conn = sqlite3.connect(db_file)
    try:
        stored = dict(
            conn.execute("SELECT key, value FROM system_settings").fetchall()
        )
    finally:
        conn.close()

    for key in RETENTION_OPS_KEYS:
        assert key in stored, f"{key} still resolves from the image after boot"

    for key, floor in COMMUNITY_FRESH_INSTALL_SEED.items():
        assert stored[key] == floor, (
            f"{key}={stored[key]!r} — the #1638 fresh-install floor must win "
            "the keys both seeds write"
        )
    for key in set(RETENTION_OPS_KEYS) - set(COMMUNITY_FRESH_INSTALL_SEED):
        assert stored[key] == OPS_SETTINGS_DEFAULTS[key]
