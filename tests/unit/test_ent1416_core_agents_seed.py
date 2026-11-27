"""
A fresh install comes with the Company Brain and a Chief of Staff
(abilityai/trinity-enterprise#1416).

Driven at the layer each change lives in:

  * the first-run orchestrator (`system_seed_service.ensure_first_run_seeded`)
    with the REAL Cornelius and Chief of Staff seeders; only the create door
    (`routers.agents.create_agent_internal`), the settings KV, Docker presence,
    Redis and the tag/permission writes are stand-ins;
  * the create path's memory ceiling (`crud._apply_memory_ceiling`) and its
    hand-off inside the real `crud.create_agent_internal`;
  * the first-run state the browser reads (`onboarding_service`).

The Chief of Staff template (`github:Abilityai/corbin`) is never fetched here:
the create door is the boundary, and the name is what is pinned.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import MagicMock

import pytest

import services.cornelius_agent_service as cas
import services.system_seed_service as sss
import services.system_service as system_service
from services.system_seed_service import ensure_first_run_seeded

pytestmark = pytest.mark.unit


class _Settings:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


@pytest.fixture
def install(monkeypatch):
    """A fresh, set-up install. The create door records each create and
    "creates" the agent (so the owner lookup sees it) unless told to fail."""
    settings = _Settings()
    agents = set()
    created = []
    fail = {}
    tags = {}
    permissions = []

    for mod in (sss, cas):
        monkeypatch.setattr(mod, "docker_client", object())
        monkeypatch.setattr(mod, "get_breaker_redis", lambda: None)
    monkeypatch.setattr(sss.db, "get_setting_value", settings.get)
    monkeypatch.setattr(sss.db, "set_setting", settings.set)
    monkeypatch.setattr(sss.db, "count_non_system_agents", lambda: len(agents))
    monkeypatch.setattr(sss.db, "is_agent_name_reserved", lambda n: False)
    monkeypatch.setattr(
        sss.db, "get_user_by_username",
        lambda u: {"id": 1, "username": "admin", "email": "a@example.com", "role": "admin"},
    )
    monkeypatch.setattr(sss.db, "get_agent_owner", lambda n: {"owner_id": 1} if n in agents else None)
    monkeypatch.setattr(sss.db, "add_agent_tag", lambda n, t: tags.setdefault(n, set()).add(t))
    monkeypatch.setattr(
        sss.db, "add_agent_permission",
        lambda src, dst, by: permissions.append((src, dst, by)),
    )
    observe = MagicMock(return_value="created")
    clear = MagicMock(return_value=0)
    monkeypatch.setattr(sss.platform_alerts, "observe", observe)
    monkeypatch.setattr(sss.platform_alerts, "clear", clear)
    monkeypatch.setattr(sss, "_connect_seeded_agents", lambda *r: None)
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)
    monkeypatch.setitem(sys.modules, "services.system_service", system_service)
    deploy = MagicMock(side_effect=AssertionError("the Acme fleet must not be deployed"))
    monkeypatch.setattr(system_service, "deploy_manifest", deploy)

    async def fake_create(config, current_user, request=None,
                          allow_unforked_pull_only=False, memory_ceiling=None):
        created.append({
            "name": config.name, "template": config.template, "kind": config.kind,
            "source_mode": config.source_mode, "pull_only": allow_unforked_pull_only,
            "memory_ceiling": memory_ceiling,
        })
        if config.name in fail:
            raise fail[config.name]
        agents.add(config.name)
        return MagicMock()

    fake_router = types.ModuleType("routers.agents")
    fake_router.create_agent_internal = fake_create
    monkeypatch.setitem(sys.modules, "routers.agents", fake_router)
    # The 409 presence probe (#1790) reads the create path's own predicate.
    import services.agent_service.crud as crud
    monkeypatch.setattr(crud, "agent_name_is_taken", lambda n: n in agents)

    return types.SimpleNamespace(
        settings=settings, agents=agents, created=created, fail=fail, tags=tags,
        permissions=permissions, observe=observe, clear=clear, deploy=deploy,
    )


def _pass():
    return asyncio.run(ensure_first_run_seeded())


def _alarm_kinds(observe):
    return [c.args[2] for c in observe.call_args_list]


class TestFreshInstall:
    def test_both_core_agents_are_created_and_nothing_else(self, install):
        _pass()

        assert [c["name"] for c in install.created] == ["cornelius", "corbin"]
        assert install.agents == {"cornelius", "corbin"}
        install.deploy.assert_not_called()   # the three Acme agents are gone

    def test_the_chief_of_staff_follows_the_brains_rules(self, install):
        _pass()

        corbin = next(c for c in install.created if c["name"] == "corbin")
        assert corbin["template"] == "github:Abilityai/corbin"
        assert corbin["kind"] == "deployment"      # pinned pull-only (ent#705)
        assert corbin["source_mode"] is True       # tokenless public clone (ent#123)
        assert corbin["pull_only"] is True         # fork_to_own door, pull-only shape
        assert install.settings.get("chief_of_staff_seeded") == "true"

    def test_memory_ceilings_go_to_the_create_door(self, install):
        _pass()

        assert {c["name"]: c["memory_ceiling"] for c in install.created} == {
            "cornelius": "2g", "corbin": "2g",
        }

    def test_they_are_one_named_group_with_a_line_and_a_permission(self, install):
        _pass()

        assert install.tags["corbin"] == {"dept-headquarters"}
        assert install.tags["cornelius"] == {"dept-headquarters", "reports-to-corbin"}
        assert install.permissions == [("corbin", "cornelius", "admin")]

    def test_the_brain_orb_is_still_switched_on_by_the_brain_only(self, install):
        _pass()

        assert install.settings.get("brain_orb_enabled") == "true"


class TestChiefOfStaffMiss:
    def test_a_miss_leaves_the_brain_and_is_reported(self, install):
        install.fail["corbin"] = RuntimeError("repository not found")

        _pass()

        assert install.agents == {"cornelius"}
        assert install.settings.get("cornelius_seeded") == "true"
        assert install.settings.get("chief_of_staff_seeded") is None   # retried later
        assert "chief-of-staff-failed" in _alarm_kinds(install.observe)
        # The Brain is grouped; no line and no permission to a missing agent.
        assert install.tags == {"cornelius": {"dept-headquarters"}}
        assert install.permissions == []

    def test_the_next_boot_retries_once_and_organizes(self, install):
        install.fail["corbin"] = RuntimeError("repository not found")
        _pass()
        install.fail.clear()

        _pass()

        assert [c["name"] for c in install.created].count("corbin") == 2
        assert [c["name"] for c in install.created].count("cornelius") == 1
        assert install.agents == {"cornelius", "corbin"}
        assert install.permissions == [("corbin", "cornelius", "admin")]
        assert "reports-to-corbin" in install.tags["cornelius"]
        install.clear.assert_any_call("trinity-system", "system_seed", "chief-of-staff-failed")

    def test_a_retry_that_finds_it_already_there_does_not_duplicate(self, install):
        """A 409 with the agent present converges the flag (#1790 rule)."""
        install.settings.set("first_run_fresh", "true")
        install.settings.set("chief_of_staff_fresh", "true")
        install.settings.set("cornelius_seeded", "true")
        install.agents.update({"cornelius", "corbin"})
        exc = RuntimeError("Agent already exists")
        exc.status_code = 409
        install.fail["corbin"] = exc

        _pass()

        assert install.settings.get("chief_of_staff_seeded") == "true"
        assert install.agents == {"cornelius", "corbin"}

    def test_a_later_pass_that_creates_nothing_never_reorganizes(self, install):
        _pass()
        install.tags.clear()
        install.permissions.clear()   # the person removed them

        _pass()

        assert install.tags == {} and install.permissions == []


class TestExistingInstall:
    def test_an_install_seeded_before_this_version_gets_no_new_agent(self, install):
        """Upgrade: first run already finished under an earlier version."""
        install.settings.set("first_run_fresh", "true")
        install.settings.set("cornelius_seeded", "true")
        install.agents.add("cornelius")

        _pass()

        assert install.created == []
        assert install.settings.get("chief_of_staff_fresh") == "false"
        assert install.settings.get("chief_of_staff_seeded") == "true"

    def test_an_acme_seeded_install_gets_no_new_agent(self, install):
        install.settings.set("first_run_fresh", "true")
        install.settings.set("default_system_seeded", "true")

        _pass()

        assert "corbin" not in [c["name"] for c in install.created]

    def test_an_established_fleet_gets_neither(self, install):
        install.agents.add("my-agent")

        _pass()

        assert install.created == []

    def test_a_stored_fresh_verdict_with_only_corbin_seeded_is_kept(self, install):
        """#3262's stale-verdict reconcile must count the new flag: a Brain that
        failed beside a seeded Chief of Staff is retried, not abandoned."""
        install.settings.set("first_run_fresh", "true")
        install.settings.set("chief_of_staff_fresh", "true")
        install.settings.set("chief_of_staff_seeded", "true")
        install.agents.add("corbin")

        _pass()

        assert [c["name"] for c in install.created] == ["cornelius"]


class TestDefaultManifest:
    def test_no_manifest_is_deployed_unless_named(self, install):
        result = asyncio.run(sss.system_seed_service.ensure_seeded(fresh=True))

        assert result["action"] == "no_default_manifest"
        assert install.settings.get("default_system_seeded") is None
        install.deploy.assert_not_called()

    def test_the_bundled_fleet_is_still_one_setting_away(self, install, monkeypatch):
        from unittest.mock import AsyncMock
        from models import SystemDeployResponse

        from pathlib import Path
        repo = Path(__file__).resolve().parents[2]
        monkeypatch.setattr(sss, "BUNDLED_MANIFEST_PATH", str(repo / sss.BUNDLED_MANIFEST_PATH))
        monkeypatch.setenv(sss.MANIFEST_ENV_VAR, "bundled")
        deploy = AsyncMock(return_value=SystemDeployResponse(
            status="deployed", system_name="acme",
            agents_created=["acme-scout", "acme-sage", "acme-scribe"],
            prompt_updated=False, warnings=[], failed=[],
        ))
        monkeypatch.setattr(system_service, "deploy_manifest", deploy)

        result = asyncio.run(sss.system_seed_service.ensure_seeded(fresh=True))

        assert result["status"] != "skipped"
        deploy.assert_awaited_once()
        assert "name: acme" in deploy.await_args.args[0]


class TestSeededNames:
    def test_nothing_is_derived_from_the_bundled_file_by_default(self, install):
        assert sss.seeded_agent_names() == set()

    def test_what_an_earlier_seed_created_still_counts(self, install):
        install.settings.set("default_system_seed_info", json.dumps(
            {"system": "acme", "agents_created": ["acme-scout", "acme-sage"]}
        ))

        assert sss.seeded_agent_names() == {"acme-scout", "acme-sage"}


class TestMemoryCeiling:
    def _config(self, memory):
        from models import AgentConfig
        return AgentConfig(name="x", template="github:o/r", resources={"cpu": "2", "memory": memory})

    def test_caps_a_larger_declaration(self, monkeypatch):
        import services.agent_service.crud as crud
        cfg = self._config("4g")
        original = cfg.resources

        crud._apply_memory_ceiling(cfg, "2g")

        assert cfg.resources == {"cpu": "2", "memory": "2g"}
        assert original["memory"] == "4g"     # the template's own dict untouched

    def test_keeps_a_smaller_declaration(self):
        import services.agent_service.crud as crud
        cfg = self._config("1g")

        crud._apply_memory_ceiling(cfg, "2g")

        assert cfg.resources["memory"] == "1g"

    def test_an_invalid_declaration_is_left_for_the_named_400(self):
        import services.agent_service.crud as crud
        cfg = self._config("512Mi")

        crud._apply_memory_ceiling(cfg, "2g")

        assert cfg.resources["memory"] == "512Mi"

    def test_create_applies_it_before_staging(self, monkeypatch):
        """The real `crud.create_agent_internal`: the config that reaches
        `_stage_config_files` (labels, limits, config file) is capped."""
        import services.agent_service.crud as crud
        from models import User

        seen = {}

        class _Stop(Exception):
            pass

        def stage(config, template_data, path):
            seen["memory"] = config.resources["memory"]
            raise _Stop()

        async def enforce(*a, **k):
            return None

        async def resolve(config, user, allow_unforked_pull_only=False):
            config.resources = {"cpu": "2", "memory": "4g"}
            return types.SimpleNamespace(template_data={}, github_template_path=None)

        async def no_leftover(config, adopt):
            return None

        monkeypatch.setattr(crud, "_stage_config_files", stage)
        monkeypatch.setattr(crud, "_resolve_template", resolve)
        monkeypatch.setattr(crud.template_source_policy, "enforce", enforce)
        monkeypatch.setattr(crud, "_apply_ephemeral_pregates", lambda c, u: None)
        monkeypatch.setattr(crud, "_check_name_availability", lambda c: None)
        monkeypatch.setattr(crud, "_guard_leftover_workspace_volume", no_leftover)
        monkeypatch.setattr(crud, "clear_agent_breakers", lambda n: None)
        monkeypatch.setattr(crud, "_enforce_role_quota", lambda c, u: None)
        monkeypatch.setattr(crud, "validate_base_image", lambda b: None)
        cfg = self._config("4g")
        user = User(id=1, username="admin", email="a@example.com", role="admin")

        with pytest.raises(_Stop):
            asyncio.run(crud.create_agent_internal(cfg, user, memory_ceiling="2g"))

        assert seen["memory"] == "2g"


class TestFirstRunState:
    def test_core_agents_are_named_and_still_count_as_seeded(self, monkeypatch):
        import services.onboarding_service as onb
        from models import FirstRunState, User

        monkeypatch.setattr(onb, "manifest_seeded_agent_names", lambda: set())
        monkeypatch.setattr(onb, "_install_marker", lambda: None)
        monkeypatch.setattr(onb, "_visible_agents", lambda u: [
            {"name": "corbin"}, {"name": "cornelius"},
        ])
        state = onb.get_first_run_state(User(id=1, username="admin", role="admin"))

        assert state["first_run"] is True
        assert state["seeded_agents"] == ["corbin", "cornelius"]
        assert state["core_agents"] == ["corbin", "cornelius"]
        assert FirstRunState(**state).core_agents == ["corbin", "cornelius"]

    def test_a_built_agent_ends_the_first_run_and_is_not_core(self, monkeypatch):
        import services.onboarding_service as onb
        from models import User

        monkeypatch.setattr(onb, "manifest_seeded_agent_names", lambda: {"acme-scout"})
        monkeypatch.setattr(onb, "_install_marker", lambda: None)
        monkeypatch.setattr(onb, "_visible_agents", lambda u: [
            {"name": "cornelius"}, {"name": "acme-scout"}, {"name": "my-agent"},
        ])
        state = onb.get_first_run_state(User(id=1, username="admin", role="admin"))

        assert state["first_run"] is False
        assert state["core_agents"] == ["cornelius"]   # corbin deleted: not named
        assert "acme-scout" in state["seeded_agents"]
        assert "acme-scout" not in state["core_agents"]
