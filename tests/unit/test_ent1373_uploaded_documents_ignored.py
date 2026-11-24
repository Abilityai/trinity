"""Uploaded documents never reach an agent's repository
(abilityai/trinity-enterprise#1373).

The platform writes the files a person hands an agent into the agent's home,
which is its repository root: one folder per person under `inbox/`, one
folder per session under `uploads/`. Two managed patterns, `/inbox/*/**` and
`/uploads/*/**`, keep them out of the auto-sync's `git add -A`, and match only
those SUBFOLDERS, so a file the agent itself writes directly under `inbox/`
keeps syncing.

Every test runs the shipped builders against a real temporary git repo, the
same way `test_3727_policy_limits_stamp_ignored.py` does — ignore semantics
are git's, never re-implemented here.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock


_project_root = Path(__file__).resolve().parents[2]
backend_path = str(_project_root / "src" / "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)


PERSON_DOC = "inbox/ada-1a2b3c4d/contract.pdf"
LEGACY_PERSON_DOC = "inbox/ada/notes.docx"
DOT_PERSON_DOC = "inbox/.hidden/z.pdf"
SPACED_DOC = "inbox/ada-1a2b3c4d/board minutes.pdf"
SESSION_DOC = "uploads/sess-123/photo.png"
# Agent-authored content that must keep syncing.
ROOT_INBOX_FILE = "inbox/batch.json"
ROOT_UPLOADS_FILE = "uploads/readme.txt"
KB_INBOX = "Brain/00-Inbox/note.md"
NESTED_INBOX = "x/inbox/p/t.md"

DOCS = (PERSON_DOC, LEGACY_PERSON_DOC, DOT_PERSON_DOC, SPACED_DOC, SESSION_DOC)
AUTHORED = (ROOT_INBOX_FILE, ROOT_UPLOADS_FILE, KB_INBOX, NESTED_INBOX)
NEW_PATTERNS = ("/inbox/*/**", "/uploads/*/**")

_STUBBED_MODULE_NAMES = (
    "docker", "docker.errors", "docker.types",
    "redis", "redis.asyncio",
    "database",
    "services.docker_service",
)


def _load_gitignore(monkeypatch):
    for name in _STUBBED_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, name, Mock())
    sys.modules["database"].db = Mock()
    sys.modules["database"].AgentGitConfig = Mock
    sys.modules["database"].GitSyncResult = Mock
    for key in list(sys.modules):
        if key.startswith("services.git_service"):
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.git_service.gitignore as gs
    return gs


def _git(repo: Path):
    def run(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True,
            check=check, timeout=10,
        )
    return run


def _init_repo(repo: Path):
    git = _git(repo)
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    return git


def _write(repo: Path, rel: str, content: str = "x"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _run_shell(command: str, repo: Path) -> str:
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, (
        f"command failed in {repo}\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    return result.stdout


def _merge(gs, repo: Path) -> str:
    return _run_shell(gs._build_gitignore_merge_command(str(repo)), repo)


def _pre_fix(gs, monkeypatch):
    """The managed block as it was before this fix — the state every existing
    agent's `.gitignore` is in."""
    monkeypatch.setattr(
        gs, "_GITIGNORE_PATTERNS",
        tuple(p for p in gs._GITIGNORE_PATTERNS if p not in NEW_PATTERNS),
    )
    monkeypatch.setattr(
        gs, "_GITIGNORE_MANAGED_LINES",
        tuple(p for p in gs._GITIGNORE_MANAGED_LINES if p not in NEW_PATTERNS),
    )


def _commit_everything_pre_fix(gs, repo, git, monkeypatch, extra_user_lines=""):
    for rel in DOCS + AUTHORED:
        _write(repo, rel)
    with monkeypatch.context() as m:
        _pre_fix(gs, m)
        _merge(gs, repo)
    if extra_user_lines:
        with open(repo / ".gitignore", "a") as fh:
            fh.write(extra_user_lines)
    git("add", "-A")
    git("commit", "-q", "-m", "auto-sync before the fix")
    tracked = set(git("ls-files").stdout.splitlines())
    assert set(DOCS) <= tracked, "fixture did not track the documents"


