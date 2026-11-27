"""The agent container lives as long as its agent server (#4412).

`startup.sh` is the container's main process. It started the agent server in
the background and ended in `tail -f /dev/null`, so a server that exited (or
never started, #3106) left a container Docker reported as Up: the restart
policy never fired and every dispatch failed until someone restarted the agent
by hand. The image declared no healthcheck either.

The shell below is the shipped text, run under bash: the block that starts the
server and the block that ends the script are cut out of `startup.sh` and
executed with `python3` replaced by a stand-in server on `PATH`. The image's
healthcheck command is cut out of the Dockerfile and run the same way with a
stand-in `curl`.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
]

_IMAGE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
_STARTUP = _IMAGE / "startup.sh"
_DOCKERFILE = _IMAGE / "Dockerfile"

_START_MARK = "# Start Agent Web Server (self-contained UI)"
_AFTER_START_MARK = "# === Persistent Setup Script ==="
_END_MARK = "# === The container lives as long as the agent server (#4412) ==="


def _blocks() -> tuple[str, str]:
    text = _STARTUP.read_text()
    assert text.count(_START_MARK) == 1 and text.count(_END_MARK) == 1
    start = text[text.index(_START_MARK):text.index(_AFTER_START_MARK)]
    end = text[text.index(_END_MARK):]
    return start, end


def _script(tmp_path: Path, *, server: str, between: str = "") -> Path:
    """The two shipped blocks around `between`, with `python3` on PATH being a
    stand-in agent server whose body is `server`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    fake.write_text(f"#!/bin/bash\n{server}\n")
    fake.chmod(0o755)
    start, end = _blocks()
    script = tmp_path / "startup-under-test.sh"
    script.write_text(
        "#!/bin/bash\n"
        f'export PATH="{bin_dir}:$PATH"\n'
        f"{start}\n{between}\n{end}\n"
    )
    return script


def _run(script: Path, *, ui: str = "true", timeout: float = 10.0):
    env = dict(os.environ, ENABLE_AGENT_UI=ui)
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# The server's exit ends the container
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", [1, 3, 137])
def test_a_server_that_dies_ends_the_script_with_its_exit_code(tmp_path, code):
    result = _run(_script(tmp_path, server=f"sleep 0.2; exit {code}"))

    assert result.returncode == code
    assert f"agent server exited (code {code})" in result.stdout


def test_a_server_that_exits_cleanly_still_ends_the_script_non_zero(tmp_path):
    """Exit 0 from the main process would read as a deliberate stop."""
    result = _run(_script(tmp_path, server="sleep 0.2; exit 0"))

    assert result.returncode == 1
    assert "agent server exited (code 0)" in result.stdout


def test_a_server_that_never_starts_ends_the_script(tmp_path):
    """#3106's shape: the server fails at import, long before the last block."""
    result = _run(_script(tmp_path, server="exit 2", between="sleep 0.4"))

    assert result.returncode == 2


