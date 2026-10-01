"""#2985 — the nightly prod deploy script must fail closed and stay POSIX.

Runs the workflow's OWN ``script:`` (parsed from the YAML, same harness shape as
test_2995) under ``bash``, ``sh`` and ``dash`` with stub ``git`` / ``docker`` /
``sudo`` / ``curl`` / ``sleep`` on ``PATH``. ssh-action runs the script in the
remote login shell, which may be dash, so a bashism there fails every night.

Behaviour asserted, not implementation: the deployed-SHA marker is written only
on the happy path; a failed health probe, a failed dump, or a dump missing its
completion trailer each fail the run with an ``::error::`` and leave the marker
untouched.

No network, no Docker: every external command is a stub.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-prod-nightly.yml"

_SHA = "a" * 40
_PREV = "b" * 40
_SHELLS = ["bash", "sh", "dash"]


def _deploy_script() -> str:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["deploy"]["steps"]:
        with_ = step.get("with") or {}
        if "script" in with_:
            return with_["script"]
    raise AssertionError("deploy job has no ssh-action step carrying a `script:`")


_GIT_STUB = f"""\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/git.log"
[ "$1" = "-C" ] && shift 2
case "$1" in
  rev-parse)
    case "$2" in --short=8) echo {_SHA[:8]} ;; *) echo {_SHA} ;; esac ;;
  log) echo "{_SHA[:8]} stub commit" ;;
  *) exit 0 ;;
esac
"""

# DUMP_MODE: ok (trailer present), fail (pg_dump exits 1 mid-stream),
# truncated (exits 0 but no trailer).
_DOCKER_STUB = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/docker.log"
case "$1" in
  exec)
    case "$*" in
      *psql*) echo 0 ;;
      *pg_dump*)
        echo "-- partial dump"
        case "$DUMP_MODE" in
          fail) exit 1 ;;
          truncated) exit 0 ;;
          *) echo "-- PostgreSQL database dump complete" ;;
        esac ;;
    esac ;;
  compose|rm) exit 0 ;;
  ps) exit 0 ;;
  inspect)
    case "$*" in
      *StartedAt*) echo 2026-01-01T00:00:00Z ;;
      *State.Health.Status*) echo healthy ;;
      *) echo "running health=healthy restarts=0" ;;
    esac ;;
  logs)
    echo "Trinity Enterprise modules registered"
    echo "ERROR: connect to internal-db.example:5432 refused" ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
"""

_CURL_STUB = """\
#!/bin/sh
[ "$CURL_MODE" = ok ] && exit 0
exit 22
"""

_SUDO_STUB = '#!/bin/sh\nexec "$@"\n'
_SLEEP_STUB = "#!/bin/sh\nexit 0\n"


@dataclass
class Run:
    rc: int
    stdout: str
    stderr: str
    marker: str | None

    def explain(self) -> str:
        return f"rc={self.rc}\nstdout:\n{self.stdout}\nstderr:\n{self.stderr}"


def _run(shell: str, tmp_path: Path, dump: str = "ok", curl: str = "ok") -> Run:
    exe = shutil.which(shell)
    if exe is None:
        pytest.skip(f"{shell} not available")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in {
        "git": _GIT_STUB,
        "docker": _DOCKER_STUB,
        "curl": _CURL_STUB,
        "sudo": _SUDO_STUB,
        "sleep": _SLEEP_STUB,
    }.items():
        p = stubs / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    (tmp_path / "trinity").mkdir()
    marker = tmp_path / ".trinity-last-deployed-sha"
    marker.write_text(_PREV + "\n")
    proc = subprocess.run(
        [exe, "-c", _deploy_script()],
        env={
            "PATH": f"{stubs}:/usr/bin:/bin",
            "STUB_DIR": str(stubs),
            "DUMP_MODE": dump,
            "CURL_MODE": curl,
            "HOME": str(tmp_path),
            "LC_ALL": "C",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return Run(proc.returncode, proc.stdout, proc.stderr, marker.read_text().strip())


@pytest.mark.parametrize("shell", _SHELLS)
def test_happy_path_deploys_and_records_the_sha(shell, tmp_path):
    r = _run(shell, tmp_path)
    assert r.rc == 0, r.explain()
    assert "::error::" not in r.stdout, r.explain()
    assert "Backend: OK" in r.stdout, r.explain()
    assert r.marker == _SHA, r.explain()


@pytest.mark.parametrize("shell", _SHELLS)
def test_failed_health_probe_fails_and_keeps_marker(shell, tmp_path):
    r = _run(shell, tmp_path, curl="fail")
    assert r.rc != 0, r.explain()
    assert "::error::" in r.stdout, r.explain()
    assert r.marker == _PREV, r.explain()
    assert "internal-db.example" not in r.stdout, "raw backend log line reached the public run log\n" + r.explain()


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("dump", ["fail", "truncated"])
def test_bad_dump_blocks_the_deploy(shell, dump, tmp_path):
    r = _run(shell, tmp_path, dump=dump)
    assert r.rc != 0, r.explain()
    assert "incomplete -- not deploying" in r.stdout, r.explain()
    assert "=== Checkout ===" not in r.stdout, r.explain()
    assert r.marker == _PREV, r.explain()