def _staged(git):
    return set(git("diff", "--cached", "--name-only").stdout.splitlines())


# --- (a) a fresh agent -------------------------------------------------------

def test_add_all_stages_no_uploaded_document(tmp_path, monkeypatch):
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    for rel in DOCS + AUTHORED:
        _write(tmp_path, rel)

    _merge(gs, tmp_path)
    git("add", "-A")
    staged = _staged(git)

    leaked = sorted(set(DOCS) & staged)
    assert not leaked, f"uploaded documents staged by `git add -A`: {leaked}"
    missing = sorted(set(AUTHORED) - staged)
    assert not missing, f"agent-authored files stopped syncing: {missing}"


def test_merge_is_idempotent(tmp_path, monkeypatch):
    gs = _load_gitignore(monkeypatch)
    _init_repo(tmp_path)
    _merge(gs, tmp_path)
    first = (tmp_path / ".gitignore").read_text()
    _merge(gs, tmp_path)
    assert (tmp_path / ".gitignore").read_text() == first
    for pattern in NEW_PATTERNS:
        assert first.splitlines().count(pattern) == 1, pattern


# --- (b) an agent that already committed documents ---------------------------

def test_push_sweep_untracks_committed_documents(tmp_path, monkeypatch):
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _commit_everything_pre_fix(gs, tmp_path, git, monkeypatch)

    merge_out = _merge(gs, tmp_path)
    sweep_out = _run_shell(gs._build_rm_cached_ignored_command(str(tmp_path)), tmp_path)

    tracked = set(git("ls-files").stdout.splitlines())
    assert not (set(DOCS) & tracked), sorted(set(DOCS) & tracked)
    assert set(AUTHORED) <= tracked, sorted(set(AUTHORED) - tracked)
    for rel in DOCS:
        assert (tmp_path / rel).exists(), f"sweep deleted {rel} from disk"

    report = gs.gitignore_sweep._parse_gitignore_sweep(merge_out, sweep_out)
    assert set(DOCS) <= set(report.removed), report
    assert report.removed_count == len(DOCS)


# --- (c) keeping one on purpose ----------------------------------------------

def test_negation_keeps_a_document_tracked(tmp_path, monkeypatch):
    """A per-file negation below the managed block, written without a leading
    slash, survives the Push sweep. Under a dir-form rule (`/inbox/*/`) the
    negation would be inert and this goes red."""
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _commit_everything_pre_fix(gs, tmp_path, git, monkeypatch)
    with open(tmp_path / ".gitignore", "a") as fh:
        fh.write(f"!{PERSON_DOC}\n")

    _merge(gs, tmp_path)
    _run_shell(gs._build_rm_cached_ignored_command(str(tmp_path)), tmp_path)

    assert PERSON_DOC in set(git("ls-files").stdout.splitlines())
    assert git("ls-files", "-ci", "--exclude-standard").stdout.strip() == ""
    no_index = git("check-ignore", "--no-index", PERSON_DOC, check=False)
    assert no_index.returncode == 1, f"still ignored: {no_index.stdout!r}"
    # The negation is narrow: the person's other documents stay ignored.
    assert git("check-ignore", "--no-index", SPACED_DOC, check=False).returncode == 0


# --- (d) a force-replace pull spares the documents ---------------------------

def test_force_reset_clean_spares_documents(tmp_path, monkeypatch):
    """`force_reset` runs `git clean -fd` (no `-x`): it deletes every untracked
    file that is NOT ignored. Before the fix that included the documents."""
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _merge(gs, tmp_path)
    git("add", ".gitignore")
    git("commit", "-q", "-m", "init")
    for rel in DOCS:
        _write(tmp_path, rel)
    _write(tmp_path, ROOT_INBOX_FILE)  # control

    git("clean", "-fd")

    for rel in DOCS:
        assert (tmp_path / rel).exists(), f"`git clean -fd` deleted {rel}"
    assert not (tmp_path / ROOT_INBOX_FILE).exists(), (
        "control: an untracked authored file directly under inbox/ is not ignored"
    )


