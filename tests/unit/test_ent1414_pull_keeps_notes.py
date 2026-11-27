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
