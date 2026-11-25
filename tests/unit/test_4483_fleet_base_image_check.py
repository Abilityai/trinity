"""The fleet base-image check: which running agents are on an old image (#4483).

After an upgrade, agents keep the image they were created from until their next
cold start (#1809 — deliberate, and unchanged here), and only ``trinity-system``
said so. Agent-side fixes in a release were therefore silently inert on every
agent nobody stopped. The check reads the fleet, files ONE platform alert naming
the stale agents, updates it as the set changes and ends it when none are left.

What is pinned here:

* the alert names every stale agent and only those; ephemeral agents, stopped
  agents and the system agent (which has its own alert) are left out;
* "could not check" — Docker unreadable, an agent whose comparison failed, a
  base image with no version label — is never reported as current (no clear)
  and never as stale (no alert);
* a base image older than the backend is reported even when no agent drifted,
  with the command for a source install and for a hosted one;
* nothing here stops, starts, restarts or removes a container;
* only the lease holder runs a cycle, and `main.py` starts the loop.

The Docker and database reads are the module's own thin seams, patched here;
`platform_alerts` is patched at the seam's public functions.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from services import fleet_image_service as fis  # noqa: E402
from services import platform_alerts  # noqa: E402

SYS = "trinity-system"


class _Fleet:
    """A scripted fleet: container states, per-agent image state, the base
    image's version label and the backend's version."""

    def __init__(self):
        self.states = {}            # name -> "running" | "stopped"; None = Docker unreadable
        self.image_state = {}       # name -> "match" | "drift" | "unknown"
        self.ephemeral = set()
        self.label = "1.0.5"        # None = no label; an Exception = image unreadable
        self.backend = "1.0.5"
        self.containers = {}
        self.observe = MagicMock(return_value="filed")
        self.clear = MagicMock(return_value=0)

    def agent(self, name, image_state="match", *, state="running", ephemeral=False):
        self.states[name] = state
        self.image_state[name] = image_state
        if ephemeral:
            self.ephemeral.add(name)
        return self

    def install(self, monkeypatch):
        async def states():
            return self.states

        def container(name):
            return self.containers.setdefault(name, MagicMock(name=f"container-{name}"))

        async def image_state(container, name):
            return self.image_state[name]

        async def labels():
            if isinstance(self.label, Exception):
                raise self.label
            return {} if self.label is None else {"trinity.base-image-version": self.label}

        monkeypatch.setattr(fis, "_container_states", states)
        monkeypatch.setattr(fis, "_get_container", container)
        monkeypatch.setattr(fis, "_image_state", image_state)
        monkeypatch.setattr(fis, "_is_ephemeral", lambda name: name in self.ephemeral)
        monkeypatch.setattr(fis, "_base_image_labels", labels)
        monkeypatch.setattr(fis, "_backend_version", lambda: self.backend)
        monkeypatch.setattr(platform_alerts, "observe", self.observe)
        monkeypatch.setattr(platform_alerts, "clear", self.clear)
        return self

    def run(self):
        return asyncio.run(fis.run_once())


@pytest.fixture
def fleet(monkeypatch):
    return _Fleet().install(monkeypatch)


# ---------------------------------------------------------------------------
# The version comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("image, backend, expected", [
    ("1.0.0", "1.0.5", fis.BASE_OUTDATED),
    ("1.0.5-rc2", "1.0.5", fis.BASE_OUTDATED),       # a candidate is older than its release
    ("1.0.5-rc2", "1.0.5-rc3", fis.BASE_OUTDATED),
    ("0.9.5", "1.0.0", fis.BASE_OUTDATED),
    ("1.0.5", "1.0.5", fis.BASE_CURRENT),
    ("v1.0.5", "1.0.5", fis.BASE_CURRENT),
    ("1.0.5", "1.0.5-rc3", fis.BASE_CURRENT),         # a newer image is not "outdated"
    ("1.1.0", "1.0.5", fis.BASE_CURRENT),
])
def test_an_image_version_is_compared_to_the_backend_version(image, backend, expected):
    assert fis.compare_versions(image, backend) == expected


@pytest.mark.parametrize("image, backend", [
    (None, "1.0.5"), ("", "1.0.5"), ("latest", "1.0.5"),   # build-base-image.sh's no-VERSION fallback
    ("1.0", "1.0.5"), ("nightly-3", "1.0.5"),
    ("1.0.5", "unknown"), ("1.0.5", ""), ("1.0.5", None),
])
def test_a_version_that_cannot_be_read_is_unknown_never_current_or_outdated(image, backend):
    assert fis.compare_versions(image, backend) == fis.BASE_UNKNOWN


# ---------------------------------------------------------------------------
# run_once — one alert for the fleet
# ---------------------------------------------------------------------------

def test_stale_agents_are_named_in_one_alert(fleet):
    fleet.agent("alpha", "drift").agent("bravo", "match").agent("charlie", "drift")
    result = fleet.run()

    assert result["status"] == "stale" and result["stale"] == ["alpha", "charlie"]
    fleet.observe.assert_called_once()
    args, kwargs = fleet.observe.call_args
    assert args == (SYS, "fleet_base_image_stale", "fleet")
    assert "alpha" in kwargs["question"] and "charlie" in kwargs["question"]
    assert "bravo" not in kwargs["question"]
    assert "/api/ops/fleet/restart" in kwargs["question"]
    assert kwargs["context"]["agents"] == ["alpha", "charlie"]
    fleet.clear.assert_not_called()


def test_the_alert_ends_when_no_stale_agent_is_left(fleet):
    fleet.agent("alpha", "match").agent("bravo", "match")
    result = fleet.run()

    assert result["status"] == "current"
    fleet.observe.assert_not_called()
    fleet.clear.assert_called_once_with(SYS, "fleet_base_image_stale", "fleet")


def test_a_shrinking_stale_set_updates_the_same_subject(fleet):
    fleet.agent("alpha", "drift").agent("bravo", "drift")
    fleet.run()
    fleet.image_state["alpha"] = "match"          # alpha was restarted
    fleet.run()

    subjects = {c.args for c in fleet.observe.call_args_list}
    assert subjects == {(SYS, "fleet_base_image_stale", "fleet")}
    assert fleet.observe.call_args.kwargs["context"]["agents"] == ["bravo"]


def test_ephemeral_stopped_and_system_agents_are_left_out(fleet):
    fleet.agent("ghost", "drift", ephemeral=True)
    fleet.agent("parked", "drift", state="stopped")
    fleet.agent(SYS, "drift")                      # has its own alert (#1816)
    fleet.agent("alpha", "drift")
    result = fleet.run()

    assert result["stale"] == ["alpha"]
    assert fleet.observe.call_args.kwargs["context"]["agents"] == ["alpha"]


def test_an_older_base_image_is_reported_even_when_no_agent_drifted(fleet):
    """The gap in front of drift: an update path that never rebuilt the image
    leaves every agent matching it, so there is no drift to detect."""
    fleet.agent("alpha", "match")
    fleet.label, fleet.backend = "1.0.0", "1.0.5"
    result = fleet.run()

    assert result["status"] == "stale" and result["base_image"] == fis.BASE_OUTDATED
    question = fleet.observe.call_args.kwargs["question"]
    assert "1.0.0" in question and "1.0.5" in question
    assert "build-base-image.sh" in question          # source install
    assert "start.sh --hosted" in question            # hosted install
    assert fleet.observe.call_args.kwargs["context"]["base_image"] == fis.BASE_OUTDATED
    fleet.clear.assert_not_called()


@pytest.mark.parametrize("label", [None, "latest", RuntimeError("docker down")])
def test_a_base_image_whose_version_cannot_be_read_is_could_not_check(fleet, label):
    fleet.agent("alpha", "match")
    fleet.label = label
    result = fleet.run()

    assert result["base_image"] == fis.BASE_UNKNOWN
    assert result["status"] == "could_not_check"      # never "current"
    fleet.observe.assert_not_called()                 # never "stale"
    fleet.clear.assert_not_called()


def test_an_unreadable_version_does_not_hide_agents_that_did_drift(fleet):
    fleet.agent("alpha", "drift")
    fleet.label = None
    result = fleet.run()

    assert result["status"] == "stale" and result["stale"] == ["alpha"]
    question = fleet.observe.call_args.kwargs["question"]
    assert "could not be checked" in question
    assert "build-base-image.sh" not in question      # no rebuild claimed without evidence


def test_docker_unreadable_is_could_not_check(fleet):
    fleet.states = None
    result = fleet.run()

    assert result == {"status": "could_not_check", "reason": "docker_unreadable"}
    fleet.observe.assert_not_called()
    fleet.clear.assert_not_called()


def test_an_agent_whose_comparison_failed_is_neither_stale_nor_current(fleet):
    fleet.agent("alpha", "unknown").agent("bravo", "match")
    result = fleet.run()

    assert result["status"] == "could_not_check" and result["unknown"] == ["alpha"]
    fleet.observe.assert_not_called()
    fleet.clear.assert_not_called()      # a pending alert may be about alpha


def test_one_agent_that_raises_does_not_skip_the_next(fleet, monkeypatch):
    fleet.agent("alpha", "drift").agent("bravo", "drift")

    async def image_state(container, name):
        if name == "alpha":
            raise RuntimeError("inspect failed")
        return "drift"
    monkeypatch.setattr(fis, "_image_state", image_state)
    result = fleet.run()

    assert result["stale"] == ["bravo"] and result["unknown"] == ["alpha"]


def test_no_container_is_touched(fleet):
    fleet.agent("alpha", "drift").agent("bravo", "match")
    fleet.run()

    for container in fleet.containers.values():
        assert container.method_calls == [], container.method_calls


def test_run_once_never_raises(fleet, monkeypatch):
    async def boom():
        raise RuntimeError("unexpected")
    monkeypatch.setattr(fis, "_container_states", boom)

    assert fleet.run() == {"status": "could_not_check", "reason": "error"}
    fleet.observe.assert_not_called()
    fleet.clear.assert_not_called()


# ---------------------------------------------------------------------------
# The alert kind
# ---------------------------------------------------------------------------

def test_the_kind_is_registered_under_a_reserved_prefix():
    from services.operator_queue_service import _RESERVED_ID_PREFIXES

    kind = platform_alerts.KINDS["fleet_base_image_stale"]
    assert kind.prefix.startswith(_RESERVED_ID_PREFIXES)
    assert kind.clears == platform_alerts.CLEARS_HOOK
    assert platform_alerts.subject_for("fleet_base_image_stale", "fleet") == "fleet_base_image_stale:fleet"


def test_a_row_id_reads_back_as_this_kind_not_the_system_agents():
    """The system agent's kind owns the shorter `base-image-stale-` prefix; a
    fleet row must not read back as an alert about an agent named `fleet-fleet`."""
    match = platform_alerts.derive_legacy_subject(
        "base-image-stale-fleet-fleet-2026-11-15T10:00:00Z", {})
    assert match.kind == "fleet_base_image_stale"
    assert match.subject == "fleet_base_image_stale:fleet"


def test_a_dismissed_alert_comes_back_when_a_new_agent_goes_stale():
    kind = platform_alerts.KINDS["fleet_base_image_stale"]
    assert kind.escalates({"agents": ["alpha", "bravo"]}, {"agents": ["alpha"]})
    assert not kind.escalates({"agents": ["alpha"]}, {"agents": ["alpha", "bravo"]})


# ---------------------------------------------------------------------------
# The loop: one leader, started at boot
# ---------------------------------------------------------------------------

class _Redis:
    def __init__(self, holder=None):
        self.store = {} if holder is None else {fis.LEADER_KEY: holder}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        return True

    def delete(self, key):
        self.store.pop(key, None)


def _cycle(monkeypatch, redis):
    ran = []

    async def run_once():
        ran.append(1)
        return {"status": "current"}
    monkeypatch.setattr(fis, "run_once", run_once)
    monkeypatch.setattr(fis, "_redis", lambda: redis)
    # trinity-enterprise#1176: the same cycle runs the managed-hooks check.
    from services import managed_hooks_check_service

    async def hooks_once():
        return {"status": "clear"}
    monkeypatch.setattr(managed_hooks_check_service, "run_once", hooks_once)
    service = fis.FleetImageCheckService()
    asyncio.run(service.run_cycle())
    return ran, service


def test_a_worker_that_does_not_hold_the_lease_runs_nothing(monkeypatch):
    ran, _ = _cycle(monkeypatch, _Redis(holder="another-worker"))
    assert ran == []


def test_the_lease_holder_runs_the_check(monkeypatch):
    redis = _Redis()
    ran, service = _cycle(monkeypatch, redis)
    assert ran == [1] and redis.get(fis.LEADER_KEY) == service._worker_id


def test_without_redis_the_check_still_runs(monkeypatch):
    """Fail-open: every write goes through the one-row-per-subject seam, so a
    duplicated cycle updates the same row. Failing closed would silence it."""
    ran, _ = _cycle(monkeypatch, None)
    assert ran == [1]


def test_main_starts_the_loop_with_the_watch_loops():
    """@source-text-pin: the consumer is the composition root — `main.py` cannot
    be imported in the unit island, and deleting this call leaves every other
    test here green while the check never runs."""
    tree = ast.parse((_BACKEND / "main.py").read_text())
    phase = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "_schedule_watch_loops")
    calls = [ast.unparse(n.func) for n in ast.walk(phase) if isinstance(n, ast.Call)]
    assert "_fleet_image_boot.fleet_image_check_service.start" in calls


def test_start_does_not_run_the_check_inline(monkeypatch):
    """Boot is never blocked: `start()` only schedules; the first cycle waits
    out its stagger."""
    ran = []

    async def run_once():
        ran.append(1)
        return {"status": "current"}
    monkeypatch.setattr(fis, "run_once", run_once)
    monkeypatch.setattr(fis, "_redis", lambda: None)

    async def scenario():
        service = fis.FleetImageCheckService()
        service.start()
        started_inline = list(ran)
        service.stop()
        return started_inline
    assert asyncio.run(scenario()) == []


# ---------------------------------------------------------------------------
# `409 env_unreadable` on an agent whose image predates the `missing` list
# ---------------------------------------------------------------------------

_OLD_IMAGE_BODY = {"files": {}, "files_b64": {}}                 # no `missing` key at all
_CURRENT_IMAGE_BODY = {"files": {}, "files_b64": {}, "missing": []}   # current image, read failed


def _env_refusal(read, body):
    import httpx

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        async with httpx.AsyncClient(transport=transport) as client:
            await read(client, "alpha")
    with pytest.raises(Exception) as info:
        asyncio.run(run())
    return info.value


def _sink_read():
    from services import credential_sink
    return credential_sink._read_env


def _delegation_read():
    from services import nevermined_delegation_service
    return nevermined_delegation_service._read_agent_env


@pytest.mark.parametrize("body, expected", [
    (_OLD_IMAGE_BODY, True),
    ({}, True),
    (_CURRENT_IMAGE_BODY, False),
    ({"files": {}, "missing": [".env"]}, False),
    (None, False),
])
def test_a_read_answer_without_the_missing_list_is_from_an_older_image(body, expected):
    from services import agent_env_read
    assert agent_env_read.image_predates_missing(body) is expected


@pytest.mark.parametrize("read", [_sink_read, _delegation_read])
def test_env_unreadable_on_an_old_image_names_the_image_and_the_restart(read):
    """The remedy is a restart, not the `.env` file the old message pointed at."""
    err = _env_refusal(read(), _OLD_IMAGE_BODY)

    assert (err.status_code, err.code) == (409, "env_unreadable")
    assert "older agent image" in err.message and "stop and start" in err.message
    assert "check its .env file" not in err.message


@pytest.mark.parametrize("read", [_sink_read, _delegation_read])
def test_env_unreadable_on_a_current_image_still_points_at_the_file(read):
    err = _env_refusal(read(), _CURRENT_IMAGE_BODY)

    assert (err.status_code, err.code) == (409, "env_unreadable")
    assert "check its .env file" in err.message
    assert "older agent image" not in err.message


def test_start_outside_an_event_loop_does_not_raise():
    service = fis.FleetImageCheckService()
    service.start()          # no running loop: logged, never raised into boot
    service.stop()
