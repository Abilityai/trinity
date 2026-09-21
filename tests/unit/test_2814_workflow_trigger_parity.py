"""#2814 — a workflow trigger that GitHub registers from the DEFAULT branch is
inert on `dev` until it reaches `main`, and nothing says so.

`Issue Status on PR Merge` stopped running the moment #2769 landed on `dev`.
Not red — *absent*: zero runs across six merges, three of them carrying a valid
`Fixes #N`, every one of those issues left in `status-in-progress` until a human
moved it. The file was fine. #2769 changed its trigger from `pull_request` to
`pull_request_target`, and GitHub registers `pull_request_target` from the
default branch (`main`, whose copy still said `pull_request`) while it EXECUTES
the base branch's copy (`dev`, which no longer said `pull_request`). Neither
copy could start a run. The evidence was zero `pull_request_target` events
repo-wide: if `dev`'s copy were being consulted the runs would exist and fail,
not never appear.

The class is wider than one file, and this repo already knew it twice: the
`schedule` triggers on `deploy-dev.yml` and `alembic-head-watch.yml` each carry
a comment saying "`main` only" / "starts firing once this file reaches `main`".
The `pull_request_target` case is the same rule unrecognised. So the rule is
stated ONCE, here, as a guard:

    every default-branch-registered event a workflow declares on this branch
    must ALSO be declared by `main`'s copy of that workflow — or the delay
    must be a recorded decision in `ACCEPTED_UNTIL_RELEASE` with a reason.

Only the events GitHub registers from the default branch are checked.
`push` / `pull_request` / `pull_request_review*` are read from the ref being
built, so every PR exercises them and a divergence there is visible the day it
lands. The set below is from "Events that trigger workflows" (each entry's
docs say the workflow "runs on the default branch" / "must be in the default
branch" / "only trigger[s] ... if the workflow file is on the default branch"),
plus `pull_request_target`, which the docs describe only as "runs in the
context of the base of the pull request" and which #2814 established
EMPIRICALLY needs default-branch registration — the six-merge, zero-run record
above is the proof, and the docs are not.

`main`'s copy is read with `git show`; the ref is fetched shallowly if the
clone lacks it (CI checks out at `fetch-depth: 1` — a public repo, so an
anonymous fetch works). If `main` cannot be read at all the file SKIPS, loudly:
an offline clone is not evidence of parity, and a guard that fails on a
network blip would redden unrelated PRs (the #2019 class), so the honest
outcome is "unverified", stated as such.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
DEFAULT_BRANCH = "main"

# Events whose workflow GitHub registers from the DEFAULT branch. A workflow
# declaring one of these on a non-default branch does nothing until the file
# reaches `main` — silently, since a run that never starts is not red.
DEFAULT_BRANCH_REGISTERED_EVENTS: frozenset[str] = frozenset({
    "schedule",
    "workflow_dispatch",
    "repository_dispatch",
    "workflow_run",
    "pull_request_target",   # empirical — see module docstring
    "issues",
    "issue_comment",
    "label",
    "milestone",
    "release",
    "discussion",
    "discussion_comment",
    "fork",
    "watch",
    "public",
    "deployment",
    "deployment_status",
    "status",
    "check_run",
    "check_suite",
    "create",
    "delete",
    "gollum",
    "page_build",
    "registry_package",
    "member",
    "branch_protection_rule",
    "merge_group",
    "project",
    "project_card",
    "project_column",
})

# (workflow file, event) pairs whose divergence from `main` is a DECISION,
# with the reason — the delay is accepted and will close at the next release
# cut. An entry here is what turns "silent for as long as the release cycle"
# into "recorded"; remove it once `main` carries the trigger. A stale entry
# is a HARD failure, not untidiness — `test_every_accepted_entry_names_a_real
# _divergence` asserts every row still names a live gap, so the record prunes
# itself at the release cut instead of rotting. Expect that test to go red on
# `main`'s push CI once the release lands: that is the prune order, by design.
ACCEPTED_UNTIL_RELEASE: dict[tuple[str, str], str] = {
    # Pruned at the v0.9.5 release cut (2026-09-21): every gap recorded here
    # — deploy-dev `schedule` + `workflow_dispatch`, alembic-head-watch
    # `schedule` + `workflow_dispatch`, journey-smoke `workflow_dispatch`,
    # publish-images `workflow_dispatch` — is now declared on `main`, and the
    # guard below said so on every run since. Add a row only for a NEW
    # divergence, with its reason.
}


# ---------------------------------------------------------------------------
# Reading the two copies
# ---------------------------------------------------------------------------

def _git(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    """Run git, degrading to a non-zero result rather than raising.

    The #2019 fail-soft contract has to cover BOTH network failure modes. A
    *refusing* network returns non-zero and the caller skips; a *hanging* one
    trips `timeout=` and, uncaught, would raise `TimeoutExpired` out of the
    test — reddening the PR for an offline runner, which is exactly what this
    helper exists to avoid. `OSError` covers a missing git binary.
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1, stdout="", stderr=str(exc),
        )


