"""
HTTP-level tests for #1169 — the agent-data export/import endpoints.

Mounts the real `routers/agent_data.py` on a minimal FastAPI app with the auth
dependencies overridden and Docker/Redis/audit mocked, then drives it through a
`TestClient`. This exercises the genuine FastAPI routing, dependency injection,
the full export handler (op lock → drain → manifest → stream/base64 branch), and
the import proxy — the integration layer the pure-helper unit tests don't reach.

A full sibling-stack / verify-local run additionally exercises the real agent
container restore path; this test covers everything up to that boundary.
"""
import base64
import io
import tarfile
import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.agent_data as ad
from dependencies import get_current_user, get_owned_agent_by_name

_AGENT = "myagent"


def _running():
    """A stand-in container the running-state gate accepts (#1169)."""
    return types.SimpleNamespace(status="running")


def _stopped():
    return types.SimpleNamespace(status="exited")


def _data_tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Minimal app with just the data router + overridden auth."""
    app = FastAPI()
    app.include_router(ad.router)
    app.dependency_overrides[get_owned_agent_by_name] = lambda: _AGENT
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(
        username="admin", email="admin@example.com"
    )
    # Stage temp tars in a writable dir, not the container path /data.
    monkeypatch.setattr(ad, "_TMP_DIR", str(tmp_path / "agent-data-tmp"))
    # Audit + declared-paths read are best-effort; stub them out.
    monkeypatch.setattr(
        ad.platform_audit_service, "log", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(ad.git_service, "_data_paths_for", AsyncMock(return_value=[]))
    # Fail-open op lock (no Redis in unit env).
    monkeypatch.setattr("routers.auth.get_redis_client", lambda: None)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_rejects_bad_format(client):
    with patch.object(ad, "get_agent_container", return_value=_running()):
        resp = client.post(f"/api/agents/{_AGENT}/data/export?format=bogus")
    assert resp.status_code == 400


def test_export_409_when_agent_not_running(client):
    with patch.object(ad, "get_agent_container", return_value=None):
        resp = client.post(f"/api/agents/{_AGENT}/data/export")
    assert resp.status_code == 409
    assert "not running" in resp.json()["detail"]


def test_export_409_when_agent_stopped(client):
    """A present-but-stopped container is treated as not-running (#1169)."""
    with patch.object(ad, "get_agent_container", return_value=_stopped()):
        resp = client.post(f"/api/agents/{_AGENT}/data/export")
    assert resp.status_code == 409
    assert "not running" in resp.json()["detail"]


def test_export_base64_returns_tar_with_manifest(client):
    tar = _data_tar({"data/app.db": b"SQLITEDATA"})
    archive = AsyncMock(return_value=(iter([tar]), {}))
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "container_get_archive", archive
    ):
        resp = client.post(f"/api/agents/{_AGENT}/data/export?format=base64")

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_name"] == _AGENT
    assert body["format"] == "base64"
    raw = base64.b64decode(body["tar_base64"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tf:
        names = set(tf.getnames())
        assert "data/app.db" in names
        assert "manifest.json" in names  # self-describing


def test_export_stream_returns_tar(client):
    tar = _data_tar({"data/x.csv": b"1,2,3"})
    archive = AsyncMock(return_value=(iter([tar]), {}))
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "container_get_archive", archive
    ):
        resp = client.post(f"/api/agents/{_AGENT}/data/export?format=stream")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-tar"
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r") as tf:
        assert "data/x.csv" in set(tf.getnames())


def test_export_missing_data_dir_yields_manifest_only(client):
    """No data/ dir (declared-but-unused) → a valid manifest-only tar, not 500."""
    import docker

    archive = AsyncMock(side_effect=docker.errors.NotFound("no data dir"))
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "container_get_archive", archive
    ):
        resp = client.post(f"/api/agents/{_AGENT}/data/export?format=base64")

    assert resp.status_code == 200
    raw = base64.b64decode(resp.json()["tar_base64"])
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tf:
        assert tf.getnames() == ["manifest.json"]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_import_409_when_agent_not_running(client):
    with patch.object(ad, "get_agent_container", return_value=None):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", b"x", "application/x-tar")},
        )
    assert resp.status_code == 409


def test_import_409_when_agent_stopped(client):
    """A present-but-stopped container is treated as not-running (#1169)."""
    with patch.object(ad, "get_agent_container", return_value=_stopped()):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", b"x", "application/x-tar")},
        )
    assert resp.status_code == 409


