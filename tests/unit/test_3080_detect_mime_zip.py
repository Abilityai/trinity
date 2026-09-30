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

from services import agent_shared_files_service as svc
from utils.zip_signature import ZIP_SIGNATURES, is_zip_container


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.pdf", "x" * 40000)
    return buf.getvalue()


def _zip(entries, compression=zipfile.ZIP_DEFLATED, first_stored=None, zip64=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        if first_stored:
            name, data = first_stored
            zf.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_STORED)
        for name, data in entries:
            if zip64:
                with zf.open(name, "w", force_zip64=True) as fh:
                    fh.write(data.encode())
            else:
                zf.writestr(name, data)
    return buf.getvalue()


_CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'

# libmagic 5.46 misreads the plain-ZIP rows from a buffer but still recognises the
# ZIP-based formats, which is why detect_mime() consults libmagic first.
_ZIP_VARIANTS = {
    "deflated": _zip([("a.txt", "x" * 40000)]),
    "stored": _zip([("a.txt", "x" * 40000)], zipfile.ZIP_STORED),
    "tiny": _zip([("a.txt", "hi")]),
    "empty": _zip([]),
    "zip64": _zip([("a.txt", "x" * 1000)], zip64=True),
    "docx": _zip([("[Content_Types].xml", _CT), ("word/document.xml", "<w/>")]),
    "xlsx": _zip([("[Content_Types].xml", _CT), ("xl/workbook.xml", "<w/>")]),
    "pptx": _zip([("[Content_Types].xml", _CT), ("ppt/presentation.xml", "<p/>")]),
    "jar": _zip([("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n"), ("A.class", "\xca\xfe")]),
    "epub": _zip([("META-INF/container.xml", "<c/>")], first_stored=("mimetype", "application/epub+zip")),
    "odt": _zip([("content.xml", "<o/>")], first_stored=("mimetype", "application/vnd.oasis.opendocument.text")),
    "apk": _zip([("AndroidManifest.xml", "\x03\x00"), ("classes.dex", "dex\n035")]),
}


@pytest.mark.skipif(not svc._MAGIC_AVAILABLE, reason="python-magic/libmagic not installed")
@pytest.mark.parametrize("name", sorted(_ZIP_VARIANTS))
def test_buffer_detection_matches_libmagic_file_detection(name, tmp_path):
    """The buffer path agrees with libmagic's file path, which 5.46 still gets right."""
    import magic

    data = _ZIP_VARIANTS[name]
    path = tmp_path / name
    path.write_bytes(data)
    assert svc.detect_mime(data) == magic.from_file(str(path), mime=True)


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


def test_executable_is_still_blocked_after_detection():
    """The ZIP fallback never relabels an executable: the blocklist checks the prefix first."""
    from fastapi import HTTPException

    exe = b"MZ\x90\x00" + b"\x00" * 60
    with (
        patch.object(svc, "_MAGIC_AVAILABLE", True),
        patch.object(svc, "magic", _fake_magic("application/octet-stream"), create=True),
    ):
        mime = svc.detect_mime(exe)
    assert mime == "application/octet-stream"
    with pytest.raises(HTTPException):
        svc.check_mime_blocklist(exe, mime)

