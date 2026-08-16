"""
Unit tests for the first-run default-system seeder (trinity-enterprise#124).

Covers the invariants surfaced by the autoplan review:
  * the REAL bundled manifest validates through the executor's own parser and
    every `local:` template it references exists in-tree (learnings 2026-07-23:
    an absent local: template silently creates a blank agent — deploy reports
    "deployed" and the flag latches)
  * first-run gating via the durable `default_system_seeded` flag; deleting a
    seeded fleet never re-provisions
  * the persisted first-run verdict (`first_run_fresh`): computed once BEFORE
    the Cornelius seeder runs, consumed by both seeders, reused on later
    passes (the cross-pass poisoning bug both review voices flagged)
  * disable sentinels on TRINITY_DEFAULT_SYSTEM_MANIFEST (no flag burn)
  * override-path resolution: an explicitly-set-but-unreadable override fails
    loudly (operator alert) and never falls back to the bundled manifest
  * flag policy per deploy status: deployed/partial → set; failed (0 created)
    / exception → NOT set (retry-safe: nothing exists to duplicate)
  * the existence backstop: any already-reserved `{system}-{short}` name
    converges the flag WITHOUT deploying (the deploy path suffixes collisions
    instead of 409ing, so a fail-open lock race would otherwise double-seed)
  * the SETNX lock (held → skip; Redis down → fail-open)
  * operator-queue alerts on partial/failed/unreadable-override (best-effort)

True unit tests: no Docker, no running backend, no Redis. The db/docker/redis
seams and `system_service.deploy_manifest` are patched.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.system_seed_service as sss
import services.system_service as system_service
from services.system_seed_service import (
    ensure_first_run_seeded,
    system_seed_service as svc,
)
from models import SystemDeployFailure, SystemDeployResponse

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST = REPO_ROOT / "config" / "manifests" / "default-system.yaml"

# #1931: the two on-disk checks below are properties of ANY bundled manifest,
# not just the auto-seeded one — a demo manifest referencing a template that
# ships no CLAUDE.md deploys an agent with no instructions just as silently.
# Enumerate by glob so the next bundled manifest is guarded on arrival.
ALL_MANIFESTS = sorted((REPO_ROOT / "config" / "manifests").glob("*.yaml"))
ALL_MANIFEST_IDS = [p.name for p in ALL_MANIFESTS]

TEST_MANIFEST = """
name: testsys
agents:
  one:
    template: local:one
  two:
    template: local:two
"""


class _FakeSettings:
    """In-memory stand-in for the system_settings KV store."""

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


def _resp(status, created, failed=()):
    return SystemDeployResponse(
        status=status,
        system_name="testsys",
        agents_created=list(created),
        prompt_updated=False,
        warnings=[],
        failed=[
            SystemDeployFailure(
                name=n, short_name=n.split("-", 1)[1], template="local:x",
                reason="boom", status_code=500,
            )
            for n in failed
        ],
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Default: a fresh, set-up install — Docker up, admin present, Redis down
    (lock fail-open), verdict fresh=True, deploy fully succeeds against a tmp
    bundled manifest. Individual tests override handles as needed."""
    # Own the lazily-imported module so a sibling's sys.modules stub can't leak
    # into the call-time `from services.system_service import deploy_manifest`
    # (learnings 2026-07-12).
    monkeypatch.setitem(sys.modules, "services.system_service", system_service)

    settings = _FakeSettings()
    monkeypatch.setattr(sss.db, "get_setting_value", settings.get)
    monkeypatch.setattr(sss.db, "set_setting", settings.set)
    monkeypatch.setattr(sss.db, "count_non_system_agents", lambda: 0)
    monkeypatch.setattr(sss.db, "is_agent_name_reserved", lambda n: False)
    opq = MagicMock()
    monkeypatch.setattr(sss.db, "create_operator_queue_item", opq)
    monkeypatch.setattr(
        sss.db, "get_user_by_username",
        lambda u: {"id": 1, "username": "admin", "email": "a@example.com", "role": "admin"},
    )
    monkeypatch.setattr(sss, "docker_client", object())          # Docker available
    monkeypatch.setattr(sss, "get_breaker_redis", lambda: None)  # lock fail-open
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)

    bundled = tmp_path / "default-system.yaml"
    bundled.write_text(TEST_MANIFEST, encoding="utf-8")
    monkeypatch.setattr(sss, "BUNDLED_MANIFEST_PATH", str(bundled))

    deploy = AsyncMock(return_value=_resp("deployed", ["testsys-one", "testsys-two"]))
    monkeypatch.setattr(system_service, "deploy_manifest", deploy)

    return types.SimpleNamespace(
        settings=settings, deploy=deploy, opq=opq,
        bundled=bundled, monkeypatch=monkeypatch,
    )