def test_import_proxies_to_agent_server_restore(client):
    tar = _data_tar({"data/app.db": b"RESTOREME"})
    fake_resp = types.SimpleNamespace(
        status_code=200,
        json=lambda: {"restored": ["data/app.db"], "skipped_outside_allowlist": []},
        text="",
    )
    proxy = AsyncMock(return_value=fake_resp)
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "agent_http_request", proxy
    ):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", tar, "application/x-tar")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_name"] == _AGENT
    assert body["restored"] == ["data/app.db"]
    assert body["bytes_received"] == len(tar)
    # The proxy targeted the agent-server restore primitive with the data/** allowlist.
    call = proxy.await_args
    assert call.args[1] == "POST"
    assert call.args[2] == "/api/agent-server/restore"
    assert call.kwargs["data"]["paths"] == '["data/**"]'


def test_import_filters_manifest_from_skipped(client):
    """The self-describing manifest.json is outside data/** and is always
    skipped by restore; the response must not surface it as a skip (#1169)."""
    tar = _data_tar({"data/app.db": b"RESTOREME", "manifest.json": b"{}"})
    fake_resp = types.SimpleNamespace(
        status_code=200,
        json=lambda: {
            "restored": ["data/app.db"],
            "skipped_outside_allowlist": ["manifest.json"],
        },
        text="",
    )
    proxy = AsyncMock(return_value=fake_resp)
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "agent_http_request", proxy
    ):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", tar, "application/x-tar")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["restored"] == ["data/app.db"]
    assert body["skipped"] == []  # manifest.json filtered out


# ---------------------------------------------------------------------------
# Import — M1 streaming cap (#1169): an oversized upload is rejected without
# buffering the whole body in memory and without leaking the staged temp file.
# ---------------------------------------------------------------------------


def _staged_tars() -> list[str]:
    import glob
    import os

    return (
        glob.glob(os.path.join(ad._TMP_DIR, "*.tar"))
        if os.path.isdir(ad._TMP_DIR)
        else []
    )


def test_import_413_when_content_length_exceeds_cap(client, monkeypatch):
    """Content-Length over the cap is rejected fast (413) before the upload is
    staged, and never reaches the agent restore proxy (M1, #1169)."""
    monkeypatch.setattr(ad, "AGENT_DATA_EXPORT_MAX_BYTES", 32)
    proxy = AsyncMock()
    big = _data_tar({"data/big.bin": b"x" * 4096})
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "agent_http_request", proxy
    ):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", big, "application/x-tar")},
        )
    assert resp.status_code == 413
    proxy.assert_not_awaited()
    assert _staged_tars() == []  # nothing staged


def test_import_413_from_stream_cap_cleans_up(client):
    """When the streaming drain trips the cap (not the Content-Length shortcut),
    the handler maps it to 413, never forwards, and cleans up the staged temp
    file (M1, #1169)."""

    async def _boom(_upload, path, _max):
        with open(path, "wb") as fh:  # prove the staged file is cleaned up
            fh.write(b"partial")
        raise ad._ExportTooLarge(999)

    proxy = AsyncMock()
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "agent_http_request", proxy
    ), patch.object(ad, "_drain_upload_to_file", _boom):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", _data_tar({"data/x": b"x"}), "application/x-tar")},
        )
    assert resp.status_code == 413
    proxy.assert_not_awaited()
    assert _staged_tars() == []


def test_import_cleans_up_staged_temp_on_success(client):
    """A successful import deletes its staged temp file (no leak, M1, #1169)."""
    tar = _data_tar({"data/app.db": b"OK"})
    fake_resp = types.SimpleNamespace(
        status_code=200,
        json=lambda: {"restored": ["data/app.db"], "skipped_outside_allowlist": []},
        text="",
    )
    with patch.object(ad, "get_agent_container", return_value=_running()), patch.object(
        ad, "agent_http_request", AsyncMock(return_value=fake_resp)
    ):
        resp = client.post(
            f"/api/agents/{_AGENT}/data/import",
            files={"tarball": ("data.tar", tar, "application/x-tar")},
        )
    assert resp.status_code == 200
    assert _staged_tars() == []


@pytest.mark.asyncio
async def test_drain_upload_to_file_enforces_cap(tmp_path):
    """The streaming helper bails at the cap rather than buffering the whole
    body — the core M1 guard (#1169)."""

    class _FakeUpload:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, _n):
            return self._chunks.pop(0) if self._chunks else b""

    out = tmp_path / "staged.tar"
    with pytest.raises(ad._ExportTooLarge):
        await ad._drain_upload_to_file(
            _FakeUpload([b"a" * 10, b"b" * 10]), str(out), max_bytes=15
        )


@pytest.mark.asyncio
async def test_drain_upload_to_file_returns_size_under_cap(tmp_path):
    """Under the cap, the helper writes the full body and returns its size."""

    class _FakeUpload:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, _n):
            return self._chunks.pop(0) if self._chunks else b""

    out = tmp_path / "staged.tar"
    written = await ad._drain_upload_to_file(
        _FakeUpload([b"hello", b"world"]), str(out), max_bytes=1000
    )
    assert written == 10
    assert out.read_bytes() == b"helloworld"
