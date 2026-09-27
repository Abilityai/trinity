"""Git-write hooks that re-read `template.yaml` — ent#477 (R4).

Three git routes can bring a new `template.yaml` into a container and none of
them ever re-read it before ent#477: `pull`, `reset-to-main-preserve-state`,
and `sync` when `strategy == "pull_first"` (S1 — the plain `normal` /
`force_push` strategies push, so nothing arrives).

Two properties carry the weight:

  * the hook fires **only on the success branch**, so a conflicted pull never
    reconciles against a template that was not adopted;
  * an unreadable template leaves the registry **untouched** (#2196's tri-state
    lesson) — a failed exec is absence of evidence, not "the author removed the
    block", and retiring on it would wipe a live registry on one Docker hiccup.

The routes are invoked as plain async functions (the reports-test pattern):
`agent_name` is the already-resolved `AuthorizedAgentByName` string, so no
TestClient or auth stack is needed to exercise the hook wiring.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required")

from fastapi import HTTPException  # noqa: E402
from database import db  # noqa: E402
from routers import git as git_router  # noqa: E402
from services import metric_registry  # noqa: E402

AGENT = "hooked-agent"

_TEMPLATE = """
name: demo
description: d
metrics:
  - name: cycles
    type: counter
    label: Cycles
"""


def _user():
    return SimpleNamespace(id=1, username="owner", email="o@example.com",
                           agent_name=None, connector_agent=None,
                           mcp_scope=None, role="creator")


def _request():
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={}, url=SimpleNamespace(path="/"), state=SimpleNamespace(),
        method="POST",
    )


@pytest.fixture
def audited(monkeypatch):
    """Capture `_audit_git` calls — the hook's summary rides in `details`, and
    the git response shapes stay untouched (S10)."""
    calls = []

    async def _capture(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(git_router, "_audit_git", _capture)
    return calls


@pytest.fixture
def readable_template(monkeypatch):
    """A running agent whose `template.yaml` reads cleanly."""
    monkeypatch.setattr(
        metric_registry, "_read_template_from_container",
        _async_return(_TEMPLATE))
    return _TEMPLATE


def _async_return(value):
    async def _f(*args, **kwargs):
        return value
    return _f


def _async_raise(exc):
    async def _f(*args, **kwargs):
        raise exc
    return _f


def _rows(agent=AGENT):
    return {r["name"]: r for r in db.list_metric_definitions(
        agent, include_retired=True)}


def _seed_registry():
    """A registry with one active definition, so a wrongful retire is visible."""
    metric_registry.reconcile_metric_definitions(
        AGENT, {"metrics": [{"name": "cycles", "type": "counter"}]},
        source="create")
    assert _rows()["cycles"]["status"] == "active"


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _call_pull(monkeypatch, *, success=True, strategy="clean"):
    monkeypatch.setattr(git_router, "get_agent_container",
                        lambda name: SimpleNamespace(status="running"),
                        raising=False)
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="running"))
    result = {"success": success, "message": "m"}
    if not success:
        result["conflict_type"] = "dirty_tree"
    monkeypatch.setattr(git_router.git_service, "pull_from_github",
                        _async_return(result))
    return asyncio.run(git_router.pull_from_github(
        AGENT, _request(), SimpleNamespace(strategy=strategy), _user()))


def test_a_successful_pull_reconciles_the_registry(
        db_backend, monkeypatch, audited, readable_template):
    _call_pull(monkeypatch)

    assert _rows()["cycles"]["source"] == "pull"
    assert _rows()["cycles"]["label"] == "Cycles"


def test_the_pull_summary_rides_in_the_audit_details_not_the_response(
        db_backend, monkeypatch, audited, readable_template):
    """S10 — the git panel and the MCP `git_pull` tool both consume this
    response; an opaque extra key there is worse than a log line and an audit
    row for the one operator who needs it."""
    response = _call_pull(monkeypatch)

    assert "metric_registry" not in response
    (audit,) = [c for c in audited if c["action"] == "pull" and c["success"]]
    assert audit["details"]["metric_registry"]["status"] == "ok"
    assert audit["details"]["metric_registry"]["created"] == ["cycles"]


def test_a_failed_pull_never_reconciles(
        db_backend, monkeypatch, audited, readable_template):
    """A conflicted pull did not adopt the remote template, so reconciling
    against it would register metrics the working tree does not have."""
    _seed_registry()
    before = _rows()["cycles"]["updated_at"]

    with pytest.raises(HTTPException):
        _call_pull(monkeypatch, success=False)

    assert _rows()["cycles"]["updated_at"] == before
    failure = [c for c in audited if not c["success"]][0]
    assert "metric_registry" not in failure["details"]


def test_an_unreadable_template_leaves_the_registry_untouched(
        db_backend, monkeypatch, audited):
    """#2196 — a failed exec is absence of evidence. Retiring on it would wipe
    a live registry on one Docker hiccup and ent#478 would then reject every
    truthfully-declared point."""
    _seed_registry()
    monkeypatch.setattr(
        metric_registry, "_read_template_from_container",
        _async_raise(metric_registry.RefreshUnavailable(
            "template_unreadable", "exec failed")))

    _call_pull(monkeypatch)

    assert _rows()["cycles"]["status"] == "active", \
        "an unreadable read must never look like a removed block"
    (audit,) = [c for c in audited if c["action"] == "pull" and c["success"]]
    assert audit["details"]["metric_registry"] == {
        "status": "unavailable", "reason": "template_unreadable"}


def test_a_stopped_agent_is_reported_not_raised(
        db_backend, monkeypatch, audited):
    """The hook is non-fatal in every direction: a pull must not fail because
    the registry could not be refreshed."""
    _seed_registry()
    monkeypatch.setattr(
        metric_registry, "_read_template_from_container",
        _async_raise(metric_registry.RefreshUnavailable(
            "agent_not_running", "stopped")))

    response = _call_pull(monkeypatch)
    assert response["success"] is True


def test_an_unexpected_raise_never_fails_the_pull(
        db_backend, monkeypatch, audited, caplog):
    _seed_registry()
    monkeypatch.setattr(metric_registry, "refresh_from_running_agent",
                        _async_raise(RuntimeError("boom")))

    with caplog.at_level(logging.WARNING):
        response = _call_pull(monkeypatch)

    assert response["success"] is True
    assert _rows()["cycles"]["status"] == "active"
    (audit,) = [c for c in audited if c["action"] == "pull" and c["success"]]
    assert audit["details"]["metric_registry"] == {"status": "error"}
    assert any("metric registry refresh failed" in r.message
               for r in caplog.records)


def test_a_template_that_dropped_its_block_retires_on_pull(
        db_backend, monkeypatch, audited):
    """The other side of the tri-state: a template that PARSED and genuinely
    carries no `metrics:` is the author's decision, and it retires."""
    _seed_registry()
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return("name: demo\ndescription: d\n"))

    _call_pull(monkeypatch)
    assert _rows()["cycles"]["status"] == "retired"


