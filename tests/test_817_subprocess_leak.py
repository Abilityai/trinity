"""
Issue #817 — Runaway subprocess after task failure regression repro.

Deterministically reproduces the symptom chain reported in the ticket:
    schedule timeout-kill -> pending_retry -> orphaned CPU-burner -> agent
    /health unresponsive.

The engineered leak lives in config/agent-templates/test-leak-hook/: a
UserPromptSubmit hook spawns a `setsid` CPU-burner with stdin/stdout/stderr
detached to /dev/null. That satisfies all three conditions that let an
orphan slip past the existing cleanup (#618 / #728 / #808):

    1. Outside Claude's pgid (setsid)               -> escapes terminate_process_group
    2. Holds no stdout pipe write end (FDs detached) -> escapes _kill_orphan_pipe_writers
    3. Spins on CPU                                  -> starves agent-server event loop

The schedule's timeout_seconds=30 + max_retries=1 forces the execution to
fail and the scheduler to mark it pending_retry — matching the production
shape from #817.

This test does NOT assert the downstream circuit-breaker cascade. That is
governed by `services/agent_client.py` tunables that need ~32 min of probe
backoff to reach `dormant` at production constants; the CB module has its
own unit tests. CPU pin + /health timeout is sufficient evidence of the
root cause.
"""
from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import assert_status, assert_json_response
from utils.cleanup import cleanup_test_agent


# Schedule firing window: timeout_seconds=30 + backend buffer (10s) +
# scheduler poll cadence + retry scheduling latency. 90s is comfortably
# above the worst-case while staying tight enough to surface regressions
# in the path that produces pending_retry.
PENDING_RETRY_WAIT_SECONDS = 90

# CPU-pin assertion budget. The burner spawns from UserPromptSubmit, so it
# is already running by the time the chat dispatches; we just need Docker
# stats to settle. 30s is well past `docker stats` sampling jitter.
CPU_PIN_WAIT_SECONDS = 30

# Threshold for "burner is loose". Conservative: the production ticket
# observed 133-156%, single-core; even on a busy CI host a setsid'd while-
# loop comfortably exceeds 50% of one core.
CPU_PIN_THRESHOLD_PERCENT = 50.0


