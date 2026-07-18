"""Per-user GitHub credentials — ent#162 (Shape A).

Exercises the REAL code against an ephemeral migrated SQLite (db/tables.py
MetaData → `create_all`, so a missing `users.github_pat_encrypted` Column fails
loudly per learnings 2026-06-23), and the REAL DatabaseManager facade (a missing
delegation raises AttributeError — a wholesale-mocked `database` is blind to it,
learnings 2026-07-06). No inline reimplementations: every assertion drives the
shipped function.

Load-bearing test: `test_adding_per_user_pat_does_not_recreate_running_agent`.
The recreate/restart ladder (`check_github_pat_env_matches` →
`get_github_pat_for_agent`) MUST stay 2-tier (per-agent → global). If a future
"consistency" fix makes it re-derive the live per-user tier, adding a personal
PAT in Settings would force-recreate the owner's running agents and kill
in-flight work (the #1560/#1557 class). This test fails the moment that happens.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# --- sys.path: real backend modules, ahead of any leaked stub ----------------
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# Import the REAL modules at collection time (leak-free — the known
# sys.modules.setdefault offenders sort later) and re-own them per test via an
# autouse monkeypatch.setitem, so a sibling's stubbed `database`/`services`
# cannot poison our call-time `from database import db` (learnings 2026-07-12).
_OWNED = [
    "db.engine", "db.connection", "db.tables", "db.users",
    "database", "models",
    "services.settings_service", "services.github_service",
    "services.agent_service.helpers",
    "routers.users",
]
_REAL = {name: importlib.import_module(name) for name in _OWNED}

_ENC_KEY = "ab" * 32  # 32-byte hex — CredentialEncryptionService format
_GLOBAL_PAT = "ghp_GLOBAL_admin_token"


class _FakeContainer:
    """Minimal stand-in for a docker container: only `.attrs` is read."""

    def __init__(self, env_pairs):
        self.attrs = {"Config": {"Env": list(env_pairs)}}


@pytest.fixture()
def env_db(tmp_path, monkeypatch):
    """Fresh migrated sqlite + real modules owned in sys.modules.

    Seeds one owner user (id=1) and two git-configured agents with NO per-agent
    PAT (the global-fallback shape). The global tier is the `GITHUB_PAT` env var.
    """
    for name, mod in _REAL.items():
        monkeypatch.setitem(sys.modules, name, mod)

    db_file = tmp_path / "ent162.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(_REAL["db.connection"], "DB_PATH", str(db_file), raising=False)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _ENC_KEY)
    monkeypatch.setenv("GITHUB_PAT", _GLOBAL_PAT)

    from db.engine import get_engine, dispose_engines
    from db.tables import metadata, users, agent_git_config, system_settings
    from sqlalchemy import insert

    metadata.create_all(get_engine(), tables=[users, agent_git_config, system_settings])
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(
            id=1, username="owner", role="creator", email="owner@example.com",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        ))
        conn.execute(insert(users).values(
            id=2, username="other", role="creator", email="other@example.com",
            created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
        ))
        for name in ("agentA", "agentB"):
            conn.execute(insert(agent_git_config).values(
                id=f"gc-{name}", agent_name=name, github_repo=f"owner/{name}",
            ))
    try:
        yield str(db_file)
    finally:
        dispose_engines()


def _db():
    from database import db
    return db


# ---------------------------------------------------------------------------
# (a) LOAD-BEARING: the safety pin — adding a per-user PAT must not recreate
# ---------------------------------------------------------------------------

class TestRecreateNoStorm:
    def test_adding_per_user_pat_does_not_recreate_running_agent(self, env_db):
        from services.agent_service.helpers import check_github_pat_env_matches
        db = _db()

        # A running global-fallback agent: its container carries the global PAT,
        # it has no per-agent PAT of its own.
        container = _FakeContainer([f"GITHUB_PAT={_GLOBAL_PAT}", "HOME=/home/developer"])
        assert check_github_pat_env_matches(container, "agentA") is True

        # Owner adds a personal PAT in Settings.
        assert db.set_user_github_pat(1, "ghp_owner_personal_token") is True

        # The recreate matcher MUST still say "no recreate" — the per-user tier
        # is invisible to the 2-tier recreate ladder. A False here means adding a
        # PAT force-recreates running agents (the bug this test guards).
        assert check_github_pat_env_matches(container, "agentA") is True

    def test_tokenless_agent_not_recreated_when_owner_adds_pat(self, env_db):
        from services.agent_service.helpers import check_github_pat_env_matches
        db = _db()

        # Tokenless container (public/global-less agent), no per-agent PAT.
        container = _FakeContainer(["HOME=/home/developer"])
        assert check_github_pat_env_matches(container, "agentB") is True

        assert db.set_user_github_pat(1, "ghp_owner_personal_token") is True
        # Per-user PAT does not make a tokenless agent "need injection".
        assert check_github_pat_env_matches(container, "agentB") is True


# ---------------------------------------------------------------------------
# (b) Resolution ladder: per-agent → per-user → global → none
# ---------------------------------------------------------------------------

class TestResolutionLadder:
    def test_ladder_prefers_agent_then_user_then_global(self, env_db):
        from services.settings_service import resolve_github_pat
        db = _db()

        # No per-agent, no per-user → global.
        assert resolve_github_pat(agent_name="agentA", owner_id=1) == (_GLOBAL_PAT, "global")

        # Owner's per-user PAT wins over global.
        db.set_user_github_pat(1, "ghp_user_token")
        assert resolve_github_pat(agent_name="agentA", owner_id=1) == ("ghp_user_token", "per_user")

        # An explicit per-agent PAT wins over per-user.
        db.set_agent_github_pat("agentA", "ghp_agent_token")
        assert resolve_github_pat(agent_name="agentA", owner_id=1) == ("ghp_agent_token", "per_agent")

    def test_owner_isolation_other_users_pat_not_used(self, env_db):
        from services.settings_service import resolve_github_pat
        db = _db()
        # user 2 has a PAT; resolving for owner 1 must NOT see it.
        db.set_user_github_pat(2, "ghp_other_users_token")
        assert resolve_github_pat(agent_name="agentA", owner_id=1) == (_GLOBAL_PAT, "global")
        assert resolve_github_pat(agent_name="agentA", owner_id=2) == ("ghp_other_users_token", "per_user")

    def test_none_when_nothing_configured(self, env_db, monkeypatch):
        from services.settings_service import resolve_github_pat
        monkeypatch.delenv("GITHUB_PAT", raising=False)  # remove the global tier
        assert resolve_github_pat(agent_name="agentA", owner_id=1) == ("", "none")


# ---------------------------------------------------------------------------
# (c) Persist only the user tier — a global-fallback agent stays a propagation
#     target (keeps github_pat_encrypted NULL). crud gates persist on tier ∈
#     {fork, per_user}; the observable db effect is asserted here.
# ---------------------------------------------------------------------------

class TestPersistOnlyUserTier:
    def test_global_fallback_agent_is_not_a_per_agent_pat_holder(self, env_db):
        from services.settings_service import resolve_github_pat
        db = _db()
        # A creator with no personal PAT resolves to the global tier...
        _, tier = resolve_github_pat(agent_name="agentA", owner_id=1)
        assert tier == "global"
        # ...and crud does NOT persist a global-tier PAT, so the agent has no
        # per-agent PAT and therefore remains a target of
        # github_pat_propagation_service (has_agent_github_pat False = not skipped).
        assert db.has_agent_github_pat("agentA") is False

    def test_per_user_resolved_pat_when_persisted_marks_agent_skipped(self, env_db):
        # When crud persists a per_user/fork PAT, the agent flips to "has its own
        # PAT" → github_pat_propagation_service skips it (correct: it has its own
        # identity and must not be clobbered by an admin global rotation).
        db = _db()
        db.set_agent_github_pat("agentA", "ghp_persisted_user_token")
        assert db.has_agent_github_pat("agentA") is True


# ---------------------------------------------------------------------------
# (d) Live column + encrypted-at-rest round-trip through the real engine
# ---------------------------------------------------------------------------

class TestColumnAndEncryption:
    def test_live_select_and_encrypted_round_trip(self, env_db):
        from db.engine import get_engine
        from db.tables import users
        from sqlalchemy import select
        db = _db()

        db.set_user_github_pat(1, "ghp_secret_value")

        # The column exists in tables.py (else this select raises) and the stored
        # value is an encrypted envelope, NOT the plaintext.
        with get_engine().connect() as conn:
            stored = conn.execute(
                select(users.c.github_pat_encrypted).where(users.c.id == 1)
            ).scalar_one()
        assert stored is not None
        assert "ghp_secret_value" not in stored  # encrypted at rest

        # The accessor decrypts back to the original.
        assert db.get_user_github_pat(1) == "ghp_secret_value"


# ---------------------------------------------------------------------------
# (e) Facade delegation on the REAL DatabaseManager (mock-blind gap guard)
# ---------------------------------------------------------------------------

class TestFacadeDelegation:
    def test_get_set_clear_has_round_trip_through_facade(self, env_db):
        db = _db()
        assert db.has_user_github_pat(1) is False
        assert db.set_user_github_pat(1, "ghp_x") is True
        assert db.has_user_github_pat(1) is True
        assert db.get_user_github_pat(1) == "ghp_x"
        assert db.clear_user_github_pat(1) is True
        assert db.has_user_github_pat(1) is False
        assert db.get_user_github_pat(1) is None


# ---------------------------------------------------------------------------
# (f) Read endpoint never echoes the token
# ---------------------------------------------------------------------------

class TestReadEndpointNoEcho:
    def test_status_endpoint_returns_flags_only(self, env_db):
        from routers.users import get_my_github_pat_status
        from models import User
        db = _db()
        db.set_user_github_pat(1, "ghp_should_never_be_echoed")

        current = User(id=1, username="owner", role="creator")
        result = asyncio.run(get_my_github_pat_status(current_user=current))

        assert result == {"configured": True, "has_global": True}
        # Belt-and-suspenders: the token never appears in the response, whatever
        # keys are present.
        assert "ghp_should_never_be_echoed" not in str(result)
        for leaky in ("pat", "token", "github_pat", "github_pat_encrypted"):
            assert leaky not in result