@functools.lru_cache(maxsize=1)
def _main_ref() -> str | None:
    """A ref naming `main`'s tip, or None when it cannot be reached."""
    if _git("rev-parse", "--verify", "--quiet", f"origin/{DEFAULT_BRANCH}").returncode == 0:
        return f"origin/{DEFAULT_BRANCH}"
    # CI clones at depth 1 with only the PR ref; fetch just the tip of main.
    fetched = _git("fetch", "--depth=1", "--quiet", "origin", DEFAULT_BRANCH)
    if fetched.returncode == 0 and _git("rev-parse", "--verify", "--quiet", "FETCH_HEAD").returncode == 0:
        return "FETCH_HEAD"
    return None


@functools.lru_cache(maxsize=None)
def _main_copy(rel_path: str) -> str | None:
    """The file's content on `main`, or None if `main` has no such file."""
    ref = _main_ref()
    if ref is None:
        pytest.skip(
            f"could not read origin/{DEFAULT_BRANCH} (offline clone?) — workflow "
            f"trigger parity is UNVERIFIED, not proven"
        )
    shown = _git("show", f"{ref}:{rel_path}")
    return shown.stdout if shown.returncode == 0 else None


def _events(text: str) -> set[str]:
    doc = yaml.safe_load(text) or {}
    # YAML 1.1 parses a bare `on:` key as the boolean True.
    on = doc.get("on") if "on" in doc else doc.get(True)
    if on is None:
        return set()
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return set(on)
    return set(on.keys())


def _head_workflows() -> list[Path]:
    return sorted(p for p in WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", _head_workflows(), ids=lambda p: p.name)
def test_default_branch_registered_triggers_are_also_on_main(path: Path):
    """Every default-branch-registered event this branch declares is either
    declared by `main`'s copy too, or is an accepted, reasoned delay."""
    rel = path.relative_to(REPO).as_posix()
    here = _events(path.read_text(encoding="utf-8")) & DEFAULT_BRANCH_REGISTERED_EVENTS
    if not here:
        return

    main_text = _main_copy(rel)
    on_main = _events(main_text) if main_text is not None else set()

    silent = sorted(
        ev for ev in here - on_main if (path.name, ev) not in ACCEPTED_UNTIL_RELEASE
    )
    assert not silent, (
        f"{rel} declares {silent} on this branch but `{DEFAULT_BRANCH}` "
        f"{'has no copy of it' if main_text is None else 'does not declare it'}. "
        f"GitHub registers these events from the DEFAULT branch, so the trigger "
        f"will not fire until the file reaches `{DEFAULT_BRANCH}` — and nothing "
        f"will be red meanwhile (#2814). Either hotfix the workflow file onto "
        f"`{DEFAULT_BRANCH}`, or record the delay in ACCEPTED_UNTIL_RELEASE with "
        f"a reason."
    )


def test_every_accepted_entry_names_a_real_divergence():
    """An allowlist entry that no longer matches anything is a decision nobody
    can find the subject of. Fail it so the record is pruned when the release
    closes the gap."""
    stale = []
    for (name, ev), reason in ACCEPTED_UNTIL_RELEASE.items():
        assert reason.strip(), f"{name}:{ev} is allowlisted without a reason"
        path = WORKFLOWS / name
        if not path.exists():
            stale.append(f"{name} (no such workflow on this branch)")
            continue
        if ev not in _events(path.read_text(encoding="utf-8")):
            stale.append(f"{name}:{ev} (this branch no longer declares it)")
            continue
        main_text = _main_copy(name and f".github/workflows/{name}")
        if main_text is not None and ev in _events(main_text):
            stale.append(f"{name}:{ev} (`{DEFAULT_BRANCH}` now declares it — the gap closed)")
    assert not stale, "prune these ACCEPTED_UNTIL_RELEASE entries: " + "; ".join(stale)


def test_pr_triggers_are_deliberately_out_of_scope():
    """`push` and `pull_request` are read from the ref being built, so a PR
    exercises its own trigger change; flagging them would red every workflow
    edit between releases for a divergence that is never silent."""
    assert "push" not in DEFAULT_BRANCH_REGISTERED_EVENTS
    assert "pull_request" not in DEFAULT_BRANCH_REGISTERED_EVENTS
    assert "pull_request_target" in DEFAULT_BRANCH_REGISTERED_EVENTS, (
        "the #2814 event itself must stay in the set"
    )
