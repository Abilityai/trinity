"""#1816 — base-image adoption for ``trinity-system``, and the AC2 gate.

``ensure_deployed`` returned ``action: none`` the instant the container reported
``running``, without evaluating a single drift predicate. Combined with
``restart_policy: unless-stopped`` and a canonical upgrade path
(``build-base-image.sh`` → ``start.sh``) that never touches agent containers,
that made the platform orchestrator the most-stale agent in every fleet —
indefinitely, and silently.

The three boundaries asserted here:

* **running** → read-only. Report ``base_image_state``, WARN, and alarm on
  ``stale`` **only**. Never recreate: the orchestrator may be mid-execution and
  nothing an operator did caused the drift.
* **stopped** → delegate to ``start_agent_internal``, the cold boundary where
  the #1809 image gate fires and adoption happens.
* **``POST /api/agents/trinity-system/start`` on a running system agent** →
  the structural AC2 gate suppresses the recreate and says so.

The convergence half of #1816 lives in
``test_1816_system_agent_convergence.py``.

Deliberately driving the REAL modules with their module-level names patched,
rather than an importlib stub package: those harnesses register
``services.agent_service*`` in ``sys.modules`` for the rest of the session and
are a known cross-file contamination source (#762). The only ``sys.modules``
touch here is via ``monkeypatch.setitem``, which restores itself.

Not being a contaminator is not the same as being immune to one, so this file
also defends the other direction (see ``_real_modules_pinned``): five sibling
unit files replace ``services.agent_service`` / ``.helpers`` with ``Mock``
objects at COLLECTION time, i.e. before any test here runs. A leaked Mock is
silent rather than loud — ``is_system_agent_name`` becomes "no agent is the
system agent", so the AC2 gate never fires and the recreate it exists to
suppress happens while every assertion still *looks* meaningful. These tests
must therefore pin the real modules rather than inherit whichever ones import
order happened to leave behind.
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys

import docker
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

# Imported here, at COLLECTION time, while `sys.modules` is still clean — the
# sibling harnesses that overwrite these names (test_start_agent_skip_inject,
# test_inject_assigned_credentials, test_monitoring_router_signatures,
# test_subscription_auto_switch_no_cred_import) all sort after this file, so
# collection reaches them second. Binding the real objects now is what makes
# the pinning below possible at all: by the time a fixture runs, an `import`
# would resolve to the Mock.
import services.agent_service as _real_agent_service  # noqa: E402
import services.agent_service.helpers as _real_helpers  # noqa: E402
import services.agent_service.lifecycle as _real_lifecycle  # noqa: E402
import services.system_agent_service as _real_system_agent_service  # noqa: E402

_REAL_MODULES = {
    "services.agent_service": _real_agent_service,
    "services.agent_service.helpers": _real_helpers,
    "services.agent_service.lifecycle": _real_lifecycle,
    "services.system_agent_service": _real_system_agent_service,
}


@pytest.fixture(autouse=True)
def _real_modules_pinned(monkeypatch):
    """Pin the real modules for the duration of every test in this file (#762).

    ``monkeypatch.setitem`` restores whatever the siblings left behind at
    teardown, so their own harnesses — which hold direct module references, not
    ``sys.modules`` lookups — are unaffected. Without this, running this file
    alongside ``test_start_agent_skip_inject.py`` fails 21 tests: the leaked
    ``helpers`` Mock returns ``False`` from ``is_system_agent_name`` and a
    ``Mock`` (never ``"unknown"``) from ``check_base_image_state``.
    """
    for name, module in _REAL_MODULES.items():
        monkeypatch.setitem(sys.modules, name, module)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEM_AGENT_SERVICE = _BACKEND / "services" / "system_agent_service.py"


def _human_caller():
    """A JWT/user-scoped principal. `User.agent_name` is set only for
    scope="agent" keys, and `reject_agent_principal` keys off exactly that — so
    a bare MagicMock (truthy `.agent_name`) would read as an agent key and 403."""
    caller = MagicMock()
    caller.agent_name = None
    return caller


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Container:
    def __init__(self, status="running", labels=None, host_config=None):
        self.status = status
        self.short_id = "cafe1234"
        self.attrs = {
            "Image": "sha256:old",
            "Config": {
                "Env": [],
                "Labels": (
                    labels if labels is not None else {"trinity.ssh-port": "2222"}
                ),
                "Image": "trinity-agent-base:latest",
            },
            "Mounts": [],
            "HostConfig": (
                host_config
                if host_config is not None
                else {"RestartPolicy": {"Name": "unless-stopped"}}
            ),
        }


# ===========================================================================
# T-C #1/#2/#3 — the running branch is read-only, and alarms honestly
# ===========================================================================


@pytest.fixture
def sas(monkeypatch):
    """The real ``system_agent_service`` with its Docker/DB surface patched to a
    DEPLOYED, RUNNING system agent. Each test overrides what it cares about."""
    import services.system_agent_service as mod

    container = _Container(status="running")
    monkeypatch.setattr(mod, "get_agent_container", lambda name: container)
    monkeypatch.setattr(mod, "container_reload", AsyncMock())
    monkeypatch.setattr(mod, "container_start", AsyncMock())
    monkeypatch.setattr(mod, "db", MagicMock())
    monkeypatch.setattr(mod, "network_get", AsyncMock())
    monkeypatch.setattr(mod, "is_port_available", lambda port: True)
    # Cooldown state is class-level and persists across tests by design; reset it
    # so each test observes the first-emission edge.
    monkeypatch.setattr(mod.SystemAgentService, "_last_base_image_alert_at", None)
    mod._container_under_test = container
    return mod


def test_running_with_drift_never_recreates_and_reports_stale(sas, monkeypatch):
    """THE AC2 test at the boot boundary."""
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="drift"))
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())
    emitted = []
    monkeypatch.setattr(
        sas.SystemAgentService,
        "_emit_base_image_stale_alert",
        lambda self: emitted.append(1),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "none"
    assert result["status"] == "running"
    assert result["base_image_state"] == "stale"
    assert "restart" in result["message"].lower()
    sas.start_agent_internal.assert_not_awaited()
    sas.container_start.assert_not_awaited()
    assert emitted == [1], "a stale running system agent must raise the alarm"


def test_running_with_unknown_reports_honestly_and_never_alarms(sas, monkeypatch):
    """A fail-open probe must not manufacture an alert. This is exactly the
    conflation the 3-state split exists to prevent: on the pre-#1816 boolean,
    'the check failed' was indistinguishable from 'the image is current'."""
    monkeypatch.setattr(
        sas, "check_base_image_state", AsyncMock(return_value="unknown")
    )
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())
    emitted = []
    monkeypatch.setattr(
        sas.SystemAgentService,
        "_emit_base_image_stale_alert",
        lambda self: emitted.append(1),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "none"
    assert result["base_image_state"] == "unknown"
    assert emitted == [], "an unreadable check must never raise an alarm"
    sas.start_agent_internal.assert_not_awaited()


def test_running_and_current_is_the_quiet_path(sas, monkeypatch):
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="match"))
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())
    emitted = []
    monkeypatch.setattr(
        sas.SystemAgentService,
        "_emit_base_image_stale_alert",
        lambda self: emitted.append(1),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["base_image_state"] == "current"
    assert result["action"] == "none"
    assert emitted == []


def test_stale_alarm_fires_once_then_is_suppressed_by_cooldown(sas, monkeypatch):
    """'running + stale' persists across every backend restart until an operator
    acts, and a restart resets the in-memory edge — so without a cooldown a
    restart loop files one queue item per boot."""
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="drift"))
    created = []
    fake_db = MagicMock()
    fake_db.create_operator_queue_item.side_effect = lambda name, item: created.append(
        item
    )
    monkeypatch.setattr(sas, "db", fake_db)

    svc = sas.SystemAgentService()
    _run(svc.ensure_deployed())
    _run(svc.ensure_deployed())

    assert len(created) == 1, "the second cycle must be suppressed by the cooldown"
    item = created[0]
    assert item["id"].startswith(sas.BASE_IMAGE_STALE_ALERT_PREFIX)
    assert item["priority"] == "high"
    assert item["agent_name"] == "trinity-system"
    assert item["status"] == "pending"


def test_stale_alarm_payload_carries_no_image_identifiers(sas, monkeypatch):
    """canary G-04's lesson: this row is durable and operator-visible, so it
    carries identifiers and instructions — never image ids or digests."""
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="drift"))
    created = []
    fake_db = MagicMock()
    fake_db.create_operator_queue_item.side_effect = lambda name, item: created.append(
        item
    )
    monkeypatch.setattr(sas, "db", fake_db)

    _run(sas.SystemAgentService().ensure_deployed())

    blob = repr(created[0])
    assert "sha256:" not in blob
    assert not re.search(r"\b[0-9a-f]{12,}\b", blob), "no image id/digest in the alarm"


def test_alarm_emit_failure_never_breaks_the_boot(sas, monkeypatch):
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="drift"))
    fake_db = MagicMock()
    fake_db.create_operator_queue_item.side_effect = RuntimeError("db down")
    monkeypatch.setattr(sas, "db", fake_db)

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "none"
    assert result["base_image_state"] == "stale"


# ===========================================================================
# T-C #4 — the stopped branch delegates
# ===========================================================================


def test_stopped_delegates_to_start_agent_internal_and_surfaces_the_recreate(
    sas, monkeypatch
):
    sas._container_under_test.status = "exited"
    monkeypatch.setattr(
        sas,
        "start_agent_internal",
        AsyncMock(return_value={"recreated": True, "recreate_reason": "image_drift"}),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    sas.start_agent_internal.assert_awaited_once_with("trinity-system")
    sas.container_start.assert_not_awaited(), "the bare start is superseded"
    assert result["action"] == "started"
    assert result["recreated"] is True
    assert result["recreate_reason"] == "image_drift"
    assert "adopted" in result["message"]


def test_stopped_without_drift_still_reports_a_message(sas, monkeypatch):
    sas._container_under_test.status = "exited"
    monkeypatch.setattr(
        sas,
        "start_agent_internal",
        AsyncMock(return_value={"recreated": False, "recreate_reason": None}),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "started"
    assert result["recreated"] is False
    assert result["message"] == "System agent started"


def test_delegated_start_failure_alarms_and_does_not_raise(sas, monkeypatch):
    """R1: a recreate removes the old container BEFORE running the replacement,
    so a run failure can leave the platform with no orchestrator — and nobody is
    watching a boot log."""
    sas._container_under_test.status = "exited"
    monkeypatch.setattr(
        sas, "start_agent_internal", AsyncMock(side_effect=RuntimeError("no network"))
    )
    created = []
    fake_db = MagicMock()
    fake_db.create_operator_queue_item.side_effect = lambda name, item: created.append(
        item
    )
    monkeypatch.setattr(sas, "db", fake_db)

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "start_failed"
    assert result["status"] == "error"
    assert len(created) == 1
    assert created[0]["priority"] == "critical"


# ===========================================================================
# R1 pre-flight
# ===========================================================================


def test_preflight_missing_network_falls_back_to_a_plain_start(sas, monkeypatch):
    """Declining the adoption costs one stale boot (the pre-#1816 status quo);
    handing a stopped container to a path that removes it and then cannot run
    the replacement costs the orchestrator."""
    import docker

    sas._container_under_test.status = "exited"
    monkeypatch.setattr(
        sas, "network_get", AsyncMock(side_effect=docker.errors.NotFound("nope"))
    )
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())
    monkeypatch.setattr(sas, "db", MagicMock())

    result = _run(sas.SystemAgentService().ensure_deployed())

    sas.start_agent_internal.assert_not_awaited()
    sas.container_start.assert_awaited_once()
    assert result["action"] == "started"
    assert result["status"] == "running"
    assert "pre-flight" in result["message"]


def test_preflight_bound_ssh_port_falls_back_to_a_plain_start(sas, monkeypatch):
    sas._container_under_test.status = "exited"
    monkeypatch.setattr(sas, "is_port_available", lambda port: False)
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())
    monkeypatch.setattr(sas, "db", MagicMock())

    result = _run(sas.SystemAgentService().ensure_deployed())

    sas.start_agent_internal.assert_not_awaited()
    sas.container_start.assert_awaited_once()
    assert result["action"] == "started"


def test_preflight_fails_open_on_an_unreadable_network(sas, monkeypatch):
    """An unexpected probe error must not block a start that would have worked."""
    sas._container_under_test.status = "exited"
    monkeypatch.setattr(
        sas, "network_get", AsyncMock(side_effect=RuntimeError("socket hiccup"))
    )
    monkeypatch.setattr(
        sas, "start_agent_internal", AsyncMock(return_value={"recreated": False})
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    sas.start_agent_internal.assert_awaited_once()
    assert result["action"] == "started"


# ===========================================================================
# T-C #7 — fail-closed creation when AGENT_AUTH_SECRET is unset
# ===========================================================================


def test_creation_without_agent_auth_secret_fails_closed_without_blocking_boot(
    sas, monkeypatch
):
    """Strictly more honest than creating a container the backend can never talk
    to: every backend→agent call already raises on an empty master, and
    `check_agent_auth_token_env_matches` already raises for every agent.

    `_create_system_agent` resolves its template through a CWD-RELATIVE fallback
    (`./config/agent-templates`, used whenever `/agent-configs/templates` is
    absent, i.e. off-container). Without pinning the CWD this test passes from
    the repo root and fails from `tests/` — which is exactly where verify-local
    runs pytest — because creation then dies on a missing template BEFORE it
    reaches the token derive, and the test would be asserting the wrong failure.
    """
    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setattr(sas, "get_agent_container", lambda name: None)
    monkeypatch.setattr(
        sas,
        "derive_agent_token",
        MagicMock(side_effect=RuntimeError("AGENT_AUTH_SECRET")),
    )

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["action"] == "create_failed"
    assert result["status"] == "error"
    assert "AGENT_AUTH_SECRET" in result["message"]


def test_every_ensure_deployed_return_sets_action_and_message(sas, monkeypatch):
    """main.py's lifespan logs `result['action']` and `result['message']` by
    direct index — a branch that omits either raises KeyError INSIDE the boot
    log line, turning a cosmetic gap into a boot-path exception."""
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="drift"))
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock(return_value={}))
    monkeypatch.setattr(sas, "db", MagicMock())

    svc = sas.SystemAgentService()
    scenarios = []

    # running + drift
    scenarios.append(_run(svc.ensure_deployed()))
    # stopped + delegated success
    sas._container_under_test.status = "exited"
    scenarios.append(_run(svc.ensure_deployed()))
    # stopped + delegated failure
    monkeypatch.setattr(
        sas, "start_agent_internal", AsyncMock(side_effect=RuntimeError("x"))
    )
    scenarios.append(_run(svc.ensure_deployed()))
    # no container + creation failure
    monkeypatch.setattr(sas, "get_agent_container", lambda name: None)
    monkeypatch.setattr(
        sas.SystemAgentService,
        "_create_system_agent",
        AsyncMock(side_effect=RuntimeError("y")),
    )
    scenarios.append(_run(svc.ensure_deployed()))

    for r in scenarios:
        assert r["action"] is not None, r
        assert r["message"] is not None, r


# ===========================================================================
# T-C #5/#6 — the AC2 structural gate in start_agent_internal
# ===========================================================================


@pytest.fixture
def lifecycle(monkeypatch):
    """The real ``lifecycle`` module with every side-effecting name patched."""
    import services.agent_service.lifecycle as mod

    container = _Container(status="running")
    monkeypatch.setattr(mod, "get_agent_container", lambda name: container)
    monkeypatch.setattr(mod, "container_reload", AsyncMock())
    monkeypatch.setattr(mod, "container_start", AsyncMock())
    monkeypatch.setattr(mod, "clear_agent_breakers", MagicMock())
    monkeypatch.setattr(mod, "recreate_container_with_updated_config", AsyncMock())
    monkeypatch.setattr(mod, "wait_for_agent_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(
        mod,
        "inject_assigned_credentials",
        AsyncMock(return_value={"status": "success"}),
    )
    monkeypatch.setattr(
        mod, "inject_assigned_skills", AsyncMock(return_value={"status": "success"})
    )
    monkeypatch.setattr(
        mod, "inject_read_only_hooks", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(
        mod, "remove_read_only_hooks", AsyncMock(return_value={"success": True})
    )
    # All eight config predicates satisfied by default; each test flips one.
    monkeypatch.setattr(
        mod, "check_shared_folder_mounts_match", AsyncMock(return_value=True)
    )
    for name in (
        "check_public_folder_mount_matches",
        "check_api_key_env_matches",
        "check_github_pat_env_matches",
        "check_resource_limits_match",
        "check_full_capabilities_match",
        "check_guardrails_env_matches",
        "check_agent_auth_token_env_matches",
    ):
        monkeypatch.setattr(mod, name, MagicMock(return_value=True))
    monkeypatch.setattr(mod, "check_base_image_matches", AsyncMock(return_value=True))

    fake_db = MagicMock()
    fake_db.get_agent_owner.return_value = {"owner_username": "admin"}
    fake_db.get_agent_ephemeral_info.return_value = None
    fake_db.get_read_only_mode.return_value = {"enabled": False}
    monkeypatch.setattr(mod, "db", fake_db)

    mod._container_under_test = container
    return mod


def test_running_system_agent_start_defers_the_recreate(lifecycle, monkeypatch):
    """T-C #5 — the AC2 gate. A config predicate fires, but the container is a
    RUNNING trinity-system, so nothing is replaced and the caller is told why."""
    monkeypatch.setattr(
        lifecycle, "check_api_key_env_matches", MagicMock(return_value=False)
    )

    result = _run(lifecycle.start_agent_internal("trinity-system"))

    lifecycle.recreate_container_with_updated_config.assert_not_awaited()
    assert result["recreated"] is False
    assert result["recreate_reason"] is None
    assert result["recreate_deferred"] == "system_agent_running"


def test_the_gate_covers_every_predicate_not_just_the_image_one(lifecycle, monkeypatch):
    """The recreate resolves the image from the container's own Config.Image
    TAG, so ANY predicate that fires is also an image adoption. Gating only the
    image predicate would leave AC2 open through the other eight."""
    for name in (
        "check_public_folder_mount_matches",
        "check_api_key_env_matches",
        "check_github_pat_env_matches",
        "check_resource_limits_match",
        "check_full_capabilities_match",
        "check_guardrails_env_matches",
        "check_agent_auth_token_env_matches",
    ):
        mod_backup = getattr(lifecycle, name)
        monkeypatch.setattr(lifecycle, name, MagicMock(return_value=False))
        lifecycle.recreate_container_with_updated_config.reset_mock()

        result = _run(lifecycle.start_agent_internal("trinity-system"))

        lifecycle.recreate_container_with_updated_config.assert_not_awaited()
        assert result["recreate_deferred"] == "system_agent_running", name
        monkeypatch.setattr(lifecycle, name, mod_backup)


def test_stopped_system_agent_start_recreates_normally(lifecycle, monkeypatch):
    """T-C #6 — the gate is about RUNNING, not about being the system agent.
    A cold start is the boundary where adoption is supposed to happen."""
    lifecycle._container_under_test.status = "exited"
    monkeypatch.setattr(
        lifecycle, "check_base_image_matches", AsyncMock(return_value=False)
    )

    result = _run(lifecycle.start_agent_internal("trinity-system"))

    lifecycle.recreate_container_with_updated_config.assert_awaited_once()
    assert result["recreated"] is True
    assert result["recreate_reason"] == "image_drift"
    assert result["recreate_deferred"] is None


def test_running_regular_agent_still_recreates_on_config_drift(lifecycle, monkeypatch):
    """The gate must not leak to the fleet: config drift on a regular running
    agent is an owner-INTENTIONAL change and still applies immediately."""
    monkeypatch.setattr(
        lifecycle, "check_api_key_env_matches", MagicMock(return_value=False)
    )

    result = _run(lifecycle.start_agent_internal("some-regular-agent"))

    lifecycle.recreate_container_with_updated_config.assert_awaited_once()
    assert result["recreated"] is True
    assert result["recreate_reason"] == "config_drift"
    assert result["recreate_deferred"] is None


def test_deferred_recreate_does_not_reset_the_breaker(lifecycle, monkeypatch):
    """#1560's guard: a no-op start of an already-running agent must not clear a
    breaker protecting a wedged agent. The gate turns this into a no-op start,
    so the clear must not fire either."""
    monkeypatch.setattr(
        lifecycle, "check_api_key_env_matches", MagicMock(return_value=False)
    )

    _run(lifecycle.start_agent_internal("trinity-system"))

    lifecycle.clear_agent_breakers.assert_not_called()


# ===========================================================================
# T-C #10/#11 — restart policy carry-forward + capabilities override
# ===========================================================================


@pytest.fixture
def provision_capture(monkeypatch):
    """Capture the kwargs ``_provision_folders_and_run_agent_container`` hands to
    ``containers_run``."""
    import services.agent_service.lifecycle as mod

    captured = {}

    async def _fake_run(image, **kwargs):
        captured["image"] = image
        captured.update(kwargs)
        return _Container()

    monkeypatch.setattr(mod, "containers_run", _fake_run)
    fake_db = MagicMock()
    fake_db.get_shared_folder_config.return_value = None
    fake_db.get_file_sharing_enabled.return_value = False
    monkeypatch.setattr(mod, "db", fake_db)
    return mod, captured


@pytest.mark.parametrize(
    "policy,forwarded",
    [
        ({"Name": "unless-stopped"}, True),
        ({"Name": "always"}, True),
        ({"Name": ""}, False),  # Docker's "no policy" — same as omitting it
        ({}, False),
        (None, False),
    ],
)
def test_restart_policy_is_forwarded_only_when_it_names_a_policy(
    provision_capture, policy, forwarded
):
    mod, captured = provision_capture

    _run(
        mod._provision_folders_and_run_agent_container(
            "a",
            image="trinity-agent-base:latest",
            env_vars={},
            labels={},
            base_volumes={},
            ssh_port=2222,
            cpu="2",
            memory="4g",
            full_capabilities=False,
            restart_policy=policy,
        )
    )

    assert ("restart_policy" in captured) is forwarded
    if forwarded:
        assert captured["restart_policy"] == policy


def test_restart_policy_defaults_to_absent_for_pre_1816_callers(provision_capture):
    """`recreate_missing_container` (#1559) passes nothing — byte-identical."""
    mod, captured = provision_capture

    _run(
        mod._provision_folders_and_run_agent_container(
            "a",
            image="trinity-agent-base:latest",
            env_vars={},
            labels={},
            base_volumes={},
            ssh_port=2222,
            cpu="2",
            memory="4g",
            full_capabilities=False,
        )
    )

    assert "restart_policy" not in captured


@pytest.fixture
def recreate_capture(monkeypatch):
    """Drive ``recreate_container_with_updated_config`` and capture what it hands
    to the shared run tail."""
    import services.agent_service.lifecycle as mod

    captured = {}

    async def _fake_provision(agent_name, **kwargs):
        captured["agent_name"] = agent_name
        captured.update(kwargs)
        return _Container()

    monkeypatch.setattr(
        mod, "_provision_folders_and_run_agent_container", _fake_provision
    )
    monkeypatch.setattr(mod, "container_stop", AsyncMock())
    monkeypatch.setattr(mod, "container_remove", AsyncMock())
    monkeypatch.setattr(mod, "validate_base_image", MagicMock())
    monkeypatch.setattr(mod, "image_get", AsyncMock(return_value=MagicMock(labels={})))
    monkeypatch.setattr(mod, "get_anthropic_api_key", lambda: "sk-test")
    monkeypatch.setattr(mod, "get_agent_full_capabilities", lambda: False)
    monkeypatch.setattr(
        mod, "get_agent_default_resources", lambda: {"cpu": "2", "memory": "4g"}
    )
    monkeypatch.setattr(mod, "derive_agent_token", lambda name: f"tok-{name}")

    fake_db = MagicMock()
    fake_db.get_agent_subscription_id.return_value = None
    fake_db.get_use_platform_api_key.return_value = True
    fake_db.get_agent_github_pat.return_value = None
    fake_db.get_git_config.return_value = None
    fake_db.get_guardrails_config.return_value = None
    fake_db.get_resource_limits.return_value = None
    fake_db.get_public_mount_path.return_value = "/home/developer/public"
    monkeypatch.setattr(mod, "db", fake_db)
    return mod, captured


def test_recreate_carries_the_restart_policy_forward(recreate_capture):
    """The regression: `old_host_config` was extracted and never read, so
    `unless-stopped` silently vanished from every recreated agent."""
    mod, captured = recreate_capture
    old = _Container(host_config={"RestartPolicy": {"Name": "unless-stopped"}})

    _run(mod.recreate_container_with_updated_config("trinity-system", old, "system"))

    assert captured["restart_policy"] == {"Name": "unless-stopped"}


def test_recreate_is_null_safe_when_restart_policy_is_null(recreate_capture):
    """`.get("RestartPolicy", {})` returns None when the key exists with a null
    value, and `.get` on None would abort the recreate — after the old container
    is already removed, leaving the agent with none at all."""
    mod, captured = recreate_capture
    old = _Container(host_config={"RestartPolicy": None})

    _run(mod.recreate_container_with_updated_config("a", old, "system"))

    assert captured["restart_policy"] == {}


def test_recreate_is_null_safe_when_host_config_has_no_policy(recreate_capture):
    mod, captured = recreate_capture
    old = _Container(host_config={})

    _run(mod.recreate_container_with_updated_config("a", old, "system"))

    assert captured["restart_policy"] == {}


def test_recreate_preserves_full_capabilities_for_the_system_agent(recreate_capture):
    """T-C #11 — the fleet setting is False, but trinity-system's
    FULL_CAPABILITIES are contractual. Without this the orchestrator silently
    loses the ability to install packages on its first adoption."""
    mod, captured = recreate_capture
    old = _Container()

    _run(mod.recreate_container_with_updated_config("trinity-system", old, "system"))

    assert captured["full_capabilities"] is True
    assert captured["labels"]["trinity.full-capabilities"] == "true"


def test_recreate_follows_the_fleet_setting_for_a_regular_agent(recreate_capture):
    mod, captured = recreate_capture
    old = _Container()

    _run(mod.recreate_container_with_updated_config("regular", old, "system"))

    assert captured["full_capabilities"] is False
    assert captured["labels"]["trinity.full-capabilities"] == "false"


def test_recreate_honours_an_explicit_full_capabilities_override(recreate_capture):
    mod, captured = recreate_capture
    old = _Container()

    _run(
        mod.recreate_container_with_updated_config(
            "regular", old, "system", full_capabilities=True
        )
    )

    assert captured["full_capabilities"] is True


def test_recreate_does_not_arm_the_system_agents_heartbeat(recreate_capture):
    """TRINITY_BACKEND_URL gates the agent-side heartbeat loop, and
    authorize_heartbeat accepts only scope='agent' keys — the system agent's is
    scope='system', so arming it is a permanent 5s 403 loop. #1816 makes this
    recreate a ROUTINE path for it."""
    mod, captured = recreate_capture
    old = _Container()

    _run(mod.recreate_container_with_updated_config("trinity-system", old, "system"))

    assert "TRINITY_BACKEND_URL" not in captured["env_vars"]


def test_recreate_still_arms_a_regular_agents_backend_url(recreate_capture):
    mod, captured = recreate_capture
    old = _Container()

    _run(mod.recreate_container_with_updated_config("regular", old, "system"))

    assert captured["env_vars"].get("TRINITY_BACKEND_URL")


# ===========================================================================
# T-C #12 — wrapper parity
# ===========================================================================


@pytest.mark.parametrize(
    "state,expected",
    [("match", True), ("unknown", True), ("drift", False)],
)
def test_boolean_wrapper_is_state_is_not_drift(monkeypatch, state, expected):
    """#1809's consumer must be byte-identical: fail-open exits and a match both
    mean 'do not recreate'."""
    from services.agent_service import helpers

    monkeypatch.setattr(
        helpers, "check_base_image_state", AsyncMock(return_value=state)
    )
    assert _run(helpers.check_base_image_matches(object(), "a")) is expected


# ===========================================================================
# T-C #12b — the 3-state itself: `unknown` must never be reported as `match`
#
# Everything above that asserts the alarm's core safety property ("never alarm
# on a check that could not run") MOCKS `check_base_image_state` outright, and
# every #1809 test asserts the BOOLEAN wrapper — where `match` and `unknown`
# are indistinguishable by construction (both return True). So nothing else in
# the suite pins what the real function returns.
#
# Without these, a refactor returning "match" on an unreadable probe keeps the
# whole suite green while `ensure_deployed` logs "running on a current base
# image" and `GET /api/system-agent/status` tells an admin `current` about a
# comparison that never happened — the #1809 symptom (an unreadable check
# indistinguishable from no-drift) rebuilt one layer up, which is the entire
# reason the 3-state split exists.
# ===========================================================================


_RUNNING_ID = "sha256:" + "b" * 64
_REBUILT_ID = "sha256:" + "a" * 64


class _StateContainer:
    """Minimal docker-SDK stand-in: the probe reads only ``.attrs``."""

    def __init__(self, attrs):
        self.attrs = attrs


def _image_get(result):
    async def _fake(_ref):
        if isinstance(result, Exception):
            raise result
        return MagicMock(id=result)

    return _fake


def _patch_image_get(monkeypatch, fn, result):
    """Patch the name in the globals of the function actually under test.

    Not ``setattr(helpers, ...)``: another file re-importing the module leaves
    ``helpers`` and ``fn.__globals__`` as different dicts, and the patch would
    land on the one nobody calls (the module-identity gotcha). ``setitem``
    restores itself.
    """
    monkeypatch.setitem(fn.__globals__, "image_get", _image_get(result))


def test_state_match_when_the_reference_resolves_to_the_running_id(monkeypatch):
    from services.agent_service import helpers

    _patch_image_get(monkeypatch, helpers.check_base_image_state, _RUNNING_ID)
    c = _StateContainer({"Image": _RUNNING_ID, "Config": {"Image": "b:latest"}})
    assert _run(helpers.check_base_image_state(c, "a")) == "match"


def test_state_drift_when_the_tag_resolves_elsewhere(monkeypatch):
    from services.agent_service import helpers

    _patch_image_get(monkeypatch, helpers.check_base_image_state, _REBUILT_ID)
    c = _StateContainer({"Image": _RUNNING_ID, "Config": {"Image": "b:latest"}})
    assert _run(helpers.check_base_image_state(c, "a")) == "drift"


@pytest.mark.parametrize(
    "attrs",
    [
        pytest.param({"Config": {"Image": "b:latest"}}, id="no_running_image_id"),
        pytest.param({"Image": _RUNNING_ID, "Config": {"Image": ""}}, id="empty_ref"),
        pytest.param({"Image": _RUNNING_ID, "Config": {}}, id="no_reference"),
        pytest.param({"Image": _RUNNING_ID}, id="no_config_section"),
        pytest.param({}, id="empty_attrs"),
    ],
)
def test_state_unknown_when_the_container_attrs_are_unreadable(monkeypatch, attrs):
    """Never ``match``: the comparison never happened."""
    from services.agent_service import helpers

    _patch_image_get(monkeypatch, helpers.check_base_image_state, _RUNNING_ID)
    assert _run(helpers.check_base_image_state(_StateContainer(attrs), "a")) == "unknown"


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(docker.errors.ImageNotFound("gone"), id="image_not_found"),
        pytest.param(docker.errors.APIError("boom"), id="docker_api_error"),
        pytest.param(RuntimeError("boom"), id="unexpected_error"),
    ],
)
def test_state_unknown_when_the_reference_cannot_be_resolved(monkeypatch, exc):
    """Never ``match``: an unresolvable reference is an unrun check, not a clean
    bill of health."""
    from services.agent_service import helpers

    _patch_image_get(monkeypatch, helpers.check_base_image_state, exc)
    c = _StateContainer({"Image": _RUNNING_ID, "Config": {"Image": "b:latest"}})
    assert _run(helpers.check_base_image_state(c, "a")) == "unknown"


def test_unknown_never_reaches_the_operator_as_current(sas, monkeypatch):
    """End-to-end with the predicate UNMOCKED — the property the mocked running
    -branch tests above assume but cannot prove. A container whose image
    reference no longer resolves must surface `unknown`, never `current`, and
    must raise no alarm."""
    _patch_image_get(
        monkeypatch,
        sas.check_base_image_state,
        docker.errors.ImageNotFound("gone"),
    )
    monkeypatch.setattr(sas, "start_agent_internal", AsyncMock())

    result = _run(sas.SystemAgentService().ensure_deployed())

    assert result["base_image_state"] == "unknown"
    assert result["action"] == "none"
    sas.db.create_operator_queue_item.assert_not_called()


# ===========================================================================
# T-C #8/#9 — the admin router
# ===========================================================================


@pytest.fixture
def rsa(monkeypatch):
    """The real ``routers.system_agent`` with its auth + Docker surface patched."""
    import routers.system_agent as mod

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)
    # `reject_agent_principal` is deliberately NOT stubbed — the #1816 human-only
    # gate should be exercised for real. Callers pass `_human_caller()`.
    monkeypatch.setattr(mod, "container_reload", AsyncMock())
    monkeypatch.setattr(mod, "container_stop", AsyncMock())
    monkeypatch.setattr(mod, "container_start", AsyncMock())
    monkeypatch.setattr(mod, "db", MagicMock())
    return mod


