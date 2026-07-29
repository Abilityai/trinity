"""
Unit tests for the Cornelius first-run seeder (ent#107).

Covers the orchestration invariants surfaced by the autoplan review:
  * first-run gating via the durable `cornelius_seeded` flag (a deleted Cornelius
    is never resurrected)
  * fresh-install scoping (an established fleet with existing agents is skipped +
    flag-converged, never surprised by a heavy container on upgrade)
  * owner-must-exist deferral (pre-setup boot: skip WITHOUT burning the flag)
  * Docker-unavailable skip
  * Brain Orb flag defaulted ON, existence-guarded (never clobbers an admin OFF)
  * 409-on-exists convergence (race backstop) vs. generic-failure retry, and
    (#1790) the distinction between a 409 that means "the agent is there" and a
    409 that means "creation was blocked" — only the former may burn the flag
  * the `--workers 2` Redis SETNX provisioning lock (held → skip; Redis down →
    fail-open)
  * `_provision` builds a `github:Abilityai/cornelius` create with request=None,
    source_mode left at its default True (#1656 — the tokenless public-clone path
    of trinity-enterprise#123 rejects a non-source-mode request)
  * a real-DB smoke of `db.count_non_system_agents()` — defends the facade-
    delegation pitfall (learnings 2026-07-04) that a wholesale-mocked test misses

True unit tests: no Docker, no running backend. `create_agent_internal` and the
`db`/redis/docker seams are patched.
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.cornelius_agent_service as cas
from services.cornelius_agent_service import cornelius_agent_service as svc


class _FakeSettings:
    """In-memory stand-in for the system_settings KV store."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


@pytest.fixture
def env(monkeypatch):
    """Default: a fresh, set-up install — Docker up, admin present, zero agents,
    Redis down (lock fail-open), and `_provision` stubbed to a no-op success.
    Individual tests override handles as needed."""
    settings = _FakeSettings()
    monkeypatch.setattr(cas.db, "get_setting_value", settings.get)
    monkeypatch.setattr(cas.db, "set_setting", settings.set)
    monkeypatch.setattr(cas.db, "count_non_system_agents", lambda: 0)
    monkeypatch.setattr(
        cas.db, "get_user_by_username",
        lambda u: {"id": 1, "username": "admin", "email": "a@example.com", "role": "admin"},
    )
    monkeypatch.setattr(cas, "docker_client", object())        # Docker available
    monkeypatch.setattr(cas, "get_breaker_redis", lambda: None)  # lock fail-open
    provision = AsyncMock()
    monkeypatch.setattr(svc, "_provision", provision)
    return types.SimpleNamespace(settings=settings, provision=provision, monkeypatch=monkeypatch)


def _run():
    return asyncio.run(svc.ensure_seeded())


def test_seeds_on_fresh_install(env):
    result = _run()
    env.provision.assert_awaited_once()
    assert env.settings.get("cornelius_seeded") == "true"
    assert env.settings.get("brain_orb_enabled") == "true"   # defaulted ON
    assert result["action"] == "created"


def test_noop_when_already_seeded(env):
    env.settings.set("cornelius_seeded", "true")
    result = _run()
    env.provision.assert_not_awaited()
    assert result["action"] == "none"


def test_skip_when_existing_agents_and_converge_flag(env):
    """An established fleet (non-system agents present) is NOT provisioned, and the
    flag is set so we stop re-checking every boot — but the orb flag is left alone."""
    env.monkeypatch.setattr(cas.db, "count_non_system_agents", lambda: 3)
    result = _run()
    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") == "true"
    assert env.settings.get("brain_orb_enabled") is None      # not enabled on an established fleet
    assert result["action"] == "skipped_not_fresh"


def test_defer_when_no_admin_does_not_burn_flag(env):
    """Pre-setup boot: admin row absent. Must skip WITHOUT setting the flag so a
    later trigger/boot retries."""
    env.monkeypatch.setattr(cas.db, "get_user_by_username", lambda u: None)
    result = _run()
    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") is None       # flag NOT burned
    assert result["action"] == "deferred"


def test_skip_when_docker_unavailable(env):
    env.monkeypatch.setattr(cas, "docker_client", None)
    result = _run()
    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") is None
    assert result["action"] == "docker_unavailable"


