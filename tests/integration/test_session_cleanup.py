"""
Phase 4.2 — JSONL cleanup service integration tests.

Validates the SessionCleanupService end-to-end against the live
``agent-testfix`` container:

  - Immediate reap path: POST /sessions/{id}/reset and DELETE
    /sessions/{id} both call ``reap_jsonl()`` synchronously.
  - Periodic sweep keeps active JSONLs and reaps orphans (we plant a
    synthetic orphan inside the agent and assert it disappears).
  - Age-guard path: a brand-new orphan within the race window is NOT
    reaped (prevents the cold-turn-vs-cleanup race).

Tests construct their own SessionCleanupService instance with tightened
intervals so we don't wait on the production 6h cadence. The production
service in the running backend stays untouched.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
import uuid

import pytest

from utils.api_client import TrinityApiClient


TESTFIX_AGENT = os.getenv("SESSION_TEST_AGENT", "testfix")
PROJECTS_DIR = "/home/developer/.claude/projects/-home-developer"


# ---------------------------------------------------------------------------
# Module fixtures (shared pattern with other Phase 2/4 integration tests)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def testfix_running(api_client: TrinityApiClient):
    info = api_client.get(f"/api/agents/{TESTFIX_AGENT}")
    if info.status_code != 200:
        pytest.skip(f"Agent {TESTFIX_AGENT} not visible via API ({info.status_code})")
    if info.json().get("status") != "running":
        pytest.skip(f"Agent {TESTFIX_AGENT} not running")
    return TESTFIX_AGENT


@pytest.fixture(scope="module", autouse=True)
def session_tab_enabled(api_client: TrinityApiClient):
    key = "session_tab_enabled"
    prior = api_client.get(f"/api/settings/{key}")
    prior_value = prior.json().get("value") if prior.status_code == 200 else None
    set_resp = api_client.put(f"/api/settings/{key}", json={"value": "true"})
    if set_resp.status_code not in (200, 201):
        pytest.skip(f"Cannot enable session_tab flag: {set_resp.status_code}")
    yield
    if prior_value is None:
        api_client.delete(f"/api/settings/{key}")
    else:
        api_client.put(f"/api/settings/{key}", json={"value": prior_value})


def _docker_exec(container: str, *cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run a command inside a container via the docker SDK. Returns (rc, stdout)."""
    import docker  # type: ignore
    client = docker.from_env()
    c = client.containers.get(container)
    rc, output = c.exec_run(list(cmd), demux=True)
    stdout, _ = output if isinstance(output, tuple) else (output, b"")
    return rc or 0, (stdout or b"").decode("utf-8", "replace")


def _send_turn(api_client: TrinityApiClient, agent: str, sid: str, msg: str) -> dict:
    resp = api_client.post(
        f"/api/agents/{agent}/sessions/{sid}/message",
        json={"message": msg},
        timeout=180.0,
    )
    if resp.status_code in (429, 503):
        pytest.skip(f"Agent transient state ({resp.status_code})")
    assert resp.status_code == 200, f"turn failed: {resp.status_code} {resp.text[:300]}"
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_reset_immediately_reaps_jsonl(api_client: TrinityApiClient, testfix_running):
    """POST /sessions/{id}/reset must delete the JSONL synchronously.

    The router calls ``SessionCleanupService.reap_jsonl()`` best-effort
    after clearing the cached UUID. We send a turn (creates JSONL),
    capture the on-disk path, reset, then verify the file is gone before
    the periodic sweep would have run.
    """
    agent = testfix_running
    sid = api_client.post(f"/api/agents/{agent}/session", json={}).json()["id"]
    try:
        result = _send_turn(api_client, agent, sid, "Reply OK.")
        uuid_before = result["claude_session_id"]
        path = f"{PROJECTS_DIR}/{uuid_before}.jsonl"

        rc, out = _docker_exec(f"agent-{agent}", "test", "-f", path)
        assert rc == 0, f"JSONL not present pre-reset: {out!r}"

        reset = api_client.post(f"/api/agents/{agent}/sessions/{sid}/reset")
        assert reset.status_code == 200
        assert reset.json()["cached_claude_session_id"] is None

        # Give Docker exec a moment — the reap is best-effort and the
        # router awaits it, but file-system propagation isn't atomic.
        time.sleep(0.5)
        rc, _ = _docker_exec(f"agent-{agent}", "test", "-f", path)
        assert rc != 0, f"JSONL {path} should be gone after reset"
    finally:
        api_client.delete(f"/api/agents/{agent}/sessions/{sid}")


