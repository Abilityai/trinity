"""
Archive Security Validation Tests (test_archive_security.py)

Unit tests for safe tar archive extraction in local agent deployment.
Tests the _validate_tar_member and _safe_extract_tar helpers.

These tests do NOT require a running backend - they test the validation
logic directly using in-memory tar archives.

NOTE: These tests require running from within the backend context.
They are skipped when run from the main test suite due to import path issues.
"""

import pytest
import tarfile
import tempfile
import io
import os
import sys
from pathlib import Path

# Add backend to path for service imports
_backend_path = str(Path(__file__).parent.parent / "src" / "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Try to import the validation helpers - skip entire module if it fails
try:
    from services.agent_service.deploy import (
        _is_path_within,
        _validate_tar_member,
        _safe_extract_tar,
    )
except ImportError as e:
    pytest.skip(f"Cannot import backend modules: {e}", allow_module_level=True)


class TestIsPathWithin:
    """Unit tests for _is_path_within helper."""

    def test_path_within_base(self, tmp_path):
        """Path inside base directory returns True."""
        base = tmp_path
        target = tmp_path / "subdir" / "file.txt"
        assert _is_path_within(base, target) is True

    def test_path_is_base(self, tmp_path):
        """Path equal to base directory returns True."""
        base = tmp_path
        assert _is_path_within(base, base) is True

    def test_path_outside_base(self, tmp_path):
        """Path outside base directory returns False."""
        base = tmp_path / "inside"
        base.mkdir()
        target = tmp_path / "outside" / "file.txt"
        assert _is_path_within(base, target) is False

    def test_path_traversal_rejected(self, tmp_path):
        """Path with .. traversal outside base returns False."""
        base = tmp_path / "inside"
        base.mkdir()
        # This would resolve to tmp_path/file.txt, outside base
        target = base / ".." / "file.txt"
        assert _is_path_within(base, target) is False


class TestValidateTarMember:
    """Unit tests for _validate_tar_member helper."""

    def test_regular_file_allowed(self, tmp_path):
        """Regular file with safe path is allowed."""
        member = tarfile.TarInfo(name="file.txt")
        member.type = tarfile.REGTYPE
        member.size = 100

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is True
        assert error == ""

    def test_nested_file_allowed(self, tmp_path):
        """Nested file path is allowed."""
        member = tarfile.TarInfo(name="subdir/nested/file.txt")
        member.type = tarfile.REGTYPE
        member.size = 100

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is True

    def test_absolute_path_rejected(self, tmp_path):
        """Absolute path is rejected."""
        member = tarfile.TarInfo(name="/etc/passwd")
        member.type = tarfile.REGTYPE
        member.size = 100

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Absolute path" in error

    def test_path_traversal_rejected(self, tmp_path):
        """Path with .. traversal is rejected."""
        member = tarfile.TarInfo(name="../outside/file.txt")
        member.type = tarfile.REGTYPE
        member.size = 100

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Path traversal" in error

    def test_nested_traversal_rejected(self, tmp_path):
        """Nested path traversal (dir/../../../outside) is rejected."""
        member = tarfile.TarInfo(name="subdir/../../outside.txt")
        member.type = tarfile.REGTYPE
        member.size = 100

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Path traversal" in error

    def test_device_file_rejected(self, tmp_path):
        """Character device files are rejected."""
        member = tarfile.TarInfo(name="device")
        member.type = tarfile.CHRTYPE

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Device" in error

    def test_block_device_rejected(self, tmp_path):
        """Block device files are rejected."""
        member = tarfile.TarInfo(name="block")
        member.type = tarfile.BLKTYPE

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Device" in error

    def test_fifo_rejected(self, tmp_path):
        """FIFO files are rejected."""
        member = tarfile.TarInfo(name="fifo")
        member.type = tarfile.FIFOTYPE

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "FIFO" in error


class TestValidateTarMemberSymlinks:
    """Unit tests for symlink validation in _validate_tar_member."""

    def test_internal_symlink_allowed(self, tmp_path):
        """Symlink pointing to file inside archive is allowed."""
        member = tarfile.TarInfo(name="link.txt")
        member.type = tarfile.SYMTYPE
        member.linkname = "target.txt"  # Points to file in same directory

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is True

    def test_nested_internal_symlink_allowed(self, tmp_path):
        """Symlink in subdir pointing to another subdir file is allowed."""
        member = tarfile.TarInfo(name="subdir/link.txt")
        member.type = tarfile.SYMTYPE
        member.linkname = "../other/target.txt"  # Relative, but stays inside

        # Create the structure so resolve works
        (tmp_path / "subdir").mkdir(parents=True, exist_ok=True)
        (tmp_path / "other").mkdir(parents=True, exist_ok=True)

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is True

    def test_absolute_symlink_rejected(self, tmp_path):
        """Symlink with absolute target is rejected."""
        member = tarfile.TarInfo(name="evil_link")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Absolute symlink" in error

    def test_escaping_symlink_rejected(self, tmp_path):
        """Symlink pointing outside extraction directory is rejected."""
        member = tarfile.TarInfo(name="evil_link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../../etc/passwd"

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "escapes" in error.lower()


class TestValidateTarMemberHardlinks:
    """Unit tests for hardlink validation in _validate_tar_member."""

    def test_internal_hardlink_allowed(self, tmp_path):
        """Hardlink pointing to file inside archive is allowed."""
        member = tarfile.TarInfo(name="hardlink.txt")
        member.type = tarfile.LNKTYPE
        member.linkname = "original.txt"  # Points to file in archive root

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is True

    def test_absolute_hardlink_rejected(self, tmp_path):
        """Hardlink with absolute target is rejected."""
        member = tarfile.TarInfo(name="evil_hardlink")
        member.type = tarfile.LNKTYPE
        member.linkname = "/etc/passwd"

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "Absolute hardlink" in error

    def test_escaping_hardlink_rejected(self, tmp_path):
        """Hardlink pointing outside extraction directory is rejected."""
        member = tarfile.TarInfo(name="evil_hardlink")
        member.type = tarfile.LNKTYPE
        member.linkname = "../../../etc/passwd"

        is_valid, error = _validate_tar_member(member, tmp_path)
        assert is_valid is False
        assert "escapes" in error.lower()


class TestSafeExtractTar:
    """Integration tests for _safe_extract_tar with real archives."""

    def _create_tar_with_file(self, name: str, content: bytes) -> io.BytesIO:
        """Create a tar archive with a single file."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buffer.seek(0)
        return buffer

    def _create_tar_with_symlink(self, link_name: str, target: str) -> io.BytesIO:
        """Create a tar archive with a symlink."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            # Add a target file first
            content = b"target content"
            target_info = tarfile.TarInfo(name="target.txt")
            target_info.size = len(content)
            tar.addfile(target_info, io.BytesIO(content))

            # Add the symlink
            link_info = tarfile.TarInfo(name=link_name)
            link_info.type = tarfile.SYMTYPE
            link_info.linkname = target
            tar.addfile(link_info)
        buffer.seek(0)
        return buffer

    def test_extract_safe_archive(self, tmp_path):
        """Safe archive extracts successfully."""
        buffer = self._create_tar_with_file("test.txt", b"hello world")

        with tarfile.open(fileobj=buffer, mode='r:gz') as tar:
            _safe_extract_tar(tar, tmp_path, max_files=100)

        assert (tmp_path / "test.txt").exists()
        assert (tmp_path / "test.txt").read_bytes() == b"hello world"

    def test_extract_internal_symlink_allowed(self, tmp_path):
        """Archive with internal symlink extracts successfully."""
        buffer = self._create_tar_with_symlink("link.txt", "target.txt")

        with tarfile.open(fileobj=buffer, mode='r:gz') as tar:
            _safe_extract_tar(tar, tmp_path, max_files=100)

        assert (tmp_path / "target.txt").exists()
        assert (tmp_path / "link.txt").is_symlink()

    def test_extract_escaping_symlink_rejected(self, tmp_path):
        """Archive with escaping symlink raises HTTPException."""
        buffer = self._create_tar_with_symlink("evil.txt", "../../../etc/passwd")

        from fastapi import HTTPException

        with tarfile.open(fileobj=buffer, mode='r:gz') as tar:
            with pytest.raises(HTTPException) as exc_info:
                _safe_extract_tar(tar, tmp_path, max_files=100)

        assert exc_info.value.status_code == 400
        assert "INVALID_ARCHIVE" in str(exc_info.value.detail)

    def test_extract_too_many_files_rejected(self, tmp_path):
        """Archive with too many files raises HTTPException."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            for i in range(10):
                content = f"file {i}".encode()
                info = tarfile.TarInfo(name=f"file_{i}.txt")
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        buffer.seek(0)

        from fastapi import HTTPException

        with tarfile.open(fileobj=buffer, mode='r:gz') as tar:
            with pytest.raises(HTTPException) as exc_info:
                _safe_extract_tar(tar, tmp_path, max_files=5)  # Only allow 5

        assert exc_info.value.status_code == 400
        assert "TOO_MANY_FILES" in str(exc_info.value.detail)

    def test_extract_path_traversal_rejected(self, tmp_path):
        """Archive with path traversal raises HTTPException."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
            content = b"malicious"
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        buffer.seek(0)

        from fastapi import HTTPException

        with tarfile.open(fileobj=buffer, mode='r:gz') as tar:
            with pytest.raises(HTTPException) as exc_info:
                _safe_extract_tar(tar, tmp_path, max_files=100)

        assert exc_info.value.status_code == 400
        assert "INVALID_ARCHIVE" in str(exc_info.value.detail)
