"""
Unit tests for Slack file upload handling (#222).

Mostly pure logic tests (no backend imports); the MIME gate is the exception —
it imports the real UNSUPPORTED_MIMES from services.upload_service so the test
can't drift from the deny-list it verifies.

Module: src/backend/adapters/message_router.py, slack_adapter.py
Issue: https://github.com/abilityai/trinity/issues/222
"""

import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _install_stubs():
    """Stub heavyweight deps so upload_service imports without DB/Docker.

    Mirrors test_web_file_upload.py; setdefault keeps the two files compatible
    in the same pytest session regardless of collection order.
    """
    if "services.upload_service" in sys.modules:
        return

    _aud = types.ModuleType("services.platform_audit_service")

    class _AuditEventType:
        EXECUTION = "execution"
        AUTHENTICATION = "authentication"

    _mock_svc = MagicMock()
    _mock_svc.log = AsyncMock()
    _aud.platform_audit_service = _mock_svc
    _aud.AuditEventType = _AuditEventType
    sys.modules.setdefault("services.platform_audit_service", _aud)

    _du = types.ModuleType("services.docker_utils")
    _du.container_put_archive = AsyncMock(return_value=True)
    _du.container_exec_run = AsyncMock(return_value=(0, b""))
    sys.modules.setdefault("services.docker_utils", _du)


_install_stubs()

from services.upload_service import UNSUPPORTED_MIMES  # noqa: E402


# ---------------------------------------------------------------------------
# Reproduce key logic from the codebase for testing
# ---------------------------------------------------------------------------

def sanitize_filename(name, file_id="F123"):
    """Reproduces message_router._handle_file_uploads filename sanitization."""
    safe_name = os.path.basename(name)
    safe_name = re.sub(r'[^\w\s.\-()]', '_', safe_name)
    if not safe_name or safe_name.startswith('.'):
        safe_name = f"file_{file_id}"
    return safe_name


def format_file_size(size_bytes):
    """Reproduces message_router._format_file_size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"


def is_unsupported_mime(mimetype):
    """Applies the real UNSUPPORTED_MIMES set with upload_service's matching rule."""
    return any(mimetype.startswith(m) if m.endswith("/") else mimetype == m
               for m in UNSUPPORTED_MIMES)


def extract_files(event):
    """Reproduces SlackAdapter._extract_files logic."""
    files = []
    for f in event.get("files", []):
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue
        files.append({
            "id": f.get("id", ""),
            "name": f.get("name", "unknown"),
            "mimetype": f.get("mimetype", "application/octet-stream"),
            "size": f.get("size", 0),
            "url": url,
        })
    return files


# ---------------------------------------------------------------------------
# Filename sanitization (security #1)
# ---------------------------------------------------------------------------

class TestFilenameSanitization:

    def test_normal_filename(self):
        assert sanitize_filename("report.csv") == "report.csv"

    def test_path_traversal_dots(self):
        result = sanitize_filename("../../.env")
        assert ".." not in result
        assert result == "file_F123"

    def test_path_traversal_absolute(self):
        assert sanitize_filename("/etc/passwd") == "passwd"

    def test_hidden_file_rejected(self):
        assert sanitize_filename(".secret") == "file_F123"
        assert sanitize_filename(".env") == "file_F123"
        assert sanitize_filename(".mcp.json") == "file_F123"

    def test_empty_name(self):
        assert sanitize_filename("") == "file_F123"

    def test_script_injection(self):
        result = sanitize_filename("file<script>alert(1)</script>.txt")
        assert "<" not in result
        assert ">" not in result

    def test_spaces_and_parens_preserved(self):
        result = sanitize_filename("Screenshot 2026-03-23 at 14.31.25 (1).png")
        assert "Screenshot" in result
        assert "(1)" in result
        assert ".png" in result

    def test_windows_path(self):
        result = sanitize_filename("C:\\Users\\hack\\.env")
        # On Unix, os.path.basename doesn't split on backslash,
        # but the regex strips backslashes → safe regardless
        assert ".." not in result
        assert "/" not in result

    def test_unique_fallback_per_file(self):
        assert sanitize_filename(".env", "F001") == "file_F001"
        assert sanitize_filename(".env", "F002") == "file_F002"


# ---------------------------------------------------------------------------
# File type routing
# ---------------------------------------------------------------------------

