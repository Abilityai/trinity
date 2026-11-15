"""The ref probes against a REAL git remote (trinity#4165).

`test_4165_ref_existence_checks.py` drives the probes through a fake
`ls-remote`. A fake can only be as right as its author's model of git, and
that model was wrong once: it listed `refs/tags/<r>^{}` whenever
`refs/tags/<r>` was asked for. Real git lists a ref only when a pattern
matches the ref's own name, and the peeled line's name ends in `^{}`, so a
probe that asks for `refs/tags/<r>` alone gets the TAG OBJECT for an annotated
tag, not the commit a clone checks out. A raw read pinned to that id is not
the template the agent runs.

So these run `git ls-remote` for real, over `file://`, against a bare
repository holding a lightweight tag, an annotated tag, and a branch and a tag
that share a name but point at different commits. No network, no fake.
"""
import asyncio
import os
import shutil
import subprocess

import pytest

import services.git_service as gs

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

REPO = "acme/template"

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1", "HOME": "",
}


def _git(cwd, *args):
    _ENV["HOME"] = str(cwd)
    return subprocess.run(
        ("git", *args), cwd=cwd, env=_ENV, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare `acme/template.git` under a file:// base URL, and the ids that
    matter: `one` and `two` are commits, `ann_obj` is the annotated tag's own
    object id (what the probe must NOT return)."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "commit", "-q", "--allow-empty", "-m", "one")
    one = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "v1.0.0")                                  # lightweight
    _git(work, "branch", "shared")                               # branch at one
    _git(work, "commit", "-q", "--allow-empty", "-m", "two")
    two = _git(work, "rev-parse", "HEAD")
    _git(work, "tag", "-a", "v2.0.0", "-m", "release two")       # annotated
    _git(work, "tag", "-a", "shared", "-m", "same name as a branch")  # at two
    ann_obj = _git(work, "rev-parse", "refs/tags/v2.0.0")

    bare = tmp_path / "base" / "acme" / "template.git"
    bare.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))

    monkeypatch.setenv("TRINITY_GIT_BASE_URL", (tmp_path / "base").as_uri())
    assert ann_obj not in (one, two)
    return {"one": one, "two": two, "ann_obj": ann_obj}


def _probe(ref):
    return asyncio.run(gs.probe_anonymous_ref(REPO, ref))


class TestRefProbesAgainstARealRemote:
    def test_a_lightweight_tag_is_the_commit_it_names(self, remote):
        assert _probe("v1.0.0") == ("ok", remote["one"])

    def test_an_annotated_tag_is_its_commit_not_the_tag_object(self, remote):
        outcome, sha = _probe("v2.0.0")
        assert sha != remote["ann_obj"], "pinned to the tag object, not the commit"
        assert (outcome, sha) == ("ok", remote["two"])

    def test_a_branch_wins_over_a_same_named_annotated_tag(self, remote):
        # The branch is at `one`, the tag (and its peel) at `two`: `git clone
        # -b shared` checks out the branch, so the probe must answer `one`.
        assert _probe("shared") == ("ok", remote["one"])

    def test_the_default_branch(self, remote):
        assert _probe(None) == ("ok", remote["two"])

    def test_no_such_ref(self, remote):
        assert _probe("v9.9.9") == ("missing_ref", "")

    def test_the_tokened_probe_peels_too(self, remote, monkeypatch):
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        outcome, sha = asyncio.run(
            gs.probe_tokened_repo_access(REPO, "pat-4165-real", "v2.0.0"))
        assert (outcome, sha) == ("ok", remote["two"])
