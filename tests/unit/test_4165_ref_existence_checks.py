"""A tag accepted by the parser must survive every ref-existence check (trinity#4165).

`_parse_github_ref` learned to take a dotted tag whole (`v1.2.0`), but the
value it stores in `config.source_branch` is consumed by three more checks
before the clone, and each asked git or GitHub for a BRANCH of that name:

  * `provisioning.check_remote_branch_exists` — `ls-remote --heads refs/heads/<ref>`
    (the tokenless create path);
  * `ref_probes._ls_remote_ref` — `ls-remote <url> refs/heads/<ref>` (the
    anonymous template read and the #3725 tokened re-check after a REST 403);
  * the REST `GET /repos/{repo}/branches/{ref}` in `_validate_github_access`
    (the PAT path — the case a private template repo with a read-only token is).

So a tag passed the parser and was then refused with "Branch 'v1.2.0' not
found" on every path. These tests drive the checks themselves — the real
subprocess spawn is replaced by a fake `git` whose listing the test writes —
and `_validate_github_access` end to end on all three paths. The ref still
reaches git as a VALUE (a positional after the URL, prefixed `refs/…`), never
as an option.

Contract: a ref is a branch or a tag; when both exist the branch wins, as in
`git clone -b`. Harness shape: `test_3725_identity_and_validation.py` (real
`crud`, stubbed `GitHubService`, fake subprocess on the owning module).
"""
import asyncio
import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import services.agent_service.crud as crud
import services.git_service as gs
from services.github_service import GitHubError

pytestmark = pytest.mark.unit

SHA = "c" * 40
PEELED = "d" * 40
REPO = "acme/template"
URL = f"https://github.com/{REPO}.git"
PAT = "pat-4165-e"


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._out = stdout
        self._err = stderr

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        pass


def _remote(owner, refs):
    """A fake `git ls-remote` on the module that owns the probe, answering
    from `refs` ({refname: sha}) the way git does: a ref is listed only when
    a pattern names it — the peeled `refs/tags/<r>^{}` line included, which
    git does NOT list for a bare `refs/tags/<r>` pattern (pinned against a
    real remote in `test_4165_ref_probes_real_git.py`) — `--heads` narrows to
    `refs/heads/`, and with `--exit-code` an empty answer exits 2. So a check
    that asks for `refs/heads/<ref>` only cannot see a tag, whatever the
    remote holds."""
    async def spawn(*argv, **kw):
        assert argv[:2] == ("git", "ls-remote")
        opts = [a for a in argv[2:] if a.startswith("--")]
        rest = [a for a in argv[2:] if not a.startswith("--")]
        url, patterns = rest[0], rest[1:]
        assert url == URL
        lines = []
        for name, sha in refs.items():
            if "--heads" in opts and not name.startswith("refs/heads/"):
                continue
            if not patterns or name in patterns:
                lines.append(f"{sha}\t{name}\n")
        if not lines and "--exit-code" in opts:
            return _FakeProc(2, b"")
        return _FakeProc(0, "".join(lines).encode())

    mock = AsyncMock(side_effect=spawn)
    return patch.object(owner.asyncio, "create_subprocess_exec", mock)


# ---------------------------------------------------------------------------
# provisioning.check_remote_branch_exists — the tokenless create's check
# ---------------------------------------------------------------------------

class TestCheckRemoteBranchExistsAcceptsATag:
    def test_a_tag_answers(self):
        with _remote(gs.provisioning, {"refs/tags/v1.2.0": SHA}) as spawn:
            assert asyncio.run(gs.check_remote_branch_exists(REPO, "v1.2.0")) is True
        argv = spawn.await_args.args
        # Asked for both forms of the ref, as values after the URL, and no
        # longer narrowed to heads.
        assert "--heads" not in argv
        assert argv[argv.index(URL) + 1:] == ("refs/heads/v1.2.0", "refs/tags/v1.2.0")

    def test_a_branch_still_answers(self):
        with _remote(gs.provisioning, {"refs/heads/release-1.0": SHA}):
            assert asyncio.run(gs.check_remote_branch_exists(REPO, "release-1.0")) is True

    def test_neither_is_still_absent(self):
        with _remote(gs.provisioning, {"refs/heads/main": SHA, "refs/tags/v1.2.0": SHA}):
            assert asyncio.run(gs.check_remote_branch_exists(REPO, "v9.9.9")) is False


# ---------------------------------------------------------------------------
# ref_probes — the anonymous template read and the tokened 403 re-check
# ---------------------------------------------------------------------------