def test_restart_delegates_and_reads_a_FRESH_container_for_the_response(
    rsa, monkeypatch
):
    """T-C #8. A recreate REPLACES the container, so the local handle points at a
    removed one — reporting its `.status` would describe the corpse."""
    old = _Container(status="running")
    replacement = _Container(status="running")
    replacement.short_id = "new00000"
    handles = [old, replacement]
    monkeypatch.setattr(rsa, "get_agent_container", lambda name: handles[-1])
    monkeypatch.setattr(
        rsa,
        "start_agent_internal",
        AsyncMock(return_value={"recreated": True, "recreate_reason": "image_drift"}),
    )

    # The endpoint resolves its own container first; feed it the old handle.
    handles.pop()
    result = _run(rsa.restart_system_agent(MagicMock(), _human_caller()))
    handles.append(replacement)

    rsa.start_agent_internal.assert_awaited_once_with("trinity-system")
    rsa.container_stop.assert_awaited_once()
    rsa.container_start.assert_not_awaited(), "the bare start is superseded"
    assert result["success"] is True
    assert result["recreated"] is True
    assert result["recreate_reason"] == "image_drift"


def test_restart_reports_unknown_rather_than_crashing_if_the_container_vanished(
    rsa, monkeypatch
):
    """Belt for the R1 window: if the replacement did not come up, the response
    must still return rather than AttributeError on a None handle."""
    calls = {"n": 0}

    def _get(name):
        calls["n"] += 1
        return _Container(status="running") if calls["n"] == 1 else None

    monkeypatch.setattr(rsa, "get_agent_container", _get)
    monkeypatch.setattr(
        rsa, "start_agent_internal", AsyncMock(return_value={"recreated": True})
    )

    result = _run(rsa.restart_system_agent(MagicMock(), _human_caller()))

    assert result["status"] == "unknown"


