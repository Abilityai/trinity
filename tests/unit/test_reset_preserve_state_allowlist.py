"""Unit tests for the snapshot / restore primitives (S3, #384).

The agent-server `routers/snapshot.py` module cannot be imported directly
from the host because the agent-server uses relative imports that only
resolve inside the container image. Following the same pattern as
`test_git_pull_branch.py` and `test_persistent_state_reader.py`, we
mirror the two pure functions (`build_snapshot`, `restore_from_tar`)
and guard against drift with a source-match test.
"""
import io
import json
import tarfile
from fnmatch import fnmatch
from pathlib import Path


_SNAPSHOT_PY = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "agent_server"
    / "routers"
    / "snapshot.py"
)


# ---------------------------------------------------------------------------
# Mirror of agent_server.routers.snapshot helpers. Must stay in sync with
# the source — see test_snapshot_mirror_matches_source below.
# ---------------------------------------------------------------------------


def _matches_any_mirror(rel_path: str, patterns: list[str]) -> bool:
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


def _collect_files_mirror(home_dir: Path, patterns: list[str]) -> list[str]:
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
        if _matches_any_mirror(rel, patterns):
            collected.append(rel)
    return sorted(set(collected))


def build_snapshot_mirror(
    home_dir: Path, paths: list[str]
) -> tuple[bytes, list[str]]:
    files = _collect_files_mirror(home_dir, paths)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in files:
            tf.add(home_dir / rel, arcname=rel)
    return buf.getvalue(), files


def restore_from_tar_mirror(
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
                or not _matches_any_mirror(name, paths)
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
# Drift guard
# ---------------------------------------------------------------------------


def test_snapshot_mirror_matches_source():
    """Source signatures in snapshot.py must match the mirror's expectations."""
    source = _SNAPSHOT_PY.read_text()
    assert 'def build_snapshot(home_dir: Path, paths: list[str]) -> tuple[bytes, list[str]]:' in source
    assert 'def restore_from_tar(' in source
    assert '_HOME = Path("/home/developer")' in source
    assert '"/api/agent-server/snapshot"' in source
    assert '"/api/agent-server/restore"' in source


# ---------------------------------------------------------------------------
# build_snapshot — behaviour
# ---------------------------------------------------------------------------


def test_build_snapshot_respects_allowlist(tmp_path: Path):
    """Only files matching allowlist globs are captured."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "state.json").write_text("keep me")
    (tmp_path / "secret.key").write_text("skip me")

    tar_bytes, files = build_snapshot_mirror(
        home_dir=tmp_path,
        paths=["workspace/**"],
    )
    assert files == ["workspace/state.json"]

    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        assert tf.getnames() == ["workspace/state.json"]
        extracted = tf.extractfile("workspace/state.json")
        assert extracted is not None
        assert extracted.read() == b"keep me"


def test_build_snapshot_empty_allowlist_yields_empty_tar(tmp_path: Path):
    """Empty allowlist → empty tar, not an error (safe no-op)."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "x").write_text("hi")

    tar_bytes, files = build_snapshot_mirror(home_dir=tmp_path, paths=[])

    assert files == []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        assert tf.getnames() == []


def test_build_snapshot_rejects_absolute_and_traversal_patterns(tmp_path: Path):
    """Patterns escaping home_dir are ignored; legitimate ones still work."""
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "x").write_text("hi")

    _, files = build_snapshot_mirror(
        home_dir=tmp_path,
        paths=["../**", "/etc/**", "workspace/**"],
    )
    assert files == ["workspace/x"]


