"""
Unit tests for web chat file upload (#364).

Tests the shared upload_service helpers:
- sanitize_filename (path traversal, unicode, dedup)
- decode_web_file (data: URI prefix stripping, raw base64)
- process_file_uploads (size limits, MIME gating, image vs file dispatch)
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Stub heavyweight backend deps so upload_service can be imported without
# a running database or Docker daemon.
# ---------------------------------------------------------------------------

def _install_stubs():
    """Install sys.modules stubs once per process."""
    if "services.upload_service" in sys.modules:
        return  # already loaded (or already stubbed)

    # Stub platform_audit_service
    _aud = types.ModuleType("services.platform_audit_service")

    class _AuditEventType:
        EXECUTION = "execution"
        AUTHENTICATION = "authentication"

    _mock_svc = MagicMock()
    _mock_svc.log = AsyncMock()
    _aud.platform_audit_service = _mock_svc
    _aud.AuditEventType = _AuditEventType
    sys.modules.setdefault("services.platform_audit_service", _aud)

    # Stub docker_utils
    _du = types.ModuleType("services.docker_utils")
    _du.container_put_archive = AsyncMock(return_value=True)
    _du.container_exec_run = AsyncMock(return_value=(0, b""))
    sys.modules.setdefault("services.docker_utils", _du)


_install_stubs()

# Now import the module under test
import services.upload_service as _svc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_basic(self):
        assert _svc.sanitize_filename("report.csv", "1", set()) == "report.csv"

    def test_path_traversal_stripped(self):
        result = _svc.sanitize_filename("../../etc/passwd", "1", set())
        assert "/" not in result
        assert ".." not in result

    def test_hidden_file_rejected(self):
        result = _svc.sanitize_filename(".env", "abc", set())
        assert result == "file_abc"

    def test_unicode_normalized(self):
        # Fullwidth filename should be normalized
        result = _svc.sanitize_filename("ｆｉｌｅ.txt", "1", set())
        assert "ｆ" not in result

    def test_dedup_suffix(self):
        used = {"report.csv"}
        result = _svc.sanitize_filename("report.csv", "2", used)
        assert result == "report-1.csv"

    def test_truncate_long_name(self):
        long_name = "a" * 250 + ".txt"
        result = _svc.sanitize_filename(long_name, "x", set())
        assert len(result) <= _svc._FILENAME_MAX_LENGTH


# ---------------------------------------------------------------------------
# decode_web_file
# ---------------------------------------------------------------------------

class TestDecodeWebFile:
    def test_raw_base64(self):
        data = b"hello world"
        result = _svc.decode_web_file({"name": "f.txt", "data_base64": _b64(data)})
        assert result == data

    def test_data_uri_prefix_stripped(self):
        data = b"hello world"
        uri = f"data:text/plain;base64,{_b64(data)}"
        result = _svc.decode_web_file({"name": "f.txt", "data_base64": uri})
        assert result == data

    def test_empty_returns_none(self):
        result = _svc.decode_web_file({"name": "f.txt", "data_base64": ""})
        assert result is None

    def test_bad_base64_returns_none(self):
        result = _svc.decode_web_file({"name": "f.txt", "data_base64": "!!!not_base64!!!"})
        assert result is None


# ---------------------------------------------------------------------------
# process_file_uploads (async)
# ---------------------------------------------------------------------------

class TestProcessFileUploads:
    """Tests the core upload processing logic with mocked container ops."""

    def _make_raw(self, name="test.txt", mimetype="text/plain", data=b"hello", file_id="f1"):
        return {"name": name, "mimetype": mimetype, "size": len(data), "data": data, "id": file_id}

    @pytest.mark.asyncio
    async def test_empty_list_returns_empty(self):
        container = MagicMock()
        descs, udir, failed, imgs = await _svc.process_file_uploads(
            raw_files=[], agent_name="a", container=container,
            session_id="s1", uploader="user@x.com"
        )
        assert descs == []
        assert udir is None
        assert not failed
        assert imgs == []

    @pytest.mark.asyncio
    async def test_download_failure_described(self):
        container = MagicMock()
        raw = self._make_raw()
        raw["data"] = None  # simulate download failure
        descs, _, failed, imgs = await _svc.process_file_uploads(
            raw_files=[raw], agent_name="a", container=container,
            session_id="s1", uploader="u"
        )
        assert any("download failed" in d for d in descs)
        assert not failed  # write never attempted → not all_writes_failed

    @pytest.mark.asyncio
    async def test_unsupported_mime_rejected(self):
        container = MagicMock()
        raw = self._make_raw(mimetype="application/pdf", data=b"%PDF")
        descs, _, failed, imgs = await _svc.process_file_uploads(
            raw_files=[raw], agent_name="a", container=container,
            session_id="s1", uploader="u"
        )
        assert any("unsupported format" in d for d in descs)
        assert imgs == []

    @pytest.mark.asyncio
    async def test_oversized_file_rejected(self):
        container = MagicMock()
        big_data = b"x" * (_svc.WEB_MAX_FILE_SIZE + 1)
        raw = self._make_raw(data=big_data)
        descs, _, failed, imgs = await _svc.process_file_uploads(
            raw_files=[raw], agent_name="a", container=container,
            session_id="s1", uploader="u",
            max_file_size=_svc.WEB_MAX_FILE_SIZE,
        )
        assert any("rejected" in d or "exceeds" in d or "limit" in d for d in descs)

    @pytest.mark.asyncio
    async def test_image_collected_as_vision_block(self):
        container = MagicMock()
        # 1x1 PNG (smallest valid PNG)
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
            b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        raw = self._make_raw(name="img.png", mimetype="image/png", data=png)
        # Images now persist their original bytes before becoming vision blocks.
        # Patch the consumer directly: another test may have imported it before
        # this module's optional dependency stubs were installed.
        with (
            patch.object(_svc, "container_exec_run", new=AsyncMock(return_value=(0, b""))),
            patch.object(_svc, "container_put_archive", new=AsyncMock(return_value=True)),
            patch.object(_svc.platform_audit_service, "log", new=AsyncMock()),
        ):
            descs, udir, failed, imgs = await _svc.process_file_uploads(
                raw_files=[raw], agent_name="a", container=container,
                session_id="s1", uploader="u"
            )
        assert len(imgs) == 1
        assert imgs[0]["media_type"].startswith("image/")
        assert udir is not None
        assert any(f"saved to {udir}/img.png" in d for d in descs)
        assert not failed

    @pytest.mark.asyncio
    async def test_text_file_written_to_container(self):
        container = MagicMock()

        # Patch the module-level references used inside upload_service
        with (
            patch.object(_svc, "container_exec_run", new=AsyncMock(return_value=(0, b""))) as mock_exec,
            patch.object(_svc, "container_put_archive", new=AsyncMock(return_value=True)) as mock_put,
        ):
            raw = self._make_raw(name="data.csv", mimetype="text/csv", data=b"a,b\n1,2")
            descs, udir, failed, imgs = await _svc.process_file_uploads(
                raw_files=[raw], agent_name="myagent", container=container,
                session_id="sess123", uploader="u"
            )

        assert udir is not None
        assert any("data.csv" in d for d in descs)
        assert not failed
        assert imgs == []

    @staticmethod
    def _zip_bytes() -> bytes:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.pdf", "x" * 40000)
        return buf.getvalue()

    async def _upload(self, raw):
        with (
            patch.object(_svc, "container_exec_run", new=AsyncMock(return_value=(0, b""))),
            patch.object(_svc, "container_put_archive", new=AsyncMock(return_value=True)),
        ):
            return await _svc.process_file_uploads(
                raw_files=[raw], agent_name="a", container=MagicMock(),
                session_id="s", uploader="u"
            )

    @pytest.mark.asyncio
    async def test_real_zip_accepted_with_unmocked_magic(self):
        """#3046: a real ZIP must pass MIME validation with python-magic un-mocked."""
        raw = self._make_raw(name="doc.zip", mimetype="application/zip", data=self._zip_bytes())
        descs, udir, failed, _ = await self._upload(raw)
        assert udir is not None
        assert not any("rejected" in d for d in descs)
        assert not failed

    @pytest.mark.asyncio
    async def test_zip_accepted_when_libmagic_misdetects_buffer(self):
        """#3046: libmagic 5.46 reports a ZIP buffer as application/octet-stream."""
        fake_magic = MagicMock()
        fake_magic.from_buffer.return_value = "application/octet-stream"
        raw = self._make_raw(name="doc.zip", mimetype="application/zip", data=self._zip_bytes())
        with (
            patch.object(_svc, "_MAGIC_AVAILABLE", True),
            patch.object(_svc, "magic", fake_magic, create=True),
        ):
            descs, udir, failed, _ = await self._upload(raw)
        assert udir is not None
        assert not any("rejected" in d for d in descs)
        assert not failed

    @pytest.mark.asyncio
    async def test_non_zip_declared_as_zip_still_rejected(self):
        """The ZIP shortcut only applies to bytes that carry a ZIP signature."""
        fake_magic = MagicMock()
        fake_magic.from_buffer.return_value = "application/x-dosexec"
        raw = self._make_raw(name="doc.zip", mimetype="application/zip", data=b"MZ\x90\x00" + b"\x00" * 64)
        with (
            patch.object(_svc, "_MAGIC_AVAILABLE", True),
            patch.object(_svc, "magic", fake_magic, create=True),
        ):
            descs, _, _, _ = await self._upload(raw)
        assert any("rejected (file type mismatch)" in d for d in descs)
        fake_magic.from_buffer.assert_called_once()

    @pytest.mark.asyncio
    async def test_max_files_limit_enforced(self):
        container = MagicMock()
        raws = [self._make_raw(name=f"f{i}.txt", file_id=f"f{i}") for i in range(5)]
        with patch.object(_svc, "container_exec_run", new=AsyncMock(return_value=(0, b""))):
            with patch.object(_svc, "container_put_archive", new=AsyncMock(return_value=True)):
                descs, _, failed, imgs = await _svc.process_file_uploads(
                    raw_files=raws, agent_name="a", container=container,
                    session_id="s", uploader="u",
                    max_files=3
                )
        combined = " ".join(descs)
        assert "skipped" in combined or "more file" in combined