@pytest.mark.parametrize(
    "state,label", [("match", "current"), ("drift", "stale"), ("unknown", "unknown")]
)
def test_status_reports_the_three_state_base_image(rsa, monkeypatch, state, label):
    """T-C #9. An ENUM only — never image ids or digests (the `/health
    clone_status` contract, #1439)."""
    monkeypatch.setattr(rsa, "get_agent_container", lambda name: _Container("running"))
    monkeypatch.setattr(rsa, "check_base_image_state", AsyncMock(return_value=state))
    monkeypatch.setattr(rsa, "agent_httpx_client", MagicMock(side_effect=RuntimeError()))

    result = _run(rsa.get_system_agent_status(MagicMock(), MagicMock()))

    assert result["base_image_state"] == label
    assert "sha256:" not in repr(result)


def test_status_omits_base_image_state_when_the_container_is_stopped(rsa, monkeypatch):
    """A stopped container adopts on its next start, so reporting staleness for
    it would be advice about a state that is about to be fixed."""
    monkeypatch.setattr(rsa, "get_agent_container", lambda name: _Container("exited"))
    monkeypatch.setattr(rsa, "check_base_image_state", AsyncMock(return_value="drift"))

    result = _run(rsa.get_system_agent_status(MagicMock(), MagicMock()))

    assert "base_image_state" not in result
    rsa.check_base_image_state.assert_not_awaited()