# --- (f) the shadow probe and an anchored negation ---------------------------

def test_shadow_probe_survives_an_anchored_negation(tmp_path, monkeypatch):
    """`!/inbox/...` used to reach `git check-ignore --stdin` as `/inbox/...`,
    which git rejects as outside the repository and aborts on, dropping every
    later `!` line from the report."""
    gs = _load_gitignore(monkeypatch)
    git = _init_repo(tmp_path)
    _merge(gs, tmp_path)
    # Agent rules below the block: an anchored negation first, then a negation
    # a managed rule defeats (`.ssh/` is dir-form, so it is inert).
    with open(tmp_path / ".gitignore", "a") as fh:
        fh.write(f"!/{PERSON_DOC}\n!.ssh/config\n")
    git("add", ".gitignore")
    git("commit", "-q", "-m", "init")

    sweep_out = _run_shell(gs._build_rm_cached_ignored_command(str(tmp_path)), tmp_path)
    report = gs.gitignore_sweep._parse_gitignore_sweep("", sweep_out)
    assert "!.ssh/config -> .ssh/" in report.shadowed, report


# --- (e) the start-path untrack ----------------------------------------------
#
# The one exception to #2069's "start = prevent only": an auto-sync agent that
# never Pushes would otherwise keep committing a re-dropped document under the
# same name, because the Push sweep is the only other place that untracks.

def _load_clone(monkeypatch):
    gs = _load_gitignore(monkeypatch)
    monkeypatch.setitem(sys.modules, "services.agent_auth", Mock())
    import services.git_service.gitignore_clone as gc
    return gs, gc


def test_start_untrack_is_narrow(tmp_path, monkeypatch):
    """Untracks committed documents only: not the agent's root `inbox/` file,
    not a kept (negated) document, and not a file another managed rule would
    sweep on Push (`node_modules/`) — that stays the Push's job."""
    gs, gc = _load_clone(monkeypatch)
    git = _init_repo(tmp_path)
    _write(tmp_path, "node_modules/pkg/index.js")
    git("add", "node_modules/pkg/index.js")
    git("commit", "-q", "-m", "an old commit of a dependency dir")
    _commit_everything_pre_fix(
        gs, tmp_path, git, monkeypatch, extra_user_lines=f"!{LEGACY_PERSON_DOC}\n"
    )

    _merge(gs, tmp_path)
    out = _run_shell(gc._build_untrack_dropped_documents_command(str(tmp_path)), tmp_path)

    tracked = set(git("ls-files").stdout.splitlines())
    swept = set(DOCS) - {LEGACY_PERSON_DOC}
    assert not (swept & tracked), sorted(swept & tracked)
    assert LEGACY_PERSON_DOC in tracked, "a negated document was untracked"
    assert set(AUTHORED) <= tracked, sorted(set(AUTHORED) - tracked)
    assert "node_modules/pkg/index.js" in tracked, "start untrack is not narrow"
    for rel in DOCS:
        assert (tmp_path / rel).exists()

    report = gs.gitignore_sweep._parse_gitignore_sweep("", out)
    assert set(report.removed) == swept, report
    assert report.removed_count == len(swept)
    # Idempotent: nothing left to report the second time.
    again = _run_shell(gc._build_untrack_dropped_documents_command(str(tmp_path)), tmp_path)
    assert not gs.gitignore_sweep._parse_gitignore_sweep("", again).changed_tracking


