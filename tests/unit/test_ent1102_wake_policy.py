"""ent#1102 — the wake-on-ending policy resolver (tri-state + instance default).

The wake spends a turn on the caller, so exactly one place decides whether it
may: ``services.wake_policy.resolve``. These tests pin the three properties
that make "default ON" safe to ship rather than merely present:

* the tri-state column is the owner's voice and wins over everything — a NULL
  (every row that predates the migration) is "no opinion", NOT "off", and falls
  through to the instance default (ent#790's flip, absorbed here);
* ent#329's ``operator_resume_enabled = 1`` is an explicit opt-in made before
  the tri-state existed, so it keeps meaning "on" even if the instance default
  is later turned off;
* an unreadable flag reads OFF — never spend a turn on a flag you could not
  read (the ent#329 rule).

Both halves run against a real SQLite file through the real mixin, so the
column name, the COALESCE and the resolver's branch order are all executed.
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


@pytest.fixture
def tmp_db_conn(tmp_path, monkeypatch):
    """One file behind both the seeding connection and the SQLAlchemy engine."""
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
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
def mixin(tmp_db_conn):
    try:
        from db.agent_settings.operator_resume import OperatorResumeMixin
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required (no `db.agent_settings` import)")

    class _Ops(OperatorResumeMixin):
        pass

    return _Ops()


def _seed(conn, name: str, *, resume=None, wake=None, deleted_at=None):
    conn.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at,"
        " operator_resume_enabled, wake_on_ending, deleted_at)"
        " VALUES (?, 1, '2026-01-01T00:00:00Z', ?, ?, ?)",
        (name, resume, wake, deleted_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# The tri-state column (DB layer)
# ---------------------------------------------------------------------------

class TestTriStateColumn:
    def test_null_reads_as_no_opinion(self, mixin, tmp_db_conn):
        """NULL must be distinguishable from an explicit 0 — that is the whole
        point of the tri-state (ent#790's B2 flip could not tell them apart)."""
        _seed(tmp_db_conn, "legacy", wake=None)
        assert mixin.get_wake_on_ending("legacy") is None

    def test_explicit_off_reads_false(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "quiet", wake=0)
        assert mixin.get_wake_on_ending("quiet") is False

    def test_explicit_on_reads_true(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "loud", wake=1)
        assert mixin.get_wake_on_ending("loud") is True

    def test_unknown_agent_has_no_opinion(self, mixin):
        assert mixin.get_wake_on_ending("no-such-agent") is None

    def test_set_round_trips_all_three_states(self, mixin, tmp_db_conn):
        _seed(tmp_db_conn, "a", wake=None)
        assert mixin.set_wake_on_ending("a", True) is True
        assert mixin.get_wake_on_ending("a") is True
        assert mixin.set_wake_on_ending("a", False) is True
        assert mixin.get_wake_on_ending("a") is False
        assert mixin.set_wake_on_ending("a", None) is True
        assert mixin.get_wake_on_ending("a") is None

    def test_soft_deleted_agent_cannot_be_flipped_on(self, mixin, tmp_db_conn):
        """A soft-deleted agent is recoverable; it must not come back pre-armed."""
        _seed(tmp_db_conn, "gone", wake=None, deleted_at="2026-08-01T00:00:00Z")
        assert mixin.set_wake_on_ending("gone", True) is False
        assert mixin.get_wake_on_ending("gone") is None


# ---------------------------------------------------------------------------
# The resolver (service layer) — the ONLY reader of the policy
# ---------------------------------------------------------------------------

@pytest.fixture
def policy(tmp_db_conn, monkeypatch):
    pytest.importorskip("database")  # backend venv
    # NOT importorskip: a missing resolver is the defect, not a missing venv.
    from services import wake_policy

    return wake_policy


def _stub_setting(monkeypatch, wake_policy, value):
    """Point the resolver's instance-default read at a known value."""
    from database import db as real_db

    monkeypatch.setattr(
        real_db,
        "get_setting_value",
        lambda key, default=None: value if key == wake_policy.SETTING_DEFAULT else default,
        raising=False,
    )


class TestResolver:
    def test_default_is_on_for_an_agent_with_no_opinion(
        self, policy, tmp_db_conn, monkeypatch
    ):
        """D4: the default is ON. A fresh instance that wrote no setting row and
        an agent that predates the column both get the wake.

        The setting read is stubbed to answer NOTHING rather than left to raise
        against the partial fixture DB — that is what a real install with no
        row does, and a raising read now resolves OFF (fail-closed), which is a
        different property, pinned below."""
        _stub_setting(monkeypatch, policy, None)
        _seed(tmp_db_conn, "fresh", wake=None)
        verdict = policy.resolve("fresh")
        assert verdict.enabled is True
        assert verdict.source == "instance"

    def test_agent_off_beats_instance_on(self, policy, tmp_db_conn, monkeypatch):
        _stub_setting(monkeypatch, policy, "true")
        _seed(tmp_db_conn, "quiet", wake=0)
        verdict = policy.resolve("quiet")
        assert verdict.enabled is False
        assert verdict.source == "agent"

    def test_agent_on_beats_instance_off(self, policy, tmp_db_conn, monkeypatch):
        _stub_setting(monkeypatch, policy, "false")
        _seed(tmp_db_conn, "loud", wake=1)
        verdict = policy.resolve("loud")
        assert verdict.enabled is True
        assert verdict.source == "agent"

    def test_instance_off_silences_an_agent_with_no_opinion(
        self, policy, tmp_db_conn, monkeypatch
    ):
        _stub_setting(monkeypatch, policy, "false")
        _seed(tmp_db_conn, "fresh", wake=None)
        verdict = policy.resolve("fresh")
        assert verdict.enabled is False
        assert verdict.source == "instance"

    def test_legacy_resume_optin_survives_an_instance_default_of_off(
        self, policy, tmp_db_conn, monkeypatch
    ):
        """ent#329's flag is an owner who already said "wake me". Turning the
        instance default off must not revoke a decision somebody made by hand."""
        _stub_setting(monkeypatch, policy, "false")
        _seed(tmp_db_conn, "opted-in", resume=1, wake=None)
        verdict = policy.resolve("opted-in")
        assert verdict.enabled is True
        assert verdict.source == "legacy_resume"

    def test_a_legacy_off_is_the_owners_voice_since_ent1108(
        self, policy, tmp_db_conn, monkeypatch
    ):
        """ent#1108 made the ent#329 column tri-state and moved every undecided
        ``DEFAULT 0`` to NULL, so a 0 that survives is an owner's explicit OFF —
        and the wider switch being NULL must not let the instance default
        outvote it. The read is RAW: the COALESCEd boolean getter would fold
        the NULL row above into "on" and make the instance layer unreachable."""
        _stub_setting(monkeypatch, policy, "true")
        _seed(tmp_db_conn, "opted-out", resume=0, wake=None)
        verdict = policy.resolve("opted-out")
        assert verdict.enabled is False
        assert verdict.source == "legacy_resume"
        assert verdict.reason == "wake_disabled_by_legacy_resume"

    def test_explicit_tristate_off_beats_the_legacy_optin(
        self, policy, tmp_db_conn, monkeypatch
    ):
        """The newer, more specific control is the owner's current voice."""
        _stub_setting(monkeypatch, policy, "true")
        _seed(tmp_db_conn, "changed-mind", resume=1, wake=0)
        verdict = policy.resolve("changed-mind")
        assert verdict.enabled is False
        assert verdict.source == "agent"

    def test_unreadable_flag_reads_off(self, policy, monkeypatch):
        """Never spend a turn on a flag you could not read (the ent#329 rule)."""
        from database import db as real_db

        def _boom(agent_name):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(real_db, "get_wake_on_ending", _boom, raising=False)
        verdict = policy.resolve("anyone")
        assert verdict.enabled is False
        assert verdict.source == "unreadable"

    def test_an_unreadable_legacy_column_reads_off(self, policy, tmp_db_conn, monkeypatch):
        """The second read is as fallible as the first. This path used to
        swallow the error and fall through to an instance default of ON, so a
        broken read BOUGHT a billed turn — the one trade the module forbids."""
        from database import db as real_db

        _stub_setting(monkeypatch, policy, "true")
        _seed(tmp_db_conn, "anyone", wake=None)

        def _boom(agent_name):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(real_db, "get_operator_resume_override", _boom, raising=False)
        verdict = policy.resolve("anyone")
        assert verdict.enabled is False
        assert verdict.source == "unreadable"

    def test_an_unreadable_instance_default_reads_off(self, policy, tmp_db_conn, monkeypatch):
        """Ruling D4 is the policy for an absent or malformed VALUE, not for a
        read that failed: an install whose settings are unreadable gets no wake
        rather than the default's."""
        from database import db as real_db

        _seed(tmp_db_conn, "anyone", wake=None)

        def _boom(key, default=None):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(real_db, "get_setting_value", _boom, raising=False)
        verdict = policy.resolve("anyone")
        assert verdict.enabled is False
        assert verdict.source == "unreadable"

    def test_a_malformed_setting_value_still_falls_back_to_the_default(
        self, policy, tmp_db_conn, monkeypatch
    ):
        """The other side of the line above — the read worked, the value is
        nonsense, so D4 applies and the fleet is not silenced by a typo."""
        _stub_setting(monkeypatch, policy, "maybe")
        _seed(tmp_db_conn, "fresh", wake=None)
        verdict = policy.resolve("fresh")
        assert verdict.enabled is True
        assert verdict.source == "instance"

    def test_missing_agent_name_reads_off(self, policy):
        assert policy.resolve("").enabled is False
        assert policy.resolve(None).enabled is False