def test_reinitialize_deliberately_does_not_adopt():
    """Documented non-goal: /reinitialize is already an explicit stop and carries
    zero incremental AC coverage; its four-site handle rebinding was the
    highest-risk edit considered and was cut."""
    src = (_BACKEND / "routers" / "system_agent.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "reinitialize_system_agent"
    )
    body = ast.get_source_segment(src, fn)
    assert "start_agent_internal" not in body


# ===========================================================================
# T-D — source pins
# ===========================================================================


def _ensure_deployed_source() -> str:
    """The source of ``ensure_deployed`` ONLY — AST-sliced, so a match elsewhere
    in the module cannot satisfy a guard meant to pin this function."""
    text = _SYSTEM_AGENT_SERVICE.read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "ensure_deployed"
        ):
            return ast.get_source_segment(text, node)
    pytest.fail("ensure_deployed not found")


def _running_branch_source() -> str:
    """The RUNNING branch of ``ensure_deployed``, sliced between two named
    anchors rather than searched as a substring: a bare whole-function search
    passes on any refactor that merely relocates the call."""
    src = _ensure_deployed_source()
    start = src.index('if container.status == "running":')
    end = src.index("# #1816 — STOPPED", start)
    return src[start:end]


def test_the_running_branch_contains_no_recreate_or_removal(monkeypatch):
    """AC2 as a source invariant. The read-only branch must never grow a
    container-replacing call."""
    branch = _running_branch_source()
    for forbidden in (
        "recreate_container_with_updated_config",
        "start_agent_internal(",
        "container_remove",
        "container_stop",
        "containers_run",
    ):
        assert forbidden not in branch, (
            f"{forbidden} appeared in ensure_deployed's READ-ONLY running branch "
            "— a running trinity-system must never be replaced (#1816 AC2)"
        )


