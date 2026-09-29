"""
trinity-enterprise#708 — `.claude/settings.json` is kept out of a commit by its
CONTENT, not its name.

The file-level ignore (#2036) is gone: a template's project settings — hooks
included — commit normally. What #2036 actually fixed was one content: absolute
`/opt/trinity/` hook paths, which brick any clone made outside the container.
HOME is the repo root, so the file is also Claude Code's USER settings: a
credential-bearing key (`env`, `apiKeyHelper`, ...) is refused the same way.
Every platform commit path now runs a guard after staging:
  - agent server: `routers/git.py::_guard_container_only_settings`
    (heartbeat `_run_auto_sync_once`, operator Push `sync_to_github`, and
    `reset_to_main_preserve_state_impl` — the reset route + MCP tool)
  - backend:      `services/git_service/gitignore.py::CONTAINER_ONLY_SETTINGS_GUARD`
    (the shell twin, spliced into `initialize_git_in_container`)

Both run the SAME matrix below against real repos, so the two homes of the rule
cannot drift. Plus the heartbeat end to end (a guarded-out file must not turn
every later cycle into an empty-commit failure) and the Push wiring.
"""
from __future__ import annotations

import ast
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# `agent_server` is the real base-image package, installed by
# tests/unit/conftest.py::_preload_real_agent_server.
from agent_server.routers import git as git_router

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

SETTINGS = ".claude/settings.json"
HARMFUL = json.dumps({"hooks": {"PreToolUse": [{"hooks": [
    {"type": "command", "command": "python3 /opt/trinity/hooks/guard.py"}]}]}}) + "\n"
CLEAN = json.dumps({"hooks": {"Stop": [{"hooks": [
    {"type": "command", "command": "bash .claude/hooks/on-stop.sh"}]}]}}) + "\n"
CLEAN_V2 = CLEAN.replace("on-stop", "on-stop-v2")
# Ordinary template content the guard refuses as NEW content (a non-empty
# `env`) or used to refuse on a bare substring (a deny rule naming
# `/opt/trinity/`). Committed, it must be left alone: nothing leaks that is not
# already in history, and untracking it would commit a DELETION of a template's
# project settings (review of #3019).
HARMLESS_ENV = json.dumps({"env": {"BASH_DEFAULT_TIMEOUT_MS": "600000",
                                   "DISABLE_TELEMETRY": "1"}}, indent=2) + "\n"
DENY_MENTION = json.dumps({"permissions": {"deny": ["Read(/opt/trinity/**)"]}},
                          indent=2) + "\n"
NON_UTF8 = b'{"note": "caf\xe9"}\n'

# What the boot-time plugin reinstall writes on every agent today — must commit.
PLATFORM_WRITTEN = json.dumps({
    "extraKnownMarketplaces": {"abilityai": {"source": {
        "source": "github", "repo": "abilityai/abilities"}}},
    "enabledPlugins": {"trinity@abilityai": True},
}, indent=2) + "\n"

# HOME is the repo root, so this is also Claude Code's USER settings file. Every
# top-level key the settings reference documents as holding a credential or as a
# command that produces one. A value that must never reach a commit OR a log:
SECRET_VALUE = "sk-test-not-a-real-secret-0000"
CREDENTIAL_KEYS = {
    "env": {"ANTHROPIC_API_KEY": SECRET_VALUE},
    "apiKeyHelper": f"echo {SECRET_VALUE}",
    "awsAuthRefresh": f"aws sso login --profile {SECRET_VALUE}",
    "awsCredentialExport": f"echo {SECRET_VALUE}",
    "gcpAuthRefresh": f"gcloud auth login {SECRET_VALUE}",
    "otelHeadersHelper": f"echo Authorization={SECRET_VALUE}",
}


def _with_credential(key):
    base = json.loads(PLATFORM_WRITTEN)
    base[key] = CREDENTIAL_KEYS[key]
    return json.dumps(base) + "\n"


def _git(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, timeout=20, check=check)


def _repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write(repo, content):
    path = repo / SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


