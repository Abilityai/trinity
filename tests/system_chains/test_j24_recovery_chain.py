"""J24 — the B9 recovery chain (trinity-enterprise#905; ent#847 matrix row B9).

**Promise:** work my agents are doing survives the platform failing under it —
a cancel really ends it, a backend restart mid-turn neither loses the result
nor mistimes it, and a rebuilt container keeps doing the job it was doing.

The six legs are the rc3 recheck log verbatim (ent#847, dolho 2026-10-13) — the
hand-run proofs of the three 1.0-round recovery fixes:

* t#3588 — a cancel (push and pull path) closes the execution AND its activity
  rows; rc2 left them ``started`` (the #1804 class, third occurrence).
* t#3589 — a watchdog-claimed turn records the AGENT'S finish as
  ``completed_at``, not the claim time; rc2 showed 5m32s for a 64-second turn.
* t#3590 — a missing-container rebuild keeps the pull env; rc2 silently
  de-piloted the agent and its queue stopped draining.

Unit files exist for all three (``test_3588_*``, ``test_3589_*``,
``test_3590_*``) and prove the functions. They cannot prove the WIRING under a
real restart — the exact gap the #1804 class shipped through three times with
green units. That live wiring is what this chain pins (tdd guide, question 4).

**Two phases, because the pilot allowlist is boot-time env.**
``PULL_MODE_PILOT_AGENTS`` is read at backend import (``config.py``), so
de-piloting an agent — leg 6 — requires the backend recreated with the
allowlist emptied. ``scripts/system/run_recovery.sh`` orchestrates both phases;
``.github/workflows/recovery-journey.yml`` is the dispatch lane (needs a real
model credential — every leg is a real agent turn).

  phase 1 (``-k "not depilot"``): backend booted with
      ``PULL_MODE_PILOT_AGENTS=pytest-ephemeral-journey-b9pull`` — legs 1–5.
      Run with ``CHAIN_KEEP_AGENTS=1``: leg 6 needs phase 1's pilot container.
  phase 2 (``-k depilot``): backend recreated with the allowlist EMPTY — leg 6,
      then both agents are deleted whatever happened.

**Same-host only.** The legs restart the backend container, force-remove agent
containers and read container env over the Docker socket. A remote
``TRINITY_API_URL`` fails loudly here — it cannot drive this chain.

**Fixed agent names, inside the reserved prefix.** The allowlist must name the
pilot BEFORE the agent exists, so the names cannot be random — but they stay
under ``pytest-ephemeral-journey-`` so the suite's teardown rules (#1558) still
hold and a crashed run is cleaned like any other ephemeral agent.
"""
from __future__ import annotations

import concurrent.futures
import os
import time
from datetime import datetime
from typing import Optional

import pytest
import requests

from journeys.conftest import (
    add_edge,
    agent_activities,
    agent_executions,
    agent_mcp_key,
    create_agent_and_wait,
    delete_agent_idempotent,
    McpSession,
    poll_until,
)

from .conftest import model_key_available, not_run, release_agent, wait_agent_server
from .stack_reads import _backend_container

PUSH_NAME = "pytest-ephemeral-journey-b9push"
PULL_NAME = "pytest-ephemeral-journey-b9pull"
PULL_WORKERS = 3  # baked into TRINITY_MAX_PARALLEL_TASKS at the leg-4 recreate

TERMINAL = {"completed", "failed", "cancelled"}
# Activity rows a terminal writer failed to close — the #1804 / t#3588 class.
OPEN_ACTIVITY = {"started", "running", "in_progress"}

# A real turn is model start-up + the sleep; the claim path would add the 180 s
# grace plus up to a 300 s cleanup cycle. 150 s cleanly separates "the agent's
# own finish" from "the claim time" (rc2 recorded 5m32s for a 64 s turn).
REAL_TURN_MS_MAX = 150_000
# Running row appears (model boot included).
RUNNING_DEADLINE_S = 240.0
# Terminal after a restart: turn + 180 s claim grace + ≤300 s cleanup cycle
# + margin. Pays only the real wait — poll_until returns on the event.
CLAIM_DEADLINE_S = 780.0

SLEEP_300 = ('Run exactly this command and nothing else: '
             'python3 -c "import time; time.sleep(300)". Do not explain.')


