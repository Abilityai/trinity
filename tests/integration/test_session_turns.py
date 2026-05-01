"""
Phase 2.4 integration tests — Session tab turn endpoint against a live agent.

Uses the persistent ``agent-testfix`` container (SSH 2223 — 2222 belongs to
agent-trinity-system) so we don't burn the per-test agent quota or pay
container startup cost on every run. Skips cleanly if testfix is missing,
not running, or the Session tab feature flag can't be flipped.

Scenarios from docs/planning/SESSION_TAB_2026-04.md:

  A — happy path: 3 turns reattach to the SAME Claude UUID
  B — context restoration: turn 2 recalls a value mentioned in turn 1
  C — JSONL deleted mid-session → fallback fires, retry succeeds
  D — concurrent POSTs to the same session serialise via the Redis lock
  E — switch sessions A → B → A; A's UUID survives unchanged

These are real Claude API calls; mark @pytest.mark.slow.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional

import httpx
import pytest

from utils.api_client import TrinityApiClient


TESTFIX_AGENT = os.getenv("SESSION_TEST_AGENT", "testfix")


# ---------------------------------------------------------------------------
# Docker SDK helpers (avoid docker CLI dependency inside the test container)
# ---------------------------------------------------------------------------


def _docker_client():
    """Lazy docker SDK client. Returns None if the socket isn't mounted."""
    try:
        import docker  # type: ignore
        return docker.from_env()
    except Exception:
        return None


def _docker_inspect_image(container: str) -> Optional[str]:
    client = _docker_client()
    if client is None:
        return None
    try:
        c = client.containers.get(container)
        return c.image.id  # full sha256:... id
    except Exception:
        return None


