"""Unit tests for the reset-preserve-state compose routine (S3, #384).

Tests the `reset_to_main_preserve_state_impl` helper in
`agent_server/routers/git.py`. Same mirror-plus-drift-detection pattern
as `test_git_pull_branch.py` and `test_persistent_state_reader.py` — the
agent-server uses relative imports that only resolve inside the container
image, so we cannot import the implementation directly.

Coverage:
- Drift detection: source signatures in git.py match what the mirror
  expects.
- `no_git_config` guard: no `.git` dir → early return, no git commands
  run.
- `no_remote_main` guard: repo exists, origin set, but origin/main is
  missing → returns that specific error code so the backend can surface
  an operator-readable message.
- `empty_allowlist_safe_noop` invariant (§S3 in the proposal): when the
  allowlist is empty the snapshot is empty and no files are overlaid —
  but reset + commit still run (the point of the operation).
- Success path: the full snapshot → reset → overlay → commit sequence
  leaves the working tree with main's baseline plus the allowlisted
  files, and emits the exact commit message the spec requires.
"""
import io
import subprocess
import tarfile
from fnmatch import fnmatch
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


_GIT_PY = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "agent_server"
    / "routers"
    / "git.py"
)


# ---------------------------------------------------------------------------
# Local copies of the snapshot primitives (identical to the mirror in
# test_reset_preserve_state_allowlist.py — kept local to avoid the unit
# test suite growing cross-file imports).
# ---------------------------------------------------------------------------


