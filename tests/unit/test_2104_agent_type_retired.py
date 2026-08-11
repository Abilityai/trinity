"""#2104 — the free-text agent `type` taxonomy is retired.

Four guards:

1. ``AgentConfig`` silently IGNORES a ``type=`` kwarg (Pydantic's default
   ``extra="ignore"``). The enterprise skill-runner provisioner still passes
   one until its private-repo follow-up lands — it must stay inert, never a
   validation error.
2. ``AgentStatus`` carries no ``type`` field, so the field is gone from
   ``/api/agents`` and — via JSON passthrough — MCP ``list_agents``/``get_agent``.
3. A STALE ``trinity.agent-type`` label on an existing container is tolerated
   and never surfaced: the fleet keeps baked labels until natural recreate
   (no forced recreates), and zero readers must remain.
4. Residue guard: the retired tokens cannot quietly re-enter the source tree.
   The #2104 sweep found readers in five files the issue never mapped (the
   create-path WS broadcast, the permissions payload, both ops.py fleet dicts,
   the container-less agents.py fallback, network.js) — absence is enforced,
   not assumed.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from models import AgentConfig, AgentStatus
from services import docker_service

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 1. Older callers passing `type=` stay inert (the enterprise-kwarg contract)
# ---------------------------------------------------------------------------

def test_agent_config_ignores_type_kwarg():
    cfg = AgentConfig(name="demo", type="business-assistant")
    assert "type" not in AgentConfig.model_fields
    assert not hasattr(cfg, "type"), (
        "AgentConfig must IGNORE a `type=` kwarg, not store it — the enterprise "
        "skill-runner provisioner still passes one (inert until its follow-up)."
    )


# ---------------------------------------------------------------------------
# 2. The field is gone from the API surface
# ---------------------------------------------------------------------------

def test_agent_status_has_no_type_field():
    assert "type" not in AgentStatus.model_fields, (
        "AgentStatus.type is retired (#2104) — /api/agents and the MCP "
        "list_agents/get_agent passthrough must not carry it."
    )


# ---------------------------------------------------------------------------
# 3. Stale labels on the existing fleet are tolerated, never surfaced
# ---------------------------------------------------------------------------

class _FakeContainers:
    def __init__(self, containers):
        self._containers = containers

    def list(self, **_kwargs):
        return self._containers


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)


def test_stale_agent_type_label_is_ignored(monkeypatch):
    """Existing containers keep their baked trinity.agent-type label until a
    natural recreate — building AgentStatus from them must neither raise nor
    surface the value anywhere."""
    labels = {
        "trinity.platform": "agent",
        "trinity.agent-type": "business-assistant",  # stale, pre-#2104 bake
        "trinity.agent-runtime": "codex",
    }
    container = SimpleNamespace(
        labels=labels, name="agent-demo", status="running", id="container123"
    )
    monkeypatch.setattr(docker_service, "docker_client", _FakeClient([container]))

    agents = docker_service.list_all_agents_fast()

    assert len(agents) == 1
    assert not hasattr(agents[0], "type")
    assert "type" not in agents[0].model_dump()


# ---------------------------------------------------------------------------
# 4. Residue guard — the taxonomy cannot quietly re-enter the source tree
# ---------------------------------------------------------------------------

_SCAN_ROOTS = (
    "src/backend",
    "src/mcp-server/src",
    "src/frontend/src",
    "docker/base-image",
)
_SCAN_SUFFIXES = {".py", ".ts", ".vue", ".js", ".sh"}
# The private enterprise submodule drops its (inert) `type=` kwarg in its own
# follow-up; it is not this repo's tree to scan.
_EXCLUDED_PARTS = {"enterprise", "node_modules", "dist", "__pycache__"}

_FORBIDDEN = {
    # token -> {relative path allowlisted: reason}
    "trinity.agent-type": {},
    "AGENT_TYPE": {},
    "business-assistant": {
        # Historical #1759 comment explaining why a dead `local:business-assistant`
        # template fallback was unreachable — prose about the past, not a reader.
        "src/backend/services/system_service.py": "#1759 dead-code history comment",
    },
}


def _scan_files():
    for root in _SCAN_ROOTS:
        base = REPO_ROOT / root
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
                continue
            if _EXCLUDED_PARTS.intersection(path.parts):
                continue
            yield path


def test_no_agent_type_residue_in_source():
    hits: list[str] = []
    for path in _scan_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        for token, allowlist in _FORBIDDEN.items():
            if token in text and rel not in allowlist:
                for lineno, line in enumerate(text.splitlines(), 1):
                    if token in line:
                        hits.append(f"{rel}:{lineno}: {token!r} in: {line.strip()[:100]}")
    assert not hits, (
        "Retired agent-type taxonomy tokens re-entered the tree (#2104). "
        "Classification is tags; do not reintroduce the field, label, or env var:\n"
        + "\n".join(hits)
    )


def test_residue_guard_scans_something():
    """The guard is only a guard if the walk actually visits files — an empty
    iteration (moved roots, refactored layout) must fail loudly, not pass."""
    count = sum(1 for _ in _scan_files())
    assert count > 200, f"residue scan visited only {count} files — roots wrong?"
