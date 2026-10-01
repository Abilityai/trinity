"""The Dependabot auto-merge gate must not count a by-design *skipped* check as a failure.

``dependabot-auto-merge.yml`` waits on every check-run named ``build`` or ``pytest*``
and read::

    [[ "$status" == "completed" && "$conclusion" != "success" ]] && failed=1

Since the #2945 tiering every PR carries ``pytest (base, inline fallback)`` and
``pytest (push, absolute failures)`` as ``completed / skipped`` — they exist to run
on other events — so the gate answered "a gating check failed" on every Dependabot
PR from mid-September on, with ``build`` and ``pytest (head)`` green beside them.
The merge queue filled with green patch/minor bumps nobody had looked at.

The step is executed here under bash with ``gh`` stubbed to answer the rows, the
same shape as ``test_2995_deploy_health_gate.py``: the script that runs on the
runner is the script under test, not a restatement of it.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "dependabot-auto-merge.yml"
_STEP = "Wait for build + pytest to pass"

# `gh api … --jq` prints one "<status>\t<conclusion>" row per matching check-run.
_GH_STUB = """\
#!/bin/sh
printf '%b' "$GH_ROWS"
"""

_SLEEP_STUB = """\
#!/bin/sh
exit 0
"""


def _bash() -> str:
    """A bash with `mapfile` (4+). ubuntu-latest has it; macOS's /bin/bash is 3.2."""
    for cand in ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"):
        try:
            ok = subprocess.run(
                [cand, "-c", "type mapfile"], capture_output=True, timeout=10, check=False,
            ).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return cand
    pytest.skip("no bash >= 4 (mapfile) on PATH; the gate script needs one")


def _gate_script() -> str:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == _STEP:
                return step["run"]
    raise AssertionError(f"no step named {_STEP!r} in {_WORKFLOW.name}")


def _run_gate(tmp_path: Path, rows: str) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name, body in (("gh", _GH_STUB), ("sleep", _SLEEP_STUB)):
        p = stub_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "GH_ROWS": rows,
        "GH_TOKEN": "stub",
        "REPO": "example/repo",
        "SHA": "0" * 40,
    }
    return subprocess.run(
        [_bash(), "-e", "-c", _gate_script()],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_by_design_skipped_pytest_jobs_do_not_block(tmp_path: Path) -> None:
    """The live shape on every PR: build + head seed green, two tier jobs skipped."""
    rows = (
        "completed\\tsuccess\\n"      # build
        "completed\\tskipped\\n"      # pytest (base, inline fallback)
        "completed\\tsuccess\\n"      # pytest (head, seed 12345)
        "completed\\tskipped\\n"      # pytest (push, absolute failures)
    )
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "build + pytest green" in r.stdout
    assert "gating check failed" not in r.stdout


def test_a_real_failure_still_blocks(tmp_path: Path) -> None:
    """Exempting `skipped` must not exempt anything else."""
    rows = "completed\\tsuccess\\ncompleted\\tskipped\\ncompleted\\tfailure\\n"
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "a gating check failed" in r.stdout


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
def test_other_non_success_conclusions_still_block(tmp_path: Path, conclusion: str) -> None:
    rows = f"completed\\tsuccess\\ncompleted\\t{conclusion}\\n"
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 1, r.stdout + r.stderr
