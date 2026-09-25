"""#2957 — `GET /api/git/status` dropped the first character of the first path.

`_compute_git_status` ran `git status --porcelain`, then `stdout.strip()` before
splitting on newlines. Porcelain v1 lines are `XY PATH`, and when the FIRST line
is an unstaged modification its X column is a space — so the outer `.strip()`
ate it, `line[3:]` then started one character late, and ` M .a.txt` came back as
`a.txt`. The same parse also returned `"old -> new"` for renames and C-quoted
strings for paths with spaces, neither of which exists on disk.

The fix reads `git --no-optional-locks status --porcelain -z` and splits on NUL
(`_parse_porcelain_z`): no outer strip, no quoting, and a rename/copy's origin
arrives as its own record (surfaced as the additive `orig_path`).

The real-git test pins a NON-ambient first entry: ` M .a.txt` beside a real
`a.txt`, so the corrupted path collides with a real one instead of vanishing.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Explicit file-based loader, evicting any previously cached `agent_server`
# (shadow or real) so it wins regardless of sys.path order — same pattern as
# test_1595_git_maintenance.py / test_agent_server_auto_sync.py.
# Evict ONLY when the registered `agent_server` is not the real base-image
# package. An unconditional eviction here re-registers the package under a
# fresh module object, and any earlier-collected file that bound a function
# from the old copy and later patches by dotted string (test_drain_bounded.py)
# patches the wrong copy — the REAL drain then runs and its cgroup orphan
# sweep kills the CI runner / a developer's desktop session (#728 class,
# trinity-enterprise#620). Guarded pattern: test_git_status_dual_ahead_behind.py;
# enforced by tests/lint_sys_modules.py.
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _AS_INIT = _BASE_IMAGE / "agent_server" / "__init__.py"
    _as_spec = importlib.util.spec_from_file_location(
        "agent_server", str(_AS_INIT),
        submodule_search_locations=[str(_BASE_IMAGE / "agent_server")],
    )
    _as_mod = importlib.util.module_from_spec(_as_spec)
    sys.modules["agent_server"] = _as_mod
    _as_spec.loader.exec_module(_as_mod)

from agent_server.routers import git as git_mod  # noqa: E402

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    name for name in sys.modules
    if name == "agent_server" or name.startswith("agent_server.")
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# Fixtures — real throwaway git repos (local + bare remote, so `git fetch
# origin` works offline), same shape as test_2742_git_status_lock_free.py.
# ---------------------------------------------------------------------------


def _run(cmd, cwd, **kw):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60, **kw
    )


def _init_repo(local_dir: Path, remote_dir: Path) -> None:
    _run(["git", "init", "--bare", "-b", "main"], remote_dir)
    _run(["git", "init", "-b", "main"], local_dir)
    _run(["git", "config", "user.email", "test@test.com"], local_dir)
    _run(["git", "config", "user.name", "Test"], local_dir)
    _run(["git", "config", "commit.gpgsign", "false"], local_dir)
    _run(["git", "remote", "add", "origin", str(remote_dir)], local_dir)
    (local_dir / "README.md").write_text("hello")
    _run(["git", "add", "."], local_dir)
    _run(["git", "commit", "-m", "initial"], local_dir)
    _run(["git", "push", "-u", "origin", "main"], local_dir)


@pytest.fixture
def repo(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _init_repo(local, remote)
    (local / ".trinity").mkdir()
    yield local
    shutil.rmtree(local, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)


@pytest.fixture
def status_home(repo, monkeypatch):
    monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", repo)
    yield repo


# ---------------------------------------------------------------------------
# The parser, table-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stdout, expected",
    [
        pytest.param(
            " M .a.txt\0M  a.txt\0",
            [{"status": "M", "path": ".a.txt"}, {"status": "M", "path": "a.txt"}],
            id="unstaged-M-first-keeps-its-first-char",
        ),
        pytest.param(
            "?? inbox/\0",
            [{"status": "??", "path": "inbox/"}],
            id="untracked-first",
        ),
        pytest.param(
            "M  s.txt\0MM both.txt\0A  new.txt\0",
            [
                {"status": "M", "path": "s.txt"},
                {"status": "MM", "path": "both.txt"},
                {"status": "A", "path": "new.txt"},
            ],
            id="staged-mixed-added",
        ),
        pytest.param(
            "R  new.txt\0old.txt\0",
            [{"status": "R", "path": "new.txt", "orig_path": "old.txt"}],
            id="rename",
        ),
        pytest.param(
            "RM new.txt\0old.txt\0",
            [{"status": "RM", "path": "new.txt", "orig_path": "old.txt"}],
            id="rename-then-modified",
        ),
        pytest.param(
            "C  dst.txt\0src.txt\0 M after.txt\0",
            [
                {"status": "C", "path": "dst.txt", "orig_path": "src.txt"},
                {"status": "M", "path": "after.txt"},
            ],
            id="copy-origin-consumed-next-record-intact",
        ),
        pytest.param(
            " M sp ace.txt\0",
            [{"status": "M", "path": "sp ace.txt"}],
            id="space-unquoted",
        ),
        pytest.param(
            " M caf\u00e9.txt\0",
            [{"status": "M", "path": "caf\u00e9.txt"}],
            id="non-ascii-unquoted",
        ),
        pytest.param("", [], id="empty"),
        pytest.param(
            "?? only.txt\0",
            [{"status": "??", "path": "only.txt"}],
            id="single-record-trailing-nul",
        ),
    ],
)
def test_parse_porcelain_z(stdout, expected):
    assert git_mod._parse_porcelain_z(stdout) == expected


def test_truncated_rename_omits_orig_path_and_does_not_raise():
    assert git_mod._parse_porcelain_z("R  new.txt\0") == [
        {"status": "R", "path": "new.txt"}
    ]


# ---------------------------------------------------------------------------
# Through the real route computation, against real git
# ---------------------------------------------------------------------------


def test_status_changes_are_real_paths(status_home):
    r = status_home
    for name in (".a.txt", "a.txt", "both.txt", "old.txt", "sp ace.txt"):
        (r / name).write_text("v1")
    (r / "dir").mkdir()
    (r / "dir" / "a.txt").write_text("v1")
    _run(["git", "add", "-A"], r)
    _run(["git", "commit", "-m", "fixture"], r)

    for name in (".a.txt", "a.txt", "sp ace.txt"):  # unstaged edits
        (r / name).write_text("v2")
    (r / "dir" / "a.txt").write_text("v2")
    _run(["git", "add", "dir/a.txt"], r)  # staged edit
    (r / "both.txt").write_text("v2")
    _run(["git", "add", "both.txt"], r)
    (r / "both.txt").write_text("v3")  # staged + unstaged
    _run(["git", "mv", "old.txt", "new.txt"], r)  # rename

    raw = _run(["git", "status", "--porcelain"], r).stdout
    assert raw.startswith(" M .a.txt\n"), (
        f"fixture degraded — the first entry must be an unstaged ` M`: {raw!r}"
    )

    payload = git_mod._compute_git_status(r)
    changes = payload["changes"]

    assert changes == [
        {"status": "M", "path": ".a.txt"},
        {"status": "M", "path": "a.txt"},
        {"status": "MM", "path": "both.txt"},
        {"status": "M", "path": "dir/a.txt"},
        {"status": "R", "path": "new.txt", "orig_path": "old.txt"},
        {"status": "M", "path": "sp ace.txt"},
    ]
    paths = [c["path"] for c in changes]
    assert len(paths) == len(set(paths)), "path is the GitPanel v-for key"
    for c in changes:
        assert (r / c["path"]).exists(), c
    assert payload["changes_count"] == len(changes)


# ---------------------------------------------------------------------------
# -z emits RAW filename bytes (plain porcelain C-quoted them to ASCII), so a
# strict UTF-8 decode of one non-UTF-8 name used to 500 the whole status.
# ---------------------------------------------------------------------------


def test_run_registered_forwards_errors_handler():
    """Filesystem-independent: the child writes an invalid UTF-8 byte."""
    from agent_server.utils import registered_run

    argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9')"]
    with pytest.raises(UnicodeDecodeError):
        registered_run.run_registered(argv, timeout=30)  # default stays strict
    out = registered_run.run_registered(argv, timeout=30, errors="backslashreplace")
    assert out.stdout == "caf\\xe9"


def test_status_survives_a_non_utf8_filename(status_home):
    r = status_home
    try:
        with open(bytes(r) + b"/caf\xe9.txt", "wb") as fh:
            fh.write(b"x")
    except OSError as exc:  # APFS rejects invalid UTF-8 names; Linux ext4 does not
        pytest.skip(f"filesystem refuses non-UTF-8 names: {exc}")
    (r / "README.md").write_text("edited")

    payload = git_mod._compute_git_status(r)

    assert payload["changes"] == [
        {"status": "M", "path": "README.md"},
        {"status": "??", "path": "caf\\xe9.txt"},
    ]
