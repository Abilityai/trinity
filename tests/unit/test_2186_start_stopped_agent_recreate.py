"""Starting a STOPPED agent survives a recreate (#2186).

#2092 gave `recreate_container_with_updated_config` a `require_running=True`
default so it can no longer silently start an agent the caller believed was
stopped. Its note — every caller already enforces this — held for the two
running-only callers and NOT for `start_agent_internal`, which recreates stopped
containers by design and calls `container_start` on the very next line. So a
stopped agent with any drift returned HTTP 500 from `POST /agents/{name}/start`.

The #1809 base-image branch made it worse than intermittent: that predicate is
gated on `not was_already_running`, so it fires ONLY for a stopped container —
cold-start image adoption was 100% unreachable after any `build-base-image.sh`.

Why both existing suites stayed green while the composition was broken:
`test_2092_recreate_run_state.py` drives the guard with synthetic containers and
never calls `start_agent_internal`; `test_1809_image_drift_recreate.py` is an AST
test asserting the gate exists, not that a start succeeds. So these tests drive
`start_agent_internal` end to end with Docker mocked, and add a static guard that
every call site states its run-state intent — the class fix for a precondition
three callers each have to remember.
"""
from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2186.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2186-logs"))

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_LIFECYCLE = _BACKEND / "services" / "agent_service" / "lifecycle.py"


class _Container:
    """A stopped container that reports 'running' once started, like Docker's."""

    def __init__(self, status="exited"):
        self.status = status
        self.attrs = {"Config": {}, "HostConfig": {}}
        self.id = "c0ffee"


def _run_start(monkeypatch, *, predicate_that_drifts, container=None):
    """Drive `start_agent_internal` with Docker and the DB mocked out.

    Every drift predicate is satisfied EXCEPT the named one, so exactly one
    reason forces the recreate — which is how each acceptance path (config drift
    vs base-image drift) is exercised separately rather than in aggregate.
    """
    import asyncio

    from services.agent_service import lifecycle as lc

    container = container or _Container()
    recreate_calls = []
    started = []

    async def _fake_recreate(agent_name, old_container, owner, **kwargs):
        recreate_calls.append(kwargs)
        # The real helper raises exactly here when the precondition is unmet.
        was_running = getattr(old_container, "status", None) == "running"
        if kwargs.get("require_running", True) and not was_running:
            raise ValueError(
                "recreate_container_with_updated_config would START agent "
                f"{agent_name!r}, whose container is "
                f"{getattr(old_container, 'status', 'unknown')!r}."
            )
        return None

    async def _fake_start(c):
        started.append(c)
        c.status = "running"

    truthy = {
        "check_shared_folder_mounts_match": True,
        "check_public_folder_mount_matches": True,
        "check_api_key_env_matches": True,
        "check_github_pat_env_matches": True,
        "check_resource_limits_match": True,
        "check_full_capabilities_match": True,
        "check_guardrails_env_matches": True,
        "check_agent_auth_token_env_matches": True,
        "check_agent_mcp_key_matches": True,
        "check_base_image_matches": True,
    }
    truthy[predicate_that_drifts] = False

    for name, value in truthy.items():
        target = getattr(lc, name)
        if asyncio.iscoroutinefunction(target):
            monkeypatch.setattr(lc, name, AsyncMock(return_value=value))
        else:
            monkeypatch.setattr(lc, name, MagicMock(return_value=value))

    monkeypatch.setattr(lc, "get_agent_container", MagicMock(return_value=container))
    monkeypatch.setattr(lc, "container_reload", AsyncMock())
    monkeypatch.setattr(lc, "container_start", _fake_start)
    monkeypatch.setattr(lc, "recreate_container_with_updated_config", _fake_recreate)
    monkeypatch.setattr(lc, "clear_agent_runtime_state", MagicMock(), raising=False)
    monkeypatch.setattr(lc, "clear_agent_breakers", MagicMock(), raising=False)

    db_mock = MagicMock()
    db_mock.get_agent_owner.return_value = "owner"
    db_mock.get_agent_ephemeral_info.return_value = None
    monkeypatch.setattr(lc, "db", db_mock)

    # Everything after the start is post-start bookkeeping. `wait_for_agent_ready`
    # in particular POLLS the agent's HTTP port with a retry budget, so leaving it
    # real makes this test hang rather than fail — stub the whole tail so the test
    # is about the start decision, which is what #2186 broke.
    for name in ("wait_for_agent_ready", "inject_assigned_credentials",
                 "inject_assigned_skills", "inject_read_only_hooks",
                 "remove_read_only_hooks"):
        if hasattr(lc, name):
            target = getattr(lc, name)
            monkeypatch.setattr(
                lc, name,
                AsyncMock(return_value={"status": "stubbed"})
                if asyncio.iscoroutinefunction(target)
                else MagicMock(return_value={"status": "stubbed"}),
            )

    result = asyncio.run(lc.start_agent_internal("ws-scout"))
    return {"result": result, "recreate_calls": recreate_calls,
            "started": started, "container": container}