def _matches_any(rel_path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("/") or ".." in pattern.split("/"):
            continue
        prefix = pattern.rstrip("/*")
        if pattern.endswith("/**") and (
            rel_path == prefix or rel_path.startswith(prefix + "/")
        ):
            return True
        if fnmatch(rel_path, pattern):
            return True
    return False


def _collect_files(home_dir: Path, patterns: list[str]) -> list[str]:
    collected: list[str] = []
    if not patterns:
        return collected
    for candidate in home_dir.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            rel = candidate.relative_to(home_dir).as_posix()
        except ValueError:
            continue
        if ".." in rel.split("/"):
            continue
        if _matches_any(rel, patterns):
            collected.append(rel)
    return sorted(set(collected))


def _build_snapshot(home_dir: Path, paths: list[str]) -> tuple[bytes, list[str]]:
    files = _collect_files(home_dir, paths)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in files:
            tf.add(home_dir / rel, arcname=rel)
    return buf.getvalue(), files


def _restore_from_tar(
    home_dir: Path, tar_bytes: bytes, paths: list[str]
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    skipped: list[str] = []
    home_resolved = home_dir.resolve()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
        for member in tf.getmembers():
            name = member.name
            if (
                name.startswith("/")
                or ".." in Path(name).parts
                or not _matches_any(name, paths)
            ):
                skipped.append(name)
                continue
            target = (home_dir / name).resolve()
            try:
                target.relative_to(home_resolved)
            except ValueError:
                skipped.append(name)
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                skipped.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
            restored.append(name)
    return sorted(restored), sorted(skipped)


# ---------------------------------------------------------------------------
# Mirror of reset_to_main_preserve_state_impl.
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def reset_to_main_preserve_state_mirror(
    home_dir: Path,
    read_allowlist: Callable[[], list[str]],
    skip_push: bool = True,
) -> dict:
    """Host-side mirror of the agent-server compose routine.

    The real implementation hard-codes home_dir=/home/developer and uses the
    S4 reader by default. Here both are parameterised so we can drive the
    routine against a tmpdir + a stub allowlist.
    """
    if not (home_dir / ".git").exists():
        return {"error": "no_git_config"}

    if _git(["remote", "get-url", "origin"], home_dir).returncode != 0:
        return {"error": "no_git_config"}

    _git(["fetch", "origin", "main"], home_dir)
    if _git(["rev-parse", "--verify", "origin/main"], home_dir).returncode != 0:
        return {"error": "no_remote_main"}

    current = _git(["rev-parse", "--abbrev-ref", "HEAD"], home_dir)
    if current.returncode != 0:
        return {"error": "no_git_config"}
    working_branch = current.stdout.strip()

    patterns = read_allowlist()
    iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    backup_rel = Path(".trinity/backup") / iso
    backup_dir = home_dir / backup_rel
    backup_dir.mkdir(parents=True, exist_ok=True)

    tar_bytes, files_preserved = _build_snapshot(home_dir, patterns)
    (backup_dir / "snapshot.tar").write_bytes(tar_bytes)
    (backup_dir / "files.txt").write_text("\n".join(files_preserved) + "\n")

    reset_res = _git(["reset", "--hard", "origin/main"], home_dir)
    if reset_res.returncode != 0:
        return {"error": "reset_failed", "stderr": reset_res.stderr}

    restored, _skipped = _restore_from_tar(home_dir, tar_bytes, patterns)

    _git(["add", "-A"], home_dir)
    commit_res = _git(
        ["commit", "-m", "Adopt main baseline, preserve state", "--allow-empty"],
        home_dir,
    )
    if commit_res.returncode != 0:
        return {"error": "commit_failed", "stderr": commit_res.stderr}

    commit_sha = _git(["rev-parse", "HEAD"], home_dir).stdout.strip()

    if not skip_push:
        push_res = _git(
            ["push", "--force-with-lease", "origin", f"HEAD:{working_branch}"],
            home_dir,
            timeout=120,
        )
        if push_res.returncode != 0:
            return {
                "error": "push_failed",
                "stderr": push_res.stderr,
                "commit_sha": commit_sha,
            }

    return {
        "snapshot_dir": str(backup_rel) + "/",
        "files_preserved": restored,
        "commit_sha": commit_sha,
        "working_branch": working_branch,
    }


# ---------------------------------------------------------------------------
# Drift guard
# ---------------------------------------------------------------------------


def test_reset_impl_source_has_expected_signatures():
    """git.py must expose the compose routine with the spec's contract."""
    source = _GIT_PY.read_text()
    assert (
        "def reset_to_main_preserve_state_impl(" in source
    ), "compose routine renamed or removed"
    assert '"Adopt main baseline, preserve state"' in source, (
        "commit message drifted — spec requires exact 'Adopt main "
        "baseline, preserve state'"
    )
    assert '"--force-with-lease"' in source, (
        "push must use --force-with-lease, not bare --force"
    )
    assert '"/api/git/reset-to-main-preserve-state"' in source, (
        "agent-server route missing"
    )
    assert "from .snapshot import build_snapshot, restore_from_tar" in source
    assert "from .files import _read_persistent_state" in source, (
        "reset routine must consume S4's allowlist reader, not re-implement it"
    )


# ---------------------------------------------------------------------------
# Test fixtures — bare-origin + worker repo with parallel history
# ---------------------------------------------------------------------------


def _init_bare_origin(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )


def _commit(cwd: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@e",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            message,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _setup_parallel_history(tmp_path: Path) -> Path:
    """Build: bare origin with main + trinity/demo/abc parallel histories.

    Returns: path to the worker clone sitting on trinity/demo/abc.
    The worker has workspace state that must survive the reset.
    """
    origin = tmp_path / "origin.git"
    pristine = tmp_path / "pristine"
    worker = tmp_path / "worker"

    _init_bare_origin(origin)

    subprocess.run(
        ["git", "clone", "-q", str(origin), str(pristine)],
        check=True,
        capture_output=True,
    )
    (pristine / "template.conf").write_text("v1\n")
    (pristine / "workspace").mkdir()
    (pristine / "workspace" / "seed").write_text("seed\n")
    _commit(pristine, "init")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"],
        cwd=pristine,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "clone", "-q", str(origin), str(worker)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "trinity/demo/abc"],
        cwd=worker,
        check=True,
        capture_output=True,
    )
    # Parallel history: worker has its own "init" commit with different content
    (worker / "template.conf").write_text("v1-fork\n")
    (worker / "workspace" / "state").write_text("instance\n")
    _commit(worker, "init")
    (worker / "workspace" / "state").write_text("instance\nmore\n")
    _commit(worker, "accumulated")
    subprocess.run(
        ["git", "push", "-q", "origin", "trinity/demo/abc"],
        cwd=worker,
        check=True,
        capture_output=True,
    )

    # Upstream advances with a conflicting change to template.conf
    (pristine / "template.conf").write_text("v2\n")
    _commit(pristine, "upstream-update")
    subprocess.run(
        ["git", "push", "-q", "origin", "main"],
        cwd=pristine,
        check=True,
        capture_output=True,
    )

    return worker


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


def test_refuses_when_no_git_config(tmp_path: Path):
    """Empty dir with no .git → returns error before touching anything."""
    result = reset_to_main_preserve_state_mirror(
        home_dir=tmp_path, read_allowlist=lambda: ["workspace/**"]
    )
    assert result == {"error": "no_git_config"}


def test_refuses_when_no_remote_main(tmp_path: Path):
    """Repo exists but origin has no main branch → no_remote_main."""
    origin = tmp_path / "origin.git"
    worker = tmp_path / "worker"
    _init_bare_origin(origin)
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(worker)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-q", "-b", "trinity/demo/abc"],
        cwd=worker,
        check=True,
        capture_output=True,
    )
    (worker / "x").write_text("hi")
    _commit(worker, "only commit")

    result = reset_to_main_preserve_state_mirror(
        home_dir=worker, read_allowlist=lambda: ["workspace/**"]
    )
    assert result["error"] == "no_remote_main"