def _run(fresh=True):
    return asyncio.run(svc.ensure_seeded(fresh=fresh))


# --- the real bundled manifest -------------------------------------------------

def test_bundled_manifest_parses_and_validates():
    """Executor's-own-parser check over the file that actually ships."""
    manifest = system_service.parse_manifest(REAL_MANIFEST.read_text(encoding="utf-8"))
    warnings = system_service.validate_manifest(manifest)

    assert warnings == []
    assert manifest.name == "acme"
    assert set(manifest.agents) == {"scout", "sage", "scribe"}
    # Never mutate the platform-wide trinity_prompt from the seed.
    assert manifest.prompt is None
    # Zero-credential installs must not accumulate failing cron executions.
    for cfg in manifest.agents.values():
        assert cfg.schedules is None
    # The trio's CLAUDE.md content assumes the acme team shape.
    assert manifest.permissions.preset == "full-mesh"


@pytest.mark.parametrize("manifest_path", ALL_MANIFESTS, ids=ALL_MANIFEST_IDS)
def test_bundled_manifest_local_templates_exist_in_tree(manifest_path):
    """learnings 2026-07-23: an absent local: template used to create a BLANK
    agent — deploy reported success and the seed flag latched. #1759 closed
    that (create now 404s `UNKNOWN_LOCAL_TEMPLATE`), so this check is now
    belt-and-braces rather than the only line of defence. KEPT deliberately: it
    fails at collection time with a precise message naming the offending
    manifest entry, whereas the runtime gate would surface as a first-run seed
    landing in the operator queue. It also still covers the CLAUDE.md half
    (ent#239), which the create gate does not check at all.

    #1931: widened from `default-system.yaml` to every bundled manifest. Every
    manifest the repo ships is `local:`-only today, which is the property that
    keeps a deploy PAT-free — asserting it here is what keeps it true."""
    manifest = system_service.parse_manifest(manifest_path.read_text(encoding="utf-8"))
    for short, cfg in manifest.agents.items():
        assert cfg.template.startswith("local:"), (
            f"{manifest_path.name}/{short}: PAT-free deploy requires local: templates"
        )
        template_dir = REPO_ROOT / "config" / "agent-templates" / cfg.template.split(":", 1)[1]
        assert (template_dir / "template.yaml").is_file(), (
            f"{manifest_path.name}/{short}: manifest references '{cfg.template}' but "
            f"{template_dir}/template.yaml does not exist — this would seed a blank agent"
        )
        # ent#239: template.yaml alone still deploys "successfully" but seeds an
        # agent with no instructions — CLAUDE.md is the zero-cred usefulness bar.
        assert (template_dir / "CLAUDE.md").is_file(), (
            f"{manifest_path.name}/{short}: '{cfg.template}' ships no CLAUDE.md — the "
            f"agent would deploy but have no instructions (fails the zero-cred useful bar)"
        )