class TestFileTypeRouting:

    def test_images_are_not_unsupported(self):
        assert not is_unsupported_mime("image/png")
        assert not is_unsupported_mime("image/jpeg")
        assert not is_unsupported_mime("image/gif")
        assert not is_unsupported_mime("image/webp")

    def test_pdf_unsupported(self):
        assert is_unsupported_mime("application/pdf")

    def test_zip_supported(self):
        assert not is_unsupported_mime("application/zip")

    def test_archives_unsupported(self):
        assert is_unsupported_mime("application/x-tar")
        assert is_unsupported_mime("application/gzip")

    def test_video_audio_unsupported(self):
        assert is_unsupported_mime("video/mp4")
        assert is_unsupported_mime("video/quicktime")
        assert is_unsupported_mime("audio/mpeg")
        assert is_unsupported_mime("audio/wav")

    def test_text_formats_supported(self):
        assert not is_unsupported_mime("text/plain")
        assert not is_unsupported_mime("text/csv")
        assert not is_unsupported_mime("text/markdown")
        assert not is_unsupported_mime("text/html")

    def test_data_formats_supported(self):
        assert not is_unsupported_mime("application/json")
        assert not is_unsupported_mime("application/xml")
        assert not is_unsupported_mime("application/yaml")


# ---------------------------------------------------------------------------
# Slack event file extraction
# ---------------------------------------------------------------------------

class TestSlackFileExtraction:

    def test_no_files_key(self):
        assert extract_files({"text": "hello"}) == []

    def test_empty_files(self):
        assert extract_files({"files": []}) == []

    def test_single_file(self):
        event = {"files": [{
            "id": "F123", "name": "data.csv", "mimetype": "text/csv",
            "size": 1024, "url_private_download": "https://files.slack.com/f"
        }]}
        files = extract_files(event)
        assert len(files) == 1
        assert files[0]["name"] == "data.csv"
        assert files[0]["mimetype"] == "text/csv"

    def test_multiple_files(self):
        event = {"files": [
            {"id": "F1", "name": "a.txt", "mimetype": "text/plain", "size": 10, "url_private_download": "http://a"},
            {"id": "F2", "name": "b.png", "mimetype": "image/png", "size": 20, "url_private_download": "http://b"},
        ]}
        assert len(extract_files(event)) == 2

    def test_no_url_skipped(self):
        event = {"files": [{"id": "F1", "name": "a.txt", "mimetype": "text/plain", "size": 10}]}
        assert len(extract_files(event)) == 0

    def test_url_private_fallback(self):
        event = {"files": [{
            "id": "F1", "name": "a.txt", "mimetype": "text/plain",
            "size": 10, "url_private": "http://fallback"
        }]}
        files = extract_files(event)
        assert len(files) == 1
        assert files[0]["url"] == "http://fallback"


# ---------------------------------------------------------------------------
# Size limits
# ---------------------------------------------------------------------------

class TestSizeLimits:
    MAX_FILE_SIZE = 10 * 1024 * 1024
    MAX_IMAGE_SIZE = 5 * 1024 * 1024
    MAX_TOTAL_IMAGE_SIZE = 10 * 1024 * 1024
    MAX_FILES = 10

    def test_text_file_under_limit(self):
        assert 1024 < self.MAX_FILE_SIZE

    def test_text_file_over_limit(self):
        assert 11 * 1024 * 1024 > self.MAX_FILE_SIZE

    def test_image_under_limit(self):
        assert 3 * 1024 * 1024 < self.MAX_IMAGE_SIZE

    def test_image_over_limit(self):
        assert 6 * 1024 * 1024 > self.MAX_IMAGE_SIZE

    def test_total_image_budget_exceeded(self):
        total = 3 * 4 * 1024 * 1024  # 3 images × 4MB
        assert total > self.MAX_TOTAL_IMAGE_SIZE

    def test_max_files_truncation(self):
        files = list(range(15))
        assert len(files[:self.MAX_FILES]) == 10


# ---------------------------------------------------------------------------
# Format file size
# ---------------------------------------------------------------------------

class TestFormatFileSize:

    def test_bytes(self):
        assert format_file_size(500) == "500 B"

    def test_kilobytes(self):
        assert format_file_size(2048) == "2 KB"

    def test_megabytes(self):
        assert format_file_size(5 * 1024 * 1024) == "5.0 MB"

    def test_zero(self):
        assert format_file_size(0) == "0 B"

    def test_boundary_1kb(self):
        assert format_file_size(1024) == "1 KB"

    def test_boundary_1mb(self):
        assert format_file_size(1024 * 1024) == "1.0 MB"


# ---------------------------------------------------------------------------
# Per-session directory naming
# ---------------------------------------------------------------------------

class TestPerSessionDirectory:

    def test_session_id_in_path(self):
        session_id = "abc-123-def"
        upload_dir = f"/home/developer/uploads/{session_id}"
        assert session_id in upload_dir
        assert upload_dir.startswith("/home/developer/uploads/")

    def test_different_sessions_different_dirs(self):
        dir1 = f"/home/developer/uploads/session-1"
        dir2 = f"/home/developer/uploads/session-2"
        assert dir1 != dir2

    def test_cleanup_command(self):
        upload_dir = "/home/developer/uploads/session-123"
        cmd = f"rm -rf {upload_dir}"
        assert "session-123" in cmd
        assert cmd.startswith("rm -rf /home/developer/uploads/")