def test_the_running_branch_evaluates_the_shared_predicate():
    assert "check_base_image_state(" in _running_branch_source()


def test_no_hand_rolled_image_comparison_in_the_system_agent_service():
    """AC3: the adoption path REUSES the #1809 predicate. One comparison, one
    place — a forked compare is exactly how the two paths drift apart again."""
    src = _SYSTEM_AGENT_SERVICE.read_text()
    assert "from services.agent_service.helpers import check_base_image_state" in src
    assert "image_get(" not in src, "no direct image lookup — use the shared predicate"
    assert '.attrs.get("Image")' not in src
    assert ".attrs.get('Image')" not in src


def test_the_stopped_branch_delegates_to_the_shared_lifecycle():
    src = _ensure_deployed_source()
    stopped = src[src.index("# #1816 — STOPPED") :]
    assert "start_agent_internal(SYSTEM_AGENT_NAME)" in stopped


def test_the_alert_prefix_is_registered_in_the_reserved_guard():
    """#1632: an unregistered prefix lets an agent pre-create — and, via
    create_item's on_conflict_do_nothing, silently suppress — the alarm raised
    about it."""
    import services.operator_queue_service as oq
    import services.system_agent_service as sas

    assert sas.BASE_IMAGE_STALE_ALERT_PREFIX in oq._RESERVED_ID_PREFIXES