# ---------------------------------------------------------------------------
# sync — pull_first only
# ---------------------------------------------------------------------------

def _call_sync(monkeypatch, strategy):
    monkeypatch.setattr(
        "services.docker_service.get_agent_container",
        lambda name: SimpleNamespace(status="running"))
    monkeypatch.setattr(git_router.git_service, "sync_to_github", _async_return(
        SimpleNamespace(
            success=True, commit_sha="abc", files_changed=1, branch="main",
            message="ok", sync_time=None, conflict_type=None,
            conflict_class=None, removed_paths=[], unignored_paths=[],
            shadowed_negations=[],
        )))
    return asyncio.run(git_router.sync_to_github(
        AGENT, _request(),
        SimpleNamespace(strategy=strategy, message=None, paths=None),
        _user()))


def test_sync_with_pull_first_reconciles(
        db_backend, monkeypatch, audited, readable_template):
    _call_sync(monkeypatch, "pull_first")
    assert _rows()["cycles"]["source"] == "sync"
    (audit,) = [c for c in audited if c["action"] == "sync"]
    assert audit["details"]["metric_registry"]["status"] == "ok"


@pytest.mark.parametrize("strategy", ["normal", "force_push"])
def test_a_push_only_sync_does_not_reconcile(
        db_backend, monkeypatch, audited, readable_template, strategy):
    """Nothing arrived, so there is nothing to re-read — and a docker exec on
    every sync would be a real cost for no signal."""
    _call_sync(monkeypatch, strategy)
    assert _rows() == {}
    (audit,) = [c for c in audited if c["action"] == "sync"]
    assert "metric_registry" not in audit["details"]


