"""#3080 — outbound `detect_mime()` must classify a ZIP as `application/zip`.

libmagic 5.46 (Debian trixie, the current `python:3.13-slim`) returns
`application/octet-stream` for a ZIP read from a buffer, the same regression
#3046 hit on the inbound upload path. `detect_mime()` therefore falls back to
the ZIP container signature, but only when libmagic's answer is the generic
one, so DOCX/XLSX/JAR keep the more specific type a working libmagic reports.
"""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from services import agent_shared_files_service as svc  # noqa: E402
from utils.zip_signature import ZIP_SIGNATURES, is_zip_container  # noqa: E402


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.pdf", "x" * 40000)
    return buf.getvalue()


def _fake_magic(detected: str) -> MagicMock:
    fake = MagicMock()
    fake.from_buffer.return_value = detected
    return fake


def test_zip_classified_when_libmagic_reports_octet_stream():
    with (
        patch.object(svc, "_MAGIC_AVAILABLE", True),
        patch.object(svc, "magic", _fake_magic("application/octet-stream"), create=True),
    ):
        assert svc.detect_mime(_zip_bytes()) == "application/zip"


def test_zip_classified_with_unmocked_magic():
    assert svc.detect_mime(_zip_bytes()) == "application/zip"


def test_specific_zip_based_type_from_libmagic_is_kept():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with (
        patch.object(svc, "_MAGIC_AVAILABLE", True),
        patch.object(svc, "magic", _fake_magic(docx), create=True),
    ):
        assert svc.detect_mime(_zip_bytes()) == docx


def test_non_zip_octet_stream_is_unchanged():
    with (
        patch.object(svc, "_MAGIC_AVAILABLE", True),
        patch.object(svc, "magic", _fake_magic("application/octet-stream"), create=True),
    ):
        assert svc.detect_mime(b"\x00\x01\x02\x03" * 16) == "application/octet-stream"


def test_zip_classified_without_python_magic():
    with patch.object(svc, "_MAGIC_AVAILABLE", False):
        assert svc.detect_mime(_zip_bytes()) == "application/zip"


@pytest.mark.parametrize("sig", ZIP_SIGNATURES)
def test_is_zip_container_accepts_each_signature(sig):
    assert is_zip_container(sig + b"\x00" * 26)


def test_is_zip_container_rejects_other_bytes():
    assert not is_zip_container(b"%PDF-1.4")
    assert not is_zip_container(b"PK")
