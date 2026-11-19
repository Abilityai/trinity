"""#4406 review: agent create reads the Docker client once.

With the reconnect accessor, `docker_service.get_docker_client()` can return
None on one call and the client on the next, while Docker comes back. Create
used to read it twice: once in `_guard_leftover_workspace_volume` (the #1667
leftover-volume 409) and once to decide whether to build the container. A None
on the first read skipped the guard; the client on the second built the agent
and silently adopted the leftover workspace volume instead of returning 409.

Create now reads the client once and passes the answer to the guard, so the
guard and the build always agree.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

import services.agent_service.crud as crud
from models import AgentConfig

pytestmark = pytest.mark.unit


def _create_tree():
    return ast.parse(textwrap.dedent(inspect.getsource(crud.create_agent_internal)))


def test_the_guard_and_the_build_gate_use_the_same_single_read():
    """One `get_docker_client()` read, stored once; the #1667 guard is given
    that value, and the `if` that builds the container tests that same name.
    Before the fix the guard did its own read, so the two could disagree."""
    tree = _create_tree()
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "bool"
        and any(
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Attribute)
            and a.func.attr == "get_docker_client"
            for a in node.value.args
        )
    ]
    assert len(reads) == 1, "create must read the Docker client exactly once"
    (name,) = [t.id for t in reads[0].targets]

    guard_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_guard_leftover_workspace_volume"
    ]
    assert len(guard_calls) == 1
    passed = {kw.arg: kw.value for kw in guard_calls[0].keywords}
    assert isinstance(passed.get("docker_available"), ast.Name)
    assert passed["docker_available"].id == name

    other_reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_docker_client"
    ]
    assert len(other_reads) == 1, "no second read anywhere in create"
    assert any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == name
        for node in ast.walk(tree)
    ), "the container build must be gated on the same read"


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


def test_guard_still_refuses_a_leftover_volume_when_docker_is_up(probe):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _guard(docker_available=True)
    assert exc.value.status_code == 409
    assert probe["probed"] == ["agent-acme-a-workspace"]