@pytest.mark.parametrize("manifest_path", ALL_MANIFESTS, ids=ALL_MANIFEST_IDS)
def test_bundled_manifest_templates_ship_declared_commands(manifest_path):
    """ent#239 live finding: Claude Code intercepts any leading-`/` message
    before the model sees it, so a command that is only *documented* (in
    template.yaml / CLAUDE.md prose) fails as typed — `/research` returned
    "Unknown command: /research" and `/status` hit the built-in. Every command
    a seeded template declares must ship a real `.claude/commands/<name>.md`
    (the pattern trinity-system / demo-* templates already follow); a shipped
    file also shadows same-named built-ins (verified live for /status).

    #1931: widened to every bundled manifest — a demo fleet's commands fail as
    typed exactly the same way the seeded fleet's would."""
    import yaml

    manifest = system_service.parse_manifest(manifest_path.read_text(encoding="utf-8"))
    for short, cfg in manifest.agents.items():
        template_dir = REPO_ROOT / "config" / "agent-templates" / cfg.template.split(":", 1)[1]
        declared = [
            c["name"]
            for c in (yaml.safe_load((template_dir / "template.yaml").read_text()) or {}).get("commands", [])
        ]
        for cmd in declared:
            cmd_file = template_dir / ".claude" / "commands" / f"{cmd}.md"
            assert cmd_file.is_file(), (
                f"{manifest_path.name}/{short}: template.yaml declares command "
                f"'{cmd}' but ships no {cmd_file.relative_to(REPO_ROOT)} — as "
                f"typed, /{cmd} is intercepted by the CLI and fails "
                f"('Unknown command')"
            )


# --- gating --------------------------------------------------------------------

def test_seeds_on_fresh_install(env):
    result = _run()

    env.deploy.assert_awaited_once()
    manifest_arg = env.deploy.await_args.args[0]
    assert "testsys" in manifest_arg
    assert env.deploy.await_args.kwargs.get("strict") is False
    assert env.settings.get("default_system_seeded") == "true"
    info = json.loads(env.settings.get("default_system_seed_info"))
    assert info["status"] == "deployed"
    assert info["source"] == "bundled"
    assert info["agents_created"] == 2
    assert result["action"] == "created"


def test_noop_when_already_seeded(env):
    env.settings.set("default_system_seeded", "true")
    result = _run()
    env.deploy.assert_not_awaited()
    assert result["action"] == "none"


def test_skip_when_docker_unavailable(env):
    env.monkeypatch.setattr(sss, "docker_client", None)
    result = _run()
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "docker_unavailable"


@pytest.mark.parametrize("sentinel", ["disabled", "none", "off", "0", "false", " Disabled "])
def test_disable_sentinel_skips_without_burning_flag(env, sentinel):
    """Re-enabling later on a still-fresh install must still seed."""
    env.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, sentinel)
    result = _run()
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "disabled"


def test_not_fresh_converges_flag_without_deploying(env):
    result = _run(fresh=False)
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") == "true"
    assert result["action"] == "skipped_not_fresh"


def test_undetermined_verdict_skips_without_flag(env):
    result = _run(fresh=None)
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "skipped_error"


def test_defer_when_no_admin_does_not_burn_flag(env):
    env.monkeypatch.setattr(sss.db, "get_user_by_username", lambda u: None)
    result = _run()
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "deferred"


# --- manifest resolution -------------------------------------------------------

def test_override_path_used_when_set(env, tmp_path):
    override = tmp_path / "custom.yaml"
    override.write_text(TEST_MANIFEST.replace("testsys", "customsys"), encoding="utf-8")
    env.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, str(override))
    env.deploy.return_value = _resp("deployed", ["customsys-one", "customsys-two"])

    result = _run()

    assert "customsys" in env.deploy.await_args.args[0]
    info = json.loads(env.settings.get("default_system_seed_info"))
    assert info["source"] == "override"
    assert result["action"] == "created"


