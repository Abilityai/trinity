"""Repointing a source's ref/ref_type must sync (#4534).

`_clone` runs `git clone --branch <ref> --depth 1`, so the checkout's remote
refspec covers only the ref the clone started on. `_update_branch` fetched
`origin <ref>` (which lands in FETCH_HEAD only) and then reset to
`origin/<ref>`, a ref that never exists for a branch the clone did not start
on: `reset failed: fatal: ambiguous argument 'origin/main'`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_IDENT = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *_IDENT, *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


def _commit(repo: Path, text: str) -> str:
    d = repo / ".claude" / "skills" / "pdf-export"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: pdf-export\ndescription: {text}\n---\n{text}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", text)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def upstream(tmp_path):
    """main @ c1 (tag v1), main moves to c2 (tag v2), branch `dev` @ c3."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas = {"v1": _commit(repo, "one")}
    _git(repo, "tag", "-a", "-m", "v1", "v1")
    shas["v2"] = _commit(repo, "two")
    _git(repo, "tag", "-a", "-m", "v2", "v2")
    _git(repo, "checkout", "-q", "-b", "dev")
    shas["dev"] = _commit(repo, "three")
    _git(repo, "checkout", "-q", "main")
    shas["main"] = shas["v2"]
    return repo, shas


def _clone_for(upstream: Path, root: Path, ref: str, ref_type: str):
    from services.skill_source_clone import SkillSourceClone

    return SkillSourceClone("src_" + "ab" * 8, str(upstream), ref, ref_type, root)


TRANSITIONS = [
    (("v1", "tag"), ("main", "branch")),
    (("main", "branch"), ("dev", "branch")),
    (("dev", "branch"), ("v1", "tag")),
    (("v1", "tag"), ("v2", "tag")),
]


@pytest.mark.parametrize("old,new", TRANSITIONS, ids=lambda t: "-".join(t) if isinstance(t, tuple) else None)
def test_ref_change_syncs(old, new, upstream, tmp_path):
    repo, shas = upstream
    first = _clone_for(repo, tmp_path / "lib", *old)
    r1 = first.sync(str(repo))
    assert r1["success"], r1

    second = _clone_for(repo, tmp_path / "lib", *new)
    r2 = second.sync(str(repo))

    assert r2["success"], r2
    assert second.current_commit() == shas[new[0]][: len(second.current_commit())]
