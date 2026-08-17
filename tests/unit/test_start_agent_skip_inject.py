"""
Unit tests for #421: skip credential/skill injection when starting an
already-running agent container.

`start_agent_internal` previously called `inject_assigned_credentials`
and `inject_assigned_skills` unconditionally after `container_start`.
On an already-running, busy container this produced 3 connection retries
per call and an ERROR log even though the workspace volume already
carries `.env` and `.claude/skills/` across restarts.

Issue: https://github.com/abilityai/trinity/issues/421
Module: src/backend/services/agent_service/lifecycle.py
"""

import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

_BACKEND = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'src', 'backend'
))

# ── Shared mocks ──────────────────────────────────────────────────────────
_mock_db = MagicMock()
_mock_docker_service = MagicMock()
_mock_docker_utils = MagicMock()
_mock_settings = MagicMock()
_mock_skill_service = MagicMock()
_mock_helpers = MagicMock()
_mock_read_only = MagicMock()


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        self.status_code = status_code
        self.detail = detail


_SYS_MOCKS = {
    'database': Mock(db=_mock_db),
    'docker': Mock(),
    'services.docker_service': Mock(
        docker_client=Mock(),
        get_agent_container=_mock_docker_service.get_agent_container,
    ),
    'services.docker_utils': Mock(
        container_stop=AsyncMock(),
        container_remove=AsyncMock(),
        container_start=_mock_docker_utils.container_start,
        container_reload=_mock_docker_utils.container_reload,
        volume_get=AsyncMock(),
        volume_create=AsyncMock(),
        containers_run=AsyncMock(),
    ),
    'services.settings_service': Mock(
        get_anthropic_api_key=Mock(return_value="sk-test"),
        get_github_pat=Mock(return_value=""),
        get_agent_full_capabilities=Mock(return_value=False),
    ),
    'services.skill_service': Mock(skill_service=_mock_skill_service),
    'fastapi': Mock(HTTPException=_HTTPException),
}

# Make the async docker utils actually awaitable
_mock_docker_utils.container_start = AsyncMock()
_mock_docker_utils.container_reload = AsyncMock()
_SYS_MOCKS['services.docker_utils'].container_start = _mock_docker_utils.container_start
_SYS_MOCKS['services.docker_utils'].container_reload = _mock_docker_utils.container_reload