# ---------------------------------------------------------------------------
# The regression: a stopped agent starts
# ---------------------------------------------------------------------------

def test_a_stopped_agent_with_config_drift_starts(monkeypatch):
    """The reported P0: any drift predicate + a stopped container = HTTP 500."""
    out = _run_start(monkeypatch, predicate_that_drifts="check_resource_limits_match")
    assert out["recreate_calls"], "the drift predicate did not force a recreate"
    assert out["started"], "the agent never started"
    assert out["container"].status == "running"


def test_a_stopped_agent_on_a_stale_base_image_starts_and_adopts_it(monkeypatch):
    """#1809 cold-start adoption — the path that was 100% unreachable, since its
    own gate (`not was_already_running`) fires only for a stopped container."""
    out = _run_start(monkeypatch, predicate_that_drifts="check_base_image_matches")
    assert out["recreate_calls"], "image drift did not force a recreate"
    assert out["started"]
    assert out["container"].status == "running"


def test_the_start_path_asks_for_a_stopped_container_explicitly(monkeypatch):
    """The intent is passed, not inherited from a default that means the opposite."""
    out = _run_start(monkeypatch, predicate_that_drifts="check_resource_limits_match")
    assert out["recreate_calls"][0].get("require_running") is False


def test_the_replacement_is_not_left_stopped(monkeypatch):
    """`preserve_run_state` must stay False here: this caller starts the
    container on the next line, so restoring 'stopped' would undo the start."""
    out = _run_start(monkeypatch, predicate_that_drifts="check_resource_limits_match")
    assert out["recreate_calls"][0].get("preserve_run_state") in (None, False)


def test_an_already_running_agent_is_unaffected(monkeypatch):
    """The #2092 property that must survive: start-on-running stays an
    idempotent no-op, and image drift must NOT kill a running container."""
    out = _run_start(
        monkeypatch,
        predicate_that_drifts="check_base_image_matches",
        container=_Container(status="running"),
    )
    assert out["recreate_calls"] == [], "image drift recreated a RUNNING agent"


def test_a_running_agent_with_config_drift_still_recreates(monkeypatch):
    """Config drift is owner-intentional and applies in both run states — the
    fix must not turn the running case into a no-op."""
    out = _run_start(
        monkeypatch,
        predicate_that_drifts="check_api_key_env_matches",
        container=_Container(status="running"),
    )
    assert out["recreate_calls"], "config drift no longer recreates a running agent"
    assert out["recreate_calls"][0].get("require_running") is False


# ---------------------------------------------------------------------------
# The class, not just the instance
# ---------------------------------------------------------------------------

def _call_sites():
    """Every in-tree call to the helper, as (file, lineno, kwargs)."""
    sites = []
    for path in _BACKEND.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - not our files
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name != "recreate_container_with_updated_config":
                continue
            sites.append((path, node.lineno, {k.arg for k in node.keywords if k.arg}))
    return sites


def test_every_call_site_states_its_run_state_intent():
    """The guard's own failure mode was a precondition three callers each had to
    remember; #2186 is what happens when one does not. A call site that omits
    `require_running` is taking a default that means "refuse a stopped
    container" — which is right for two of the three and was a P0 for the third,
    so make the choice visible rather than inherited.
    """
    missing = [
        f"{p.relative_to(_BACKEND)}:{line}"
        for p, line, kwargs in _call_sites()
        if "require_running" not in kwargs
    ]
    assert missing == [], (
        "these call sites inherit `require_running`'s default instead of stating "
        f"their intent (#2186): {missing}"
    )


def test_the_start_path_passes_require_running_false_in_source():
    """A behavioural test can be satisfied by a mock; this pins the real file, so
    the fix cannot regress while the harness keeps passing."""
    tree = ast.parse(_LIFECYCLE.read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "start_agent_internal")
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", None))
        == "recreate_container_with_updated_config"
    ]
    assert calls, "start_agent_internal no longer recreates — re-check this test"
    for call in calls:
        kwargs = {k.arg: k.value for k in call.keywords if k.arg}
        assert "require_running" in kwargs, "the start path must state its intent"
        assert kwargs["require_running"].value is False
