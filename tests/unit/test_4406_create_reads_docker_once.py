"""#4406 review: agent create reads the Docker client once.

With the reconnect accessor, `docker_service.get_docker_client()` can return
None on one call and the client on the next, while Docker comes back. Create
used to read it twice: once in `_guard_leftover_workspace_volume` (the #1667
leftover-volume 409) and once to decide whether to build the container. A None
on the first read skipped the guard; the client on the second built the agent
and silently adopted the leftover workspace volume instead of returning 409.

Create now reads the client once and passes the answer to the guard, so the
guard and the build always agree. The end-to-end case, Docker coming back
between the two reads, is in test_1484_create_agent_characterization.py
(it needs that file's create harness).
"""

from __future__ import annotations

import asyncio

import pytest

import services.agent_service.crud as crud
from models import AgentConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def probe(monkeypatch):
    """A client is available at the guard's own read; record volume probes."""
    calls = {"get": 0, "probed": []}

    def get_docker_client():
        calls["get"] += 1
        return object()

    async def volume_get(name):
        calls["probed"].append(name)
        return object()  # the volume exists -> 409 when the guard probes

    monkeypatch.setattr(crud.docker_service, "get_docker_client", get_docker_client)
    monkeypatch.setattr(crud, "volume_get", volume_get)
    return calls


def _guard(**kw):
    return asyncio.run(
        crud._guard_leftover_workspace_volume(
            AgentConfig(name="acme-a", template="local:plain"), False, **kw
        )
    )


def test_guard_follows_the_value_create_read_not_a_fresh_read(probe):
    # Create read None: the guard must not probe on a later read that sees a
    # client, because create will refuse with 503 on the same None.
    assert _guard(docker_available=False) is None
    assert probe["probed"] == []
    assert probe["get"] == 0, "the guard must not read the client itself"


def test_guard_still_refuses_a_leftover_volume_when_docker_is_up(probe):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _guard(docker_available=True)
    assert exc.value.status_code == 409
    assert probe["probed"] == ["agent-acme-a-workspace"]
