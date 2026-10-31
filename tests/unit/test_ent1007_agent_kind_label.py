"""trinity-enterprise#1007 (item 7) — an agent's create-time `kind` is readable.

`kind` ("agent" | "deployment", trinity-enterprise#705) decides an agent's git
mode at create time and was persisted nowhere, so a reconciler comparing a
desired fleet against the observed one could not read it back. It now rides on
the Docker label `trinity.kind` (Invariant #11 — no column, no migration) and is
read back on `AgentStatus.kind` (GET /api/agents/{name}, the list) and on
GET /api/agents/{name}/info, which MCP `get_agent` / `get_agent_info` pass
through.

The label stores the EFFECTIVE kind (`config.kind or "agent"`, what the platform
applied). A missing or unrecognised label reads as None — "unknown" — never as a
default kind: agents created before this change, containers rebuilt by
`recreate_missing_container` (no source survives the container) and the system
agent all carry no label, and a reconciler must see that as unobserved.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent1007.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent1007-logs"))

import pytest

pytestmark = pytest.mark.unit

BASE_LABELS = {
    "trinity.platform": "agent",
    "trinity.ssh-port": "2230",
    "trinity.created": "2026-10-31T07:00:00Z",
    "trinity.cpu": "2",
    "trinity.memory": "4g",
    "trinity.template": "github:org/repo",
}

CASES = [
    ({"trinity.kind": "agent"}, "agent"),
    ({"trinity.kind": "deployment"}, "deployment"),
    ({}, None),                                  # pre-existing / rebuilt / system agent
    ({"trinity.kind": ""}, None),
    ({"trinity.kind": "Deployment"}, None),      # not a value the platform writes
    ({"trinity.kind": "service"}, None),
]


def _container(extra_labels, status="running"):
    return SimpleNamespace(
        name="agent-a1", id="cid", status=status,
        labels={**BASE_LABELS, **extra_labels},
        attrs={"Config": {"Env": []}}, image=SimpleNamespace(labels={}),
    )


# ---------------------------------------------------------------------------
# Read: label -> AgentStatus.kind, on both the full and the fast path
# ---------------------------------------------------------------------------

# An unrecognised value passed through the read would not reach these two
# assertions on its own: `AgentStatus.kind` is a Literal, so the full path
# raises a ValidationError and the fast path swallows it and lists nothing.
# Both tests turn that into a failure that names the value or the lost agent,
# never an incidental error.

@pytest.mark.parametrize("extra,expected", CASES)
def test_full_status_reads_kind_from_the_label(extra, expected):
    from pydantic import ValidationError
    from services import docker_service
    try:
        status = docker_service.get_agent_status_from_container(_container(extra))
    except ValidationError as e:
        pytest.fail(f"the label value {e.errors()[0].get('input')!r} reached "
                    f"AgentStatus.kind; expected {expected!r}")
    assert status.kind == expected
    assert status.model_dump()["kind"] == expected


@pytest.mark.parametrize("extra,expected", CASES)
def test_fast_list_reads_kind_from_the_label(monkeypatch, extra, expected):
    from services import docker_service
    client = MagicMock()
    client.containers.list.return_value = [_container(extra)]
    monkeypatch.setattr(docker_service, "docker_client", client)
    statuses = docker_service.list_all_agents_fast()
    assert len(statuses) == 1, (
        f"label {extra!r} dropped the agent from the fast list (a value the "
        f"read passed through failed AgentStatus and the list swallowed it)")
    assert statuses[0].kind == expected


def test_kind_is_a_declared_nullable_field_defaulting_to_none():
    from models import AgentStatus
    s = AgentStatus(name="a", status="running", port=0, created="2026-10-31T00:00:00Z", resources={})
    assert s.kind is None


# ---------------------------------------------------------------------------
# Write: create stamps the effective kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind,label", [(None, "agent"), ("agent", "agent"), ("deployment", "deployment")])
def test_create_stamps_the_effective_kind(monkeypatch, kind, label):
    from models import AgentConfig
    from services.agent_service import crud
    captured = {}

    async def fake_run(image, **kw):
        captured.update(kw)
        return SimpleNamespace(labels=kw["labels"])
    monkeypatch.setattr(crud, "containers_run", fake_run)
    monkeypatch.setattr(crud, "get_agent_full_capabilities", lambda: False)
    config = AgentConfig(name="a1", template="github:org/repo", kind=kind, port=2230,
                         resources={"cpu": "2", "memory": "4g"})
    user = SimpleNamespace(agent_name=None, username="owner")
    asyncio.run(crud._create_agent_container(config, {}, {}, user, None))
    assert captured["labels"]["trinity.kind"] == label


# ---------------------------------------------------------------------------
# Recreate: an ordinary recreate keeps it; a rebuild from nothing cannot
# ---------------------------------------------------------------------------

def test_rebuild_from_nothing_carries_no_kind_label(monkeypatch):
    """No source for `kind` survives the container: the rebuilt agent reads
    as unknown, never a fabricated "agent"."""
    import services.agent_service.lifecycle as mod
    captured: dict = {}

    async def _provision(name, **kw):
        captured.update(kw)
        return SimpleNamespace(name=f"agent-{name}", status="created")
    monkeypatch.setattr(mod, "_provision_folders_and_run_agent_container", _provision)
    monkeypatch.setattr(mod, "_read_template_yaml_from_volume", AsyncMock(return_value={}))
    monkeypatch.setattr(mod, "_apply_persisted_auth_env", Mock())
    monkeypatch.setattr(mod, "validate_base_image", Mock())
    monkeypatch.setattr(mod, "get_next_available_port", Mock(return_value=2245))
    monkeypatch.setattr(mod, "get_agent_full_capabilities", Mock(return_value=False))
    monkeypatch.setattr(mod, "get_agent_default_resources", Mock(return_value={"cpu": "2", "memory": "4g"}))
    fake_db = MagicMock()
    fake_db.get_agent_owner.return_value = {"owner_username": "bob"}
    fake_db.get_resource_limits.return_value = {}
    fake_db.get_git_config.return_value = None
    fake_db.deactivate_agent_mcp_keys.return_value = 0
    fake_db.create_agent_mcp_api_key.return_value = Mock(api_key="trinity_mcp_x")
    fake_db.get_volume_base_name.side_effect = lambda n: n
    monkeypatch.setattr(mod, "db", fake_db)

    asyncio.run(mod.recreate_missing_container("a1"))
    labels = captured["labels"]
    assert "trinity.kind" not in labels
    from services import docker_service
    rebuilt = SimpleNamespace(name="agent-a1", id="c", status="created", labels=labels,
                              attrs={"Config": {"Env": []}}, image=SimpleNamespace(labels={}))
    assert docker_service.get_agent_status_from_container(rebuilt).kind is None


def test_ordinary_recreate_copies_the_old_labels_wholesale():
    """The ordinary recreate starts from the old container's label dict and
    only overwrites named keys — `trinity.kind` is not one of them."""
    import ast
    import inspect
    import services.agent_service.lifecycle as mod
    src = inspect.getsource(mod.recreate_container_with_updated_config)
    tree = ast.parse(src)
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    copied = [a for a in assigns
              if any(isinstance(t, ast.Name) and t.id == "labels" for t in a.targets)
              and "Labels" in ast.unparse(a.value)]
    assert copied, "recreate must seed `labels` from the old container's Labels"
    overwritten = {ast.unparse(t.slice) for a in assigns for t in a.targets
                   if isinstance(t, ast.Subscript) and ast.unparse(t.value) == "labels"}
    assert "'trinity.kind'" not in overwritten


# ---------------------------------------------------------------------------
# /info: every return shape carries kind
# ---------------------------------------------------------------------------

def _drive_info(monkeypatch, container, agent_reply=None, raise_exc=None):
    from routers import agent_files
    monkeypatch.setattr(agent_files, "get_agent_container", lambda name: container)
    monkeypatch.setattr(agent_files, "container_reload", AsyncMock())

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if raise_exc:
                raise raise_exc
            return agent_reply

    monkeypatch.setattr(agent_files, "agent_httpx_client", lambda name, timeout=None: _Client())
    return asyncio.run(agent_files.get_agent_info_endpoint("a1", None))


@pytest.mark.parametrize("extra,expected", CASES)
def test_info_stopped_shape_carries_kind(monkeypatch, extra, expected):
    out = _drive_info(monkeypatch, _container(extra, status="exited"))
    assert out["status"] == "stopped"
    assert "kind" in out and out["kind"] == expected


@pytest.mark.parametrize("extra,expected", CASES)
def test_info_agent_answer_shape_carries_the_label_kind(monkeypatch, extra, expected):
    reply = SimpleNamespace(status_code=200, json=lambda: {"has_template": True, "kind": "bogus"})
    out = _drive_info(monkeypatch, _container(extra), agent_reply=reply)
    assert out["status"] == "running"
    assert "kind" in out and out["kind"] == expected


@pytest.mark.parametrize("extra,expected", CASES)
def test_info_old_agent_server_shape_carries_kind(monkeypatch, extra, expected):
    reply = SimpleNamespace(status_code=404, json=lambda: {})
    out = _drive_info(monkeypatch, _container(extra), agent_reply=reply)
    assert "kind" in out and out["kind"] == expected


@pytest.mark.parametrize("extra,expected", CASES)
def test_info_fetch_error_shape_carries_kind(monkeypatch, extra, expected):
    out = _drive_info(monkeypatch, _container(extra), raise_exc=RuntimeError("boom"))
    assert "kind" in out and out["kind"] == expected