def test_the_router_surfaces_recreate_deferred():
    """#1809's own learning: `start_agent_endpoint` rebuilds a fresh dict from a
    whitelist, so a field added to `start_agent_internal`'s return value alone
    dies at the router."""
    src = (_BACKEND / "routers" / "agents.py").read_text()
    assert '"recreate_deferred": result.get("recreate_deferred")' in src


# ---------------------------------------------------------------------------
# Review follow-ups: the orchestrator must not be rebuilt by the generic
# recovery path, and the start-failure alarm must not carry raw secrets.
# ---------------------------------------------------------------------------


def test_recovery_rebuild_refuses_the_system_agent(lifecycle):
    """`recreate_missing_container` reconstructs a REGULAR agent: it deactivates
    the existing **system-scoped** MCP key and mints an *agent-scoped* one in its
    place (plaintext unrecoverable ⇒ irreversible), drops `trinity.is-system`,
    the `/template` bind and `unless-stopped`, and arms the scope-403
    `TRINITY_BACKEND_URL` heartbeat.

    #1816 newly exposed it: both `ensure_deployed`'s stopped branch and
    `POST /api/system-agent/restart` now delegate to `start_agent_internal`,
    whose container lookup can come back None while a concurrent `--workers N`
    boot is mid-recreate. Fail closed — `ensure_deployed`'s create branch is the
    correct rebuild path.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _run(lifecycle.recreate_missing_container("trinity-system"))
    assert exc.value.status_code == 409
    assert "ensure_deployed" in exc.value.detail


def test_start_of_a_container_less_system_agent_never_downgrades_it(lifecycle, monkeypatch):
    """End-to-end shape of the guard: the refusal propagates out of
    `start_agent_internal` instead of silently provisioning a degraded
    orchestrator."""
    from fastapi import HTTPException

    monkeypatch.setattr(lifecycle, "get_agent_container", lambda name: None)
    monkeypatch.setattr(lifecycle.db, "get_agent_owner", lambda name: {"owner_username": "admin"})

    with pytest.raises(HTTPException) as exc:
        _run(lifecycle.start_agent_internal("trinity-system"))
    assert exc.value.status_code == 409


def test_start_failure_alarm_sanitizes_the_interpolated_reason(sas, monkeypatch):
    """`reason` is an arbitrary exception string and lands in
    `operator_queue.question` — durable, operator-visible state. Canary G-04's
    rule: the emit chokepoint sanitizes."""
    created = []
    monkeypatch.setattr(sas.db, "create_operator_queue_item", lambda agent, item: created.append(item))

    sas.SystemAgentService()._emit_start_failed_alert(
        "docker refused: token=sk-ant-api03-DEADBEEFDEADBEEFDEADBEEFDEADBEEF"
    )

    assert len(created) == 1
    blob = created[0]["question"] + str(created[0]["context"])
    assert "sk-ant-api03-DEADBEEFDEADBEEFDEADBEEFDEADBEEF" not in blob
    assert "docker refused" in created[0]["question"], "sanitizing must not eat the diagnosis"


def test_the_alarm_cooldown_uses_a_monotonic_clock(sas):
    """A wall-clock cursor (`datetime.utcnow()`) lets an NTP step skip or extend
    the cooldown, and `utcnow()` is deprecated. `time.monotonic()` is the right
    primitive for an elapsed-time gate."""
    src = _SYSTEM_AGENT_SERVICE.read_text()
    assert "time.monotonic()" in src
    assert "datetime.utcnow(" not in src
    assert "from datetime import" not in src


def test_the_destructive_system_agent_endpoints_are_human_only():
    """`assert_admin` rejects CONNECTOR principals, not AGENT ones, and
    `get_current_user` hands an agent-scoped MCP key its owner's role — so on a
    default admin-owned install any non-ephemeral agent's TRINITY_MCP_API_KEY
    passes it. #1816 turns `/restart` into a container REPLACEMENT, so the gate
    has to be human-only (trinity-ops-agent#232 precedent)."""
    src = (_BACKEND / "routers" / "system_agent.py").read_text()
    tree = ast.parse(src)
    guarded = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and "reject_agent_principal" in ast.dump(node)
    }
    assert {"restart_system_agent", "reinitialize_system_agent"} <= guarded