def _sleep_turn(marker: str, seconds: int = 60) -> str:
    return (f'Run exactly this command: python3 -c "import time; '
            f'time.sleep({seconds})" and when it finishes reply with exactly: '
            f'{marker}')


# ---------------------------------------------------------------------------
# Docker-side reads and the restart primitive
# ---------------------------------------------------------------------------

def _docker():
    try:
        import docker  # test-only dependency (requirements-test.txt)
        client = docker.from_env()
        client.ping()
        return client
    except Exception as e:  # noqa: BLE001 — the finding, not a skip
        raise AssertionError(
            f"J24 needs the Docker socket of the host running the stack "
            f"({type(e).__name__}) — it restarts the backend and reads agent "
            f"container env. Run it where the stack runs."
        ) from e


def _agent_container(name: str):
    containers = _docker().containers.list(
        all=True, filters={"label": f"trinity.agent-name={name}"})
    return containers[0] if containers else None


def _agent_env(name: str) -> dict:
    container = _agent_container(name)
    assert container is not None, (
        f"no container carries trinity.agent-name={name} — the platform said "
        f"the agent exists but the Docker host has nothing for it")
    env = container.attrs.get("Config", {}).get("Env") or []
    return dict(kv.split("=", 1) for kv in env if "=" in kv)


def _pilot_allowlist() -> str:
    backend = _backend_container()
    assert backend is not None, "no backend container on this Docker host"
    env = backend.attrs.get("Config", {}).get("Env") or []
    for kv in env:
        if kv.startswith("PULL_MODE_PILOT_AGENTS="):
            return kv.split("=", 1)[1]
    return ""


def _container_has_process(name: str, needle: str) -> bool:
    container = _agent_container(name)
    if container is None:
        return False
    try:
        rows = container.top().get("Processes") or []
    except Exception:  # noqa: BLE001 — a restarting container reads as "no process"
        return False
    return any(needle in " ".join(map(str, row)) for row in rows)


def restart_backend_and_relogin(client) -> None:
    """``docker restart`` on the backend, then health, then a fresh token.

    A restart invalidates every JWT, so the client re-authenticates — retried,
    because the API can answer /health before the token route is warm.
    """
    backend = _backend_container()
    assert backend is not None, "no backend container to restart"
    backend.restart(timeout=30)
    url = os.getenv("TRINITY_API_URL", "http://localhost:8000")

    def healthy():
        try:
            return requests.get(f"{url}/health", timeout=5).status_code == 200
        except requests.RequestException:
            return None

    poll_until(healthy, deadline_s=240,
               describe="the backend never answered /health after its restart")

    def reauth():
        try:
            client.authenticate()
            return True
        except Exception:  # noqa: BLE001 — retried until the deadline names it
            return None

    poll_until(reauth, deadline_s=60,
               describe="re-login after the backend restart never succeeded")


# ---------------------------------------------------------------------------
# Execution / activity reads
# ---------------------------------------------------------------------------

def _ids(rows) -> set:
    return {r.get("id") for r in rows}


def _new_running_row(client, agent: str, before: set) -> dict:
    def appeared():
        fresh = [r for r in agent_executions(client, agent)
                 if r.get("id") not in before]
        running = [r for r in fresh if r.get("status") == "running"]
        return running[0] if running else None
    return poll_until(
        appeared, deadline_s=RUNNING_DEADLINE_S,
        describe=f"no new running execution appeared on '{agent}'")


def _execution_detail(client, agent: str, execution_id: str) -> dict:
    resp = client.get(f"/api/agents/{agent}/executions/{execution_id}")
    assert resp.status_code == 200, (
        f"reading execution {execution_id} of '{agent}' answered "
        f"{resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _wait_terminal(client, agent: str, execution_id: str,
                   deadline_s: float) -> dict:
    def done():
        detail = _execution_detail(client, agent, execution_id)
        return detail if detail.get("status") in TERMINAL else None
    return poll_until(
        done, deadline_s=deadline_s,
        describe=f"execution {execution_id} on '{agent}' never reached a "
                 f"terminal state")


def _open_activities(client, agent: str) -> list:
    return [a for a in agent_activities(client, agent)
            if (a.get("status") or "").lower() in OPEN_ACTIVITY]