def _commit(repo, msg="c"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def _index_blob(repo):
    res = _git(repo, "show", f":{SETTINGS}", check=False)
    return res.stdout if res.returncode == 0 else None


_SHELL_NAMES = ("_CREDENTIAL_SETTINGS_KEYS", "_SETTINGS_PARSE", "_SETTINGS_CHECK", "_SETTINGS_HEAL_CHECK",
                "CONTAINER_ONLY_SETTINGS_GUARD")


def _backend_shell_names():
    """The backend constants' real values, evaluated off the module's AST
    (pure literal/str-join expressions) — no import, so no sys.modules juggling."""
    src = (_ROOT / "src/backend/services/git_service/gitignore.py").read_text()
    ns: dict = {}
    for n in ast.parse(src).body:
        targets = n.targets if isinstance(n, ast.Assign) else (
            [n.target] if isinstance(n, ast.AnnAssign) else [])
        name = next((t.id for t in targets if getattr(t, "id", "") in _SHELL_NAMES), None)
        if name:
            ns[name] = eval("(" + ast.get_source_segment(src, n.value) + ")", {"__builtins__": {}}, ns)
    return ns


@pytest.fixture(scope="module")
def shell_guard():
    return _backend_shell_names()["CONTAINER_ONLY_SETTINGS_GUARD"]


def _run_python_guard(repo, _shell):
    git_router._guard_container_only_settings(repo)


def _run_shell_guard(repo, shell):
    # Exactly how provisioning splices it — `bash -c "cd <dir> && <cmd>"` as ONE
    # string — and how docker-py's exec_create splits it (shlex, POSIX mode).
    subprocess.run(shlex.split(f'bash -c "cd {repo} && {shell}"'), check=True, timeout=20)


GUARDS = pytest.mark.parametrize(
    "run_guard", [_run_python_guard, _run_shell_guard], ids=["agent-python", "backend-shell"])


@GUARDS
class TestTheRule:
    def test_a_clean_settings_file_commits(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, CLEAN)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == CLEAN

    def test_a_harmful_untracked_file_stays_out_and_on_disk(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, HARMFUL)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None
        assert (repo / SETTINGS).read_text() == HARMFUL

    def test_a_harmful_edit_of_a_clean_tracked_file_restores_the_clean_copy(
            self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _write(repo, CLEAN)
        _commit(repo, "clean settings")
        _write(repo, HARMFUL)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == CLEAN, "the committed clean copy must stay tracked"
        assert (repo / SETTINGS).read_text() == HARMFUL

    def test_a_harmful_copy_already_committed_is_untracked(self, tmp_path, shell_guard, run_guard):
        """The pre-#2036 repos: the harmful copy is IN HEAD. Untracking it makes
        the next commit delete it from the remote — unbricking future clones."""
        repo = _repo(tmp_path)
        _write(repo, HARMFUL)
        _commit(repo, "legacy leak")
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None
        _git(repo, "commit", "-q", "-m", "heal")
        assert SETTINGS not in _git(repo, "ls-files").stdout.split()
        assert (repo / SETTINGS).read_text() == HARMFUL

    def test_a_fresh_repo_with_no_head(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _write(repo, HARMFUL)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None

    def test_no_settings_file_is_a_no_op(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        (repo / "a.txt").write_text("a\n")
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _git(repo, "diff", "--cached", "--name-only").stdout.split() == ["a.txt"]

    @pytest.mark.parametrize("content", [HARMLESS_ENV, DENY_MENTION], ids=["env", "deny"])
    def test_a_committed_copy_that_is_not_a_hook_path_is_left_alone(
            self, tmp_path, shell_guard, run_guard, content):
        """Review of #3019 (blocking): the index copy equals HEAD, so nothing
        new would leak. Untracking it commits a deletion of the template's
        settings from the remote."""
        repo = _repo(tmp_path)
        _write(repo, content)
        _commit(repo, "template settings")
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == content
        assert _git(repo, "diff", "--cached", "--name-only").stdout == ""

    def test_a_deny_rule_naming_opt_trinity_is_not_a_hook_path(
            self, tmp_path, shell_guard, run_guard):
        """Only hook COMMANDS under /opt/trinity/ brick a foreign clone; a
        permission rule that merely names the path is portable."""
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, DENY_MENTION)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == DENY_MENTION

    def test_a_refused_edit_of_a_committed_refused_copy_restores_the_head_copy(
            self, tmp_path, shell_guard, run_guard):
        """HEAD already holds a refused (non-hook) copy: the new content stays
        out, and the HEAD copy stays tracked — no deletion is committed."""
        repo = _repo(tmp_path)
        _write(repo, HARMLESS_ENV)
        _commit(repo, "template settings")
        _write(repo, _with_credential("apiKeyHelper"))
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == HARMLESS_ENV

    def test_a_non_utf8_file_is_refused_not_a_crash(self, tmp_path, shell_guard, run_guard):
        """Review of #3019: a strict decode raised before the check ran, so the
        Python guard failed every cycle while the shell twin refused the file."""
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, NON_UTF8)
        (repo / "w.md").write_text("w\n")
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None
        assert _git(repo, "diff", "--cached", "--name-only").stdout.split() == ["w.md"]


@GUARDS
class TestCredentialBearingSettings:
    """Fix B: with the ignore gone, a credential written to the user settings
    file would be committed and pushed. Same rule, same two homes."""

    def test_the_platform_written_file_still_commits(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, PLATFORM_WRITTEN)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == PLATFORM_WRITTEN

    def test_an_empty_env_block_is_not_a_credential(self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        content = json.dumps({"env": {}, "enabledPlugins": {"x@y": True}}) + "\n"
        _write(repo, content)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == content

    @pytest.mark.parametrize("key", sorted(CREDENTIAL_KEYS))
    def test_a_credential_bearing_key_stays_out_and_on_disk(
            self, tmp_path, shell_guard, run_guard, key):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        content = _with_credential(key)
        _write(repo, content)
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None
        assert (repo / SETTINGS).read_text() == content

    @pytest.mark.parametrize("key", sorted(CREDENTIAL_KEYS))
    def test_adding_a_credential_to_a_clean_tracked_file_restores_the_clean_copy(
            self, tmp_path, shell_guard, run_guard, key):
        repo = _repo(tmp_path)
        _write(repo, PLATFORM_WRITTEN)
        _commit(repo, "platform settings")
        _write(repo, _with_credential(key))
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == PLATFORM_WRITTEN

    def test_a_committed_credential_copy_is_left_in_history_not_deleted(
            self, tmp_path, shell_guard, run_guard):
        """Untracking cannot un-leak what history already holds; it would only
        commit a deletion. Rotating the credential is the remedy, not a commit."""
        repo = _repo(tmp_path)
        content = _with_credential("env")
        _write(repo, content)
        _commit(repo, "leak")
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) == content

    def test_unparseable_content_cannot_be_cleared_so_it_stays_out(
            self, tmp_path, shell_guard, run_guard):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, '{"apiKeyHelper": "echo ' + SECRET_VALUE + '",\n')
        _git(repo, "add", "-A")
        run_guard(repo, shell_guard)
        assert _index_blob(repo) is None


class TestOneRule:
    def test_the_shell_twin_refuses_exactly_the_python_key_set(self, shell_guard):
        py_keys = git_router._CREDENTIAL_SETTINGS_KEYS
        assert set(py_keys) == set(CREDENTIAL_KEYS)
        assert _backend_shell_names()["_CREDENTIAL_SETTINGS_KEYS"] == py_keys
        # The shell twin passes the keys to its checker as bare argv words.
        for key in py_keys:
            assert f" {key} " in shell_guard

    def test_the_shell_twin_survives_the_splice(self, shell_guard):
        """Spliced into `bash -c "..."` and shlex-split by docker-py: a double
        quote, `$`, backslash or backtick would be mangled before bash sees it."""
        assert not set(shell_guard) & set('"$\\`')

    def test_the_log_names_the_reason_never_the_value(self, tmp_path, caplog):
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, _with_credential("env"))
        _git(repo, "add", "-A")
        with caplog.at_level("WARNING"):
            git_router._guard_container_only_settings(repo)
        text = caplog.text
        assert "env" in text and "credential" in text
        assert SECRET_VALUE not in text
        assert "ANTHROPIC_API_KEY" not in text


