"""
ZIP container signature check shared by the inbound and outbound file paths.

libmagic 5.46 (Debian trixie) detects a ZIP as ``application/octet-stream``
from a buffer, so both paths recognise the container by its signature
instead of trusting ``magic.from_buffer`` for it (#3046, #3080).
"""

# Local file header, empty archive, spanned archive
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def is_zip_container(data: bytes) -> bool:
    """Return True if `data` starts with a ZIP container signature."""
    return data.startswith(ZIP_SIGNATURES)