def _assert_activities_close(client, agent: str, context: str) -> None:
    """t#3588's promise: no activity row is left open once the work is terminal.

    Polled briefly — the close is the CAS winner's own write, but the timeline
    read is eventually consistent with it."""
    poll_until(lambda: (not _open_activities(client, agent)) or None,
               deadline_s=90,
               describe=f"{context}: '{agent}' still has open activity rows "
                        f"(the t#3588 / #1804 class)")


def _parse_ts(value: str) -> datetime:
    # Stored ISO-Z (Invariant #16); detail rows may also carry +00:00 offsets.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _assert_real_duration(detail: dict, context: str) -> None:
    """t#3589's promise: the row records what the agent did, not what the
    recovery machinery did later."""
    duration = detail.get("duration_ms")
    assert duration is not None and duration > 0, (
        f"{context}: terminal row carries no duration_ms — {detail.get('id')}")
    assert duration <= REAL_TURN_MS_MAX, (
        f"{context}: duration_ms={duration} reads as the claim time, not the "
        f"agent's own finish (t#3589: rc2 recorded 5m32s for a 64 s turn)")
    started, completed = detail.get("started_at"), detail.get("completed_at")
    assert started and completed, (
        f"{context}: terminal row is missing started_at/completed_at")
    span_s = (_parse_ts(completed) - _parse_ts(started)).total_seconds()
    assert span_s >= 0, (
        f"{context}: completed_at precedes started_at (the ent#882 floor)")
    assert abs(span_s - duration / 1000.0) <= 5, (
        f"{context}: completed_at−started_at is {span_s:.1f}s but duration_ms "
        f"says {duration / 1000.0:.1f}s — the row disagrees with itself")


def _submit(pool, client, path: str, body: dict, timeout_s: float):
    """Fire a turn without holding the test on its HTTP call.

    The sync calls here are MEANT to outlive a backend restart or a terminate —
    the connection dying is part of the scenario, so the future swallows its
    exception and the assertions read the platform's records instead."""
    def run():
        try:
            return client.post(path, json=body, timeout=timeout_s)
        except Exception as e:  # noqa: BLE001 — the row, not the call, is the evidence
            return e
    return pool.submit(run)


def _run_marker_task(client, agent: str, pool, marker: str,
                     deadline_s: float = 300.0) -> dict:
    """POST /task and wait for ITS row to complete with the marker's reply —
    proves the path (push or pull) actually drains, never that an old row did."""
    before = _ids(agent_executions(client, agent))
    _submit(pool, client, f"/api/agents/{agent}/task",
            {"message": f"Reply with exactly: {marker}"}, timeout_s=deadline_s)

    def completed_with_marker():
        fresh = [r for r in agent_executions(client, agent)
                 if r.get("id") not in before and r.get("status") in TERMINAL]
        for row in fresh:
            detail = _execution_detail(client, agent, row["id"])
            if marker in (detail.get("response") or ""):
                return detail
        return None
    return poll_until(
        completed_with_marker, deadline_s=deadline_s,
        describe=f"the task on '{agent}' never completed with its marker "
                 f"'{marker}' — the path is not draining")


