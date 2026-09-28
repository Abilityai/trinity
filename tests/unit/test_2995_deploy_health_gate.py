"""#2995 — Deploy to Dev's post-deploy backend health check must be a gate, not a print.

The ``=== Health ===`` section of the dev deploy script read::

    sleep 10
    curl -sf http://localhost:8000/health && echo "Backend: OK"

under ``set -e``. A command on the LEFT of ``&&`` is exempt from ``set -e``, so a
failing probe skipped the ``echo`` and the script carried on: the run went green
and the ``notify-failure`` job (#2204) never filed its incident. ``docker compose
up -d`` in ``=== Restart ===`` already waits for the backend to be Docker-healthy
(its dependents declare ``depends_on: service_healthy``), so the window this closes
is "Docker saw it healthy, then the host ``:8000`` probe fails" — but the stated
check has to be true either way.

The test runs the workflow's OWN Health section verbatim (sliced out of the parsed
YAML, like test_2578) under ``bash`` and ``sh`` with stub ``curl`` / ``sleep`` /
``sudo`` / ``docker`` on ``PATH``, and asserts behaviour, not implementation:

  * a never-answering backend (curl's real ``-f`` exit 22 on a 503, exit 7 on a
    refused connection) fails the run with an ``::error::`` and no ``Backend: OK``,
    and every probe is time-bounded (the #1230 GIL-stall property);
  * a stale enterprise tree (``ENT_STALE=1``) is still announced, because the new
    exit pre-empts the later #2578 STALE announcement;
  * a backend that answers on a later probe, or at once, passes — the latter with
    no wasted sleep.

Teeth: the pre-fix lines, built from a LITERAL (never a ``str.replace`` on the
live slice, which would silently re-run the new block if it matched nothing), go
green on the same never-answering stub — the defect reproduces.

No network, no Docker: every external command is a stub.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-dev.yml"

_START = 'echo "=== Health ==="'
_END = 'echo "=== Backend boot log ==="'

# The origin/dev lines this issue replaces, verbatim (the teeth control).
_PRE_FIX_SLICE = """\
echo "=== Health ==="
sleep 10
curl -sf http://localhost:8000/health && echo "Backend: OK"
SCHED=$(sudo docker inspect trinity-scheduler --format='{{.State.Health.Status}}')
echo "Scheduler: $SCHED"
[ "$SCHED" = "healthy" ] || echo "WARNING: scheduler not healthy yet"
"""

_SHELLS = ["bash", "sh"]


def _deploy_script() -> str:
    """The shell that actually runs on the dev VM (same shape as test_2204/test_2578)."""
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["deploy"]["steps"]:
        with_ = step.get("with") or {}
        if "script" in with_:
            return with_["script"]
    raise AssertionError("deploy job has no ssh-action step carrying a `script:`")


def _health_slice() -> str:
    """Lines from the Health header up to (not including) the boot-log header.

    Each anchor must occur exactly once and in order, and the slice must carry the
    probe: a reshaped script fails loudly here instead of slicing to nothing.
    """
    lines = [l.strip() for l in _deploy_script().splitlines()]
    starts = [i for i, l in enumerate(lines) if l == _START]
    ends = [i for i, l in enumerate(lines) if l == _END]
    assert len(starts) == 1, f"expected exactly one {_START!r}, found {len(starts)}"
    assert len(ends) == 1, f"expected exactly one {_END!r}, found {len(ends)}"
    a, b = starts[0], ends[0]
    assert a < b, "the Health section must precede the Backend boot log section"
    block = "\n".join(lines[a:b])
    assert block.strip() and "localhost:8000/health" in block, "Health slice lost its probe"
    return block + "\n"


_CURL_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/curl.log"
n=$(cat "$STUB_DIR/curl.count" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STUB_DIR/curl.count"
case "$CURL_MODE" in
  ok) echo '{"status":"healthy"}'; exit 0 ;;
  503) echo "curl: (22) The requested URL returned error: 503" >&2; exit 22 ;;
  refused) echo "curl: (7) Failed to connect to localhost port 8000: Connection refused" >&2; exit 7 ;;
  third)
    if [ "$n" -ge 3 ]; then echo '{"status":"healthy"}'; exit 0; fi
    echo "curl: (22) The requested URL returned error: 503" >&2; exit 22 ;;
  *) echo "unknown CURL_MODE=$CURL_MODE" >&2; exit 99 ;;
esac
"""

