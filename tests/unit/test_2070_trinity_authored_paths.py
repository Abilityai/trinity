"""The `.trinity/` authored-vs-runtime split survives a backend Push (#2070).

Every Push runs `git rm --cached` over tracked-but-now-ignored files. The
fleet-wide list ignored `.trinity/` **wholesale**, so any file a template
legitimately commits there was untracked and its deletion pushed — with a
hardcoded two-entry pathspec exemption as the only protection. That list was
wrong three times, and the docstring recorded the first two:

1. `.trinity/brain-orb/` (trinity-enterprise#76)
2. `.trinity/setup.sh` — "verified live: the e2e Push swept setup.sh before the
   second exemption was added"
3. `.trinity/pre-check` — #2070, the SCHED-COND-001 hook (#454) that
   `architecture.md`, `requirements/scheduling.md`, `agent-validation-spec.md`
   (A-004) and the agent guide all tell template authors to commit

The symptom is silent: only the index is touched, so the working tree keeps
running until the next re-clone or container recreate boots without the hook —
and then every cron tick runs a full LLM turn instead of skipping, which is
exactly the cost SCHED-COND-001 exists to remove.

These tests exercise the REAL commands (`_build_gitignore_merge_command`,
`_build_rm_cached_ignored_command`) against REAL git repositories in a temp
directory: the defect lives in git's own pathspec/negation semantics, which a
mocked container cannot express. They assert the SPLIT (authored survives,
runtime does not), never the current exemption strings — a fourth forgotten
string must fail here, not ship.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


def _gs():
    try:
        # #1028: git_service is a package; the alias names the module that
        # owns the functions under test, so patches land where the code looks.
        import services.git_service.gitignore as gs
    except Exception:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return gs


# Runtime state the platform writes under `.trinity/`. Each must stay ignored;
# a file here that becomes tracked is repo bloat or a state leak.
RUNTIME_FILES = {
    ".trinity/operator-queue.json": "{}",                     # OPS-001
    ".trinity/sync-state.json": "{}",                         # #389 S1a
    ".trinity/read-only-config.json": "{}",                   # read-only mode
    ".trinity/persistent-state.yaml": "paths: []\n",          # S4 #383
    ".trinity/data-paths.yaml": "paths: []\n",                # #1169
    ".trinity/pipeline-state/research/2026-01-01.json": "{}",  # #919 state
    ".trinity/pending-results/e1.json": "{}",                 # #1083
    ".trinity/pending-pull-results/e2.json": "{}",            # #1081
    ".trinity/backup/2026-01-01/x": "x",                      # S3 snapshot
}

# Authored content a TEMPLATE commits and the platform reads back.
AUTHORED_FILES = {
    ".trinity/pre-check": "#!/bin/sh\necho run\n",            # #454
    ".trinity/post-check": "#!/bin/sh\n",                     # compat I-005
    ".trinity/pre-snapshot": "#!/bin/sh\n",                   # #1169
    ".trinity/setup.sh": "#!/bin/sh\n",                       # ent#76
    ".trinity/persistent-processes.allow": "my-daemon\n",     # #1501
    ".trinity/brain-orb/search": "#!/bin/sh\n",               # #58/#60
    ".trinity/brain-orb/scopes": "#!/bin/sh\n",
    ".trinity/pipelines/research.yaml": "id: research\n",     # #919 definitions
    ".trinity/plugins.yaml": "plugins:\n  installed: []\n",   # #1704 (COMMITTED)
}

_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null"}


def _make_repo(tmp_path: Path, gitignore: str | None = None) -> Path:
    """A repo with every authored + runtime file COMMITTED — the pre-existing
    agent this sweep runs against."""
    home = tmp_path / "developer"
    for rel, body in {**RUNTIME_FILES, **AUTHORED_FILES}.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    (home / "CLAUDE.md").write_text("agent\n")
    if gitignore is not None:
        (home / ".gitignore").write_text(gitignore)
    _git(home, "init", "-q", ".")
    _git(home, "config", "user.email", "t@t")
    _git(home, "config", "user.name", "t")
    _git(home, "add", "-A", "-f")
    _git(home, "commit", "-qm", "init")
    return home


def _git(cwd: Path, *args: str) -> str:
    env = dict(_ENV, HOME=str(cwd))
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _run(cmd: str, cwd: Path) -> None:
    """Run a builder's command the way the backend does — docker-py splits a
    string command with shlex, so the test does too."""
    env = dict(_ENV, HOME=str(cwd))
    out = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, capture_output=True, text=True)
    assert out.returncode == 0, f"command failed: {out.stderr[:400]}"


def _push_sweep(home: Path) -> set[str]:
    """The two commands a backend Push runs, in order. Returns tracked paths."""
    gs = _gs()
    _run(gs._build_gitignore_merge_command(str(home)), home)
    _run(gs._build_rm_cached_ignored_command(str(home)), home)
    return set(_git(home, "ls-files").split())


# ---------------------------------------------------------------------------
# The reported bug, and its two predecessors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("authored", sorted(AUTHORED_FILES))
def test_authored_content_survives_the_push_sweep(tmp_path, authored):
    tracked = _push_sweep(_make_repo(tmp_path))
    assert authored in tracked, (
        f"{authored} was untracked by the Push sweep — its deletion would be "
        "pushed, and the next re-clone or container recreate boots without it"
    )


@pytest.mark.parametrize("runtime", sorted(RUNTIME_FILES))
def test_runtime_state_is_still_untracked(tmp_path, runtime):
    tracked = _push_sweep(_make_repo(tmp_path))
    assert runtime not in tracked, (
        f"{runtime} is still tracked — platform runtime state would be "
        "committed to the agent's repo"
    )


def test_a_fresh_clone_still_runs_the_hook(tmp_path):
    """The failure this issue is about lands one recreate later, so assert the
    thing that actually breaks: clone the pushed tree and run the hook."""
    home = _make_repo(tmp_path)
    _push_sweep(home)
    _git(home, "commit", "-qam", "post-sweep")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(home), str(clone)],
                   env=dict(_ENV, HOME=str(tmp_path)), check=True, capture_output=True)

    hook = clone / ".trinity" / "pre-check"
    assert hook.is_file(), "a fresh clone has no pre-check — every cron tick now runs a full LLM turn"
    os.chmod(hook, 0o755)
    out = subprocess.run([str(hook)], capture_output=True, text=True, env=_ENV)
    assert out.returncode == 0 and out.stdout.strip() == "run", (
        f"the cloned hook did not run: rc={out.returncode} {out.stderr[:200]}"
    )


# ---------------------------------------------------------------------------
# The fleet this fixes already carries the superseded line
# ---------------------------------------------------------------------------

def test_the_superseded_wholesale_line_is_replaced(tmp_path):
    """Append-only would not have fixed anything.

    Git does not descend into a directory excluded by the dir-form `.trinity/`,
    so `!.trinity/pre-check` under it never applies. Every agent synced before
    #2070 carries that exact line, which is precisely the fleet this repairs.
    """
    home = _make_repo(tmp_path, gitignore=".trinity/\ncontent/\n")
    tracked = _push_sweep(home)

    lines = (home / ".gitignore").read_text().splitlines()
    assert ".trinity/" not in lines, "the superseded wholesale line survived the merge"
    assert ".trinity/*" in lines
    assert ".trinity/pre-check" in tracked
    assert ".trinity/operator-queue.json" not in tracked


def test_user_rules_mentioning_trinity_are_left_alone(tmp_path):
    """Removal is by EXACT line, confined to what this platform itself wrote."""
    home = _make_repo(tmp_path, gitignore=".trinity/scratch/\n!.trinity/mine\n.trinity/\n")
    _push_sweep(home)

    lines = (home / ".gitignore").read_text().splitlines()
    assert ".trinity/scratch/" in lines, "a user's own rule was removed"
    assert "!.trinity/mine" in lines
    assert ".trinity/" not in lines, "the platform's own superseded line should still go"


def test_the_merge_is_idempotent(tmp_path):
    """Second Push must be a no-op — the removal step must not oscillate."""
    home = _make_repo(tmp_path, gitignore=".trinity/\n")
    _push_sweep(home)
    first = (home / ".gitignore").read_text()
    _push_sweep(home)
    assert (home / ".gitignore").read_text() == first, "the merge is not idempotent"


# ---------------------------------------------------------------------------
# The split itself, not the strings that currently implement it
# ---------------------------------------------------------------------------

def test_every_authored_path_is_re_included_in_the_pattern_list():
    """Adding a path to `_TRINITY_AUTHORED_PATHS` without a matching negation
    would leave it ignored — tracked today, swept on the next Push."""
    gs = _gs()
    missing = [p for p in gs._TRINITY_AUTHORED_PATHS
               if f"!{p}" not in gs._GITIGNORE_PATTERNS]
    assert not missing, f"authored paths with no re-include: {missing}"


def test_the_rm_cached_exemptions_are_derived_not_hardcoded():
    """The exemption list must follow the constant, so a new authored path
    cannot be protected in one place and swept in the other — which is exactly
    how this bug recurred three times."""
    gs = _gs()
    cmd = gs._build_rm_cached_ignored_command("/home/developer")
    for path in gs._TRINITY_AUTHORED_PATHS:
        assert f":!{path.rstrip('/')}" in cmd, (
            f"{path} is authored but absent from the rm --cached exemptions"
        )


def test_the_directory_is_not_excluded_wholesale():
    """The dir-form is the defect: under it git never descends, so no negation
    below it can apply and every authored file depends on an exemption string
    someone has to remember."""
    gs = _gs()
    assert ".trinity/" not in gs._GITIGNORE_PATTERNS, (
        "the wholesale exclusion is back — the re-includes below it are inert "
        "and authored content is one forgotten pathspec from deletion again"
    )
    assert ".trinity/*" in gs._GITIGNORE_PATTERNS


def test_a_new_runtime_file_needs_no_action(tmp_path):
    """The inversion's whole point: unknown files under `.trinity/` default to
    ignored, so adding platform state never risks the authored set."""
    home = _make_repo(tmp_path)
    invented = home / ".trinity" / "some-future-state.json"
    invented.write_text("{}")
    _git(home, "add", "-A", "-f")
    _git(home, "commit", "-qm", "future state")

    tracked = _push_sweep(home)
    assert ".trinity/some-future-state.json" not in tracked, (
        "a newly-invented runtime file stayed tracked"
    )
    assert ".trinity/pre-check" in tracked, "…and it must not have cost the authored set"