def test_unreadable_override_fails_loudly_and_never_falls_back(env, tmp_path):
    """Operator intent gone wrong: no silent fallback to the bundle, no flag
    burn (retry after the operator fixes it), one loud alert."""
    env.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, str(tmp_path / "missing.yaml"))

    result = _run()

    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "manifest_unavailable"
    env.opq.assert_called_once()
    agent_name, item = env.opq.call_args.args
    assert agent_name == "trinity-system"
    assert item["id"] == "system-seed-override-unreadable"


def test_empty_env_means_bundled(env):
    """Compose `${VAR:-}` arrives set-but-empty (#1076 class) — must resolve
    to the bundled manifest, not a path or a sentinel."""
    env.monkeypatch.setenv(sss.MANIFEST_ENV_VAR, "  ")
    result = _run()
    env.deploy.assert_awaited_once()
    assert result["action"] == "created"


def test_missing_bundled_manifest_skips_without_flag(env):
    env.bundled.unlink()
    result = _run()
    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "manifest_unavailable"


# --- flag policy per deploy outcome --------------------------------------------

def test_partial_deploy_sets_flag_and_alerts(env):
    """A partial fleet must NEVER be re-deployed (re-deploy suffixes duplicates
    of the survivors) — flag latches, operator gets one alert."""
    env.deploy.return_value = _resp("partial", ["testsys-one"], failed=["testsys-two"])

    result = _run()

    assert env.settings.get("default_system_seeded") == "true"
    info = json.loads(env.settings.get("default_system_seed_info"))
    assert info["status"] == "partial"
    assert info["agents_failed"] == 1
    env.opq.assert_called_once()
    assert env.opq.call_args.args[1]["id"] == "system-seed-partial"
    assert result["status"] == "partial"


def test_failed_deploy_does_not_burn_flag(env):
    """0 agents created ⇒ nothing to duplicate ⇒ a later pass may retry."""
    env.deploy.return_value = _resp("failed", [], failed=["testsys-one", "testsys-two"])

    result = _run()

    assert env.settings.get("default_system_seeded") is None
    env.opq.assert_called_once()
    assert env.opq.call_args.args[1]["id"] == "system-seed-failed"
    assert result["action"] == "create_failed"


def test_deploy_exception_never_raises_and_does_not_burn_flag(env):
    """Also covers a parse-broken override (deploy_manifest raises
    HTTPException(400)): no flag, and one loud `system-seed-error` alert so a
    broken override isn't a silent every-boot retry loop."""
    env.deploy.side_effect = RuntimeError("docker exploded")
    result = _run()
    assert env.settings.get("default_system_seeded") is None
    assert result["action"] == "create_failed"
    assert result["status"] == "error"
    env.opq.assert_called_once()
    assert env.opq.call_args.args[1]["id"] == "system-seed-error"


# --- existence backstop ----------------------------------------------------------

def test_reserved_agent_name_converges_flag_without_deploying(env):
    """The deploy path suffixes name collisions instead of 409ing, so this is
    the duplicate guard when the SETNX lock fails open (`--workers 2`, Redis
    down): a reserved final name ⇒ a prior/concurrent seed already ran. A
    partially-reserved fleet (crash mid-create) still alerts — honest status."""
    env.monkeypatch.setattr(
        sss.db, "is_agent_name_reserved", lambda n: n == "testsys-one"
    )

    result = _run()

    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") == "true"
    info = json.loads(env.settings.get("default_system_seed_info"))
    assert info["status"] == "converged"
    assert result["action"] == "already_exists"
    # 1/2 agents reserved ⇒ interrupted seed ⇒ the converge path alerts.
    env.opq.assert_called_once()
    assert env.opq.call_args.args[1]["id"] == "system-seed-partial"


def test_fully_reserved_fleet_converges_silently(env):
    """All final names reserved ⇒ a completed prior seed — converge without an
    operator alert."""
    env.monkeypatch.setattr(sss.db, "is_agent_name_reserved", lambda n: True)

    result = _run()

    env.deploy.assert_not_awaited()
    assert env.settings.get("default_system_seeded") == "true"
    env.opq.assert_not_called()
    assert result["action"] == "already_exists"


