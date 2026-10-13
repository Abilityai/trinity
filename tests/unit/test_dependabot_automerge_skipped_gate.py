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

# `gh api … --jq` prints one "<name>\t<status>\t<conclusion>" row per matching
# check-run. The first poll answers GH_ROWS; every later poll answers
# GH_ROWS_LATER (when set), so a test can model a check-run that appears or
# completes between polls -- and prove the gate did not decide on the first.
_GH_STUB = """\
#!/bin/sh
n=$(cat "$GH_CALLS" 2>/dev/null || echo 0)
echo $((n + 1)) > "$GH_CALLS"
if [ "$n" -gt 0 ] && [ -n "$GH_ROWS_LATER" ]; then printf '%b' "$GH_ROWS_LATER"; else printf '%b' "$GH_ROWS"; fi
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


def _run_gate(tmp_path: Path, rows: str, later: str = "") -> subprocess.CompletedProcess[str]:
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
        "GH_ROWS_LATER": later,
        "GH_CALLS": str(tmp_path / "gh_calls"),
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
        "build\\tcompleted\\tsuccess\\n"
        "pytest (base, inline fallback)\\tcompleted\\tskipped\\n"
        "pytest (head, seed 12345)\\tcompleted\\tsuccess\\n"
        "pytest (push, absolute failures)\\tcompleted\\tskipped\\n"
        "regression diff\\tcompleted\\tsuccess\\n"
    )
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "build + pytest green" in r.stdout
    assert "gating check failed" not in r.stdout


def test_a_real_failure_still_blocks(tmp_path: Path) -> None:
    """Exempting `skipped` must not exempt anything else."""
    rows = ("build\\tcompleted\\tsuccess\\n"
            "pytest (base, inline fallback)\\tcompleted\\tskipped\\n"
            "regression diff\\tcompleted\\tfailure\\n")
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "a gating check failed" in r.stdout


@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
def test_other_non_success_conclusions_still_block(tmp_path: Path, conclusion: str) -> None:
    rows = f"build\\tcompleted\\tsuccess\\nregression diff\\tcompleted\\t{conclusion}\\n"
    r = _run_gate(tmp_path, rows)
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_missing_regression_diff_is_pending_not_green(tmp_path: Path) -> None:
    """`regression diff` needs the pytest jobs, so GitHub creates its check-run
    only once `pytest (head)` finishes. In that window every row present is
    green or skipped; a gate that reads "nothing pending" there auto-merges a
    bump whose unit verdict it never saw. It must poll again -- here the diff
    then appears red, and that is the answer the gate must give."""
    early = (
        "build\\tcompleted\\tsuccess\\n"
        "pytest (base, inline fallback)\\tcompleted\\tskipped\\n"
        "pytest (head, seed 12345)\\tcompleted\\tsuccess\\n"
    )
    r = _run_gate(tmp_path, early, later=early + "regression diff\\tcompleted\\tfailure\\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "build + pytest green" not in r.stdout
    assert int((tmp_path / "gh_calls").read_text()) >= 2


def test_all_skipped_is_not_green(tmp_path: Path) -> None:
    """Exempting `skipped` from *failed* must not make it count as *passed*:
    a poll where every gating row is skipped proves nothing ran."""
    skipped = ("build\\tcompleted\\tskipped\\n"
               "regression diff\\tcompleted\\tskipped\\n")
    r = _run_gate(tmp_path, skipped, later="build\\tcompleted\\tsuccess\\nregression diff\\tcompleted\\tfailure\\n")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "build + pytest green" not in r.stdout
    assert int((tmp_path / "gh_calls").read_text()) >= 2


def _gate_jq_filter() -> str:
    """The `--jq` expression the step hands to `gh api`, as written in the workflow."""
    script = _gate_script()
    start = script.index("--jq '") + len("--jq '")
    return script[start:script.index("'", start)]


def test_the_regression_verdict_is_a_gating_check() -> None:
    """`pytest (head)` runs its suite with `|| true` and reports green regardless;
    the regression verdict is the `regression diff` job. A gate that does not
    select it would auto-merge a patch/minor bump that breaks unit tests the
    moment the skipped-row fix above let the gate pass at all. The workflow's
    own filter runs under real jq here, with only its output projection swapped
    for the check name."""
    import json
    import shutil

    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not on PATH")
    projection = '| "\\(.name)\\t\\(.status)\\t\\(.conclusion)"'
    flt = _gate_jq_filter()
    assert flt.endswith(projection), flt
    names = ("build", "pytest (head, seed 12345)", "regression diff",
             "Analyze (python)", "secret-scan", "journey-smoke")
    payload = {"check_runs": [{"name": n, "status": "completed", "conclusion": "success"} for n in names]}
    selected = set(subprocess.run(
        [jq, "-r", flt[: -len(projection)] + "| .name"],
        input=json.dumps(payload), capture_output=True, text=True, check=True,
    ).stdout.splitlines())
    assert selected == {"build", "pytest (head, seed 12345)", "regression diff"}