def _exec_in_container(container: str, *cmd: str, timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command inside the named container via the Docker SDK."""
    client = _docker_client()
    if client is None:
        return 127, "", "docker sdk unavailable"
    try:
        c = client.containers.get(container)
        rc, output = c.exec_run(list(cmd), demux=True)
        stdout, stderr = output if isinstance(output, tuple) else (output, b"")
        return (
            rc or 0,
            (stdout or b"").decode("utf-8", "replace"),
            (stderr or b"").decode("utf-8", "replace"),
        )
    except Exception as e:
        return 127, "", str(e)


@pytest.fixture(scope="module")
def testfix_ready(api_client: TrinityApiClient):
    """Skip module if the testfix agent isn't reachable through the platform.

    Inspects Docker directly so we can also flag the L1 image-version pitfall
    (running on the pre-Phase-1.3 base image where the parser bug yields
    bogus session IDs and Scenarios A/B are guaranteed to fail).
    """
    container = f"agent-{TESTFIX_AGENT}"
    image_sha = _docker_inspect_image(container)
    if image_sha is None:
        pytest.skip(f"Container {container} not found — Phase 2 tests need a recreated testfix")

    info = api_client.get(f"/api/agents/{TESTFIX_AGENT}")
    if info.status_code != 200:
        pytest.skip(f"Agent {TESTFIX_AGENT} not visible via API ({info.status_code})")

    return {"agent": TESTFIX_AGENT, "container": container, "image_sha": image_sha}


@pytest.fixture(scope="module", autouse=True)
def session_tab_enabled(api_client: TrinityApiClient):
    """Enable the Session tab flag for the module, restore on teardown.

    Uses the generic settings endpoint (PUT /api/settings/{key}) which writes
    to the system_settings row that ``is_session_tab_enabled()`` reads on
    every request — no backend restart needed.
    """
    key = "session_tab_enabled"
    prior = api_client.get(f"/api/settings/{key}")
    prior_value = None
    if prior.status_code == 200:
        prior_value = prior.json().get("value")

    set_resp = api_client.put(f"/api/settings/{key}", json={"value": "true"})
    if set_resp.status_code not in (200, 201):
        pytest.skip(f"Cannot enable session_tab flag ({set_resp.status_code}): {set_resp.text[:200]}")

    yield

    if prior_value is None:
        api_client.delete(f"/api/settings/{key}")
    else:
        api_client.put(f"/api/settings/{key}", json={"value": prior_value})


@pytest.fixture
def session_id(api_client: TrinityApiClient, testfix_ready):
    """Create a fresh session, return its id, delete on teardown."""
    agent = testfix_ready["agent"]
    resp = api_client.post(f"/api/agents/{agent}/session", json={})
    assert resp.status_code == 200, f"create session failed: {resp.status_code} {resp.text[:300]}"
    sid = resp.json()["id"]
    yield sid
    api_client.delete(f"/api/agents/{agent}/sessions/{sid}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send_turn(
    api_client: TrinityApiClient,
    agent: str,
    session_id: str,
    message: str,
    *,
    timeout: float = 240.0,
) -> dict:
    resp = api_client.post(
        f"/api/agents/{agent}/sessions/{session_id}/message",
        json={"message": message},
        timeout=timeout,
    )
    if resp.status_code in (429, 503):
        pytest.skip(f"Agent transient state ({resp.status_code}): {resp.text[:200]}")
    assert resp.status_code == 200, f"turn failed: {resp.status_code} {resp.text[:400]}"
    body = resp.json()
    assert body.get("claude_session_id"), f"no claude_session_id in response: {body}"
    return body


# ---------------------------------------------------------------------------
# Scenario A — 3-turn happy path
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_scenario_a_three_turns_share_claude_uuid(
    api_client: TrinityApiClient, testfix_ready, session_id: str
):
    """Three sequential turns must reattach to the same Claude session UUID.

    This is the "thesis check" — if it fails, --resume isn't being honoured
    end-to-end. Phase 1.3 parser fix + Phase 1.4 persist_session flag are
    both load-bearing here.
    """
    agent = testfix_ready["agent"]

    t1 = _send_turn(api_client, agent, session_id, "Hello, please reply with the single word OK.")
    t2 = _send_turn(api_client, agent, session_id, "Reply OK again.")
    t3 = _send_turn(api_client, agent, session_id, "One more OK.")

    uuids = {t1["claude_session_id"], t2["claude_session_id"], t3["claude_session_id"]}
    assert len(uuids) == 1, f"expected one shared UUID across 3 turns, got: {uuids}"
    assert not t1["fallback_fired"]
    assert not t2["fallback_fired"]
    assert not t3["fallback_fired"]


# ---------------------------------------------------------------------------
# Scenario B — turn 2 recalls value mentioned only in turn 1
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_scenario_b_resume_restores_conversation_memory(
    api_client: TrinityApiClient, testfix_ready, session_id: str
):
    """Turn 2 must answer with the value introduced in turn 1.

    Without --resume the agent has no way to know — the Session router does
    NOT prepend text-replay context (unlike the legacy Chat router). If
    Phase 1.4's persist_session flag isn't wired correctly, the JSONL is
    empty and the agent answers something other than the secret.
    """
    agent = testfix_ready["agent"]
    secret = f"4827{uuid.uuid4().hex[:5]}"

    _send_turn(
        api_client,
        agent,
        session_id,
        f"Please remember the magic number {secret}. Reply with just OK.",
    )
    t2 = _send_turn(
        api_client,
        agent,
        session_id,
        "What was the magic number I just asked you to remember? Reply with only the number.",
    )

    assert secret in (t2.get("response") or ""), (
        f"resume failed to restore memory; expected {secret} in: "
        f"{(t2.get('response') or '')[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Scenario C — JSONL deleted between turns → fallback fires
# ---------------------------------------------------------------------------


def _list_session_jsonl(container: str, claude_uuid: str) -> list[str]:
    """Return absolute paths to JSONLs whose name matches the UUID."""
    rc, out, _ = _exec_in_container(
        container, "find", "/home/developer/.claude/projects", "-name", f"{claude_uuid}.jsonl"
    )
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line.strip()]


@pytest.mark.slow
@pytest.mark.requires_agent
def test_scenario_c_missing_jsonl_triggers_fallback(
    api_client: TrinityApiClient, testfix_ready, session_id: str
):
    """Delete the JSONL behind the cached UUID; turn 2 must fallback + recover.

    Reproduces Anthropic's `cleanupPeriodDays` (#39667) and CLI-upgrade
    (#53417) failure modes — the cached UUID is stale, the resume call
    explodes with "no conversation found", the router clears the cache and
    retries cold under a fresh UUID.
    """
    agent = testfix_ready["agent"]
    container = testfix_ready["container"]

    t1 = _send_turn(api_client, agent, session_id, "Reply OK.")
    uuid_before = t1["claude_session_id"]

    jsonls = _list_session_jsonl(container, uuid_before)
    if not jsonls:
        pytest.skip(
            f"JSONL for {uuid_before} not found in container — "
            "agent might use a different .claude path"
        )
    for path in jsonls:
        rc, _, err = _exec_in_container(container, "rm", "-f", path)
        assert rc == 0, f"failed to delete JSONL {path}: {err}"

    t2 = _send_turn(api_client, agent, session_id, "Reply OK again.")
    assert t2["fallback_fired"], (
        f"expected fallback to fire after JSONL deletion; response: {t2}"
    )
    assert t2["fallback_reason"] == "resume_jsonl_not_found"
    assert t2["claude_session_id"] != uuid_before, (
        "fallback should produce a fresh UUID, not the deleted one"
    )

    # Subsequent turn under the new UUID must succeed cleanly (counter reset).
    t3 = _send_turn(api_client, agent, session_id, "One more OK.")
    assert not t3["fallback_fired"]
    assert t3["claude_session_id"] == t2["claude_session_id"]


# ---------------------------------------------------------------------------
# Scenario D — two concurrent POSTs serialise via Redis lock
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_scenario_d_concurrent_turns_serialise_on_lock(
    api_client: TrinityApiClient, testfix_ready, session_id: str
):
    """After a cold turn establishes the UUID, fire two simultaneous resume
    turns. Both must succeed under the same UUID; one must run after the
    other (no JSONL corruption from concurrent --resume writes).
    """
    agent = testfix_ready["agent"]
    base_url = api_client.config.base_url
    token = api_client.token

    # Cold turn first to populate cached_claude_session_id (Redis lock only
    # engages on resume turns).
    _send_turn(api_client, agent, session_id, "Reply OK.")

    url = f"{base_url}/api/agents/{agent}/sessions/{session_id}/message"
    headers = {"Authorization": f"Bearer {token}"}

    results: list[dict | str] = [None, None]
    started = [None, None]
    finished = [None, None]

    def _fire(idx: int, payload: dict):
        started[idx] = time.monotonic()
        try:
            with httpx.Client(timeout=300.0) as c:
                r = c.post(url, headers=headers, json=payload)
            finished[idx] = time.monotonic()
            if r.status_code == 200:
                results[idx] = r.json()
            else:
                results[idx] = f"status={r.status_code} body={r.text[:300]}"
        except Exception as e:
            finished[idx] = time.monotonic()
            results[idx] = f"exception={e!r}"

    t1 = threading.Thread(target=_fire, args=(0, {"message": "Reply once with OK."}))
    t2 = threading.Thread(target=_fire, args=(1, {"message": "Reply once with OK."}))
    t1.start()
    t2.start()
    t1.join(timeout=600)
    t2.join(timeout=600)

    assert isinstance(results[0], dict) and isinstance(results[1], dict), (
        f"one or both concurrent turns failed: {results}"
    )

    uuids = {results[0]["claude_session_id"], results[1]["claude_session_id"]}
    assert len(uuids) == 1, f"concurrent turns produced different UUIDs: {uuids}"

    # Both HTTP requests are open from t≈0 — the second one is parked inside
    # the Redis lock's poll loop, not yet calling the agent. Serialisation
    # has to be measured on the *finish* side: if the lock works, the second
    # response is delayed by roughly the first turn's full duration, so the
    # gap between finishes should be substantial. If the lock fails and both
    # turns hit the agent in parallel, both finish near the same wall time.
    durations = [finished[i] - started[i] for i in range(2)]
    finish_gap = max(finished) - min(finished)
    winner_work_time = min(durations)

    # When the lock works, the contended thread's HTTP request is parked
    # in the Redis poll loop, so its wall-clock finish is delayed by the
    # winner's *full execution time*. So the gap between the two finish
    # times approximates the winner's per-turn duration (within Redis
    # poll-tick slop). When the lock fails, both turns run in parallel
    # and finish near-simultaneously → finish_gap ≈ 0.
    #
    # Threshold: finish_gap must be at least 70% of the faster turn's
    # duration. The 0.3 slack absorbs lock poll interval + scheduling.
    assert finish_gap >= 0.7 * winner_work_time, (
        f"turns not serialised: finish_gap={finish_gap:.2f}s, "
        f"winner_duration={winner_work_time:.2f}s, "
        f"threshold={0.7 * winner_work_time:.2f}s, durations={durations}"
    )


# ---------------------------------------------------------------------------
# Scenario E — switching sessions preserves each one's UUID
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.requires_agent
def test_scenario_e_session_switching_preserves_resume(
    api_client: TrinityApiClient, testfix_ready
):
    """Two sessions on the same agent must keep independent Claude UUIDs.

    Caches are per-row (E6 isolation): A's UUID does NOT change when B fires
    turns; A's third turn (after B's two) still reattaches to A's original.
    """
    agent = testfix_ready["agent"]

    sa_resp = api_client.post(f"/api/agents/{agent}/session", json={})
    sb_resp = api_client.post(f"/api/agents/{agent}/session", json={})
    assert sa_resp.status_code == 200 and sb_resp.status_code == 200
    sa = sa_resp.json()["id"]
    sb = sb_resp.json()["id"]

    try:
        a1 = _send_turn(api_client, agent, sa, "Reply OK in session A.")
        a2 = _send_turn(api_client, agent, sa, "Reply OK again in A.")
        b1 = _send_turn(api_client, agent, sb, "Reply OK in session B.")
        b2 = _send_turn(api_client, agent, sb, "Reply OK again in B.")
        a3 = _send_turn(api_client, agent, sa, "Final OK in A.")

        a_uuids = {a1["claude_session_id"], a2["claude_session_id"], a3["claude_session_id"]}
        b_uuids = {b1["claude_session_id"], b2["claude_session_id"]}

        assert len(a_uuids) == 1, f"session A's UUID drifted: {a_uuids}"
        assert len(b_uuids) == 1, f"session B's UUID drifted: {b_uuids}"
        assert a_uuids.isdisjoint(b_uuids), (
            f"sessions A and B share a UUID — caches contaminated: A={a_uuids}, B={b_uuids}"
        )
    finally:
        api_client.delete(f"/api/agents/{agent}/sessions/{sa}")
        api_client.delete(f"/api/agents/{agent}/sessions/{sb}")