def test_brain_orb_flag_not_clobbered_when_admin_disabled(env):
    """A fresh install where an admin has explicitly set brain_orb_enabled=false
    (e.g. via env-seeded settings) must NOT be overridden by the seed."""
    env.settings.set("brain_orb_enabled", "false")
    _run()
    env.provision.assert_awaited_once()
    assert env.settings.get("brain_orb_enabled") == "false"    # preserved
    assert env.settings.get("cornelius_seeded") == "true"


def _conflict(status_code=409):
    exc = type("FakeHTTPException", (Exception,), {})()
    exc.status_code = status_code
    return exc


def _set_presence(env, present):
    """Patch the create path's own claim predicate — the seam the seeder probes
    to tell a real "already exists" 409 from a blocked-creation 409 (#1790)."""
    import services.agent_service.crud as crud

    env.monkeypatch.setattr(crud, "agent_name_is_taken", lambda name: present)


def test_409_on_exists_is_treated_as_seeded(env):
    """A concurrent worker (or prior partial run) already created the agent →
    create raises 409 AND the name is claimed → converge the flag instead of
    erroring/retrying forever."""
    _set_presence(env, True)
    env.provision.side_effect = _conflict()
    result = _run()
    assert env.settings.get("cornelius_seeded") == "true"
    assert env.settings.get("brain_orb_enabled") == "true"
    assert result["action"] == "already_exists"


def test_409_with_no_agent_does_not_burn_flag(env):
    """#1790 regression. The create path raises 409 for conditions where nothing
    was created — the classic one being a leftover unclaimed
    `agent-cornelius-workspace` after `docker compose down -v` (#1667). Marking
    seeded there is unrecoverable: the flag suppresses every later attempt, so the
    install stays Cornelius-less forever while reporting success. The flag must
    stay unset so the next boot retries."""
    _set_presence(env, False)
    env.provision.side_effect = _conflict()
    result = _run()
    assert env.settings.get("cornelius_seeded") is None       # flag NOT burned
    assert env.settings.get("brain_orb_enabled") is None      # nothing converged
    assert result["action"] == "create_blocked"
    assert result["status"] == "error"


def test_409_presence_probe_failure_fails_toward_retry(env):
    """The probe itself blowing up must not converge the flag. The two errors are
    not symmetric: a wrong "absent" costs one redundant create attempt, a wrong
    "present" is unrecoverable without hand-editing system_settings."""
    import services.agent_service.crud as crud

    def _boom(name):
        raise RuntimeError("db down")

    env.monkeypatch.setattr(crud, "agent_name_is_taken", _boom)
    env.provision.side_effect = _conflict()
    result = _run()
    assert env.settings.get("cornelius_seeded") is None
    assert result["action"] == "create_blocked"


def test_presence_probe_reads_every_claim_source(monkeypatch):
    """The seeder must not re-derive its own notion of "exists" — it reuses the
    create path's predicate, so a claim source added there (live row, soft-deleted
    row, container-only leftover) is honoured here for free."""
    import services.agent_service.crud as crud

    sources = ["get_agent_by_name", "get_agent_owner", "is_agent_name_reserved"]
    for claimed_by in sources:
        monkeypatch.setattr(crud, "get_agent_by_name", lambda n: None)
        monkeypatch.setattr(crud.db, "get_agent_owner", lambda n: None)
        monkeypatch.setattr(crud.db, "is_agent_name_reserved", lambda n: False)
        target = crud if claimed_by == "get_agent_by_name" else crud.db
        monkeypatch.setattr(target, claimed_by, lambda n: {"agent_name": "cornelius"})
        assert crud.agent_name_is_taken("cornelius") is True, claimed_by

    # Nothing claims it → free.
    monkeypatch.setattr(crud, "get_agent_by_name", lambda n: None)
    monkeypatch.setattr(crud.db, "get_agent_owner", lambda n: None)
    monkeypatch.setattr(crud.db, "is_agent_name_reserved", lambda n: False)
    assert crud.agent_name_is_taken("cornelius") is False


