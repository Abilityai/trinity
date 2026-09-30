"""#3103 — scheduled nightlies must not run fork-PR code or trust what it wrote.

Both nightlies fetch `pull/N/head`, merge it into `dev` and run the merged tree
in a scheduled job. A scheduled job runs in the `main` context: it skips
GitHub's approve-and-run gate for first-time contributors, and its cache saves
land in the `main` scope the required PR checks restore from. PR code in that
job can also forge anything the job writes, including artifacts.

Pinned here, per workflow that both fetches PR heads AND installs/executes the
merged tree:

  * every PR listing requests `isCrossRepository` and forks are filtered out
    with a visible warning (the single-PR dispatch path included — without the
    field `select(.isCrossRepository | not)` passes a fork through);
  * no `setup-python` step enables the pip cache.

And for each nightly's write-token `comment` job:

  * leg identity comes from the trusted `discover` matrix via
    `legsFromMatrix`, never from artifact contents or file names;
  * artifacts are extracted outside the workspace root, where the trusted
    verdict module lives;
  * (backend-unit-nightly) the empty-sweep failure counts ACCEPTED legs, so a
    sweep whose every status was rejected cannot pass green.

`alembic-head-watch.yml` also fetches PR heads but never installs or executes
them (it runs a trusted script over `git merge-tree`), so it is out of scope.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

_WF_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_UNIT = _WF_DIR / "backend-unit-nightly.yml"


def _uncommented(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(("#", "//"))
    )


def _runs_pr_code(text: str) -> bool:
    code = _uncommented(text)
    return "pull/" in code and "pip install" in code


def _pr_code_workflows() -> list[Path]:
    return sorted(p for p in _WF_DIR.glob("*.yml") if _runs_pr_code(p.read_text()))


def _jobs(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["jobs"]


def test_both_nightlies_are_in_scope():
    """If the scope detector goes blind, every other test here passes vacuously."""
    names = {p.name for p in _pr_code_workflows()}
    assert {"backend-unit-nightly.yml", "integration-nightly.yml"} <= names, names


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_every_pr_listing_requests_the_fork_field(wf):
    discover = _uncommented(_jobs(wf)["discover"]["steps"][0]["run"])
    listings = re.findall(r"gh pr (?:list|view)[^|)]*?--json\s+([\w,]+)", discover, re.S)
    assert listings, "no gh pr list/view found in discover"
    for fields in listings:
        assert "isCrossRepository" in fields.split(","), (
            f"`--json {fields}` omits isCrossRepository — a fork PR then passes "
            "`select(.isCrossRepository | not)` because the field is null"
        )


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_forks_are_filtered_visibly(wf):
    discover = _uncommented(_jobs(wf)["discover"]["steps"][0]["run"])
    assert "select(.isCrossRepository | not)" in discover
    assert "::warning::skipping fork PR" in discover, (
        "a skipped fork must be logged — a silent skip looks like a pass"
    )


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_no_setup_python_step_enables_the_pip_cache(wf):
    for job_name, job in _jobs(wf).items():
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/setup-python"):
                assert "cache" not in (step.get("with") or {}), (
                    f"{wf.name}:{job_name} caches pip in a job that runs PR code; "
                    "its post-step saves a main-scoped cache after that code ran"
                )


_ARTIFACT_DIRS = {
    "backend-unit-nightly.yml": "nightly-artifacts",
    "integration-nightly.yml": "integration-artifacts",
}


def _comment_steps(wf: Path) -> list[dict]:
    return _jobs(wf)["comment"]["steps"]


def _post_step(wf: Path) -> dict:
    return next(s for s in _comment_steps(wf) if "github-script" in str(s.get("uses", "")))


def test_every_pr_code_workflow_has_a_known_artifact_dir():
    assert {p.name for p in _pr_code_workflows()} <= set(_ARTIFACT_DIRS)


def test_dispatch_input_is_not_interpolated_into_the_script():
    discover = _jobs(_UNIT)["discover"]["steps"][0]
    assert "inputs.pr_number" not in discover["run"]
    assert "inputs.pr_number" in discover["env"]["ONLY_PR"]


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_artifacts_are_not_extracted_into_the_workspace_root(wf):
    dl = next(s for s in _comment_steps(wf)
              if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert dl["with"].get("path") == _ARTIFACT_DIRS[wf.name], (
        "extracting into the root lets a forged artifact overwrite "
        "scripts/ci/nightly-verdict.js before it is required"
    )
    assert not dl["with"].get("merge-multiple"), (
        "merge-multiple flattens legs into one directory, last writer wins"
    )


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_a_lone_artifact_is_still_found(wf):
    """download-artifact extracts into `<path>/<name>/` only when the pattern
    matched more than one artifact (`artifacts.length === 1 ? resolvedPath`
    in its source). With one open PR the named directory never exists, and
    without the fallback the comment job finds nothing and says nothing."""
    code = _uncommented(_post_step(wf)["with"]["script"])
    assert "fs.existsSync(d) ? d : artDir" in code


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_the_verdict_module_is_checked_out_from_this_commit(wf):
    co = next(s for s in _comment_steps(wf)
              if str(s.get("uses", "")).startswith("actions/checkout"))
    assert co["with"]["ref"] == "${{ github.sha }}"
    assert co["with"]["persist-credentials"] is False


@pytest.mark.parametrize("wf", _pr_code_workflows(), ids=lambda p: p.name)
def test_leg_identity_comes_from_the_trusted_matrix(wf):
    post = _post_step(wf)
    assert post["env"]["MATRIX"] == "${{ needs.discover.outputs.matrix }}"
    code = _uncommented(post["with"]["script"])
    assert "legsFromMatrix(" in code
    assert "readdirSync" not in code and "status-pr*.json" not in code, (
        "enumerating artifact files lets PR code choose which PRs get a verdict"
    )


def test_an_all_rejected_sweep_fails_the_run():
    code = _uncommented(_post_step(_UNIT)["with"]["script"])
    guard = code.index("parsed.length === 0")
    assert code.index("legsFromMatrix(") < guard < code.index("core.setFailed")