_SLEEP_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/sleep.log"
exit 0
"""

_SUDO_STUB = """\
#!/bin/sh
exec "$@"
"""

# `inspect` answers for ANY container: the scheduler line calls it too, and a
# missing answer would exit non-zero for a reason unrelated to the backend.
_DOCKER_STUB = """\
#!/bin/sh
case "$1" in
  inspect) echo "running health=healthy restarts=0" ;;
  logs) echo "ERROR: stub backend log line" ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""


@dataclass
class Run:
    rc: int
    stdout: str
    stderr: str
    curl_calls: list[str]
    sleeps: int


def _run(shell: str, block: str, mode: str, tmp_path: Path, ent_stale: str = "0") -> Run:
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not available")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in {
        "curl": _CURL_STUB,
        "sleep": _SLEEP_STUB,
        "sudo": _SUDO_STUB,
        "docker": _DOCKER_STUB,
    }.items():
        p = stubs / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "STUB_DIR": str(stubs),
        "CURL_MODE": mode,
        "ENT_STALE": ent_stale,
        "HOME": str(tmp_path),
        "LC_ALL": "C",
    }
    proc = subprocess.run(
        [exe, "-c", "set -e\n" + block],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    log = stubs / "curl.log"
    sleeps = stubs / "sleep.log"
    return Run(
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        curl_calls=log.read_text().splitlines() if log.exists() else [],
        sleeps=len(sleeps.read_text().splitlines()) if sleeps.exists() else 0,
    )


def _explain(r: Run) -> str:
    return f"rc={r.rc}\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}\ncurl calls: {r.curl_calls}"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("mode", ["503", "refused"])
def test_a_backend_that_never_answers_fails_the_run(shell, mode, tmp_path):
    r = _run(shell, _health_slice(), mode, tmp_path)
    assert r.rc != 0, "a failed /health probe must fail the deploy\n" + _explain(r)
    assert "::error::" in r.stdout, _explain(r)
    assert "Backend: OK" not in r.stdout, _explain(r)
    assert r.curl_calls, "the curl stub was never invoked — the probe did not run\n" + _explain(r)
    assert all("--max-time" in c for c in r.curl_calls), (
        "every /health probe must be time-bounded (#1230)\n" + _explain(r)
    )
    assert "STALE" not in r.stdout, _explain(r)


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_stale_enterprise_tree_is_still_announced_on_health_failure(shell, tmp_path):
    r = _run(shell, _health_slice(), "503", tmp_path, ent_stale="1")
    assert r.rc != 0, _explain(r)
    assert any(l.startswith("::error::") and "STALE" in l for l in r.stdout.splitlines()), (
        "the Health exit pre-empts the #2578 STALE announcement, so it must print it\n" + _explain(r)
    )


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_backend_that_answers_on_a_later_probe_passes(shell, tmp_path):
    r = _run(shell, _health_slice(), "third", tmp_path)
    assert r.rc == 0, _explain(r)
    assert "Backend: OK" in r.stdout, _explain(r)
    assert "::error::" not in r.stdout, _explain(r)
    assert "Scheduler:" in r.stdout, "the section must carry on past a passing probe\n" + _explain(r)


@pytest.mark.parametrize("shell", _SHELLS)
def test_a_healthy_backend_passes_without_waiting(shell, tmp_path):
    r = _run(shell, _health_slice(), "ok", tmp_path)
    assert r.rc == 0, _explain(r)
    assert "Backend: OK" in r.stdout, _explain(r)
    assert r.sleeps == 0, "no wasted wait on the healthy path\n" + _explain(r)


@pytest.mark.parametrize("shell", _SHELLS)
def test_pre_fix_lines_go_green_on_a_dead_backend(shell, tmp_path):
    """The control: the origin/dev lines exit 0 against the same never-answering
    stub, i.e. the fixture reproduces #2995 and the tests above can bite."""
    r = _run(shell, _PRE_FIX_SLICE, "503", tmp_path)
    assert r.curl_calls, _explain(r)
    assert r.rc == 0, _explain(r)
    assert "Backend: OK" not in r.stdout, _explain(r)