# ---------------------------------------------------------------------------
# Phase 1 fixture — the pair, under the pilot allowlist
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def recovery_pair(chain_client):
    """b9push (push path) and b9pull (pull pilot), with preconditions stated.

    Torn down by ``release_agent`` — run phase 1 with ``CHAIN_KEEP_AGENTS=1``
    so the pilot container survives into phase 2 (leg 6 detects drift on it).
    """
    if not model_key_available(chain_client):
        not_run("every J24 leg is a real agent turn and the target holds no "
                "Claude credential (claude_auth_configured=false)")
    allowlist = _pilot_allowlist()
    if PULL_NAME not in allowlist:
        not_run(f"phase 1 needs the backend booted with "
                f"PULL_MODE_PILOT_AGENTS={PULL_NAME} (it carries "
                f"{allowlist!r}) — see scripts/system/run_recovery.sh")

    # Re-runnable after a crash: the fixed names may be leftovers.
    for name in (PUSH_NAME, PULL_NAME):
        delete_agent_idempotent(chain_client, name)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n: create_agent_and_wait(chain_client, n),
                          (PUSH_NAME, PULL_NAME)))
        # Baked into TRINITY_MAX_PARALLEL_TASKS at the next recreate (leg 4
        # asserts it) and sized so "pull pool started" means a real pool.
        resp = chain_client.put(f"/api/agents/{PULL_NAME}/capacity",
                                json={"max_parallel_tasks": PULL_WORKERS})
        assert resp.status_code == 200, (
            f"setting '{PULL_NAME}' capacity answered {resp.status_code}: "
            f"{resp.text[:300]}")
        for name in (PUSH_NAME, PULL_NAME):
            wait_agent_server(chain_client, name)
        pull_env = _agent_env(PULL_NAME)
        assert pull_env.get("TRINITY_PULL_MODE") == "true", (
            f"'{PULL_NAME}' is allowlisted but its container carries no "
            f"TRINITY_PULL_MODE — the pilot never started as one")
        yield PUSH_NAME, PULL_NAME
    finally:
        for name in (PUSH_NAME, PULL_NAME):
            release_agent(chain_client, name)


@pytest.fixture()
def pool():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        yield executor


# ---------------------------------------------------------------------------
# Legs 1 + 2 — t#3588: a cancel closes the work, on both dispatch paths
# ---------------------------------------------------------------------------

@pytest.mark.chain("J24.1", "a cancel really ends the work, push and pull (t#3588)")
def test_j24_cancel_closes_the_work(chain_client, chain, recovery_pair, pool):
    push, pull = recovery_pair
    for agent, path_name in ((push, "push"), (pull, "pull pilot")):
        with chain.step(f"{path_name}: a long-running task is terminated"):
            before = _ids(agent_executions(chain_client, agent))
            _submit(pool, chain_client, f"/api/agents/{agent}/task",
                    {"message": SLEEP_300}, timeout_s=400)
            row = _new_running_row(chain_client, agent, before)
            poll_until(
                lambda a=agent: _container_has_process(a, "time.sleep") or None,
                deadline_s=RUNNING_DEADLINE_S,
                describe=f"the sleep process never appeared in '{agent}'s "
                         f"container — the turn is not really running")
            resp = chain_client.post(
                f"/api/agents/{agent}/executions/{row['id']}/terminate")
            assert resp.status_code == 200, (
                f"terminate answered {resp.status_code}: {resp.text[:300]}")

        with chain.step(f"{path_name}: the row is cancelled with a real duration"):
            detail = _wait_terminal(chain_client, agent, row["id"],
                                    deadline_s=120)
            assert detail.get("status") == "cancelled", (
                f"terminated execution ended '{detail.get('status')}', "
                f"not 'cancelled': {str(detail.get('error'))[:200]}")
            duration = detail.get("duration_ms")
            assert duration and duration < 290_000, (
                f"cancelled row's duration_ms={duration} does not read as "
                f"'ended when cancelled' for a 300 s task")

        with chain.step(f"{path_name}: every activity row is closed"):
            _assert_activities_close(chain_client, agent,
                                     f"after cancel on the {path_name} path")

        with chain.step(f"{path_name}: no runaway process is left behind"):
            poll_until(
                lambda a=agent: (not _container_has_process(a, "time.sleep"))
                or None,
                deadline_s=90,
                describe=f"'{agent}' still runs the sleep after its execution "
                         f"was cancelled")


# ---------------------------------------------------------------------------
# Leg 3 — t#3589: restart mid sync turn → the agent's finish is the record
# ---------------------------------------------------------------------------