# ── Load the module under test via importlib ──────────────────────────────
def _load_lifecycle():
    # Load the package stub first so relative imports (.helpers, .read_only)
    # resolve to mocks we control.
    pkg_name = "agent_service_pkg_under_test"
    pkg_spec = importlib.util.spec_from_loader(pkg_name, loader=None, is_package=True)
    pkg = importlib.util.module_from_spec(pkg_spec)
    pkg.__path__ = [os.path.join(_BACKEND, "services", "agent_service")]
    sys.modules[pkg_name] = pkg

    helpers_mod = Mock(
        check_shared_folder_mounts_match=AsyncMock(return_value=True),
        check_api_key_env_matches=Mock(return_value=True),
        check_github_pat_env_matches=Mock(return_value=True),
        check_resource_limits_match=Mock(return_value=True),
        check_full_capabilities_match=Mock(return_value=True),
        check_guardrails_env_matches=Mock(return_value=True),
        validate_base_image=Mock(),
        # #1816: MUST be stubbed explicitly. A bare Mock auto-creates missing
        # attributes and returns a truthy Mock, so an unstubbed
        # `is_system_agent_name` reads as "every agent is the system agent" and
        # the AC2 gate silently suppresses every recreate in this harness.
        is_system_agent_name=Mock(return_value=False),
    )
    read_only_mod = Mock(inject_read_only_hooks=AsyncMock(
        return_value={"success": True}
    ))
    file_sharing_mod = Mock(
        check_public_folder_mount_matches=Mock(return_value=True),
    )

    sys.modules[f"{pkg_name}.helpers"] = helpers_mod
    sys.modules[f"{pkg_name}.read_only"] = read_only_mod
    sys.modules[f"{pkg_name}.file_sharing"] = file_sharing_mod

    # #2140: the real names, needed ONLY while lifecycle.py executes (its line 27
    # is an absolute `from services.agent_service.helpers import ...`). These
    # used to be installed permanently, and `services/compatibility/spec.py`
    # resolves `is_claude_runtime` from that module lazily — so every later test
    # lost `claude_only` filtering and blamed #1187 for it.
    from conftest import stubbed_modules

    agent_service_aliases = {
        'services.agent_service': pkg,
        'services.agent_service.helpers': helpers_mod,
        'services.agent_service.read_only': read_only_mod,
        'services.agent_service.file_sharing': file_sharing_mod,
    }
    with stubbed_modules({**_SYS_MOCKS, **agent_service_aliases}):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.lifecycle",
            os.path.join(_BACKEND, "services", "agent_service", "lifecycle.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod, helpers_mod


_mod, _helpers = _load_lifecycle()


# ── Helpers ───────────────────────────────────────────────────────────────
def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_container(status: str):
    c = Mock()
    c.status = status
    return c


@pytest.fixture(autouse=True)
def _reset():
    _mock_docker_service.reset_mock()
    _mock_docker_utils.container_start.reset_mock()
    _mock_docker_utils.container_reload.reset_mock()
    _mock_db.reset_mock()
    # Re-point get_agent_container at the name imported into lifecycle.
    _mod.get_agent_container = _mock_docker_service.get_agent_container
    # By default, no recreation needed. Patch the names bound into the
    # lifecycle module at import time.
    _mod.check_shared_folder_mounts_match = AsyncMock(return_value=True)
    _mod.check_api_key_env_matches = Mock(return_value=True)
    _mod.check_github_pat_env_matches = Mock(return_value=True)
    _mod.check_resource_limits_match = Mock(return_value=True)
    _mod.check_full_capabilities_match = Mock(return_value=True)
    _mod.check_guardrails_env_matches = Mock(return_value=True)
    # #1809: image matches by default — MUST be stubbed explicitly (async), or
    # the real predicate would run against Mock containers on cold-start tests.
    _mod.check_base_image_matches = AsyncMock(return_value=True)
    # By default, public folder mount matches the file_sharing_enabled flag
    _mod.check_public_folder_mount_matches = Mock(return_value=True)
    # By default, no read-only mode
    _mock_db.get_read_only_mode.return_value = {"enabled": False}


# ── Tests ─────────────────────────────────────────────────────────────────
class TestStartAgentSkipInject:
    pytestmark = pytest.mark.unit

    def test_skips_injection_when_container_already_running(self):
        """When container was running and no recreation needed, do not
        call credential or skill injection (#421)."""
        container = _make_container("running")
        _mock_docker_service.get_agent_container.return_value = container

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills):
            result = _run(_mod.start_agent_internal("agent-a"))

        inject_creds.assert_not_awaited()
        inject_skills.assert_not_awaited()
        assert result["credentials_injection"] == "skipped"
        assert result["credentials_result"]["reason"] == "container_already_running"
        assert result["skills_injection"] == "skipped"
        assert result["skills_result"]["reason"] == "container_already_running"

    def test_injects_when_container_was_stopped(self):
        """When container was not running before start, inject as before."""
        container = _make_container("exited")
        _mock_docker_service.get_agent_container.return_value = container

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills):
            result = _run(_mod.start_agent_internal("agent-b"))

        inject_creds.assert_awaited_once_with("agent-b")
        inject_skills.assert_awaited_once_with("agent-b")
        assert result["credentials_injection"] == "success"
        assert result["skills_injection"] == "success"

    def test_injects_when_container_recreated_even_if_was_running(self):
        """Recreation produces a fresh container; injection must still run."""
        container = _make_container("running")
        _mock_docker_service.get_agent_container.return_value = container
        # Force recreation via a helper that flips `needs_recreation` True.
        _mod.check_api_key_env_matches = Mock(return_value=False)

        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})
        recreate = AsyncMock()

        with patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills), \
             patch.object(_mod, "recreate_container_with_updated_config", recreate):
            _run(_mod.start_agent_internal("agent-c"))

        recreate.assert_awaited_once()
        inject_creds.assert_awaited_once_with("agent-c")
        inject_skills.assert_awaited_once_with("agent-c")