# --- provisioning lock -----------------------------------------------------------

def test_lock_held_by_other_worker_skips(env):
    redis = MagicMock()
    redis.set.return_value = False  # SETNX lost
    env.monkeypatch.setattr(sss, "get_breaker_redis", lambda: redis)

    result = _run()

    env.deploy.assert_not_awaited()
    assert result["action"] == "skipped_locked"
    assert env.settings.get("default_system_seeded") is None


def _store_backed_redis():
    """A MagicMock Redis that honours SETNX + GET + DELETE against a real dict —
    so the #1920 compare-and-delete release (`SingleFlightLock.release_if_owned`)
    can match the internally-minted token. A bare MagicMock returns a fresh
    MagicMock on GET (never == the token), which would wrongly read as 'not our
    lock'; and a fixed `set.return_value` can't model SETNX losing on a held key."""
    redis = MagicMock()
    store = {}

    def _set(key, val, nx=None, ex=None):
        if nx and key in store:
            return None  # SETNX loses on an already-held key
        store[key] = val
        return True

    redis.set.side_effect = _set
    redis.get.side_effect = lambda key: store.get(key)
    redis.delete.side_effect = lambda key: store.pop(key, None)
    redis.store = store
    return redis


def test_lock_winner_deploys_and_releases(env):
    """#1920: release is an ownership-checked compare-and-delete against the
    minted token — it deletes our own live lock (not a tokenless delete)."""
    redis = _store_backed_redis()
    env.monkeypatch.setattr(sss, "get_breaker_redis", lambda: redis)

    result = _run()

    env.deploy.assert_awaited_once()
    # The SETNX stored a real uuid token; the release GET matched it -> DELETE.
    redis.delete.assert_called_once_with(sss._PROVISION_LOCK_KEY)
    assert result["action"] == "created"
    assert redis.store.get(sss._PROVISION_LOCK_KEY) is None  # lock is free again


def test_release_never_deletes_a_successor_lock(env):
    """THE #1920 property: worker A's TTL lapses, worker B acquires a FRESH
    lock, A finishes and releases — A's compare-and-delete must leave B's live
    lock intact (the pre-#1920 tokenless `delete` removed it)."""
    redis = _store_backed_redis()
    env.monkeypatch.setattr(sss, "get_breaker_redis", lambda: redis)

    # A acquires (via the real _acquire_lock, minting A's token).
    lock_a = svc._acquire_lock()
    assert lock_a is not None

    # A's lease TTL-expires and worker B SETNX-acquires a fresh token.
    redis.store[sss._PROVISION_LOCK_KEY] = "worker-B-token"

    # A finishes and releases — must NOT touch B's live lock.
    svc._release_lock(lock_a)
    redis.delete.assert_not_called()
    assert redis.store[sss._PROVISION_LOCK_KEY] == "worker-B-token"


def test_lock_fails_open_when_redis_down_and_still_seeds(env):
    """Redis unreachable (client None) → the lock fails open (sole worker) and
    the seed still runs; release is a safe no-op."""
    env.monkeypatch.setattr(sss, "get_breaker_redis", lambda: None)

    result = _run()

    env.deploy.assert_awaited_once()
    assert result["action"] == "created"


def test_two_overlapping_in_process_passes_do_not_leak(env):
    """The lock is LOCAL to each pass (never on the singleton), so pass B's
    acquire can't make pass A's `finally` no-op and leak A's real lock for the
    TTL. Both passes run against ONE store-backed Redis: the winner deploys +
    releases, the loser skips, and the key ends up free."""
    redis = _store_backed_redis()
    env.monkeypatch.setattr(sss, "get_breaker_redis", lambda: redis)

    # Pass A acquires first (real token minted, key now set).
    lock_a = svc._acquire_lock()
    assert lock_a is not None
    # Pass B, concurrently, finds the key held → skips (no lock object).
    lock_b = svc._acquire_lock()
    assert lock_b is None

    # A finishes and releases its OWN lock — the key is freed exactly once.
    svc._release_lock(lock_a)
    svc._release_lock(lock_b)  # no-op: B never held anything
    redis.delete.assert_called_once_with(sss._PROVISION_LOCK_KEY)
    assert redis.store.get(sss._PROVISION_LOCK_KEY) is None