def test_generic_provision_failure_does_not_burn_flag(env):
    """A real provisioning failure must leave the flag unset so the next trigger
    retries — and must never raise out of ensure_seeded."""
    env.provision.side_effect = RuntimeError("docker boom")
    result = _run()
    assert env.settings.get("cornelius_seeded") is None
    assert result["action"] == "create_failed"
    assert result["status"] == "error"


def test_provision_lock_held_skips(env):
    """Another worker holds the SETNX lock (set(nx=True) → falsy) → skip, no
    provision, flag untouched."""
    redis = MagicMock()
    redis.set.return_value = None      # nx failed — lock already held
    env.monkeypatch.setattr(cas, "get_breaker_redis", lambda: redis)
    result = _run()
    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") is None
    assert result["action"] == "skipped_locked"


def test_provision_lock_acquired_and_released(env):
    """Lock winner (set(nx=True) → truthy) provisions and releases the lock."""
    redis = MagicMock()
    redis.set.return_value = True      # we won the lock
    env.monkeypatch.setattr(cas, "get_breaker_redis", lambda: redis)
    result = _run()
    env.provision.assert_awaited_once()
    redis.set.assert_called_once()
    _, kwargs = redis.set.call_args
    assert kwargs.get("nx") is True and kwargs.get("ex")  # SETNX with a TTL
    redis.delete.assert_called_once()                     # released
    assert result["action"] == "created"


def test_provision_builds_local_template_config(monkeypatch):
    """_provision must create from the bundled LOCAL template with request=None
    (no PAT / network path)."""
    captured = {}

    async def fake_create(config, current_user, request=None):
        captured["config"] = config
        captured["request"] = request
        captured["user"] = current_user
        return MagicMock()

    fake_crud = types.ModuleType("services.agent_service.crud")
    fake_crud.create_agent_internal = fake_create
    monkeypatch.setitem(sys.modules, "services.agent_service.crud", fake_crud)

    admin = cas.User(id=1, username="admin", email="a@example.com", role="admin")
    asyncio.run(svc._provision(admin))

    assert captured["config"].template == "github:Abilityai/cornelius"
    assert captured["config"].name == "cornelius"
    # #1656: the tokenless public-repo clone (trinity-enterprise#123) is source-mode
    # only — `_gate_tokenless_request` 400s a non-source-mode request. The seeder
    # relies on the AgentConfig default rather than setting it, so pin the default.
    assert captured["config"].source_mode is True
    assert captured["request"] is None
    assert captured["user"].username == "admin"


def test_count_non_system_agents_real_db_facade():
    """Smoke the REAL db accessor through the DatabaseManager facade against the
    unit temp DB. Defends the facade-delegation pitfall (learnings 2026-07-04):
    a wholesale-mocked test would pass even if the facade delegation were missing.
    """
    from database import db as real_db
    n = real_db.count_non_system_agents()
    assert isinstance(n, int)
    assert n >= 0


# --- precomputed freshness verdict (trinity-enterprise#124) --------------------

def test_injected_fresh_true_skips_own_count(env):
    """The ent#124 orchestrator's verdict replaces the live count — sibling-
    seeded agents (the starter fleet) must not flip a fresh install to
    'not fresh' on a retry pass."""
    env.monkeypatch.setattr(
        cas.db, "count_non_system_agents",
        lambda: (_ for _ in ()).throw(AssertionError("must not count when verdict injected")),
    )

    result = asyncio.run(svc.ensure_seeded(fresh=True))

    env.provision.assert_awaited_once()
    assert result["action"] == "created"


def test_injected_fresh_false_converges_flag_without_provisioning(env):
    env.monkeypatch.setattr(cas.db, "count_non_system_agents", lambda: 0)

    result = asyncio.run(svc.ensure_seeded(fresh=False))

    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") == "true"
    assert result["action"] == "skipped_not_fresh"


def test_fresh_none_preserves_legacy_count_path(env):
    env.monkeypatch.setattr(cas.db, "count_non_system_agents", lambda: 3)

    result = asyncio.run(svc.ensure_seeded(fresh=None))

    env.provision.assert_not_awaited()
    assert env.settings.get("cornelius_seeded") == "true"
    assert result["action"] == "skipped_not_fresh"