@pytest.mark.chain("J24.2", "a restart mid sync turn records the real duration (t#3589)")
def test_j24_restart_mid_sync_turn(chain_client, chain, recovery_pair, pool):
    push, _ = recovery_pair
    marker = "J24-SYNC-DONE"

    with chain.step("a sync chat turn is running"):
        before = _ids(agent_executions(chain_client, push))
        _submit(pool, chain_client, f"/api/agents/{push}/chat",
                {"message": _sleep_turn(marker)}, timeout_s=300)
        row = _new_running_row(chain_client, push, before)

    with chain.step("the backend restarts ~15 s into the turn"):
        time.sleep(15)
        restart_backend_and_relogin(chain_client)

    with chain.step("the row completes at the agent's finish, not the claim time"):
        detail = _wait_terminal(chain_client, push, row["id"],
                                deadline_s=CLAIM_DEADLINE_S)
        assert detail.get("status") == "completed", (
            f"the turn the restart interrupted ended "
            f"'{detail.get('status')}': {str(detail.get('error'))[:200]} — "
            f"recovery lost it instead of claiming it")
        _assert_real_duration(detail, "watchdog-recovered sync turn")

    with chain.step("the retained reply reached the row"):
        assert marker in (detail.get("response") or ""), (
            "the recovered row carries no trace of the agent's actual reply — "
            "the result was fabricated, not retained")

    with chain.step("the turn's activity rows are closed"):
        _assert_activities_close(chain_client, push,
                                 "after the watchdog claim of a sync turn")


# ---------------------------------------------------------------------------
# Leg 4 — t#3588: restart mid agent-to-agent turn → the caller's record closes
# ---------------------------------------------------------------------------

@pytest.mark.chain("J24.3", "a restart mid agent-to-agent turn closes the caller's record (t#3588)")
def test_j24_restart_mid_collaboration_turn(chain_client, chain, recovery_pair,
                                            pool):
    push, pull = recovery_pair
    marker = "J24-COLLAB-DONE"

    with chain.step("pull → push over MCP, with the caller's own key"):
        add_edge(chain_client, pull, push)
        mcp = McpSession(agent_mcp_key(pull))
        rows_before = _ids(agent_executions(chain_client, push))
        collab_before = _ids(
            agent_activities(chain_client, pull, "agent_collaboration"))
        pool.submit(lambda: _swallow(
            lambda: mcp.call("chat_with_agent", agent_name=push,
                             message=_sleep_turn(marker))))
        row = _new_running_row(chain_client, push, rows_before)

    with chain.step("the backend restarts mid-turn"):
        time.sleep(15)
        restart_backend_and_relogin(chain_client)

    with chain.step("the callee's row completes with a real duration"):
        detail = _wait_terminal(chain_client, push, row["id"],
                                deadline_s=CLAIM_DEADLINE_S)
        assert detail.get("status") == "completed", (
            f"the callee's turn ended '{detail.get('status')}': "
            f"{str(detail.get('error'))[:200]}")
        _assert_real_duration(detail, "watchdog-recovered collaboration turn")

    with chain.step("the caller's collaboration record is closed at the "
                    "callee's finish"):
        def collab_terminal():
            fresh = [a for a in agent_activities(chain_client, pull,
                                                 "agent_collaboration")
                     if a.get("id") not in collab_before]
            terminal = [a for a in fresh
                        if (a.get("status") or "").lower() not in OPEN_ACTIVITY]
            return terminal or None
        closed = poll_until(
            collab_terminal, deadline_s=CLAIM_DEADLINE_S,
            describe=f"'{pull}'s collaboration record never closed after the "
                     f"restart (rc2 left it `started`, activities_closed=0)")
        for activity in closed:
            duration = activity.get("duration_ms")
            assert duration is None or duration <= REAL_TURN_MS_MAX, (
                f"collaboration record closed with duration_ms={duration} — "
                f"the claim time, not the callee's finish (t#3589 class)")
        _assert_activities_close(chain_client, pull,
                                 "after the watchdog claim, caller side")
        _assert_activities_close(chain_client, push,
                                 "after the watchdog claim, callee side")