# --- the orchestrator (persisted first-run verdict) ------------------------------

@pytest.fixture
def orch(env):
    """ensure_first_run_seeded with Cornelius + the fleet seeder stubbed;
    records the verdict each seeder receives and what was persisted at the
    moment Cornelius ran."""
    calls = []

    async def fake_cornelius(fresh=None):
        calls.append(("cornelius", fresh, env.settings.get("first_run_fresh")))
        return {}

    async def fake_fleet(fresh=None):
        calls.append(("fleet", fresh, env.settings.get("first_run_fresh")))
        return {"action": "created"}

    env.monkeypatch.setattr(
        sss.cornelius_agent_service, "ensure_seeded", fake_cornelius
    )
    env.monkeypatch.setattr(svc, "ensure_seeded", fake_fleet)
    return types.SimpleNamespace(env=env, calls=calls)


def test_orchestrator_computes_verdict_once_before_cornelius(orch):
    """The verdict must be computed and persisted BEFORE Cornelius provisions —
    otherwise the seeded Cornelius (a non-system agent) makes every later
    pass read the install as established (the cross-pass poisoning bug)."""
    counted = []
    orch.env.monkeypatch.setattr(
        sss.db, "count_non_system_agents", lambda: counted.append(1) or 0
    )

    asyncio.run(ensure_first_run_seeded())

    assert len(counted) == 1                       # computed exactly once
    assert orch.calls[0][0] == "cornelius"         # cornelius first
    assert orch.calls[0][1] is True                # received the verdict
    assert orch.calls[0][2] == "true"              # ...already persisted by then
    assert orch.calls[1] == ("fleet", True, "true")


def test_orchestrator_reuses_stored_verdict(orch):
    """A later pass must consume the persisted verdict, never recount — the
    fleet/Cornelius agents now exist and would poison the count."""
    orch.env.settings.set("first_run_fresh", "true")
    orch.env.monkeypatch.setattr(
        sss.db, "count_non_system_agents",
        lambda: (_ for _ in ()).throw(AssertionError("must not recount")),
    )

    asyncio.run(ensure_first_run_seeded())

    assert orch.calls == [("cornelius", True, "true"), ("fleet", True, "true")]


def test_orchestrator_cornelius_era_install_is_not_fresh(orch):
    """Pin: an ent#107-era install (cornelius_seeded set — possibly with every
    agent since deleted) upgrading to ent#124 gets NO starter fleet."""
    orch.env.settings.set("cornelius_seeded", "true")

    asyncio.run(ensure_first_run_seeded())

    assert orch.calls == [("cornelius", False, "false"), ("fleet", False, "false")]


def test_orchestrator_count_failure_defers_without_persisting(orch):
    orch.env.monkeypatch.setattr(
        sss.db, "count_non_system_agents",
        lambda: (_ for _ in ()).throw(RuntimeError("db locked")),
    )

    asyncio.run(ensure_first_run_seeded())

    # None verdict passed through; nothing persisted → a later pass recomputes.
    assert orch.calls == [("cornelius", None, None), ("fleet", None, None)]
    assert orch.env.settings.get("first_run_fresh") is None


def test_orchestrator_never_raises_when_cornelius_explodes(orch):
    async def boom(fresh=None):
        raise RuntimeError("cornelius exploded")

    orch.env.monkeypatch.setattr(sss.cornelius_agent_service, "ensure_seeded", boom)

    result = asyncio.run(ensure_first_run_seeded())

    # Fleet seeder still ran despite the Cornelius failure.
    assert ("fleet", True, "true") in orch.calls
    assert result == {"action": "created"}