def test_empty_allowlist_is_safe_noop(tmp_path: Path):
    """Empty allowlist → no files preserved, but reset + commit still run.

    Ensures the routine is safe even if the agent's persistent-state file
    is blank. The working tree ends up matching origin/main exactly
    (minus the .trinity/backup dir created by the snapshot).
    """
    worker = _setup_parallel_history(tmp_path)

    result = reset_to_main_preserve_state_mirror(
        home_dir=worker, read_allowlist=lambda: []
    )
    assert result.get("error") is None, result
    assert result["files_preserved"] == []
    assert result["commit_sha"]
    assert result["working_branch"] == "trinity/demo/abc"
    # Template is now upstream's v2, not the worker's v1-fork.
    assert (worker / "template.conf").read_text() == "v2\n"


def test_success_preserves_workspace_and_overlays_main(tmp_path: Path):
    """Full happy path: workspace/** survives; template.conf adopts main."""
    worker = _setup_parallel_history(tmp_path)

    result = reset_to_main_preserve_state_mirror(
        home_dir=worker, read_allowlist=lambda: ["workspace/**"]
    )
    assert result.get("error") is None, result
    assert "workspace/state" in result["files_preserved"]
    # Upstream baseline adopted.
    assert (worker / "template.conf").read_text() == "v2\n"
    # Instance state overlaid back.
    assert (worker / "workspace" / "state").read_text() == "instance\nmore\n"
    # Backup exists with a tar + manifest.
    backup_rel = Path(result["snapshot_dir"])
    assert (worker / backup_rel / "snapshot.tar").exists()
    assert "workspace/state" in (worker / backup_rel / "files.txt").read_text()
    # Commit message is the spec's exact string.
    subject = _git(
        ["log", "-1", "--pretty=%s"], worker
    ).stdout.strip()
    assert subject == "Adopt main baseline, preserve state"


def test_success_pushes_with_force_with_lease(tmp_path: Path):
    """Opting in to the push step uses --force-with-lease and updates remote."""
    worker = _setup_parallel_history(tmp_path)

    result = reset_to_main_preserve_state_mirror(
        home_dir=worker,
        read_allowlist=lambda: ["workspace/**"],
        skip_push=False,
    )
    assert result.get("error") is None, result
    # Verify remote now has the reset commit on trinity/demo/abc.
    origin_branch = _git(
        ["ls-remote", "origin", "trinity/demo/abc"], worker
    ).stdout.strip()
    assert origin_branch.startswith(result["commit_sha"])
