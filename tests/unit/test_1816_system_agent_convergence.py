"""#1816 — the convergence invariant for ``trinity-system``.

> The container produced by ``SystemAgentService._create_system_agent`` **and**
> the container produced by ``recreate_container_with_updated_config`` must both
> leave **all eight** config predicates ``True``.

Why this is the load-bearing test of the issue, and not a formality:

``recreate_container_with_updated_config`` resolves the image from the old
container's own ``Config.Image`` **reference** — the *tag*
``trinity-agent-base:latest``, not the pinned image id. So **every** config-drift
recreate is also an image adoption. A config predicate that is *permanently*
false for a freshly created system agent is therefore not merely noisy: it means
the first ``POST /api/agents/trinity-system/start`` after any fresh provision
replaces a **running** orchestrator's container and swaps its image
mid-operation. That is exactly what AC2 forbids.

Two predicates were permanently false before this issue:

* ``check_agent_auth_token_env_matches`` (#1159) — creation never wrote
  ``TRINITY_AGENT_AUTH_TOKEN``; the only three writers were ``crud.py`` and the
  two recreate paths in ``lifecycle.py``.
* ``check_full_capabilities_match`` — creation passes
  ``cap_add=FULL_CAPABILITIES`` but never wrote the
  ``trinity.full-capabilities`` label, and a missing label reads as ``false``
  while the fleet default is ``true``.

**The fixture is derived, never hand-built.** ``_create_system_agent`` is driven
for real against a mocked Docker/DB surface and the container is built from the
``environment`` / ``labels`` kwargs it actually passes to ``containers_run``. A
hand-written fake carrying both values would assert only that a correct
container is correct — true, useless, and green while production is broken. A
source-level AST guard rides alongside as the cheap drift check.

The second half runs the same eight predicates against the **post-recreate**
spec under **both** ``agent_full_capabilities`` settings — the no-loop proof:
pinning the label without also exempting the predicate would, on a
``full_capabilities=false`` install, produce a mismatch that never converges
(recreate on every start, forever).
"""

from __future__ import annotations