def test_the_script_stays_up_while_the_server_runs(tmp_path):
    script = _script(tmp_path, server="exec sleep 30")
    proc = subprocess.Popen(
        ["bash", str(script)], env=dict(os.environ, ENABLE_AGENT_UI="true"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        time.sleep(1.0)
        assert proc.poll() is None, "the script ended while its server was running"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def test_the_script_ends_when_a_running_server_is_killed(tmp_path):
    pidfile = tmp_path / "server.pid"
    script = _script(tmp_path, server=f'echo $$ > "{pidfile}"; exec sleep 30')
    proc = subprocess.Popen(
        ["bash", str(script)], env=dict(os.environ, ENABLE_AGENT_UI="true"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 5
        while not pidfile.exists() or not pidfile.read_text().strip():
            assert time.time() < deadline, "the stand-in server never started"
            time.sleep(0.05)
        time.sleep(0.3)
        assert proc.poll() is None
        os.kill(int(pidfile.read_text()), signal.SIGKILL)

        assert proc.wait(timeout=5) == 137
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Work after the server starts still runs
# ---------------------------------------------------------------------------

def test_steps_after_the_server_starts_run_while_it_is_up(tmp_path):
    """Templates rely on the setup hook and plugin restore that follow the
    server's start. They must finish before the script waits."""
    marker = tmp_path / "post-start-ran"
    script = _script(
        tmp_path, server="sleep 1; exit 5",
        between=f'sleep 0.2; echo done > "{marker}"',
    )

    result = _run(script)

    assert marker.read_text().strip() == "done"
    assert result.returncode == 5


def test_steps_after_a_server_that_already_died_still_run(tmp_path):
    marker = tmp_path / "post-start-ran"
    script = _script(
        tmp_path, server="exit 4", between=f'sleep 0.3; echo done > "{marker}"',
    )

    result = _run(script)

    assert marker.exists()
    assert result.returncode == 4


# ---------------------------------------------------------------------------
# An agent started without a server keeps the old behaviour
# ---------------------------------------------------------------------------

def test_without_the_server_the_container_is_kept_alive(tmp_path):
    started = tmp_path / "server-started"
    script = _script(tmp_path, server=f'touch "{started}"; exit 9')
    proc = subprocess.Popen(
        ["bash", str(script)], env=dict(os.environ, ENABLE_AGENT_UI="false"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        time.sleep(1.0)
        assert proc.poll() is None, "nothing kept the container alive"
        assert not started.exists(), "the server was started with the UI off"
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# The image's healthcheck
# ---------------------------------------------------------------------------

def _healthcheck() -> tuple[dict, str]:
    """(options, shell command) of the Dockerfile's single HEALTHCHECK."""
    text = _DOCKERFILE.read_text().replace("\\\n", " ")
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("HEALTHCHECK")]
    assert len(lines) == 1, f"expected one HEALTHCHECK, found {len(lines)}"
    head, command = lines[0].split(" CMD ", 1)
    options = dict(re.findall(r"--([a-z-]+)=(\S+)", head))
    return options, command.strip()


def _run_healthcheck(tmp_path: Path, *, curl_exit: int, env: dict):
    bin_dir = tmp_path / "hc-bin"
    bin_dir.mkdir()
    log = tmp_path / "curl-args"
    curl = bin_dir / "curl"
    curl.write_text(f'#!/bin/bash\nprintf "%s\\n" "$@" > "{log}"\nexit {curl_exit}\n')
    curl.chmod(0o755)
    _, command = _healthcheck()
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", **env}
    result = subprocess.run(
        ["/bin/sh", "-c", command], env=full_env, capture_output=True, text=True, timeout=10,
    )
    args = log.read_text().split("\n") if log.exists() else None
    return result.returncode, args


def test_the_image_declares_one_healthcheck_after_the_user_switch():
    text = _DOCKERFILE.read_text()

    assert text.count("\nHEALTHCHECK ") == 1
    assert text.index("\nUSER developer") < text.index("\nHEALTHCHECK ") < text.index('\nCMD ["/app/startup.sh"]')


def test_healthcheck_is_healthy_when_the_server_answers(tmp_path):
    code, args = _run_healthcheck(tmp_path, curl_exit=0, env={"ENABLE_AGENT_UI": "true"})

    assert code == 0
    assert "http://127.0.0.1:8000/health" in args


def test_healthcheck_is_unhealthy_when_the_server_does_not_answer(tmp_path):
    code, _ = _run_healthcheck(tmp_path, curl_exit=7, env={"ENABLE_AGENT_UI": "true"})

    assert code == 1


def test_healthcheck_follows_the_configured_port(tmp_path):
    _, args = _run_healthcheck(
        tmp_path, curl_exit=0, env={"ENABLE_AGENT_UI": "true", "AGENT_SERVER_PORT": "9123"},
    )

    assert "http://127.0.0.1:9123/health" in args


@pytest.mark.parametrize("env", [{"ENABLE_AGENT_UI": "false"}, {}])
def test_healthcheck_is_healthy_and_asks_nothing_without_a_server(tmp_path, env):
    code, args = _run_healthcheck(tmp_path, curl_exit=7, env=env)

    assert code == 0
    assert args is None, "the server was probed though none is started"


def test_healthcheck_curl_fails_on_an_http_error_and_is_bounded(tmp_path):
    """`-f` turns a 5xx into a failure; the time limit sits inside Docker's own."""
    options, _ = _healthcheck()
    _, args = _run_healthcheck(tmp_path, curl_exit=0, env={"ENABLE_AGENT_UI": "true"})

    assert any(a.startswith("-") and "f" in a and not a.startswith("--") for a in args)
    limit = float(args[args.index("--max-time") + 1])
    assert limit < float(options["timeout"].rstrip("s"))


def test_the_start_period_covers_boot_before_the_server_is_launched():
    options, _ = _healthcheck()

    assert float(options["start-period"].rstrip("s")) >= 120
    assert int(options["retries"]) >= 2


def test_the_probed_route_is_one_the_agent_server_serves_without_auth():
    """The check runs inside the container with no credential."""
    info = (_IMAGE / "agent_server" / "routers" / "info.py").read_text()
    assert re.search(r'@router\.get\("/health"\)', info)
