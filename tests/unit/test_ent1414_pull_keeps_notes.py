"""A brain that holds notes still takes its template's updates (trinity-enterprise#1414).

The default Cornelius follows its public template pull-only. Once the person (or
the agent) has written anything into a tracked file — its changelog, a settings
file, a note the template ships — the manual Pull (`POST /api/git/pull`, default
strategy `clean`) ran a bare `git pull --rebase`, which git refuses outright on a
dirty tree: 409 "cannot pull with rebase: You have unstaged changes". From then
on the instance never received a template fix.

Contract for the `clean` pull:
  - the person's uncommitted edits and untracked notes are set aside, the update
    comes in, and the edits are put back — nothing they wrote is lost or
    overwritten;
  - edits to a file the update also changed are merged when the two touch
    different lines;
  - when both changed the SAME lines, the update still lands everywhere else, the
    person's version of that file is kept whole, and the reply names the file in
    plain words (`kept_local_files`) — never silently dropped, never a dead end;
  - an update that would overwrite a file the person created is refused before
    anything moves, naming the file; nothing is changed;
  - `GET /api/git/status` says what an update will touch (`incoming_files`) and
    which of those the person has also changed (`incoming_overlap`).

Real repos (a bare origin = the template, the agent a clone of it) and the real
route function — the `test_4470_manual_pull_pinned_tag` harness shape.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server.routers import git as git_router


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=20, check=check)


def _out(repo, *args):
    return _git(repo, *args).stdout.strip()


def _identity(repo):
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


CHANGELOG_V1 = "# Changelog\n\n- seeded\n"
PLAYBOOK_V1 = "intake: markdown only\n"
CONFIG_V1 = "line one\nline two\nline three\nline four\nline five\n"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """(agent, template) — `template` is a working clone that publishes to the
    bare origin the agent follows; the agent sits on `main`, nothing ahead, as a
    tokenless pull-only Cornelius does."""
    origin, template = tmp_path / "origin.git", tmp_path / "template"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(template))
    _identity(template)
    (template / "CHANGELOG.md").write_text(CHANGELOG_V1)
    (template / "playbook.md").write_text(PLAYBOOK_V1)
    (template / "config.md").write_text(CONFIG_V1)
    (template / ".gitignore").write_text(".trinity/\n")
    _git(template, "add", "-A")
    _git(template, "commit", "-q", "-m", "v1")
    _git(template, "push", "-q", "origin", "HEAD:main")

    agent = tmp_path / "agent"
    _git(tmp_path, "clone", "-q", str(origin), str(agent))
    _identity(agent)
    (agent / ".trinity").mkdir()

    real_path = Path
    monkeypatch.setattr(
        git_router, "Path",
        lambda *a: real_path(agent) if a == ("/home/developer",) else real_path(*a))
    return agent, template


def _publish(template, files, message="template fix"):
    for name, text in files.items():
        (template / name).parent.mkdir(parents=True, exist_ok=True)
        (template / name).write_text(text)
    _git(template, "add", "-A")
    _git(template, "commit", "-q", "-m", message)
    _git(template, "push", "-q", "origin", "HEAD:main")
    return _out(template, "rev-parse", "HEAD")


def _pull(strategy="clean"):
    """The route function itself, as FastAPI calls it."""
    return asyncio.run(git_router.pull_from_github(git_router.GitPullRequest(strategy=strategy)))


def _ok(result):
    assert not isinstance(result, JSONResponse), (
        f"pull refused {result.status_code}: {result.body.decode()}")
    assert result["success"] is True
    return result


class TestABrainWithNotesTakesTheUpdate:
    def test_edits_and_notes_survive_a_template_update(self, brain):
        """The reported case: the changelog was touched and a note written; the
        template fixed a playbook. Before the fix: 409 'You have unstaged changes'."""
        agent, template = brain
        new_head = _publish(template, {"playbook.md": "intake: markdown, PDF and Word\n"})
        (agent / "CHANGELOG.md").write_text(CHANGELOG_V1 + "- took in one document\n")
        (agent / "notes").mkdir()
        (agent / "notes" / "meeting.md").write_text("what the person wrote\n")

        result = _ok(_pull())

        assert _out(agent, "rev-parse", "HEAD") == new_head, "the update landed"
        assert (agent / "playbook.md").read_text() == "intake: markdown, PDF and Word\n"
        assert (agent / "CHANGELOG.md").read_text() == CHANGELOG_V1 + "- took in one document\n"
        assert (agent / "notes" / "meeting.md").read_text() == "what the person wrote\n"
        assert result["kept_local_files"] == []
        assert _out(agent, "stash", "list") == "", "nothing left behind in the stash"

    def test_edits_on_different_lines_of_the_same_file_are_merged(self, brain):
        agent, template = brain
        _publish(template, {"config.md": CONFIG_V1.replace("line one", "line ONE (template)")})
        (agent / "config.md").write_text(CONFIG_V1.replace("line five", "line five (mine)"))

        result = _ok(_pull())

        text = (agent / "config.md").read_text()
        assert "line ONE (template)" in text
        assert "line five (mine)" in text
        assert result["kept_local_files"] == []


class TestBothSidesChangedTheSameLines:
    def test_the_update_lands_and_the_persons_file_is_kept_and_named(self, brain):
        agent, template = brain
        new_head = _publish(template, {
            "CHANGELOG.md": CHANGELOG_V1.replace("- seeded", "- seeded (template v2)"),
            "playbook.md": "intake: markdown, PDF and Word\n",
        })
        mine = CHANGELOG_V1.replace("- seeded", "- seeded, then my own entry")
        (agent / "CHANGELOG.md").write_text(mine)
        (agent / "notes.md").write_text("untracked note\n")

        result = _ok(_pull())

        assert _out(agent, "rev-parse", "HEAD") == new_head, "not a dead end: the update landed"
        assert (agent / "playbook.md").read_text() == "intake: markdown, PDF and Word\n"
        assert (agent / "CHANGELOG.md").read_text() == mine, "the person's version, whole"
        assert "<<<<<<<" not in (agent / "CHANGELOG.md").read_text()
        assert (agent / "notes.md").read_text() == "untracked note\n"
        assert result["kept_local_files"] == ["CHANGELOG.md"]
        assert "CHANGELOG.md" in result["message"], "reported in plain words"
        assert _git(agent, "diff", "--name-only", "--diff-filter=U").stdout == "", \
            "no unmerged paths left for the push cycle to trip on"
        assert _out(agent, "stash", "list") == ""

    def test_a_file_the_template_deleted_but_the_person_edited_is_kept(self, brain):
        agent, template = brain
        (template / "config.md").unlink()
        new_head = _publish(template, {"playbook.md": "v2\n"})
        (agent / "config.md").write_text(CONFIG_V1 + "my addition\n")

        result = _ok(_pull())

        assert _out(agent, "rev-parse", "HEAD") == new_head
        assert (agent / "config.md").read_text() == CONFIG_V1 + "my addition\n"
        assert result["kept_local_files"] == ["config.md"]


class TestAnUpdateThatWouldOverwriteANote:
    def test_refused_before_anything_moves_and_the_file_is_named(self, brain):
        agent, template = brain
        before = _out(agent, "rev-parse", "HEAD")
        _publish(template, {"notes.md": "the template now ships notes.md\n"})
        (agent / "notes.md").write_text("the person's own notes\n")
        (agent / "CHANGELOG.md").write_text(CHANGELOG_V1 + "- mine\n")

        result = _pull()

        assert isinstance(result, JSONResponse) and result.status_code == 409
        body = result.body.decode()
        assert "notes.md" in body
        assert "Nothing was changed" in body
        assert _out(agent, "rev-parse", "HEAD") == before
        assert (agent / "notes.md").read_text() == "the person's own notes\n"
        assert (agent / "CHANGELOG.md").read_text() == CHANGELOG_V1 + "- mine\n"
        assert _out(agent, "stash", "list") == ""


class TestStatusSaysWhatAnUpdateWillTouch:
    def test_incoming_files_and_overlap(self, brain, monkeypatch):
        agent, template = brain
        monkeypatch.setattr(git_router, "LAST_REMOTE_SHA_DIR", agent / ".trinity" / "sha")
        _publish(template, {"playbook.md": "v2\n", "CHANGELOG.md": CHANGELOG_V1 + "- v2\n"})
        (agent / "CHANGELOG.md").write_text(CHANGELOG_V1 + "- mine\n")

        payload = git_router._compute_git_status(agent)

        assert payload["behind"] == 1
        assert sorted(payload["incoming_files"]) == ["CHANGELOG.md", "playbook.md"]
        assert payload["incoming_overlap"] == ["CHANGELOG.md"]

    def test_up_to_date_reports_nothing_incoming(self, brain, monkeypatch):
        agent, _ = brain
        monkeypatch.setattr(git_router, "LAST_REMOTE_SHA_DIR", agent / ".trinity" / "sha")
        payload = git_router._compute_git_status(agent)
        assert payload["incoming_files"] == []
        assert payload["incoming_overlap"] == []


# ---------------------------------------------------------------------------
# Fix round after validation mLSYado3prqY_-TxTudacA (PR #4821)
# ---------------------------------------------------------------------------


def _refused(result):
    assert isinstance(result, JSONResponse), f"expected a 409, got {result!r}"
    assert result.status_code == 409, result.body.decode()
    import json
    return json.loads(result.body.decode())["detail"]


def _stash_entries(repo):
    return [line for line in _out(repo, "stash", "list").splitlines() if line]


def _intercept(monkeypatch, match, replacement):
    """Route `run_registered` calls whose argv contains every token in `match`
    to `replacement(cmd, real, **kw)`; everything else runs for real."""
    real = git_router.run_registered

    def spy(cmd, *a, **kw):
        if all(tok in cmd for tok in match):
            return replacement(cmd, real, *a, **kw)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(git_router, "run_registered", spy)


def _failed(cmd, real, *a, **kw):
    return subprocess.CompletedProcess(cmd, 1, "", "fatal: simulated failure")


class TestAFileThePersonDeletedThatTheUpdateChanged:
    """The `kept` rebind: `_keep_stashed_version` returns None for a deleted
    path (it has no stashed side), and the fall-through concatenated `None`."""

    def _setup(self, brain):
        agent, template = brain
        _publish(template, {"config.md": CONFIG_V1 + "template line\n"})
        (agent / "config.md").unlink()
        return agent, _out(agent, "rev-parse", "HEAD")

    def test_safe_to_reset_undoes_and_names_the_file(self, brain, monkeypatch):
        agent, before = self._setup(brain)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: True)

        detail = _refused(_pull())

        assert "config.md" in detail, detail
        assert detail.endswith("Nothing was changed."), detail
        assert ". Nothing was changed." in detail, f"missing full stop: {detail}"
        assert _out(agent, "rev-parse", "HEAD") == before
        assert not (agent / "config.md").exists(), "the person's deletion is put back"
        assert _stash_entries(agent) == []

    def test_execution_running_leaves_the_tree_and_names_the_stash(self, brain, monkeypatch):
        agent, _ = self._setup(brain)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: False)

        detail = _refused(_pull())

        assert "config.md" in detail, detail
        assert "git stash" in detail, f"a stash that stays must be named: {detail}"
        assert "Nothing was changed" not in detail
        assert len(_stash_entries(agent)) == 1

    def test_undo_failed_names_the_stash(self, brain, monkeypatch):
        agent, _ = self._setup(brain)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: True)
        _intercept(monkeypatch, ("reset", "--hard"), _failed)

        detail = _refused(_pull())

        assert "could not be undone" in detail, detail
        assert "git stash" in detail, detail
        assert "Nothing was changed" not in detail
        assert len(_stash_entries(agent)) == 1

    def test_put_back_failed_after_undo_names_the_stash(self, brain, monkeypatch):
        agent, before = self._setup(brain)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: True)
        real = git_router.run_registered
        pops = []

        def spy(cmd, *a, **kw):
            if "stash" in cmd and "pop" in cmd:
                pops.append(cmd)
                if len(pops) == 2:  # the put-back after the undo
                    return subprocess.CompletedProcess(cmd, 1, "", "fatal: simulated")
            return real(cmd, *a, **kw)

        monkeypatch.setattr(git_router, "run_registered", spy)

        detail = _refused(_pull())

        assert len(pops) == 2
        assert "git stash" in detail, detail
        assert "Nothing was changed" not in detail, detail
        assert _out(agent, "rev-parse", "HEAD") == before
        assert len(_stash_entries(agent)) == 1

    def test_a_failed_stash_drop_is_not_reported_as_kept(self, brain, monkeypatch):
        """Same-lines conflict, but the spent stash cannot be dropped: the pull
        is undone instead of claiming the edits were folded in."""
        agent, template = brain
        before = _out(agent, "rev-parse", "HEAD")
        _publish(template, {"CHANGELOG.md": CHANGELOG_V1.replace("- seeded", "- v2")})
        mine = CHANGELOG_V1.replace("- seeded", "- mine")
        (agent / "CHANGELOG.md").write_text(mine)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: True)
        _intercept(monkeypatch, ("stash", "drop"), _failed)

        detail = _refused(_pull())

        assert "CHANGELOG.md" in detail
        assert _out(agent, "rev-parse", "HEAD") == before
        assert (agent / "CHANGELOG.md").read_text() == mine
        assert _stash_entries(agent) == []


class TestATimeoutMidRebase:
    """A local commit ahead of origin + a pull killed part-way through its
    rebase: the rebase is aborted like a merge, and the reply never claims
    "Nothing was changed" while one is still in progress."""

    def _setup(self, brain, monkeypatch):
        agent, template = brain
        _publish(template, {"playbook.md": "template v2\n"})
        (agent / "playbook.md").write_text("my committed playbook\n")
        _git(agent, "commit", "-qam", "local")
        before = _out(agent, "rev-parse", "HEAD")

        def stuck_rebase(cmd, real, *a, **kw):
            real(["git", "fetch", "-q", "origin"], cwd=kw["cwd"], timeout=20)
            stopped = real(["git", "rebase", "origin/main"], cwd=kw["cwd"], timeout=20)
            assert stopped.returncode != 0, "harness: the rebase should stop on a conflict"
            raise subprocess.TimeoutExpired(cmd, 60)

        _intercept(monkeypatch, ("pull", "--rebase"), stuck_rebase)
        return agent, before

    def test_the_rebase_is_aborted_and_the_branch_restored(self, brain, monkeypatch):
        agent, before = self._setup(brain, monkeypatch)

        detail = _refused(_pull())

        assert not git_router._rebase_in_progress(agent), "the rebase was left in progress"
        assert _out(agent, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert _out(agent, "rev-parse", "HEAD") == before
        assert "git pull timed out" in detail, detail
        assert detail.endswith("Nothing was changed."), detail

    def test_never_nothing_changed_while_a_rebase_is_in_progress(self, brain, monkeypatch):
        agent, _ = self._setup(brain, monkeypatch)
        real = git_router.run_registered

        def spy(cmd, *a, **kw):
            if "pull" in cmd and "--rebase" in cmd:
                real(["git", "fetch", "-q", "origin"], cwd=kw["cwd"], timeout=20)
                real(["git", "rebase", "origin/main"], cwd=kw["cwd"], timeout=20)
                raise subprocess.TimeoutExpired(cmd, 60)
            if "rebase" in cmd and "--abort" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "fatal: simulated")
            return real(cmd, *a, **kw)

        monkeypatch.setattr(git_router, "run_registered", spy)
        monkeypatch.setattr(git_router, "_safe_to_reset", lambda: False)

        detail = _refused(_pull())

        assert git_router._rebase_in_progress(agent)
        assert "Nothing was changed" not in detail, detail
        assert "rebase" in detail, f"the half-done rebase must be named: {detail}"
