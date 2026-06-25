"""
Unit tests for #1169 — agent runtime-data export/import helpers.

Covers the pure, Docker-free units of `routers/agent_data.py`:

- `_drain_stream_to_file` — streams tar chunks to disk, enforcing the cap.
- `_append_manifest` — embeds a self-describing `manifest.json` without
  corrupting the captured data tar.
- The export↔import CONTRACT: an export tar (Docker `get_archive`-shaped:
  `data/` members) with an appended manifest, fed through the REAL agent-server
  `restore_from_tar(home, tar, ["data/**"])`, restores the data files and skips
  the manifest (outside the `data/**` allowlist).
- `_agent_data_op_lock` — 409 on contention, fail-open when Redis is absent.

The full HTTP path (auth, streaming response, agent-server proxy) is exercised
by the sibling-stack integration suite / verify-local.
"""
import importlib.util
import io
import tarfile
import time
from pathlib import Path

import pytest

_BASE_IMAGE = (
    Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server"
)


def _load_snapshot_module():
    """Load the agent-server snapshot primitives directly from the base image."""
    spec = importlib.util.spec_from_file_location(
        "snapshot_under_test", _BASE_IMAGE / "routers" / "snapshot.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ad():
    """The router module (pure helpers only — never hits Docker here)."""
    import routers.agent_data as module
    return module


# ---------------------------------------------------------------------------
# _drain_stream_to_file
# ---------------------------------------------------------------------------


def test_drain_writes_all_chunks(ad, tmp_path):
    out = tmp_path / "out.tar"
    total = ad._drain_stream_to_file(iter([b"abc", b"defg"]), str(out), 1000)
    assert total == 7
    assert out.read_bytes() == b"abcdefg"


def test_drain_raises_when_over_cap(ad, tmp_path):
    out = tmp_path / "out.tar"
    with pytest.raises(ad._ExportTooLarge):
        ad._drain_stream_to_file(iter([b"a" * 600, b"b" * 600]), str(out), 1000)


# ---------------------------------------------------------------------------
# _append_manifest
# ---------------------------------------------------------------------------


def _make_data_tar(members: dict[str, bytes]) -> bytes:
    """Build a tar shaped like Docker `get_archive` output (data/ members)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_append_manifest_to_empty_file_yields_manifest_only_tar(ad, tmp_path):
    """No data/ captured (0-byte temp file) → a valid manifest-only tar.

    Regression: `tarfile.open(empty_file, "a")` raises `ReadError: empty
    header`, so the empty case must use write mode, not append.
    """
    tar_path = tmp_path / "empty.tar"
    tar_path.write_bytes(b"")  # what mkstemp leaves when the data stream is None

    ad._append_manifest(str(tar_path), {"format_version": 1, "agent_name": "a"})

    with tarfile.open(str(tar_path), "r") as tf:
        assert tf.getnames() == ["manifest.json"]


def test_append_manifest_preserves_data_and_adds_manifest(ad, tmp_path):
    tar_path = tmp_path / "export.tar"
    tar_path.write_bytes(_make_data_tar({"data/app.db": b"SQLITE", "data/x/y.bin": b"\x00\x01"}))

    ad._append_manifest(str(tar_path), {"format_version": 1, "agent_name": "a"})

    with tarfile.open(str(tar_path), "r") as tf:
        names = set(tf.getnames())
        assert {"data/app.db", "data/x/y.bin", "manifest.json"} <= names
        manifest_bytes = tf.extractfile("manifest.json").read()
        import json
        assert json.loads(manifest_bytes)["agent_name"] == "a"
        # Original data survived the append, byte-for-byte.
        assert tf.extractfile("data/app.db").read() == b"SQLITE"


# ---------------------------------------------------------------------------
# Export ↔ import contract (real restore_from_tar)
# ---------------------------------------------------------------------------


def test_export_tar_round_trips_through_restore(ad, tmp_path):
    """An export tar (data/ members + manifest) restores data and skips the
    manifest — the manifest is outside the `data/**` allowlist."""
    snapshot = _load_snapshot_module()

    tar_path = tmp_path / "export.tar"
    tar_path.write_bytes(_make_data_tar({"data/app.db": b"PAYLOAD", "data/sub/d.csv": b"1,2,3"}))
    ad._append_manifest(str(tar_path), {"format_version": 1, "agent_name": "kb"})

    home = tmp_path / "home"
    home.mkdir()
    restored, skipped = snapshot.restore_from_tar(
        home, tar_path.read_bytes(), ["data/**"]
    )

    assert "data/app.db" in restored
    assert "data/sub/d.csv" in restored
    assert "manifest.json" in skipped  # outside data/** → not restored
    assert (home / "data" / "app.db").read_bytes() == b"PAYLOAD"
    assert (home / "data" / "sub" / "d.csv").read_bytes() == b"1,2,3"


def test_restore_rejects_traversal(ad, tmp_path):
    """A malicious tar entry escaping data/ is skipped by restore_from_tar."""
    snapshot = _load_snapshot_module()

    evil = _make_data_tar({"data/ok.db": b"GOOD", "../escape.sh": b"rm -rf /"})
    home = tmp_path / "home"
    home.mkdir()
    restored, skipped = snapshot.restore_from_tar(home, evil, ["data/**"])

    assert "data/ok.db" in restored
    assert "../escape.sh" in skipped
    assert not (tmp_path / "escape.sh").exists()


# ---------------------------------------------------------------------------
# _agent_data_op_lock
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal SETNX-with-TTL stand-in (decode_responses semantics)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_op_lock_blocks_concurrent_op(ad, monkeypatch):
    from fastapi import HTTPException

    fake = _FakeRedis()
    monkeypatch.setattr("routers.auth.get_redis_client", lambda: fake)

    async with ad._agent_data_op_lock("agentZ"):
        # A second acquire while the first is held → 409.
        with pytest.raises(HTTPException) as exc:
            async with ad._agent_data_op_lock("agentZ"):
                pass
        assert exc.value.status_code == 409

    # Lock released on exit → re-acquire succeeds.
    async with ad._agent_data_op_lock("agentZ"):
        pass


@pytest.mark.asyncio
async def test_op_lock_fail_open_when_redis_down(ad, monkeypatch):
    monkeypatch.setattr("routers.auth.get_redis_client", lambda: None)
    # No Redis → never blocks; both nested acquires succeed.
    async with ad._agent_data_op_lock("agentQ"):
        async with ad._agent_data_op_lock("agentQ"):
            pass