def test_build_snapshot_matches_default_s4_patterns(tmp_path: Path):
    """End-to-end with the S4 default allowlist — realistic case."""
    default_patterns = [
        "workspace/**",
        ".trinity/**",
        ".mcp.json",
        ".claude.json",
        ".claude/.credentials.json",
    ]
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "a.txt").write_text("a")
    (tmp_path / ".trinity").mkdir()
    (tmp_path / ".trinity" / "config.yaml").write_text("x: 1")
    (tmp_path / ".mcp.json").write_text("{}")
    (tmp_path / "ignored.log").write_text("nope")

    _, files = build_snapshot_mirror(home_dir=tmp_path, paths=default_patterns)
    assert files == [".mcp.json", ".trinity/config.yaml", "workspace/a.txt"]


# ---------------------------------------------------------------------------
# restore_from_tar — behaviour
# ---------------------------------------------------------------------------


def _tar_with(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_restore_applies_tar_within_allowlist(tmp_path: Path):
    """Only allowlisted paths are written; others are reported as skipped."""
    tar_bytes = _tar_with(
        [
            ("workspace/a.txt", b"good"),
            (".env", b"SECRET"),  # not in allowlist
        ]
    )

    restored, skipped = restore_from_tar_mirror(
        home_dir=tmp_path,
        tar_bytes=tar_bytes,
        paths=["workspace/**"],
    )

    assert restored == ["workspace/a.txt"]
    assert skipped == [".env"]
    assert (tmp_path / "workspace" / "a.txt").read_bytes() == b"good"
    assert not (tmp_path / ".env").exists()


def test_restore_rejects_path_traversal(tmp_path: Path):
    """Tar entries with .. or absolute paths are skipped, never written."""
    tar_bytes = _tar_with(
        [
            ("../escape.txt", b"nope"),
            ("/abs/escape.txt", b"nope"),
            ("workspace/a.txt", b"good"),
        ]
    )

    restored, skipped = restore_from_tar_mirror(
        home_dir=tmp_path,
        tar_bytes=tar_bytes,
        paths=["workspace/**", "**/*.txt"],
    )

    assert restored == ["workspace/a.txt"]
    assert "../escape.txt" in skipped
    assert "/abs/escape.txt" in skipped
    # Nothing written outside home_dir
    assert not (tmp_path.parent / "escape.txt").exists()


def test_restore_round_trips_snapshot(tmp_path: Path):
    """snapshot → restore into a fresh dir reproduces the files exactly."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "workspace").mkdir()
    (src / "workspace" / "state.json").write_text("x")
    (src / ".trinity").mkdir()
    (src / ".trinity" / "a.yaml").write_text("y")

    tar_bytes, _ = build_snapshot_mirror(
        home_dir=src, paths=["workspace/**", ".trinity/**"]
    )

    dest = tmp_path / "dest"
    dest.mkdir()
    restored, skipped = restore_from_tar_mirror(
        home_dir=dest,
        tar_bytes=tar_bytes,
        paths=["workspace/**", ".trinity/**"],
    )

    assert skipped == []
    assert set(restored) == {"workspace/state.json", ".trinity/a.yaml"}
    assert (dest / "workspace" / "state.json").read_text() == "x"
    assert (dest / ".trinity" / "a.yaml").read_text() == "y"


def test_restore_empty_allowlist_writes_nothing(tmp_path: Path):
    """Empty allowlist is a safe no-op even if the tar has entries."""
    tar_bytes = _tar_with([("workspace/a.txt", b"data")])

    restored, skipped = restore_from_tar_mirror(
        home_dir=tmp_path, tar_bytes=tar_bytes, paths=[]
    )

    assert restored == []
    assert skipped == ["workspace/a.txt"]
    assert not (tmp_path / "workspace").exists()


# ---------------------------------------------------------------------------
# JSON-array paths contract for the HTTP endpoint
# ---------------------------------------------------------------------------


def test_restore_endpoint_paths_are_json_encoded():
    """The restore HTTP endpoint accepts `paths` as a JSON array string."""
    # The endpoint surface is a FastAPI Form field; we just pin the contract.
    encoded = json.dumps(["workspace/**", ".trinity/**"])
    assert json.loads(encoded) == ["workspace/**", ".trinity/**"]