class TestLogOnce:
    def test_the_same_refused_content_is_logged_once(self, tmp_path, caplog):
        """Review nit: an untracked refused file is re-staged by `git add -A`
        and refused again every 15 minutes — one WARNING per content, not per
        cycle."""
        repo = _repo(tmp_path)
        _commit(repo, "init")
        _write(repo, _with_credential("gcpAuthRefresh"))
        with caplog.at_level("WARNING"):
            for _ in range(3):
                _git(repo, "add", "-A")
                git_router._guard_container_only_settings(repo)
        assert caplog.text.count("kept .claude/settings.json out") == 1
        _write(repo, _with_credential("awsAuthRefresh"))
        with caplog.at_level("WARNING"):
            _git(repo, "add", "-A")
            git_router._guard_container_only_settings(repo)
        assert caplog.text.count("kept .claude/settings.json out") == 2


class TestHeartbeatEndToEnd:
    @pytest.fixture
    def agent(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        _git(remote, "init", "-q", "--bare", "-b", "main")
        repo = _repo(tmp_path, "agent")
        _git(repo, "remote", "add", "origin", str(remote))
        (repo / ".gitignore").write_text(".trinity/\n")
        _commit(repo, "init")
        _git(repo, "push", "-q", "-u", "origin", "main")
        (repo / ".trinity").mkdir()
        return repo, remote

    def test_a_harmful_file_never_reaches_the_remote_and_later_cycles_stay_green(self, agent):
        repo, remote = agent
        _write(repo, HARMFUL)
        (repo / "work.md").write_text("w\n")

        first = git_router._run_auto_sync_once(repo)
        assert first["status"] == "success", first
        remote_files = _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        assert "work.md" in remote_files and SETTINGS not in remote_files
        head = _git(repo, "rev-parse", "HEAD").stdout

        # The trap: the guarded file is still untracked on disk. The old
        # `status --porcelain` test saw it, committed nothing, and failed.
        second = git_router._run_auto_sync_once(repo)
        assert second["status"] == "success", second
        assert _git(repo, "rev-parse", "HEAD").stdout == head, "no empty commit"

    def test_a_credential_in_user_settings_never_reaches_the_remote(self, agent):
        repo, remote = agent
        _write(repo, _with_credential("apiKeyHelper"))
        (repo / "work.md").write_text("w\n")
        assert git_router._run_auto_sync_once(repo)["status"] == "success"
        remote_files = _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        assert "work.md" in remote_files and SETTINGS not in remote_files

    def test_a_clean_settings_file_is_synced(self, agent):
        repo, remote = agent
        _write(repo, CLEAN)
        assert git_router._run_auto_sync_once(repo)["status"] == "success"
        assert SETTINGS in _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()

    def test_a_legacy_committed_copy_is_deleted_from_the_remote(self, agent):
        repo, remote = agent
        _write(repo, HARMFUL)
        _git(repo, "add", "-f", SETTINGS)
        _git(repo, "commit", "-q", "-m", "pre-#2036 leak")
        _git(repo, "push", "-q", "origin", "main")
        assert SETTINGS in _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()

        assert git_router._run_auto_sync_once(repo)["status"] == "success"
        assert SETTINGS not in _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        assert (repo / SETTINGS).read_text() == HARMFUL

    @pytest.mark.parametrize("content", [HARMLESS_ENV, DENY_MENTION], ids=["env", "deny"])
    def test_an_idle_heartbeat_leaves_a_committed_template_settings_file(self, agent, content):
        """Review of #3019 (blocking) repro: a committed template-style file and
        no other change made a `Trinity auto-sync` commit deleting it."""
        repo, remote = agent
        _write(repo, content)
        _git(repo, "add", "-f", SETTINGS)
        _git(repo, "commit", "-q", "-m", "template settings")
        _git(repo, "push", "-q", "origin", "main")
        head = _git(repo, "rev-parse", "HEAD").stdout

        result = git_router._run_auto_sync_once(repo)
        assert result["status"] == "success", result
        assert _git(repo, "rev-parse", "HEAD").stdout == head, "no commit on an idle cycle"
        assert _git(remote, "show", f"main:{SETTINGS}").stdout == content

    def test_a_non_utf8_settings_file_does_not_stop_syncing(self, agent):
        repo, remote = agent
        _write(repo, NON_UTF8)
        (repo / "w.md").write_text("w\n")
        result = git_router._run_auto_sync_once(repo)
        assert result["status"] == "success", result
        remote_files = _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        assert "w.md" in remote_files and SETTINGS not in remote_files


class TestResetPreserveStateEndToEnd:
    """`reset_to_main_preserve_state_impl` (reset route + MCP tool) stages with
    `git add -A`, commits and force-pushes. The file-level ignore used to keep
    the settings file out here too; the content guard must now do it."""

    @pytest.fixture
    def agent(self, tmp_path):
        remote = tmp_path / "remote.git"
        remote.mkdir()
        _git(remote, "init", "-q", "--bare", "-b", "main")
        repo = _repo(tmp_path, "agent")
        _git(repo, "remote", "add", "origin", str(remote))
        (repo / ".gitignore").write_text(".trinity/\n")
        _commit(repo, "init")
        _git(repo, "push", "-q", "-u", "origin", "main")
        return repo, remote

    def test_a_harmful_untracked_file_is_not_committed_or_pushed(self, agent):
        repo, remote = agent
        _write(repo, HARMFUL)

        result = git_router.reset_to_main_preserve_state_impl(
            repo, read_allowlist=lambda: [], skip_push=False)

        assert "error" not in result, result
        assert SETTINGS not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
        assert SETTINGS not in _git(remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        assert (repo / SETTINGS).read_text() == HARMFUL

    def test_a_preserved_harmful_copy_does_not_replace_the_clean_baseline(self, agent):
        repo, remote = agent
        _write(repo, CLEAN)
        _commit(repo, "clean settings")
        _git(repo, "push", "-q", "origin", "main")
        _write(repo, HARMFUL)

        result = git_router.reset_to_main_preserve_state_impl(
            repo, read_allowlist=lambda: [SETTINGS], skip_push=False)

        assert "error" not in result, result
        assert _git(repo, "show", f"HEAD:{SETTINGS}").stdout == CLEAN
        assert _git(remote, "show", f"main:{SETTINGS}").stdout == CLEAN
        assert (repo / SETTINGS).read_text() == HARMFUL

    def test_a_committed_template_settings_file_survives_the_force_push(self, agent):
        repo, remote = agent
        _write(repo, HARMLESS_ENV)
        _commit(repo, "template settings")
        _git(repo, "push", "-q", "origin", "main")

        result = git_router.reset_to_main_preserve_state_impl(
            repo, read_allowlist=lambda: [], skip_push=False)

        assert "error" not in result, result
        assert _git(repo, "show", f"HEAD:{SETTINGS}").stdout == HARMLESS_ENV
        assert _git(remote, "show", f"main:{SETTINGS}").stdout == HARMLESS_ENV


class TestWiring:
    """Push hardcodes /home/developer, so its wiring is pinned structurally; the
    behaviour it wires is the one TestTheRule executes."""

    def _calls_in(self, fn_name):
        tree = ast.parse(Path(git_router.__file__).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name)
        return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]

    @pytest.mark.parametrize("fn_name", ["sync_to_github", "_run_auto_sync_once"])
    def test_every_agent_commit_path_guards_after_staging_and_before_commit(self, fn_name):
        calls = self._calls_in(fn_name)

        def line_of(pred):
            return min(c.lineno for c in calls if pred(c))

        def is_git(verb):
            return lambda c: any(isinstance(a, ast.List) and len(a.elts) > 1
                                 and isinstance(a.elts[1], ast.Constant)
                                 and a.elts[1].value == verb for a in c.args)

        guard = line_of(lambda c: getattr(c.func, "id", None) == "_guard_container_only_settings")
        assert line_of(is_git("add")) < guard < line_of(is_git("commit"))

    def test_reset_preserve_state_guards_after_staging_and_before_commit(self):
        calls = self._calls_in("reset_to_main_preserve_state_impl")

        def git_verb(c):
            return (getattr(c.func, "id", None) == "_git" and c.args
                    and isinstance(c.args[0], ast.List) and c.args[0].elts
                    and getattr(c.args[0].elts[0], "value", None))

        add = min(c.lineno for c in calls if git_verb(c) == "add")
        commit = min(c.lineno for c in calls if git_verb(c) == "commit")
        guards = [c.lineno for c in calls
                  if getattr(c.func, "id", None) == "_guard_container_only_settings"]
        assert guards and add < min(guards) < commit

    def test_initialize_splices_the_guard_after_every_git_add(self):
        src = (_ROOT / "src/backend/services/git_service/provisioning.py").read_text()
        adds = src.count("'git add .',")
        guards = src.count("'git add .',\n            gitignore.CONTAINER_ONLY_SETTINGS_GUARD,")
        assert adds == guards == 2

    def test_settings_json_is_not_a_canonical_ignore(self, shell_guard):
        src = (_ROOT / "src/backend/services/git_service/gitignore.py").read_text()
        tree = ast.parse(src)
        patterns = next(n for n in ast.walk(tree) if isinstance(n, ast.AnnAssign)
                        and getattr(n.target, "id", "") == "_GITIGNORE_PATTERNS")
        values = {e.value for e in patterns.value.elts if isinstance(e, ast.Constant)}
        assert SETTINGS not in values


def _gitignore_module():
    try:
        import services.git_service.gitignore as gs
    except Exception:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return gs


class TestExistingAgentsLoseTheOldIgnoreLine:
    """Review of #3019 (should-fix): the merge strips only managed lines, so an
    agent whose `.gitignore` came from the pre-ent#708 list kept
    `.claude/settings.json` — migrated into the USER region, still ignoring the
    template's hooks. The line is superseded, but only inside a container whose
    agent server carries the content guard: a pre-guard base image would
    otherwise commit a legacy `/opt/trinity/` copy or a credential."""

    AGENT_GIT_PY = str(Path(git_router.__file__).resolve())

    def _merge(self, gs, repo):
        subprocess.run(shlex.split(gs._build_gitignore_merge_command(str(repo))),
                       cwd=str(repo), check=True, capture_output=True, timeout=20)

    def _old_repo(self, tmp_path, monkeypatch, gs):
        repo = _repo(tmp_path)
        (repo / ".gitignore").write_text("my-rule/\n")
        with monkeypatch.context() as m:
            # The pre-ent#708 canonical list: the line sat in the defaults block.
            patterns = list(gs._GITIGNORE_PATTERNS)
            patterns.insert(patterns.index(".claude/plugins/") + 1, SETTINGS)
            m.setattr(gs, "_GITIGNORE_PATTERNS", tuple(patterns))
            m.setattr(gs, "_GITIGNORE_MANAGED_LINES",
                      tuple(patterns) + gs._GITIGNORE_SUPERSEDED_LINES + gs._GITIGNORE_MARKERS)
            m.setattr(gs, "_SETTINGS_GUARD_PROBE_PATH", str(tmp_path / "absent.py"))
            self._merge(gs, repo)
        _write(repo, CLEAN)
        assert _git(repo, "check-ignore", "-q", SETTINGS, check=False).returncode == 0
        return repo

    def test_a_guarded_image_drops_the_old_line_on_merge(self, tmp_path, monkeypatch):
        gs = _gitignore_module()
        repo = self._old_repo(tmp_path, monkeypatch, gs)
        monkeypatch.setattr(gs, "_SETTINGS_GUARD_PROBE_PATH", self.AGENT_GIT_PY)
        self._merge(gs, repo)
        lines = (repo / ".gitignore").read_text().splitlines()
        assert SETTINGS not in lines
        assert "my-rule/" in lines
        assert _git(repo, "check-ignore", "-q", SETTINGS, check=False).returncode == 1

    def test_a_pre_guard_image_keeps_the_old_line(self, tmp_path, monkeypatch):
        gs = _gitignore_module()
        repo = self._old_repo(tmp_path, monkeypatch, gs)
        pre_guard = tmp_path / "old_git.py"
        pre_guard.write_text("# an agent server without the content guard\n")
        monkeypatch.setattr(gs, "_SETTINGS_GUARD_PROBE_PATH", str(pre_guard))
        self._merge(gs, repo)
        assert _git(repo, "check-ignore", "-q", SETTINGS, check=False).returncode == 0

    def test_the_probe_names_the_real_agent_server_guard(self):
        gs = _gitignore_module()
        assert gs._SETTINGS_GUARD_PROBE_PATH == "/app/agent_server/routers/git.py"
        assert gs._SETTINGS_GUARD_PROBE_TOKEN == "_guard_container_only_settings"
        assert hasattr(git_router, gs._SETTINGS_GUARD_PROBE_TOKEN)
        assert SETTINGS in gs._GITIGNORE_GUARD_GATED_SUPERSEDED_LINES
        assert SETTINGS not in gs._GITIGNORE_MANAGED_LINES
