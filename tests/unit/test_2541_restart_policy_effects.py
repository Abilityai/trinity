"""The consequences of #2541, not the fix itself.

Making every agent container `unless-stopped` changes three things that live
outside the create path, and each is asserted here rather than discovered in
production:

* ``restarting`` becomes a **reachable** Docker state for every agent — most
  visibly as the transient state during the very host-reboot recovery this
  change exists to produce, i.e. exactly when an operator is watching the
  dashboard. Before this, only ``trinity-system`` carried a policy, so
  ``RestartCount`` stayed 0 and ``restarting`` was effectively unreachable.
* The two ``RestartCount`` branches in ``monitoring_service`` go live
  fleet-wide, having effectively never executed for a regular agent.
* The platform's own services, which the SDK census cannot see, are governed by
  compose — the LOG-001 / LOG-002 split of #1871, one directory up.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.hosted.yml",
)


# ===========================================================================
# RESTART-005 — `restarting` normalizes to `stopped` on BOTH normalizers
# ===========================================================================


def _fake_container(status: str, name: str = "agent-alpha"):
    c = MagicMock()
    c.status = status
    c.name = name
    c.id = "cafe1234"
    c.labels = {
        "trinity.platform": "agent",
        "trinity.ssh-port": "2222",
        "trinity.cpu": "2",
        "trinity.memory": "4g",
        # Present so the normalizer never falls back to an image lookup — this
        # test is about status, not about the #1809 version label.
        "trinity.base-image-version": "0.0.0-test",
    }
    c.attrs = {"Config": {"Env": []}}
    return c


@pytest.mark.parametrize(
    "docker_status,expected",
    [
        ("running", "running"),
        ("exited", "stopped"),
        ("dead", "stopped"),
        ("created", "stopped"),
        ("restarting", "stopped"),  # #2541 — newly reachable for every agent
    ],
)
def test_single_agent_status_normalizes_restarting_to_stopped(docker_status, expected):
    """`get_agent_status_from_container` — the single-agent API surface.

    Passed through verbatim (the pre-#2541 `else` branch), `restarting` matches
    NEITHER of the frontend's exact-equality filters in `stores/agents.js`, so
    an agent coming back from a reboot appears in neither `runningAgents` nor
    `stoppedAgents` — a loud failure rendered invisible.
    """
    from services.docker_service import get_agent_status_from_container

    agent = get_agent_status_from_container(_fake_container(docker_status))
    assert agent.status == expected


def test_the_two_normalizers_are_twins():
    """The same six lines exist twice — `get_agent_status_from_container` and the
    inline copy inside `list_all_agents_fast` (the list-all fast path).

    A fix applied to one only is a fix that reaches one API surface. Asserted
    structurally because they are copy-pasted, not shared: extracting them is the
    cleaner shape and is deliberately NOT bundled into this change.
    """
    import inspect
    from services import docker_service

    single = inspect.getsource(docker_service.get_agent_status_from_container)
    listing = inspect.getsource(docker_service.list_all_agents_fast)
    tuple_literal = '("exited", "dead", "created", "restarting")'

    assert tuple_literal in single, (
        "get_agent_status_from_container no longer maps `restarting` to stopped "
        "(#2541)."
    )
    assert tuple_literal in listing, (
        "list_all_agents_fast's inline normalizer drifted from its twin — the "
        "roster API would report `restarting` verbatim again (#2541)."
    )


def test_all_three_mappings_agree_on_restarting():
    """`agent_container_states` (the tri-state reader, #2196) has ALWAYS called a
    non-running container "stopped". It was the outlier; now all three agree."""
    import inspect
    from services import docker_service

    src = inspect.getsource(docker_service.agent_container_states)
    assert '"running" if container.status == "running" else "stopped"' in src, (
        "agent_container_states changed shape — re-check that it still agrees "
        "with the two normalizers about `restarting` (#2541 RESTART-005)."
    )


# ===========================================================================
# RESTART-006 — the two dormant RestartCount paths
# ===========================================================================


def test_high_restart_count_threshold_still_behaves():
    """Docker increments `RestartCount` only under a restart policy, so this
    branch has effectively never run for a regular agent. It goes live
    fleet-wide with #2541 — which is desirable (it is what makes a crash loop
    visible instead of silently re-running startup.sh's ~667 lines of clone,
    credential injection and plugin installs on every retry), but it is an
    unreviewed surface. Pin the threshold so "it started alerting" is a known
    consequence and not a mystery.
    """
    import inspect
    from services import monitoring_service

    src = inspect.getsource(monitoring_service)
    assert (
        "docker.restart_count > 3" in src
    ), "the High restart count health issue changed shape (#2541 RESTART-006)"
    assert (
        "docker_check.restart_count > 3" in src
    ), "the alert_high_restart_count trigger changed shape (#2541 RESTART-006)"


# ===========================================================================
# RESTART-008 — the compose half (the platform's own services)
# ===========================================================================


def _services(rel: str) -> dict:
    doc = yaml.safe_load((_REPO / rel).read_text(encoding="utf-8"))
    return doc.get("services", {}) or {}


@pytest.mark.parametrize("rel", _COMPOSE_FILES)
def test_every_long_lived_service_declares_a_restart_policy(rel):
    """The platform half of the same bug (#2541, mirroring LOG-001).

    `docker-compose.prod.yml` and `.hosted.yml` both carried this; the BASE file
    — the one `start.sh` uses without `--hosted`, i.e. the README quickstart and
    the source install — was missing it on backend, frontend and redis, so a
    self-hosted Trinity's own control plane did not survive a host reboot.
    `test_2280_hosted_compose_parity` compares prod<->hosted wholesale and
    compares the base file to NOTHING, which is exactly how it drifted while the
    other two stayed correct.

    One-shot `*-init` services are exempt BY NAME and must declare `"no"`
    explicitly — an omission there is indistinguishable from an oversight.
    """
    missing = []
    for name, svc in sorted(_services(rel).items()):
        policy = (svc or {}).get("restart")
        if name.endswith("-init"):
            assert str(policy) == "no", (
                f"{rel}:{name} is a one-shot init service and must declare "
                f'restart: "no" explicitly, not {policy!r}'
            )
            continue
        if policy != "unless-stopped":
            missing.append(f"{name}={policy!r}")

    assert not missing, (
        f"{rel}: these long-lived services do not declare "
        f"`restart: unless-stopped`, so they do not survive a host reboot "
        f"(#2541 RESTART-008): {', '.join(missing)}"
    )


def test_the_three_compose_files_agree_on_the_core_services():
    """backend / frontend / redis are the three the base file was missing. Pin
    them across all three files, so a future edit that adds a service to one
    cannot quietly leave the base install without a control plane."""
    for name in ("backend", "frontend", "redis"):
        for rel in _COMPOSE_FILES:
            svc = _services(rel).get(name)
            assert svc is not None, f"{rel} has no `{name}` service"
            assert svc.get("restart") == "unless-stopped", (
                f"{rel}:{name} restart policy is {svc.get('restart')!r} — a "
                "reboot leaves the platform down (#2541)."
            )