@pytest.mark.slow
@pytest.mark.requires_agent
def test_delete_immediately_reaps_jsonl(api_client: TrinityApiClient, testfix_running):
    """DELETE /sessions/{id} must delete the JSONL synchronously.

    Same invariant as reset, exercising the delete path. Both use the
    same ``reap_jsonl()`` helper so this is mostly a copy of the above
    against the second call site.
    """
    agent = testfix_running
    sid = api_client.post(f"/api/agents/{agent}/session", json={}).json()["id"]
    delete_self = True
    try:
        result = _send_turn(api_client, agent, sid, "Reply OK.")
        uuid_before = result["claude_session_id"]
        path = f"{PROJECTS_DIR}/{uuid_before}.jsonl"

        rc, _ = _docker_exec(f"agent-{agent}", "test", "-f", path)
        assert rc == 0

        delete = api_client.delete(f"/api/agents/{agent}/sessions/{sid}")
        assert delete.status_code == 200
        delete_self = False

        time.sleep(0.5)
        rc, _ = _docker_exec(f"agent-{agent}", "test", "-f", path)
        assert rc != 0, f"JSONL {path} should be gone after delete"
    finally:
        if delete_self:
            api_client.delete(f"/api/agents/{agent}/sessions/{sid}")


@pytest.mark.slow
@pytest.mark.requires_agent
def test_periodic_sweep_reaps_orphan_keeps_active_and_respects_age_guard(
    api_client: TrinityApiClient, testfix_running
):
    """The periodic sweep must:
      - reap an orphan JSONL (not in keep set, older than age guard)
      - keep an active JSONL (in keep set)
      - keep a fresh orphan JSONL (in race-guard window)
    """
    import sys
    sys.path.insert(0, "src/backend")
    from services.session_cleanup_service import SessionCleanupService

    agent = testfix_running
    container = f"agent-{agent}"

    # Active session (its UUID will be in the keep set).
    sid = api_client.post(f"/api/agents/{agent}/session", json={}).json()["id"]
    try:
        active_result = _send_turn(api_client, agent, sid, "Reply OK.")
        active_uuid = active_result["claude_session_id"]
        active_path = f"{PROJECTS_DIR}/{active_uuid}.jsonl"

        # Plant a synthetic orphan with an mtime well in the past (3h ago)
        # so the 1h age guard reaps it. Its UUID is random — not in keep set.
        old_orphan_uuid = str(uuid.uuid4())
        old_orphan_path = f"{PROJECTS_DIR}/{old_orphan_uuid}.jsonl"
        rc, out = _docker_exec(container, "sh", "-c", f"touch {shlex.quote(old_orphan_path)}")
        assert rc == 0, f"could not plant orphan: {out!r}"
        # Force mtime to 3h ago.
        old_epoch = int(time.time()) - 3 * 3600
        rc, _ = _docker_exec(
            container, "sh", "-c", f"touch -d @{old_epoch} {shlex.quote(old_orphan_path)}"
        )
        assert rc == 0

        # Plant a fresh synthetic orphan (current mtime, default touch).
        # The age guard should keep this one.
        fresh_orphan_uuid = str(uuid.uuid4())
        fresh_orphan_path = f"{PROJECTS_DIR}/{fresh_orphan_uuid}.jsonl"
        rc, out = _docker_exec(container, "sh", "-c", f"touch {shlex.quote(fresh_orphan_path)}")
        assert rc == 0

        # Run one cleanup cycle with a 1h age guard.
        svc = SessionCleanupService(poll_interval_seconds=999999, min_age_seconds=3600)
        report = asyncio.run(svc.run_cycle())

        per = report.per_agent.get(agent, {})
        assert per, f"agent {agent} not in report: {report.per_agent}"
        assert per.get("deleted", 0) >= 1, f"expected at least 1 deletion: {per}"

        # The aged orphan must be gone.
        rc, _ = _docker_exec(container, "test", "-f", old_orphan_path)
        assert rc != 0, f"aged orphan {old_orphan_path} should be reaped"

        # The active JSONL must survive.
        rc, _ = _docker_exec(container, "test", "-f", active_path)
        assert rc == 0, f"active JSONL {active_path} should be kept"

        # The fresh orphan must survive (race guard).
        rc, _ = _docker_exec(container, "test", "-f", fresh_orphan_path)
        assert rc == 0, f"fresh orphan {fresh_orphan_path} should be kept by age guard"

        # Cleanup the fresh orphan ourselves (test pollution).
        _docker_exec(container, "rm", "-f", fresh_orphan_path)
    finally:
        api_client.delete(f"/api/agents/{agent}/sessions/{sid}")