@pytest.mark.asyncio
async def test_image_original_write_failure_is_explicit():
    with (patch.object(_svc, "_MAGIC_AVAILABLE", False),
          patch.object(_svc, "container_exec_run", AsyncMock(return_value=(0, b""))),
          patch.object(_svc, "container_put_archive", AsyncMock(return_value=False))):
        desc, directory, failed, images = await _svc.process_file_uploads(
            [{"name": "photo.jpg", "mimetype": "image/jpeg", "data": b"original"}],
            "agent", MagicMock(), "session", "user")
    assert failed and not images
    assert "could not save" in desc[0] and "saved to" not in desc[0]


@pytest.mark.asyncio
async def test_image_original_bytes_and_dispatch_directories_are_isolated():
    import io
    import tarfile
    archives = []
    async def save(container, directory, archive):
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.getmember("photo.jpg")
            assert member.mode == 0o600
            assert member.uid == member.gid == 1000
            archives.append((directory, tar.extractfile(member).read()))
        return True
    with (patch.object(_svc, "_MAGIC_AVAILABLE", False),
          patch.object(_svc, "container_exec_run", AsyncMock(return_value=(0, b""))),
          patch.object(_svc, "container_put_archive", save)):
        results = await asyncio.gather(*[
            _svc.process_file_uploads(
                [{"name": "photo.jpg", "mimetype": "image/jpeg", "data": data}],
                "agent", MagicMock(), "same-session", "user")
            for data in (b"first", b"second")])
    assert results[0][1] != results[1][1]
    assert {body for _, body in archives} == {b"first", b"second"}
    assert all(r[1].startswith(_svc.UPLOAD_BASE + "/") for r in results)


@pytest.mark.asyncio
async def test_failed_mkdir_does_not_claim_original_saved():
    with (patch.object(_svc, "_MAGIC_AVAILABLE", False),
          patch.object(_svc, "container_exec_run", AsyncMock(return_value=(1, b"denied")))):
        desc, directory, failed, images = await _svc.process_file_uploads(
            [{"name": "photo.jpg", "mimetype": "image/jpeg", "data": b"original"}],
            "agent", MagicMock(), "session", "user")
    assert failed and not images and directory is None
    assert "could not create" in desc[0]