class TestRefProbesAcceptATag:
    def test_an_annotated_tag_resolves_to_its_peeled_commit(self):
        with _remote(gs.ref_probes, {"refs/tags/v1.2.0": SHA, "refs/tags/v1.2.0^{}": PEELED}) as spawn:
            assert asyncio.run(gs.probe_anonymous_ref(REPO, "v1.2.0")) == ("ok", PEELED)
        argv = spawn.await_args.args
        assert argv[:3] == ("git", "ls-remote", URL)
        assert argv[3:] == ("refs/heads/v1.2.0", "refs/tags/v1.2.0", "refs/tags/v1.2.0^{}")

    def test_a_lightweight_tag_resolves_to_the_commit_it_names(self):
        with _remote(gs.ref_probes, {"refs/tags/v1.2.0": SHA}):
            assert asyncio.run(gs.probe_anonymous_ref(REPO, "v1.2.0")) == ("ok", SHA)

    def test_the_branch_wins_over_a_same_named_tag(self):
        with _remote(gs.ref_probes, {"refs/tags/main": PEELED, "refs/heads/main": SHA}):
            assert asyncio.run(gs.probe_anonymous_ref(REPO, "main")) == ("ok", SHA)

    def test_the_tokened_recheck_accepts_a_tag_too(self, monkeypatch):
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        with _remote(gs.ref_probes, {"refs/tags/v1.2.0": SHA}) as spawn:
            assert asyncio.run(gs.probe_tokened_repo_access(REPO, PAT, "v1.2.0")) == ("ok", SHA)
        assert spawn.await_args.args[3:] == ("refs/heads/v1.2.0", "refs/tags/v1.2.0", "refs/tags/v1.2.0^{}")
        assert PAT not in spawn.await_args.args  # the token rides the env, never argv

    def test_head_is_unchanged(self):
        with _remote(gs.ref_probes, {"HEAD": SHA, "refs/heads/main": SHA}) as spawn:
            assert asyncio.run(gs.probe_anonymous_ref(REPO)) == ("ok", SHA)
        assert spawn.await_args.args == ("git", "ls-remote", URL, "HEAD")

    def test_no_such_ref_is_still_missing(self):
        with _remote(gs.ref_probes, {"refs/heads/main": SHA}):
            assert asyncio.run(gs.probe_anonymous_ref(REPO, "v9.9.9")) == ("missing_ref", "")


# ---------------------------------------------------------------------------
# _validate_github_access — end to end on the three paths
# ---------------------------------------------------------------------------

def _validate(ref, pat):
    config = types.SimpleNamespace(source_branch=ref)
    return asyncio.run(crud._validate_github_access(config, REPO, pat))


class TestValidateGithubAccessWithATag:
    @pytest.fixture(autouse=True)
    def _no_push_probe(self, monkeypatch):
        monkeypatch.setattr(crud, "_validate_push_access", AsyncMock())

    def test_tokenless_path_accepts_a_tag_through_the_real_check(self, monkeypatch):
        monkeypatch.setattr(crud.git_service, "probe_anonymous_repo_access",
                            AsyncMock(return_value="ok"))
        with _remote(gs.provisioning, {"refs/heads/main": SHA, "refs/tags/v1.2.0": SHA}):
            assert _validate("v1.2.0", None) is None

    def test_tokenless_path_names_the_missing_ref_as_branch_or_tag(self, monkeypatch):
        monkeypatch.setattr(crud.git_service, "probe_anonymous_repo_access",
                            AsyncMock(return_value="ok"))
        with _remote(gs.provisioning, {"refs/heads/main": SHA}):
            with pytest.raises(HTTPException) as exc:
                _validate("v9.9.9", None)
        assert exc.value.status_code == 400
        assert "Branch or tag 'v9.9.9' not found" in exc.value.detail

    def _gh(self, monkeypatch, answers):
        """A GitHubService whose REST answers are the test's: `answers` maps
        a path to a status code; anything else is 404."""
        seen = []

        class _GH:
            def __init__(self, pat):
                self.pat = pat

            async def check_repo_exists(self, owner, name):
                return types.SimpleNamespace(exists=True, private=True, default_branch="main")

            async def _request(self, method, path, **kw):
                seen.append((method, path))
                return types.SimpleNamespace(status_code=answers.get(path, 404))

        monkeypatch.setattr(crud, "GitHubService", _GH)
        return seen

    def test_rest_path_falls_back_to_the_tag_ref_when_no_branch_has_the_name(self, monkeypatch):
        seen = self._gh(monkeypatch, {f"/repos/{REPO}/git/ref/tags/v1.2.0": 200})
        assert _validate("v1.2.0", PAT) is None
        assert seen == [("GET", f"/repos/{REPO}/branches/v1.2.0"),
                        ("GET", f"/repos/{REPO}/git/ref/tags/v1.2.0")]

    def test_rest_path_takes_the_branch_without_asking_for_a_tag(self, monkeypatch):
        seen = self._gh(monkeypatch, {f"/repos/{REPO}/branches/release-1.0": 200})
        assert _validate("release-1.0", PAT) is None
        assert seen == [("GET", f"/repos/{REPO}/branches/release-1.0")]

    def test_rest_path_refuses_a_ref_that_is_neither(self, monkeypatch):
        self._gh(monkeypatch, {})
        with pytest.raises(HTTPException) as exc:
            _validate("v9.9.9", PAT)
        assert exc.value.status_code == 400
        assert "Branch or tag 'v9.9.9' not found" in exc.value.detail

    def test_the_403_recheck_accepts_a_tag_through_the_real_probe(self, monkeypatch):
        class _GH:
            def __init__(self, pat):
                self.pat = pat

            async def check_repo_exists(self, owner, name):
                raise GitHubError("GitHub API error: 403", status=403)

        monkeypatch.setattr(crud, "GitHubService", _GH)
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        with _remote(gs.ref_probes, {"refs/heads/main": SHA, "refs/tags/v1.2.0": SHA}) as spawn:
            assert _validate("v1.2.0", PAT) is None
        assert spawn.await_args.args[3:] == ("refs/heads/v1.2.0", "refs/tags/v1.2.0", "refs/tags/v1.2.0^{}")