def _swallow(fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — the records are the evidence
        return e


# ---------------------------------------------------------------------------
# Leg 5 — t#3590: a missing-container rebuild keeps the pull env
# ---------------------------------------------------------------------------

@pytest.mark.chain("J24.4", "a missing-container rebuild keeps the pull env (t#3590)")
def test_j24_rebuild_keeps_pull_env(chain_client, chain, recovery_pair, pool):
    _, pull = recovery_pair

    with chain.step("the pilot's container is force-removed and /start rebuilds it"):
        container = _agent_container(pull)
        assert container is not None, f"no container for '{pull}' to remove"
        container.remove(force=True)
        resp = chain_client.post(f"/api/agents/{pull}/start", timeout=300)
        assert resp.status_code in (200, 202), (
            f"/start after rm -f answered {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        assert body.get("recreated") is True, (
            f"/start did not report the container as recreated: "
            f"{ {k: body.get(k) for k in ('recreated', 'recreate_reason')} }")

    with chain.step("the rebuilt container still carries the pull env"):
        env = _agent_env(pull)
        assert env.get("TRINITY_PULL_MODE") == "true", (
            "the rebuild dropped TRINITY_PULL_MODE — the t#3590 defect: the "
            "pilot silently stops draining its queue")
        assert env.get("TRINITY_MAX_PARALLEL_TASKS") == str(PULL_WORKERS), (
            f"TRINITY_MAX_PARALLEL_TASKS={env.get('TRINITY_MAX_PARALLEL_TASKS')!r}"
            f" after the rebuild; the stored capacity is {PULL_WORKERS}")

    with chain.step("the worker pool starts and the queue drains"):
        container = _agent_container(pull)
        poll_until(
            lambda: (b"pull pool started" in container.logs(tail=400)) or None,
            deadline_s=180,
            describe=f"'{pull}' never logged 'pull pool started' after the "
                     f"rebuild")
        wait_agent_server(chain_client, pull)
        _run_marker_task(chain_client, pull, pool, "J24-PULL-OK")


# ---------------------------------------------------------------------------
# Leg 6 — t#3590: de-pilot drift is detected, recreated exactly once (phase 2)
# ---------------------------------------------------------------------------

@pytest.mark.chain("J24.5", "de-pilot drift recreates exactly once (t#3590)")
def test_j24_depilot_drift_recreate(chain_client, chain):
    """Phase 2 (``-k depilot``): the backend was RECREATED with the allowlist
    empty; the pilot container from phase 1 still carries the baked flag. That
    disagreement is the drift /start must detect — once."""
    allowlist = _pilot_allowlist()
    if PULL_NAME in allowlist:
        not_run(f"phase 2 needs the backend recreated with "
                f"PULL_MODE_PILOT_AGENTS empty (it still carries "
                f"{allowlist!r}) — see scripts/system/run_recovery.sh")
    if chain_client.get(f"/api/agents/{PULL_NAME}").status_code != 200:
        not_run(f"phase 1 did not leave '{PULL_NAME}' behind — run phase 1 "
                f"with CHAIN_KEEP_AGENTS=1 first")
    if not model_key_available(chain_client):
        not_run("the drain proof is a real agent turn and the target holds no "
                "Claude credential")

    try:
        with chain.step("/start detects the config drift and recreates"):
            resp = chain_client.post(f"/api/agents/{PULL_NAME}/start",
                                     timeout=300)
            assert resp.status_code in (200, 202), (
                f"/start answered {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            assert body.get("recreated") is True, (
                "de-piloted agent's /start did not recreate — the stale "
                "TRINITY_PULL_MODE stays baked in (t#3590)")
            assert body.get("recreate_reason") == "config_drift", (
                f"recreate_reason={body.get('recreate_reason')!r}, "
                f"expected 'config_drift'")

        with chain.step("the baked pull flag is gone"):
            env = _agent_env(PULL_NAME)
            assert "TRINITY_PULL_MODE" not in env, (
                "TRINITY_PULL_MODE survived the de-pilot recreate — the "
                "worker pool would keep pulling (#1081 B1)")

        with chain.step("a second /start does not recreate again"):
            resp = chain_client.post(f"/api/agents/{PULL_NAME}/start",
                                     timeout=120)
            assert resp.status_code in (200, 202), (
                f"second /start answered {resp.status_code}: {resp.text[:300]}")
            assert resp.json().get("recreated") is False, (
                "the second /start recreated again — a recreate loop, not a "
                "one-time drift repair")

        with chain.step("the push path now runs a task"):
            wait_agent_server(chain_client, PULL_NAME)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                _run_marker_task(chain_client, PULL_NAME, pool, "J24-PUSH-OK")
    finally:
        # Phase 2 owns the teardown of BOTH phase-1 agents, whatever happened
        # above — the chain leaves nothing behind.
        for name in (PUSH_NAME, PULL_NAME):
            delete_agent_idempotent(chain_client, name)