import ast
import asyncio
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_SYSTEM_AGENT_SERVICE = _BACKEND / "services" / "system_agent_service.py"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Minimal ``docker.models.containers.Container`` stand-in.

    Only ``attrs`` matters: every config predicate reads the container purely
    through ``attrs["Config"]["Env"]``, ``attrs["Config"]["Labels"]`` and
    ``attrs["Mounts"]`` — the same shape the Docker API returns.
    """

    def __init__(self, *, env: dict, labels: dict, mounts=None, image_id="sha256:old"):
        self.attrs = {
            "Image": image_id,
            "Config": {
                "Env": [f"{k}={v}" for k, v in env.items()],
                "Labels": dict(labels),
                "Image": "trinity-agent-base:latest",
            },
            "Mounts": list(mounts or []),
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
        }
        self.short_id = "deadbeef"
        self.status = "running"


class _FakeDb:
    """Whole-object DB double.

    Deliberately a whole object swapped into each module's namespace rather than
    ``setattr`` onto the real ``database.db`` — ``tests/unit/test_904`` leaks a
    method-less ``database.db`` stub, so mutating the shared object is
    order-dependent under ``pytest-randomly``.

    Every accessor returns the state of a **default, freshly provisioned**
    system agent: no subscription, platform key in use, no PAT, no guardrails,
    no shared folders, no file sharing, no per-agent resource override.
    """

    def __init__(self):
        self.registered = []

    # ownership / identity
    def get_user_by_username(self, username):
        return {"id": 1, "username": username}

    def register_agent_owner(self, *a, **k):
        self.registered.append((a, k))
        return True

    def grant_default_permissions(self, *a, **k):
        return True

    def is_system_agent(self, name):
        return name == "trinity-system"

    # MCP key
    def create_agent_mcp_api_key(self, **k):
        return types.SimpleNamespace(
            id="key-1", key_prefix="trinity_mcp_ab", api_key="trinity_mcp_abcdef"
        )

    # predicate inputs
    def get_shared_folder_config(self, name):
        return None

    def get_available_shared_folders(self, name):
        return []

    def get_shared_mount_path(self, name):
        return f"/home/developer/shared-in/{name}"

    def get_shared_volume_name(self, name):
        return f"agent-{name}-shared"

    def get_file_sharing_enabled(self, name):
        return False

    def get_public_mount_path(self):
        return "/home/developer/public"

    def get_agent_subscription_id(self, name):
        return None

    def get_subscription_token(self, sub_id):
        return None

    def get_use_platform_api_key(self, name):
        return True

    def get_agent_github_pat(self, name):
        return None

    def get_git_config(self, name):
        return None

    def get_resource_limits(self, name):
        return None

    def get_guardrails_config(self, name):
        return None


# ---------------------------------------------------------------------------
# the derived fixture — drive creation for real, capture what it builds
# ---------------------------------------------------------------------------


@pytest.fixture
def created_spec(monkeypatch):
    """Run ``_create_system_agent`` against a mocked Docker/DB surface and
    return the ``environment`` / ``labels`` it passed to ``containers_run``."""
    import services.system_agent_service as sas

    captured = {}

    async def _fake_containers_run(image, **kwargs):
        captured["image"] = image
        captured.update(kwargs)
        return _FakeContainer(
            env=kwargs.get("environment", {}), labels=kwargs.get("labels", {})
        )

    fake_db = _FakeDb()
    monkeypatch.setattr(sas, "db", fake_db)
    monkeypatch.setattr(sas, "containers_run", _fake_containers_run)
    monkeypatch.setattr(sas, "get_next_available_port", lambda: 2222)
    monkeypatch.setattr(sas, "get_anthropic_api_key", lambda: "sk-ant-test")
    monkeypatch.setattr(sas, "clear_agent_breakers", lambda name: None)
    monkeypatch.setattr(
        sas.SystemAgentService, "_set_system_scope", lambda self, key_id: None
    )
    # The template lives at the repo-root path the service falls back to when
    # /agent-configs/templates is absent (i.e. off-container), so no stubbing
    # of the yaml load is needed — creation reads the REAL template.yaml.
    monkeypatch.chdir(_BACKEND.parents[1])

    asyncio.run(sas.SystemAgentService()._create_system_agent())
    assert "environment" in captured, "creation never reached containers_run"
    return captured


def _all_eight_predicates(container, agent_name="trinity-system"):
    """Evaluate the exact eight predicates ``start_agent_internal`` composes
    into ``needs_recreation`` (``lifecycle.py``). Returns {name: bool}."""
    from services.agent_service import helpers
    from services.agent_service.file_sharing import check_public_folder_mount_matches

    return {
        "shared_folder_mounts": asyncio.run(
            helpers.check_shared_folder_mounts_match(container, agent_name)
        ),
        "public_folder_mount": check_public_folder_mount_matches(container, agent_name),
        "api_key_env": helpers.check_api_key_env_matches(container, agent_name),
        "github_pat_env": helpers.check_github_pat_env_matches(container, agent_name),
        "resource_limits": helpers.check_resource_limits_match(container, agent_name),
        "full_capabilities": helpers.check_full_capabilities_match(
            container, agent_name
        ),
        "guardrails_env": helpers.check_guardrails_env_matches(container, agent_name),
        "agent_auth_token": helpers.check_agent_auth_token_env_matches(
            container, agent_name
        ),
    }


@pytest.fixture
def predicate_env(monkeypatch):
    """Point every predicate's ``db`` at the same whole-object double, and stub
    the one cross-module lookup (``routers.git.get_github_pat_for_agent``)."""
    from services.agent_service import helpers, file_sharing

    fake_db = _FakeDb()
    monkeypatch.setattr(helpers, "db", fake_db)
    monkeypatch.setattr(file_sharing, "db", fake_db)
    monkeypatch.setattr(helpers, "get_anthropic_api_key", lambda: "sk-ant-test")
    return fake_db


# ---------------------------------------------------------------------------
# T-A first half — creation converges
# ---------------------------------------------------------------------------


def test_created_system_agent_satisfies_all_eight_predicates(
    created_spec, predicate_env, monkeypatch
):
    """THE #1816 test. Fails before the fix on `full_capabilities` AND
    `agent_auth_token`; a fixture that carried both would prove nothing."""
    from services.agent_service import helpers

    monkeypatch.setattr(helpers, "get_agent_full_capabilities", lambda: True)
    container = _FakeContainer(
        env=created_spec["environment"], labels=created_spec["labels"]
    )

    results = _all_eight_predicates(container)
    failed = sorted(k for k, v in results.items() if not v)
    assert not failed, (
        f"predicates permanently false for a freshly created trinity-system: {failed}. "
        "Each one makes the first POST /api/agents/trinity-system/start recreate a "
        "RUNNING orchestrator — and because the recreate resolves the image from a "
        "TAG, that recreate is also an unrequested image adoption (AC2)."
    )


def test_created_system_agent_converges_when_fleet_capabilities_are_off(
    created_spec, predicate_env, monkeypatch
):
    """The no-loop proof for creation. On an ``agent_full_capabilities=false``
    install the system agent still runs FULL_CAPABILITIES by contract, so
    pinning the label alone would mismatch forever. The predicate exemption is
    what makes the pinned label safe."""
    from services.agent_service import helpers

    monkeypatch.setattr(helpers, "get_agent_full_capabilities", lambda: False)
    container = _FakeContainer(
        env=created_spec["environment"], labels=created_spec["labels"]
    )

    results = _all_eight_predicates(container)
    failed = sorted(k for k, v in results.items() if not v)
    assert not failed, (
        f"unconvergeable mismatch on a full_capabilities=false install: {failed} — "
        "this is a recreate on EVERY start, forever"
    )


def test_creation_writes_the_derived_auth_token_value_not_just_the_key(created_spec):
    """Key presence is not enough: ``check_agent_auth_token_env_matches``
    compares the VALUE against ``derive_agent_token(name)``."""
    from services.agent_auth import derive_agent_token

    assert created_spec["environment"].get("TRINITY_AGENT_AUTH_TOKEN") == (
        derive_agent_token("trinity-system")
    )


def test_creation_labels_full_capabilities_true(created_spec):
    """The container really is run with ``cap_add=FULL_CAPABILITIES``, so the
    label must say so — otherwise every reader of the label is lied to."""
    from services.agent_service.lifecycle import FULL_CAPABILITIES

    assert created_spec["labels"].get("trinity.full-capabilities") == "true"
    assert created_spec["cap_add"] == FULL_CAPABILITIES
    assert created_spec["cap_drop"] == ["ALL"]


def test_creation_does_not_arm_the_heartbeat(created_spec):
    """``TRINITY_BACKEND_URL`` is the agent-side heartbeat loop's gate, and
    ``heartbeat_service.authorize_heartbeat`` accepts only ``scope == "agent"``
    keys. The system agent's key is ``scope == "system"``, so arming it yields a
    permanent 5-second 403 loop (~17k backend log lines/day)."""
    assert "TRINITY_BACKEND_URL" not in created_spec["environment"]


def test_creation_keeps_the_agent_network_hard_coded(created_spec):
    """architecture.md → Network Topology names this as one of the three
    hard-coded create sites. Agents must never reach the platform network."""
    assert created_spec["network"] == "trinity-agent-network"


def test_creation_sets_the_unless_stopped_restart_policy(created_spec):
    assert created_spec["restart_policy"] == {"Name": "unless-stopped"}


# ---------------------------------------------------------------------------
# T-A second half — the post-recreate spec converges too, under BOTH settings
# ---------------------------------------------------------------------------


def _post_recreate_container(created_spec, *, fleet_full_caps: bool):
    """The container the shared recreate path would produce for the system
    agent, built from what ``recreate_container_with_updated_config`` actually
    writes: the auth token is re-derived, and the capabilities label is written
    from the effective setting — which, for the system agent, the override pins
    to True regardless of the fleet default."""
    from services.agent_auth import derive_agent_token

    env = dict(created_spec["environment"])
    env["TRINITY_AGENT_AUTH_TOKEN"] = derive_agent_token("trinity-system")
    labels = dict(created_spec["labels"])
    # `full_capabilities` override → the writer honours the system-agent
    # contract, so the label stays 'true' even when the fleet default is off.
    labels["trinity.full-capabilities"] = "true"
    return _FakeContainer(env=env, labels=labels)


@pytest.mark.parametrize("fleet_full_caps", [True, False])
def test_post_recreate_spec_satisfies_all_eight_predicates(
    created_spec, predicate_env, monkeypatch, fleet_full_caps
):
    """Convergence in ONE pass: the container a recreate produces must not
    itself trip a predicate, or the next start recreates again."""
    from services.agent_service import helpers

    monkeypatch.setattr(helpers, "get_agent_full_capabilities", lambda: fleet_full_caps)
    container = _post_recreate_container(created_spec, fleet_full_caps=fleet_full_caps)

    results = _all_eight_predicates(container)
    failed = sorted(k for k, v in results.items() if not v)
    assert not failed, (
        f"post-recreate spec still mismatches {failed} with "
        f"agent_full_capabilities={fleet_full_caps} — an infinite recreate loop"
    )


# ---------------------------------------------------------------------------
# AST drift guards (cheap, run on every commit)
# ---------------------------------------------------------------------------


def _create_system_agent_source() -> str:
    """The source text of ``_create_system_agent`` ONLY.

    Sliced by AST rather than searched whole-file so a match elsewhere in the
    module (``ensure_deployed`` is right above it) can never satisfy a guard
    that is meant to pin the creation path.
    """
    tree = ast.parse(_SYSTEM_AGENT_SERVICE.read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_create_system_agent"
        ):
            return ast.get_source_segment(_SYSTEM_AGENT_SERVICE.read_text(), node)
    pytest.fail("_create_system_agent not found in system_agent_service.py")


def test_ast_creation_assigns_the_two_converged_keys():
    src = _create_system_agent_source()
    assert "'TRINITY_AGENT_AUTH_TOKEN'" in src or '"TRINITY_AGENT_AUTH_TOKEN"' in src
    assert "derive_agent_token(SYSTEM_AGENT_NAME)" in src
    assert "'trinity.full-capabilities'" in src or '"trinity.full-capabilities"' in src


def _env_keys_assigned_in_creation() -> set:
    """Every string-literal dict key ``_create_system_agent`` assigns.

    AST-based, not a substring search: the function *documents* why it omits
    ``TRINITY_BACKEND_URL``, and a text search cannot tell a comment from an
    assignment — the guard would fire on its own rationale.
    """
    tree = ast.parse(_SYSTEM_AGENT_SERVICE.read_text())
    target = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_create_system_agent"
    )
    keys = set()
    for node in ast.walk(target):
        if isinstance(node, ast.Dict):
            keys |= {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.slice, ast.Constant)
                    and isinstance(tgt.slice.value, str)
                ):
                    keys.add(tgt.slice.value)
    return keys


def test_ast_creation_never_arms_trinity_backend_url():
    assert "TRINITY_BACKEND_URL" not in _env_keys_assigned_in_creation(), (
        "the system agent's MCP key is scope='system'; authorize_heartbeat "
        "accepts only scope='agent', so arming the heartbeat is a permanent 403 loop"
    )