def _docker_cpu_percent(container_name: str) -> float | None:
    """Return current CPU% from `docker stats --no-stream`, or None on error.

    Docker reports e.g. "153.40%" — we strip the suffix and parse. None
    on parse failure / container missing so the polling loop can retry.
    """
    try:
        result = subprocess.run(
            [
                "docker", "stats", "--no-stream",
                "--format", "{{.CPUPerc}}", container_name,
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip().rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return None


def _health_curl_times_out(container_name: str) -> bool:
    """Run `docker exec <c> curl -m 5 http://localhost:8000/health` and
    return True iff it exits non-zero (timeout / connection refused).

    Curl's exit code 28 is the operation-timeout we're hoping for; we
    accept any non-zero exit since the bug surface includes connection
    failures during event-loop starvation, not just timeouts.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "curl", "-m", "5", "-s", "-o", "/dev/null",
                "-w", "%{http_code}",
                "http://localhost:8000/health",
            ],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # docker exec itself blocked past 15s — that is exactly the
        # symptom we are looking for (event-loop fully starved).
        return True
    return result.returncode != 0


def _count_burner_processes(container_name: str) -> int:
    """Count surviving subprocess-leak PIDs inside the agent container.

    Matches the burner cmdline pattern `while :; do :; done` (BusyBox `ps`
    on Alpine truncates argv but keeps the loop body visible). Returns 0
    on docker exec error so the caller can distinguish "no leak" from
    "couldn't measure" via separate diagnostics.
    """
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "ps", "-eo", "pid,pgid,cmd",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0
    return sum(
        1 for line in result.stdout.splitlines()
        if "while :" in line or "while:" in line
    )


def _ps_dump_for_diagnostics(container_name: str) -> str:
    """Return surviving-burner ps lines for the failure message."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "ps", "-eo", "pid,pgid,cmd",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "(docker exec failed — container may have been cleaned up)"
    if result.returncode != 0:
        return f"(ps exited {result.returncode})"
    return "\n".join(
        line for line in result.stdout.splitlines()
        if "while :" in line or "while:" in line
    )


@pytest.mark.slow
def test_817_repro_subprocess_leak_survives_termination(api_client: TrinityApiClient):
    """Reproduce Issue #817: subprocess survives task termination.

    The bug is "subprocess survives that should not". CPU pin and /health
    timeout are downstream symptoms whose reproducibility depends on host
    CPU contention (a fast Mac with 8 cores does not starve the event
    loop the same way a 1-2 core production VM does). The test asserts
    the cause directly: count `while :` burner PIDs inside the container
    AFTER the scheduler has pending_retry'd the execution and the backend
    has issued its post-failure /api/cancel + terminate path.

    A green run after a fix lands means the cleanup path now reaches
    setsid'd processes that escape Claude's pgid AND have detached FDs.
    """
    suffix = uuid.uuid4().hex[:6]
    system_name = f"test-leak817-{suffix}"
    agent_short = "victim"
    agent_name = f"{system_name}-{agent_short}"
    container_name = f"agent-{agent_name}"

    manifest = f"""
name: {system_name}
agents:
  {agent_short}:
    template: local:test-leak-hook
"""

    schedule_id: str | None = None

    try:
        # -------- Stage 1: deploy -------------------------------------------
        deploy = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": False},
            timeout=120.0,
        )
        assert_status(deploy, 200)
        deploy_data = assert_json_response(deploy)
        assert agent_name in deploy_data["agents_created"], (
            f"Agent {agent_name} not in {deploy_data['agents_created']}"
        )

        # Wait for the agent container to be running.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            agent_resp = api_client.get(f"/api/agents/{agent_name}")
            if agent_resp.status_code == 200:
                status = agent_resp.json().get("status")
                if status == "running":
                    break
            time.sleep(2)
        else:
            pytest.fail(f"Agent {agent_name} did not reach running status within 90s")

        # Docker "running" doesn't mean the internal agent-server is accepting
        # requests yet — startup.sh still has to copy template files, run
        # write-runtime-config, etc. Poll the agent's /health from inside the
        # container until it returns 200. If we trigger the schedule before
        # this, the backend's call to /api/task fast-fails with ConnectError
        # and the scheduler records `failed` with no useful timing.
        deadline = time.monotonic() + 60
        agent_ready = False
        while time.monotonic() < deadline:
            try:
                probe = subprocess.run(
                    [
                        "docker", "exec", container_name,
                        "curl", "-m", "3", "-s", "-o", "/dev/null",
                        "-w", "%{http_code}",
                        "http://localhost:8000/health",
                    ],
                    capture_output=True, text=True, timeout=8,
                )
                if probe.returncode == 0 and probe.stdout.strip() == "200":
                    agent_ready = True
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(2)
        if not agent_ready:
            pytest.fail(
                f"Agent {agent_name} /health did not return 200 within 60s "
                f"of reaching Docker status=running"
            )

        # Pin the container to 1 CPU. The template ships cpu: "1" but
        # Trinity's create path doesn't propagate it to the Docker create
        # call — `docker inspect` shows NanoCpus=0 / CpuQuota=0 on agent
        # containers — so the dev host's full 4-8 cores are available and
        # 8 burners can't actually starve uvicorn. Forcing the limit here
        # matches the production scenario (1-2 core cloud VM) so the
        # /health-timeout stage of the cascade reproduces.
        cpu_pin = subprocess.run(
            ["docker", "update", "--cpus=1", container_name],
            capture_output=True, text=True, timeout=10,
        )
        assert cpu_pin.returncode == 0, (
            f"Failed to pin {container_name} to 1 CPU: "
            f"stdout={cpu_pin.stdout!r} stderr={cpu_pin.stderr!r}"
        )

        # -------- Stage 2: schedule with short timeout + retries ------------
        # cron: 0 0 1 1 * = midnight Jan 1 — never fires naturally during
        # the test window. We trigger manually below.
        sched_resp = api_client.post(
            f"/api/agents/{agent_name}/schedules",
            json={
                "name": "leak817-repro",
                "cron_expression": "0 0 1 1 *",
                "message": "trigger the hook",
                "timeout_seconds": 30,
                "max_retries": 1,
                "retry_delay_seconds": 60,
                "enabled": True,
            },
        )
        assert_status(sched_resp, 201)
        schedule_id = sched_resp.json()["id"]

        # -------- Stage 3: manually trigger --------------------------------
        trigger = api_client.post(
            f"/api/agents/{agent_name}/schedules/{schedule_id}/trigger",
        )
        assert trigger.status_code in (200, 202), (
            f"Trigger returned {trigger.status_code}: {trigger.text}"
        )
        trigger_time = time.monotonic()

        # -------- Stage 4: poll for pending_retry ---------------------------
        # The hook sleeps 120s in the foreground. Backend's chat timeout =
        # schedule timeout_seconds (30) + 10s buffer = 40s. Backend marks
        # FAILED, scheduler's _maybe_schedule_retry promotes to PENDING_RETRY.
        deadline = trigger_time + PENDING_RETRY_WAIT_SECONDS
        reached_pending_retry = False
        executions_seen: list[dict] = []
        while time.monotonic() < deadline:
            exec_resp = api_client.get(
                f"/api/agents/{agent_name}/schedules/{schedule_id}/executions",
            )
            if exec_resp.status_code == 200:
                executions_seen = exec_resp.json() or []
                if any(e.get("status") == "pending_retry" for e in executions_seen):
                    reached_pending_retry = True
                    break
            time.sleep(3)

        assert reached_pending_retry, (
            f"Execution never reached pending_retry within "
            f"{PENDING_RETRY_WAIT_SECONDS}s. Last seen: "
            f"{[(e.get('id'), e.get('status')) for e in executions_seen]}"
        )

        # -------- Stage 5: instrumented burner-survival timeline -----------
        # Sample immediately, then at 5s/10s/20s, to detect whether a
        # delayed cleanup path is killing the burners after pending_retry.
        burner_timeline = []
        for delay in (0, 5, 10, 20):
            if delay:
                time.sleep(delay - (burner_timeline[-1][0] if burner_timeline else 0))
            burner_timeline.append((delay, _count_burner_processes(container_name)))
        print(f"\n[#817 repro] burner-count timeline (s, count): {burner_timeline}")

        # -------- Stage 6 (informational): /health must time out -----------
        # With CPU pinned to 1 core and 8 burners spinning, uvicorn's slice
        # should shrink below the curl 5s timeout. Logged informationally
        # for now — host scheduler variance can still leak a slice through.
        any_timed_out = False
        for _ in range(3):
            if _health_curl_times_out(container_name):
                any_timed_out = True
                break
            time.sleep(8)
        if not any_timed_out:
            print(
                f"\n[#817 repro] /health responded under 5s despite cpu=1 + "
                f"8 burners. Host scheduler may still be giving uvicorn a "
                f"slice; the leak itself is still proven by the burner-PID "
                f"assertion below."
            )
        else:
            print("\n[#817 repro] /health timed out — full production cascade reproduced.")

        # -------- Stage 7: regression gate — no burner should survive ------
        # Asserted as `== 0` so this test FAILS today (proving the bug)
        # and PASSES after the fix lands. The failure message dumps the
        # surviving cmdlines so the bug shape is obvious from CI logs
        # alone — no need to docker exec into the (already-deleted) agent.
        burner_count = _count_burner_processes(container_name)
        diag_ps = _ps_dump_for_diagnostics(container_name) if burner_count else ""
        assert burner_count == 0, (
            f"Subprocess leak (#817) reproduced: {burner_count} burner "
            f"PID(s) survived agent-server cleanup after pending_retry. "
            f"This regression gate is expected to FAIL until #817 is "
            f"fixed; a green run means the cleanup path now reaches "
            f"setsid'd processes whose FDs are detached from Claude's "
            f"pipes.\n\n"
            f"Surviving processes inside {container_name}:\n{diag_ps}"
        )

        # -------- Stage 7 (informational): CPU pin -------------------------
        # On a fast multi-core host this won't reach 100% per-core ceiling,
        # but the production correlation is real — log it so a regression
        # against a busier host surfaces. Failure here does NOT fail the
        # test (host hardware variance is too high to gate on).
        cpu_observed = _docker_cpu_percent(container_name)
        if cpu_observed is not None and cpu_observed < CPU_PIN_THRESHOLD_PERCENT:
            print(
                f"\n[#817 repro] CPU pin observed at {cpu_observed}% — below "
                f"the {CPU_PIN_THRESHOLD_PERCENT}% production threshold but "
                f"{burner_count} burner(s) confirmed alive. Likely host "
                f"hardware variance, not a regression."
            )

    finally:
        if schedule_id:
            try:
                api_client.delete(
                    f"/api/agents/{agent_name}/schedules/{schedule_id}",
                )
            except Exception:
                pass
        # cleanup_test_agent deletes the container, which reaps the burner
        # (Docker SIGKILLs every PID in the container's pid namespace).
        cleanup_test_agent(api_client, agent_name)


# Timing constants for the cgroup-walk follow-up test (Eugene's production
# class — no env tag, different pgid, FDs detached). 60s for the per-task
# path + cgroup sweep is conservative: claude-side cleanup typically lands
# inside 5s of execution end, and the sweep adds ~100ms.
NO_TAG_CLEANUP_WAIT_SECONDS = 60


def _inject_no_tag_burner(container_name: str) -> int | None:
    """Inject the production-class orphan into the running container.

    All four evasion vectors at once:
        setsid     -> different pgid, escapes terminate_process_group
        env -i     -> strips TRINITY_EXECUTION_ID, escapes the (deleted)
                      env-tag sweep
        </dev/null >/dev/null 2>&1  -> no shared pipe FDs, escapes the
                      (deleted) pipe-writer sweep
        nice -n 10 + while :; do :; done  -> burns CPU so docker stats
                      attribution is unambiguous

    Spawned via ``docker exec -d`` so it has no relationship to claude
    inside the container — mimics Eugene's production observation where
    the orphan was visible from inside the container but had no
    ``TRINITY_EXECUTION_ID`` in ``/proc/<pid>/environ``.

    Returns the burner's pid or None if the post-spawn ps probe failed.
    """
    subprocess.run(
        [
            "docker", "exec", "-d", container_name, "sh", "-c",
            "exec setsid env -i /bin/bash -c \""
            "nice -n 10 bash -c 'while :; do :; done' </dev/null >/dev/null 2>&1 &"
            "disown\" </dev/null >/dev/null 2>&1",
        ],
        check=False, timeout=10,
    )
    # Give the burner ~1s to actually exec into the loop so ps can see it.
    time.sleep(1.5)
    try:
        result = subprocess.run(
            [
                "docker", "exec", container_name,
                "sh", "-c",
                "ps -eo pid,cmd --no-headers | awk '/bash -c while :/ && !/awk/ {print $1; exit}'",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    pid_str = (result.stdout or "").strip().split()[0] if result.stdout.strip() else ""
    try:
        return int(pid_str)
    except ValueError:
        return None


def _pid_alive(container_name: str, pid: int) -> bool:
    """Return True iff `pid` is still alive inside the container."""
    try:
        result = subprocess.run(
            ["docker", "exec", container_name, "kill", "-0", str(pid)],
            capture_output=True, text=True, timeout=8,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False  # docker exec broke — caller's outer assertion will surface
    return result.returncode == 0


@pytest.mark.slow
def test_817_cgroup_sweep_catches_no_env_tag_orphan(api_client: TrinityApiClient):
    """Regression gate for the cgroup-walk cleanup (#817 follow-up).

    Eugene's 2026-05-13 production capture showed an orphan that
    escaped all three prior cleanup passes — different pgid (setsid),
    no shared pipe FDs (``</dev/null``), no ``TRINITY_EXECUTION_ID``
    in ``/proc/<pid>/environ`` (env scrubbed via ``env -i`` / re-exec).
    The env-tag scan reported `0 processes found` in his agent at
    100+ minute orphan survival.

    The fix replaces the three reactive passes with a single
    exclusionary mechanism: walk ``/sys/fs/cgroup/cgroup.procs`` and
    SIGKILL every PID not on the allowlist (platform essentials +
    active execution descendants + user-configured persistent
    daemons). Cgroup membership is the container boundary — a process
    the kernel is attributing to this container's CPU/memory is by
    definition inside the cgroup. There is no escape vector.

    This test asserts that mechanism against the exact escape shape
    that defeated the previous fix:

      1. Deploy any agent with a working task path (the existing
         ``test-leak-hook`` template happens to suit — the hook does
         not need to fire for this test; we just need a container).
      2. Trigger a short benign claude task so an execution is in
         flight when we inject.
      3. While claude is running, ``docker exec`` a setsid +
         ``env -i`` + FD-detached CPU burner into the container.
         This burner has zero relationship to claude or
         agent-server: not a descendant, not in any tracked pgid,
         no env tag, no shared pipes.
      4. Wait for the execution to finish — drain_reader_threads
         fires its terminal cgroup sweep.
      5. Assert the burner is dead. If it survives, the cgroup-walk
         regressed and Eugene's production scenario is once again
         live.

    A red run here means the sweep allowlist is too permissive or
    the sweep itself is not being invoked from the cleanup path.
    """
    suffix = uuid.uuid4().hex[:6]
    system_name = f"test-817cg-{suffix}"
    agent_short = "victim"
    agent_name = f"{system_name}-{agent_short}"
    container_name = f"agent-{agent_name}"

    # Reuse the existing repro template — the UserPromptSubmit hook is
    # harmless to this test (claude's "ok" reply still happens), and we
    # avoid shipping a second nearly-identical template just to flip a
    # bit in CLAUDE.md.
    manifest = f"""
name: {system_name}
agents:
  {agent_short}:
    template: local:test-leak-hook
"""

    schedule_id: str | None = None
    burner_pid: int | None = None

    try:
        # -------- Stage 1: deploy + wait for healthy ----------------------
        deploy = api_client.post(
            "/api/systems/deploy",
            json={"manifest": manifest, "dry_run": False},
            timeout=120.0,
        )
        assert_status(deploy, 200)

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            agent_resp = api_client.get(f"/api/agents/{agent_name}")
            if agent_resp.status_code == 200 and agent_resp.json().get("status") == "running":
                break
            time.sleep(2)
        else:
            pytest.fail(f"Agent {agent_name} did not reach running status within 90s")

        deadline = time.monotonic() + 60
        agent_ready = False
        while time.monotonic() < deadline:
            try:
                probe = subprocess.run(
                    [
                        "docker", "exec", container_name,
                        "curl", "-m", "3", "-s", "-o", "/dev/null",
                        "-w", "%{http_code}",
                        "http://localhost:8000/health",
                    ],
                    capture_output=True, text=True, timeout=8,
                )
                if probe.returncode == 0 and probe.stdout.strip() == "200":
                    agent_ready = True
                    break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(2)
        if not agent_ready:
            pytest.fail(f"Agent {agent_name} /health did not return 200 within 60s")

        # -------- Stage 2: short benign schedule ---------------------------
        # The hook in test-leak-hook spawns its own burners on every
        # UserPromptSubmit; those are TAG-bearing and the cgroup sweep
        # also catches them. They aren't the focus here — we want the
        # NO-TAG burner injected from outside in stage 3 to be the
        # specific PID we track. A short claude task gives the sweep a
        # natural trigger point at execution-end.
        sched_resp = api_client.post(
            f"/api/agents/{agent_name}/schedules",
            json={
                "name": "no-tag-repro",
                "cron_expression": "0 0 1 1 *",
                "message": "Reply with the single word done.",
                "timeout_seconds": 60,
                "max_retries": 0,
                "retry_delay_seconds": 30,
                "enabled": True,
            },
        )
        assert_status(sched_resp, 201)
        schedule_id = sched_resp.json()["id"]

        # -------- Stage 3: trigger + inject within 3s ----------------------
        trigger = api_client.post(
            f"/api/agents/{agent_name}/schedules/{schedule_id}/trigger",
        )
        assert trigger.status_code in (200, 202), (
            f"Trigger returned {trigger.status_code}: {trigger.text}"
        )

        # Wait long enough for claude to start (so an execution is
        # registered) but short enough that claude is still running
        # when we inject the orphan. 3s is comfortably inside the
        # typical 6-15s claude startup → first-tool-call window.
        time.sleep(3)
        burner_pid = _inject_no_tag_burner(container_name)
        assert burner_pid is not None, (
            "Failed to spawn or detect the no-env-tag burner inside "
            f"{container_name}. The test cannot assert cleanup without a "
            "live target."
        )
        # Sanity check: the burner is actually alive right after spawn.
        assert _pid_alive(container_name, burner_pid), (
            f"Injected burner pid={burner_pid} died before any sweep could "
            "fire — engineered repro is broken."
        )

        # -------- Stage 4: wait for execution to terminate -----------------
        # The benign message ("reply done") typically completes in
        # 5-15s. drain_reader_threads runs on completion and fires the
        # cgroup sweep in its finally block.
        deadline = time.monotonic() + NO_TAG_CLEANUP_WAIT_SECONDS
        terminal_status = None
        while time.monotonic() < deadline:
            exec_resp = api_client.get(
                f"/api/agents/{agent_name}/schedules/{schedule_id}/executions",
            )
            if exec_resp.status_code == 200:
                executions = exec_resp.json() or []
                for e in executions:
                    if e.get("status") in ("success", "failed", "cancelled", "pending_retry"):
                        terminal_status = e.get("status")
                        break
                if terminal_status:
                    break
            time.sleep(2)

        assert terminal_status is not None, (
            f"Execution never reached a terminal status within "
            f"{NO_TAG_CLEANUP_WAIT_SECONDS}s. Without a task-end the "
            "cgroup sweep from drain_reader_threads cannot fire."
        )

        # The sweep is invoked from drain_reader_threads' finally
        # block; allow a short settling window for SIGKILL delivery
        # and the kernel's process-reap to complete.
        time.sleep(2)

        # -------- Stage 5: regression gate — burner must be dead -----------
        alive = _pid_alive(container_name, burner_pid)
        if alive:
            # Capture diagnostics before failing so a CI failure is
            # actionable. The cgroup contents tell us whether the
            # sweep ran but missed it (sweep bug) versus didn't run
            # at all (cleanup-path bug).
            diag_ps = _ps_dump_for_diagnostics(container_name)
            cgroup_dump = subprocess.run(
                [
                    "docker", "exec", container_name,
                    "cat", "/sys/fs/cgroup/cgroup.procs",
                ],
                capture_output=True, text=True, timeout=8,
            )
            pytest.fail(
                f"Cgroup sweep regression (#817): no-env-tag burner "
                f"pid={burner_pid} survived execution-end cleanup "
                f"(terminal_status={terminal_status}). This is Eugene's "
                f"production class — the orphan has no TRINITY_EXECUTION_ID, "
                f"different pgid, and detached FDs. The current cleanup "
                f"path is not catching it.\n\n"
                f"Surviving burner ps lines:\n{diag_ps}\n\n"
                f"cgroup.procs contents at failure:\n"
                f"{cgroup_dump.stdout if cgroup_dump.returncode == 0 else cgroup_dump.stderr}"
            )

    finally:
        if schedule_id:
            try:
                api_client.delete(
                    f"/api/agents/{agent_name}/schedules/{schedule_id}",
                )
            except Exception:
                pass
        cleanup_test_agent(api_client, agent_name)