def _ready(gc, gs, monkeypatch, exec_mock):
    from unittest.mock import AsyncMock
    import asyncio
    monkeypatch.setattr(gc, "_MERGE_READY_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(gc, "_MERGE_READY_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(gc, "_MERGE_EXEC_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(gc, "_gitignore_merge_semaphore", asyncio.Semaphore(8))
    monkeypatch.setattr(gc, "_probe_agent_server_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(gc, "_container_has_git_dir", AsyncMock(return_value=True))
    monkeypatch.setattr(gs, "_git_toplevel", AsyncMock(return_value="/home/developer"))
    monkeypatch.setattr(gc, "execute_command_in_container", exec_mock)
    alert = AsyncMock()
    monkeypatch.setattr(gs.gitignore_sweep, "_emit_gitignore_untracked_alert", alert)
    return alert


def test_start_merge_untracks_and_alerts(monkeypatch):
    from unittest.mock import AsyncMock
    import asyncio
    gs, gc = _load_clone(monkeypatch)
    tag = gs.gitignore_sweep
    sweep_out = (
        f"{tag._SWEEP_TAG_REMOVED_COUNT}1\n{tag._SWEEP_TAG_REMOVED}{PERSON_DOC}\n"
    )
    exec_mock = AsyncMock(side_effect=[
        {"exit_code": 0, "output": ""},
        {"exit_code": 0, "output": sweep_out},
    ])
    alert = _ready(gc, gs, monkeypatch, exec_mock)

    asyncio.run(gc.merge_gitignore_after_clone("a1", untrack_dropped_documents=True))

    commands = [c.kwargs["command"] for c in exec_mock.await_args_list]
    assert commands == [
        gs._build_gitignore_merge_command("/home/developer"),
        gc._build_untrack_dropped_documents_command("/home/developer"),
    ]
    alert.assert_awaited_once()
    name, sweep = alert.await_args.args
    assert name == "a1" and sweep.removed == (PERSON_DOC,)


def test_start_merge_does_not_alert_on_a_failed_untrack(monkeypatch):
    from unittest.mock import AsyncMock
    import asyncio
    gs, gc = _load_clone(monkeypatch)
    tag = gs.gitignore_sweep
    exec_mock = AsyncMock(side_effect=[
        {"exit_code": 0, "output": ""},
        {"exit_code": 1, "output": f"{tag._SWEEP_TAG_REMOVED}{PERSON_DOC}\n"},
    ])
    alert = _ready(gc, gs, monkeypatch, exec_mock)
    asyncio.run(gc.merge_gitignore_after_clone("a1", untrack_dropped_documents=True))
    alert.assert_not_awaited()


def test_creation_merge_never_untracks(monkeypatch):
    from unittest.mock import AsyncMock
    import asyncio
    gs, gc = _load_clone(monkeypatch)
    exec_mock = AsyncMock(return_value={"exit_code": 0, "output": ""})
    alert = _ready(gc, gs, monkeypatch, exec_mock)
    asyncio.run(gc.merge_gitignore_after_clone("a1"))
    assert exec_mock.await_count == 1
    alert.assert_not_awaited()


def test_spawn_forwards_the_flag(monkeypatch):
    from unittest.mock import AsyncMock
    import asyncio
    gs, gc = _load_clone(monkeypatch)
    merge = AsyncMock()
    monkeypatch.setattr(gc, "merge_gitignore_after_clone", merge)

    async def go():
        gc.spawn_gitignore_merge_after_clone("a1", untrack_dropped_documents=True)
        await asyncio.gather(*list(gc._inflight_gitignore_merge_tasks))

    asyncio.run(go())
    merge.assert_awaited_once_with("a1", untrack_dropped_documents=True)


def _spawn_calls(path: Path, fn_name: str):
    import ast
    tree = ast.parse(path.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
    )
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "spawn_gitignore_merge_after_clone"
    ]


def _flag(call):
    for kw in call.keywords:
        if kw.arg == "untrack_dropped_documents":
            return getattr(kw.value, "value", None)
    return False


def test_start_path_asks_for_the_untrack_and_creation_does_not():
    """The production call sites: start/recreate passes the flag, creation
    (PREVENT only, #2069) does not."""
    svc = _project_root / "src" / "backend" / "services" / "agent_service"
    start = _spawn_calls(svc / "lifecycle.py", "start_agent_internal")
    assert start and all(_flag(c) is True for c in start), "start path lost the untrack"
    creation = _spawn_calls(svc / "crud.py", "_materialize_agent_files")
    assert creation and all(_flag(c) is False for c in creation), "creation must not untrack"