def test_start_failure_alarm_is_deduped_across_processes(sas, monkeypatch):
    """A per-process cursor cannot bound this alarm: a crash-looping backend
    re-emits from a FRESH process every time. The bucketed id collapses the
    burst in the DB via create_item's (agent_name, request_id) conflict target.
    """
    created = []
    monkeypatch.setattr(sas.db, "create_operator_queue_item", lambda agent, item: created.append(item))

    # Two emissions from two distinct service instances — the cross-process case.
    sas.SystemAgentService()._emit_start_failed_alert("network missing")
    sas.SystemAgentService()._emit_start_failed_alert("network missing")

    assert len(created) == 2, "both still attempt the write — the DB dedupes"
    assert created[0]["id"] == created[1]["id"], (
        "same bucket must yield the same request_id so on_conflict_do_nothing collapses them"
    )
    assert not created[0]["id"].endswith("Z"), "a raw timestamp would defeat the dedup"


def test_start_failure_alarm_bucket_re_arms():
    """Deliberately a bucket, not a fixed id: a fixed id wedges shut forever once
    Clear All flips the row to `cancelled` (the conflict target ignores status,
    #1644)."""
    import services.system_agent_service as mod

    assert mod.START_FAILED_ALERT_BUCKET_SECONDS > 0
    assert mod.START_FAILED_ALERT_BUCKET_SECONDS <= 6 * 60 * 60