# ── #1559: soft-delete recovery rebuilds the missing container ─────────────
class TestRecoverRecreate1559:
    """start_agent_internal must rebuild a missing container for a recovered
    (live, non-soft-deleted) agent instead of dead-ending on 404 — and still
    404 for a genuinely nonexistent agent.
    Issue: https://github.com/abilityai/trinity/issues/1559
    """
    pytestmark = pytest.mark.unit

    def test_start_404s_when_no_container_and_no_owner(self):
        _mock_docker_service.get_agent_container.return_value = None
        _mock_db.get_agent_owner.return_value = None
        try:
            _run(_mod.start_agent_internal("ghost"))
            assert False, "expected 404"
        except _HTTPException as e:
            assert e.status_code == 404

    def test_start_rebuilds_when_container_missing_but_live(self):
        # First lookup: no container. Recovered agent → ownership row exists.
        _mock_docker_service.get_agent_container.return_value = None
        _mock_db.get_agent_owner.return_value = {"owner_username": "bob"}

        rebuilt = _make_container("created")  # fresh → injection should run
        recreate = AsyncMock(return_value=rebuilt)
        inject_creds = AsyncMock(return_value={"status": "success"})
        inject_skills = AsyncMock(return_value={"status": "success"})

        with patch.object(_mod, "recreate_missing_container", recreate), \
             patch.object(_mod, "wait_for_agent_ready", AsyncMock()), \
             patch.object(_mod, "inject_assigned_credentials", inject_creds), \
             patch.object(_mod, "inject_assigned_skills", inject_skills):
            result = _run(_mod.start_agent_internal("revived"))

        recreate.assert_awaited_once_with("revived")
        _mock_docker_utils.container_start.assert_awaited()  # actually started
        assert result["message"] == "Agent revived started"

    def test_recreate_missing_container_reconstructs_spec_from_volume(self):
        _mock_db.get_agent_owner.return_value = {"owner_username": "bob"}
        _mock_db.get_resource_limits.return_value = {}
        _mock_db.create_agent_mcp_api_key.return_value = Mock(api_key="trinity_mcp_x")
        # #1665: never renamed -> the pin is NULL and the base IS the name.
        # (Stub it: an unstubbed MagicMock attribute is truthy and would be
        # f-stringed straight into the volume name.)
        _mock_db.get_volume_base_name.side_effect = lambda n: n

        provision = AsyncMock(return_value=_make_container("created"))
        tmpl = AsyncMock(return_value={"type": "researcher", "runtime": {"type": "codex"}})

        with patch.object(_mod, "_provision_folders_and_run_agent_container", provision), \
             patch.object(_mod, "_read_template_yaml_from_volume", tmpl), \
             patch.object(_mod, "_apply_persisted_auth_env", Mock()), \
             patch.object(_mod, "get_next_available_port", Mock(return_value=2245)), \
             patch.object(_mod, "get_agent_full_capabilities", Mock(return_value=False)), \
             patch.object(_mod, "get_agent_default_resources",
                          Mock(return_value={"cpu": "2", "memory": "4g"})), \
             patch.object(_mod, "validate_base_image", Mock()):
            _run(_mod.recreate_missing_container("proj"))

        provision.assert_awaited_once()
        kwargs = provision.call_args.kwargs
        # Existing workspace volume reused (never recreated), mounted at home.
        assert kwargs["base_volumes"] == {
            "agent-proj-workspace": {"bind": "/home/developer", "mode": "rw"}
        }
        # runtime recovered from the volume's template.yaml; the template's
        # `type:` is parsed but IGNORED (#2104) — no label, no env var.
        assert kwargs["labels"]["trinity.agent-runtime"] == "codex"
        assert "trinity.agent-type" not in kwargs["labels"]
        assert kwargs["env_vars"]["AGENT_RUNTIME"] == "codex"
        assert "AGENT_TYPE" not in kwargs["env_vars"]
        assert kwargs["env_vars"]["AGENT_NAME"] == "proj"

    def test_recreate_missing_container_mounts_a_renamed_agents_real_volume(self):
        """#1665: rename keeps the volume under the pre-rename base, so recovery
        must resolve it from the ownership row. Naming off the CURRENT name is
        silent, not loud — `containers.run` creates the missing volume, so the
        agent came back on an empty /home/developer with its real data (incl.
        #1169 data_paths) stranded under the old base."""
        _mock_db.get_agent_owner.return_value = {"owner_username": "bob"}
        _mock_db.get_resource_limits.return_value = {}
        _mock_db.create_agent_mcp_api_key.return_value = Mock(api_key="trinity_mcp_x")
        _mock_db.get_volume_base_name.side_effect = (
            lambda n: "old-name" if n == "new-name" else n
        )

        provision = AsyncMock(return_value=_make_container("created"))
        with patch.object(_mod, "_provision_folders_and_run_agent_container", provision), \
             patch.object(_mod, "_read_template_yaml_from_volume", AsyncMock(return_value={})), \
             patch.object(_mod, "_apply_persisted_auth_env", Mock()), \
             patch.object(_mod, "get_next_available_port", Mock(return_value=2245)), \
             patch.object(_mod, "get_agent_full_capabilities", Mock(return_value=False)), \
             patch.object(_mod, "get_agent_default_resources",
                          Mock(return_value={"cpu": "2", "memory": "4g"})), \
             patch.object(_mod, "validate_base_image", Mock()):
            _run(_mod.recreate_missing_container("new-name"))

        assert provision.call_args.kwargs["base_volumes"] == {
            "agent-old-name-workspace": {"bind": "/home/developer", "mode": "rw"}
        }

    def test_workspace_volume_name_falls_back_on_a_db_error(self):
        """A DB hiccup must not block a rebuild — fall back to the agent name,
        which is the pre-#1665 behavior and correct for every un-renamed agent."""
        _mock_db.get_volume_base_name.side_effect = RuntimeError("db down")
        assert _mod._workspace_volume_name("solo") == "agent-solo-workspace"

    def test_template_read_uses_the_resolved_volume(self):
        """#1665: the same trap one level down — reading template.yaml off the
        CURRENT name found nothing for a renamed agent, so recovery silently
        rebuilt on default agent-type/runtime instead of the committed ones."""
        _mock_db.get_volume_base_name.side_effect = (
            lambda n: "old-name" if n == "new-name" else n
        )
        run = AsyncMock(return_value=b"type: researcher\n")
        with patch.object(_mod, "containers_run", run):
            _run(_mod._read_template_yaml_from_volume("new-name"))

        assert run.call_args.kwargs["volumes"] == {
            "agent-old-name-workspace": {"bind": "/home/developer", "mode": "ro"}
        }
