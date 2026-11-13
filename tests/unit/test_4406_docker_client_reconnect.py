"""#4406: a backend that boots before Docker is reachable recovers without a restart.

`services/docker_service.py` used to call `docker.from_env()` once, at import. When
that failed, `docker_client` stayed `None` and every reader reported an empty fleet
until the backend restarted. The readers now go through `get_docker_client()`,
which retries with backoff on a background thread.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import docker
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.docker_service as ds  # noqa: E402


_REAL_START = ds._start_docker_connect


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def docker_down(monkeypatch):
    """Boot state: the import-time connect failed and Docker is still unreachable.

    The connect attempt normally runs on its own thread. Here it runs inline, in
    the call that starts it, so each test can check the state right after.
    """
    clock = _Clock()
    state = {"up": False, "calls": 0, "client": MagicMock(name="docker_client")}

    def from_env():
        state["calls"] += 1
        if not state["up"]:
            raise docker.errors.DockerException("Error while fetching server API version")
        return state["client"]

    monkeypatch.setattr(ds, "docker_client", None)
    monkeypatch.setattr(ds, "_docker_retry_delay_s", ds._DOCKER_RETRY_INITIAL_S)
    monkeypatch.setattr(ds, "_next_docker_attempt_monotonic", None)
    monkeypatch.setattr(ds, "_last_warn_monotonic", {})
    monkeypatch.setattr(ds.docker, "from_env", from_env)
    monkeypatch.setattr(ds.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ds, "_start_docker_connect", ds._attempt_docker_connect)
    return clock, state


def test_roster_recovers_once_docker_is_reachable(docker_down):
    clock, state = docker_down
    container = MagicMock()
    container.name = "agent-alpha"
    state["client"].containers.list.return_value = [container]

    assert ds.list_all_agents_fast() == []

    state["up"] = True
    clock.now += ds._DOCKER_RETRY_MAX_S
    ds.list_all_agents_fast()  # starts the attempt, which connects
    assert ds.docker_client is state["client"]

    ds.list_all_agents_fast()
    state["client"].containers.list.assert_called()


def test_retries_back_off_while_docker_stays_down(docker_down):
    clock, state = docker_down

    assert ds.get_docker_client() is None
    assert state["calls"] == 1

    # Inside the first delay: no new attempt.
    clock.now += ds._DOCKER_RETRY_INITIAL_S / 2
    assert ds.get_docker_client() is None
    assert state["calls"] == 1

    # After it: one attempt, and the next delay is longer.
    clock.now += ds._DOCKER_RETRY_INITIAL_S
    assert ds.get_docker_client() is None
    assert state["calls"] == 2
    assert ds._docker_retry_delay_s == ds._DOCKER_RETRY_INITIAL_S * 4

    for _ in range(20):
        clock.now += ds._DOCKER_RETRY_MAX_S
        ds.get_docker_client()
    assert ds._docker_retry_delay_s == ds._DOCKER_RETRY_MAX_S


def test_connected_client_is_reused_without_reconnecting(docker_down):
    clock, state = docker_down
    state["up"] = True

    ds.get_docker_client()  # connects
    first = ds.get_docker_client()
    clock.now += ds._DOCKER_RETRY_MAX_S
    assert first is state["client"]
    assert ds.get_docker_client() is first
    assert state["calls"] == 1


def test_a_slow_failure_still_waits_out_the_delay(docker_down, monkeypatch):
    clock, state = docker_down

    def slow_from_env():
        state["calls"] += 1
        clock.now += 5 * ds._DOCKER_RETRY_INITIAL_S  # e.g. a TCP DOCKER_HOST timing out
        raise docker.errors.DockerException("timed out")

    monkeypatch.setattr(ds.docker, "from_env", slow_from_env)

    assert ds.get_docker_client() is None
    assert ds.get_docker_client() is None
    assert state["calls"] == 1


def test_a_second_caller_does_not_wait_on_a_connect_in_progress(docker_down):
    clock, state = docker_down
    state["up"] = True

    with ds._docker_connect_lock:
        assert ds.get_docker_client() is None
    assert state["calls"] == 0

    ds.get_docker_client()  # connects
    assert ds.get_docker_client() is state["client"]


def test_an_importer_reaches_the_reconnected_client(docker_down):
    """A module outside docker_service gets the new client too. docker_utils used
    to bind `docker_client` at import, so it kept the boot-time None after Docker
    came up even though docker_service itself had reconnected."""
    import services.docker_utils as du

    clock, state = docker_down
    container = MagicMock(name="agent-alpha")
    state["client"].containers.get.return_value = container

    assert ds.get_docker_client() is None  # booted with Docker down
    state["up"] = True
    clock.now += ds._DOCKER_RETRY_MAX_S
    ds.get_docker_client()  # docker_service reconnects

    try:
        got = asyncio.run(du.container_get("agent-alpha"))
    except Exception as e:  # noqa: BLE001 - a stale None client raises; report it as a mismatch
        got = e
    assert got is container
    state["client"].containers.get.assert_called_once_with("agent-alpha")


def test_a_reconnect_attempt_never_blocks_the_caller(docker_down, monkeypatch):
    """`docker.from_env` can block for a TCP timeout. Called on the event loop,
    get_docker_client() must return at once and leave the attempt to its own
    thread. `from_env` here waits for a coroutine on the loop to run: if the
    attempt ran in the caller, the coroutine could not run and the wait would time
    out."""
    _clock, state = docker_down
    monkeypatch.setattr(ds, "_start_docker_connect", _REAL_START)
    ticked = threading.Event()
    seen = {}

    def blocking_from_env():
        state["calls"] += 1
        seen["thread"] = threading.current_thread()
        seen["loop_ran_meanwhile"] = ticked.wait(timeout=5)
        return state["client"]

    monkeypatch.setattr(ds.docker, "from_env", blocking_from_env)

    async def caller():
        seen["caller"] = threading.current_thread()
        return ds.get_docker_client()

    async def tick():
        ticked.set()

    async def main():
        return await asyncio.gather(caller(), tick())

    try:
        got, _ = asyncio.run(main())
        assert got is None

        with ds._docker_connect_lock:  # the attempt releases it when done
            pass
        assert state["calls"] == 1, "no connect attempt was made"
        assert seen["loop_ran_meanwhile"] is True
        assert seen["thread"] is not seen["caller"]
        assert ds.get_docker_client() is state["client"]
    finally:
        # Never leave a worker behind to write the module state after teardown.
        ticked.set()
        for t in threading.enumerate():
            if t.name == "docker-reconnect":
                t.join(timeout=10)
