"""Git env derivation is one owner across both rebuild paths — ent#109 PR 1.

`recreate_container_with_updated_config` seeds env from the OLD container and,
before this change, re-derived only `GITHUB_PAT` — replaying whatever
`GITHUB_REPO` / `GIT_SYNC_*` each container happened to carry. Its single
production caller is `start_agent_internal`, which fires on nine config-drift
predicates **and on base-image drift at cold start**, so a base-image rebuild
arms that replay for the entire fleet. `_apply_git_env_from_db` is now the sole
writer, shared with `_apply_persisted_auth_env` (`recreate_missing_container`,
the rebuild-from-nothing path).

The four load-bearing behaviours, each with a named test:

  1. **The PAT gate is a parameter, never inherited** (`learnings.md` ent#162).
     A verbatim lift would have replaced the config-drift path's deliberate
     `#211` per-agent gate with the 2-tier per-agent -> GLOBAL resolver, baking
     the platform PAT into every tokenless container. `startup.sh`'s
     `configure_push_remote` then clears the push blackhole, and a tokenless
     agent (Cornelius) can push a private knowledge base to the shared public
     upstream.
  2. **`GIT_SYNC_AUTO` is `DB flag OR baked env`, plus a convergence backfill.**
     The two writers in `crud.py` genuinely disagree (`and not config.ephemeral`
     inside a swallowing try/except on the DB side only, column default 0), so
     DB-only derivation silently stops auto-push fleet-wide.
  3. **Set-or-clear**, because the recreate writes into a carried-forward dict.
  4. **Idempotence** — two passes produce an identical dict, so the writer can
     never feed a config-drift matcher into an infinite recreate loop.

Plus the ent#123 invariant this must not regress: the gate is the REPO, not the
PAT, so a tokenless agent rebuilt after container loss still clones (the
#843/#1439 silent-empty class).

Harness: purge-and-mock shape copied from
`test_ent123_tokenless_clone.py::_load_lifecycle` — deliberately NOT shared
cross-file (see that module's D6 note on sys.modules leaks).

Issue: abilityai/trinity-enterprise#109 (Epic ent#122)
Target: src/backend/services/agent_service/lifecycle.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_git_env.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_LIFECYCLE_SRC = (
    _PROJECT_ROOT / "src" / "backend" / "services" / "agent_service" / "lifecycle.py"
)


def _purge_real_services(monkeypatch, mocks):
    """Drop every real `services*` module not explicitly mocked so a fresh
    `from services import X` resolves the sys.modules mock, not a stale
    attribute of a previously-imported real package (1484 harness shape)."""
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)


def _load_lifecycle(monkeypatch):
    helpers = MagicMock()
    helpers.is_claude_runtime = MagicMock(return_value=False)

    routers_git = MagicMock()
    # Explicit None default: a bare MagicMock is TRUTHY, which would silently
    # satisfy every `if pat:` in the module under test.
    routers_git.get_github_pat_for_agent = MagicMock(return_value=None)

    agent_auth = MagicMock()
    agent_auth.derive_agent_token = MagicMock(return_value="tok")

    database_mod = MagicMock()
    database_mod.db.get_guardrails_config.return_value = None
    database_mod.db.get_git_config.return_value = None
    database_mod.db.get_agent_github_pat.return_value = None
    database_mod.db.set_git_auto_sync_enabled.return_value = True

    pkg = "services.agent_service"
    sibling_mocks = {
        f"{pkg}.{sib}": MagicMock()
        for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                    "files", "folders", "mcp_tool_names", "metrics",
                    "permissions", "queue", "read_only", "stats", "terminal",
                    "capabilities", "ephemeral", "pull_mode", "crud"]
    }
    sibling_mocks[f"{pkg}.helpers"] = helpers

    mocks = {
        "docker": MagicMock(),
        "docker.errors": MagicMock(),
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": database_mod,
        "routers.git": routers_git,
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": MagicMock(),
        "services.git_service": MagicMock(),
        "services.settings_service": MagicMock(),
        "services.github_service": MagicMock(),
        "services.entitlement_service": MagicMock(),
        "services.rate_limiter": MagicMock(),
        "services.agent_runtime_state": MagicMock(),
        "services.agent_auth": agent_auth,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_real_services(monkeypatch, mocks)
    import services.agent_service.lifecycle as lc
    return lc, patcher, database_mod.db, routers_git


@pytest.fixture()
def lc_env(monkeypatch):
    # TRINITY_GIT_BASE_URL is read from the live backend env; pin it off so a
    # dev shell exporting the gitea override cannot flip these assertions.
    monkeypatch.delenv("TRINITY_GIT_BASE_URL", raising=False)
    lc, patcher, db, routers_git = _load_lifecycle(monkeypatch)
    try:
        yield lc, db, routers_git
    finally:
        patcher.stop()


def _row(**overrides) -> dict:
    row = {
        "github_repo": "Abilityai/cornelius",
        "source_mode": True,
        "source_branch": "main",
        "auto_sync_enabled": False,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. The PAT gate is a parameter, never inherited (ent#162 / #211)
# ---------------------------------------------------------------------------
class TestPatGate:
    """learnings.md ent#162, verbatim: a tokenless agent plus a configured
    GLOBAL platform PAT must not come back with GITHUB_PAT baked."""

    def test_tokenless_agent_never_gets_global_platform_pat(self, lc_env):
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row()
        db.get_agent_github_pat.return_value = None       # no per-agent PAT
        rg.get_github_pat_for_agent.return_value = "ghp_GLOBAL_PLATFORM"

        env = {}                                          # tokenless container
        lc._apply_git_env_from_db("cornelius", env, pat_gate="per_agent_only")

        assert "GITHUB_PAT" not in env, (
            "ent#162: the global platform PAT was injected into a tokenless "
            "container — configure_push_remote will clear the push blackhole "
            "and the agent can push a private KB to the shared upstream"
        )
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        # ent#123 preserved: the gate is the REPO, so the clone still happens.
        assert env["GITHUB_REPO"] == "Abilityai/cornelius"
        assert env["GIT_SYNC_ENABLED"] == "true"
        assert env["GIT_SOURCE_MODE"] == "true"

    def test_effective_gate_does_resolve_the_global_pat(self, lc_env):
        """Contrast case — proves the assertion above is the GATE doing work,
        not a dead resolver. `recreate_missing_container` rebuilds from nothing
        and has always used the 2-tier resolution."""
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row()
        db.get_agent_github_pat.return_value = None
        rg.get_github_pat_for_agent.return_value = "ghp_GLOBAL_PLATFORM"

        env = {}
        lc._apply_git_env_from_db("cornelius", env, pat_gate="effective")

        assert env["GITHUB_PAT"] == "ghp_GLOBAL_PLATFORM"

    def test_per_agent_pat_row_opens_the_gate(self, lc_env):
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row()
        db.get_agent_github_pat.return_value = "ghp_PER_AGENT"
        rg.get_github_pat_for_agent.return_value = "ghp_PER_AGENT"

        env = {}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GITHUB_PAT"] == "ghp_PER_AGENT"

    def test_already_tokened_container_is_refreshed(self, lc_env):
        """The other arm of #211: a container that ALREADY carries a token has
        opted in, so the effective PAT keeps it current across rotations."""
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row()
        db.get_agent_github_pat.return_value = None
        rg.get_github_pat_for_agent.return_value = "ghp_ROTATED"

        env = {"GITHUB_PAT": "ghp_STALE"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GITHUB_PAT"] == "ghp_ROTATED"

    def test_unknown_gate_is_rejected(self, lc_env):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row()
        with pytest.raises(ValueError):
            lc._apply_git_env_from_db("a1", {}, pat_gate="whatever")

    def test_call_sites_pass_the_gate_they_need(self):
        """Static guard (the `test_agent_auth_header_guard.py` idiom): the two
        call sites must keep their own gates. Flipping the config-drift recreate
        to `effective` is the exact ent#162 credential leak, and no behavioural
        test of the helper alone would catch it."""
        src = _LIFECYCLE_SRC.read_text()

        def _body(header: str) -> str:
            start = src.index(header)
            rest = src[start + len(header):]
            end = re.search(r"^(async def |def )", rest, re.M)
            return rest[: end.start()] if end else rest

        recreate = _body("async def recreate_container_with_updated_config(")
        rebuild = _body("def _apply_persisted_auth_env(")

        assert recreate.count(
            '_apply_git_env_from_db(agent_name, env_vars, pat_gate="per_agent_only")'
        ) == 1, "the config-drift recreate must keep the #211 per-agent gate"
        assert (
            '_apply_git_env_from_db(agent_name, env_vars, pat_gate="effective")'
            in rebuild
        ), "the rebuild-from-nothing path resolves the effective PAT"


# ---------------------------------------------------------------------------
# 2. GIT_SYNC_AUTO — DB flag OR baked env, plus the convergence backfill
# ---------------------------------------------------------------------------
class TestGitSyncAuto:
    def test_baked_true_db_zero_keeps_auto_push_and_backfills(self, lc_env):
        """The disagreement is real: crud.py writes the DB flag under
        `and not config.ephemeral` inside a swallowing try/except, the env
        writer does not, and the column defaults to 0. Deriving from the DB flag
        alone would silently stop auto-push for that slice of the fleet."""
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row(
            source_mode=False, auto_sync_enabled=False
        )

        env = {"GIT_SYNC_AUTO": "true"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GIT_SYNC_AUTO"] == "true", (
            "DB-only derivation silently disabled the 15-min auto-sync "
            "heartbeat for an agent whose container was auto-pushing"
        )
        db.set_git_auto_sync_enabled.assert_called_once_with("a1", True)

    def test_db_flag_alone_enables_auto_push_without_backfill(self, lc_env):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row(
            source_mode=False, auto_sync_enabled=True
        )

        env = {}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GIT_SYNC_AUTO"] == "true"
        db.set_git_auto_sync_enabled.assert_not_called()

    def test_neither_source_clears_a_stale_flag(self, lc_env):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row(auto_sync_enabled=False)

        env = {"GIT_SYNC_AUTO": "false"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert "GIT_SYNC_AUTO" not in env
        db.set_git_auto_sync_enabled.assert_not_called()

    def test_backfill_failure_never_breaks_the_recreate(self, lc_env):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row(auto_sync_enabled=False)
        db.set_git_auto_sync_enabled.side_effect = RuntimeError("db down")

        env = {"GIT_SYNC_AUTO": "true"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GIT_SYNC_AUTO"] == "true"


# ---------------------------------------------------------------------------
# 3. Set-or-clear — the recreate writes into a carried-forward dict
# ---------------------------------------------------------------------------
class TestSetOrClear:
    def test_no_git_config_row_pops_every_owned_var(self, lc_env):
        """A deleted `agent_git_config` row is reachable from the
        `routers/git.py` orphan cleanup and `_rollback_failed_creation`. The
        recreate seeds env from the old container, so set-only would strand
        every one of these."""
        lc, db, _rg = lc_env
        db.get_git_config.return_value = None

        env = {
            "GITHUB_REPO": "old/repo",
            "GITHUB_PAT": "ghp_STALE",
            "GIT_SYNC_ENABLED": "true",
            "GIT_SOURCE_MODE": "true",
            "GIT_SOURCE_BRANCH": "main",
            "GIT_SYNC_AUTO": "true",
            "TRINITY_GIT_BASE_URL": "http://trinity-gitea-dev:3000",
            "UNRELATED": "keep-me",
        }
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        for key in lc._GIT_ENV_KEYS:
            assert key not in env, f"{key} survived a deleted git-config row"
        assert env == {"UNRELATED": "keep-me"}

    def test_source_mode_flip_clears_the_mode_branch_pair(self, lc_env):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row(
            source_mode=False, github_repo="o/new"
        )

        env = {
            "GITHUB_REPO": "o/old",
            "GIT_SOURCE_MODE": "true",
            "GIT_SOURCE_BRANCH": "release",
        }
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env["GITHUB_REPO"] == "o/new"
        assert "GIT_SOURCE_MODE" not in env
        assert "GIT_SOURCE_BRANCH" not in env

    def test_git_base_url_refreshes_from_current_backend_env(
        self, lc_env, monkeypatch
    ):
        lc, db, _rg = lc_env
        db.get_git_config.return_value = _row()

        monkeypatch.setenv("TRINITY_GIT_BASE_URL", "http://gitea:3000")
        env = {}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")
        assert env["TRINITY_GIT_BASE_URL"] == "http://gitea:3000"

        monkeypatch.delenv("TRINITY_GIT_BASE_URL")
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")
        assert "TRINITY_GIT_BASE_URL" not in env

    def test_unbound_container_loses_its_orphaned_token(self, lc_env):
        """The one deliberate divergence from a verbatim lift, asserted so it
        is a decision and not an accident: the per-agent PAT is a column ON
        `agent_git_config`, so no row means no per-agent credential and no repo
        to push to. Previously that container had its token REFRESHED from the
        global platform PAT on every recreate."""
        lc, db, rg = lc_env
        db.get_git_config.return_value = None
        rg.get_github_pat_for_agent.return_value = "ghp_GLOBAL_PLATFORM"

        env = {"GITHUB_PAT": "ghp_ORPHANED"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert "GITHUB_PAT" not in env

    def test_owned_key_set_is_complete(self, lc_env, monkeypatch):
        """`_GIT_ENV_KEYS` drives the clear sweep; a var the helper writes but
        forgets to list would never be cleared."""
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row(auto_sync_enabled=True)
        db.get_agent_github_pat.return_value = "ghp_PER_AGENT"
        rg.get_github_pat_for_agent.return_value = "ghp_PER_AGENT"
        monkeypatch.setenv("TRINITY_GIT_BASE_URL", "http://gitea:3000")

        env = {}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert set(env) == set(lc._GIT_ENV_KEYS)


# ---------------------------------------------------------------------------
# 4. Idempotence — no writer/matcher feedback loop
# ---------------------------------------------------------------------------
class TestIdempotence:
    def test_two_passes_produce_an_identical_env_dict(self, lc_env):
        """`start_agent_internal` recreates whenever a config-drift predicate
        disagrees with the container. If pass 2 produced different env than
        pass 1, every cold start would recreate forever — the hazard
        lifecycle.py already documents for the guardrails/PAT matchers."""
        lc, db, rg = lc_env
        row = _row(source_mode=False, auto_sync_enabled=False)
        db.get_git_config.return_value = row
        db.get_agent_github_pat.return_value = "ghp_PER_AGENT"
        rg.get_github_pat_for_agent.return_value = "ghp_PER_AGENT"

        # The backfill is real state: model it, so pass 2 sees the converged row.
        def _backfill(_name, enabled):
            row["auto_sync_enabled"] = enabled
            return True

        db.set_git_auto_sync_enabled.side_effect = _backfill

        env = {"GIT_SYNC_AUTO": "true", "UNRELATED": "keep-me"}
        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")
        first = dict(env)

        lc._apply_git_env_from_db("a1", env, pat_gate="per_agent_only")

        assert env == first
        # And the backfill retires itself rather than re-firing every recreate.
        assert db.set_git_auto_sync_enabled.call_count == 1

    def test_idempotent_for_the_tokenless_flagship(self, lc_env):
        lc, db, rg = lc_env
        db.get_git_config.return_value = _row()
        rg.get_github_pat_for_agent.return_value = "ghp_GLOBAL_PLATFORM"

        env = {}
        lc._apply_git_env_from_db("cornelius", env, pat_gate="per_agent_only")
        first = dict(env)
        lc._apply_git_env_from_db("cornelius", env, pat_gate="per_agent_only")

        assert env == first
        assert "GITHUB_PAT" not in env