# ---------------------------------------------------------------------------
# reset-to-main-preserve-state
# ---------------------------------------------------------------------------

def _call_reset(monkeypatch, *, error=None):
    result = {"error": error} if error else {
        "snapshot_dir": "/tmp/s", "commit_sha": "abc",
        "files_preserved": 1, "working_branch": "main"}
    monkeypatch.setattr(git_router.git_service, "reset_to_main_preserve_state",
                        _async_return(result))
    return asyncio.run(git_router.reset_to_main_preserve_state(
        AGENT, _request(), _user()))


def test_reset_reconciles_the_adopted_baseline(
        db_backend, monkeypatch, audited, readable_template):
    """Adopting `origin/main` replaces `template.yaml` wholesale — the single
    largest way a registry goes stale without anyone touching a metric."""
    _call_reset(monkeypatch)
    assert _rows()["cycles"]["source"] == "reset"
    (audit,) = [c for c in audited if c["action"] == "reset_to_main_preserve_state"]
    assert audit["details"]["metric_registry"]["status"] == "ok"


def test_a_guarded_reset_never_reconciles(
        db_backend, monkeypatch, audited, readable_template):
    _seed_registry()
    before = _rows()["cycles"]["updated_at"]

    with pytest.raises(HTTPException):
        _call_reset(monkeypatch, error="agent_busy")

    assert _rows()["cycles"]["updated_at"] == before


# ---------------------------------------------------------------------------
# The start hook (T1) — fire-and-forget
# ---------------------------------------------------------------------------

def test_the_start_hook_spawns_and_never_raises(db_backend, monkeypatch):
    """The ONE hook that covers the dominant staleness path: an agent edits its
    own `metrics:` in-container and the auto-sync heartbeat PUSHES it, so no
    backend pull ever fires."""
    # A fresh set: the module-level one can hold a task another test spawned
    # on a loop that has since closed (its done-callback never fired), and
    # `gather` refuses a future from a different loop.
    monkeypatch.setattr(metric_registry, "_inflight_refresh_tasks", set())
    monkeypatch.setattr(metric_registry, "_read_template_from_container",
                        _async_return(_TEMPLATE))

    async def _drive():
        metric_registry.spawn_refresh_from_running_agent(AGENT, source="start")
        # Let the spawned task run to completion before asserting.
        await asyncio.gather(*list(metric_registry._inflight_refresh_tasks))

    asyncio.run(_drive())
    assert _rows()["cycles"]["source"] == "start"


def test_the_start_hook_is_a_no_op_without_a_running_loop(db_backend):
    """`asyncio.create_task` with no loop raises RuntimeError; the spawn must
    close the coro and skip, never propagate into `start_agent_internal`."""
    metric_registry.spawn_refresh_from_running_agent(AGENT, source="start")
    assert _rows() == {}


def test_the_start_hook_swallows_every_failure(db_backend, monkeypatch, caplog):
    monkeypatch.setattr(metric_registry, "_inflight_refresh_tasks", set())
    monkeypatch.setattr(metric_registry, "refresh_from_running_agent",
                        _async_raise(RuntimeError("boom")))

    async def _drive():
        metric_registry.spawn_refresh_from_running_agent(AGENT, source="start")
        await asyncio.gather(*list(metric_registry._inflight_refresh_tasks))

    with caplog.at_level(logging.WARNING):
        asyncio.run(_drive())       # must not raise

    assert any("metric registry refresh failed" in r.message
               for r in caplog.records)


def test_lifecycle_start_calls_the_spawn_helper():
    """Mutation evidence for T1: deleting the hook line makes this red. A
    behavioural start test would need a real container, which this agent
    has not got (declared gap — `/verify-local` is the consumer)."""
    import inspect

    from services.agent_service import lifecycle

    source = inspect.getsource(lifecycle.start_agent_internal)
    assert "spawn_refresh_from_running_agent" in source
    assert 'source="start"' in source
