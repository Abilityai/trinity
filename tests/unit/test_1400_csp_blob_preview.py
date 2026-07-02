"""CSP must allow blob: for Files-tab preview/download (#1400).

Regression guard for the bug class where a CSP fetch/media/object directive
omits ``blob:`` and silently breaks agent file preview. The Files tab renders
blob: URLs (``URL.createObjectURL``) through several channels, each gated by a
different CSP directive:

  - text preview  → ``fetch(URL.createObjectURL(blob))``  → connect-src
  - download      → ``fetch(previewData.url)``            → connect-src
  - image preview → ``<img src="blob:">``                 → img-src
  - video/audio   → ``<video>/<audio src="blob:">``       → media-src
  - PDF preview   → ``<embed src="blob:">``                → object-src + frame-src
                    (Chrome renders the embedded PDF in an internal viewer frame,
                     so the blob load is checked against frame-src too)

``blob:`` is NOT covered by ``'self'`` — it must be listed explicitly (which is
why ``img-src`` already lists it). The original bug (#784 misdiagnosed, real
cause here) shipped because ``connect-src`` omitted ``blob:`` while ``img-src``
listed it, so images previewed but text/media did not.

Both CSP sources — the production nginx ``security-headers.conf`` and the dev
``vite.config.js`` (which explicitly mirrors it) — must list ``blob:`` in
connect-src, media-src and object-src, and stay in sync.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "src" / "frontend"
_NGINX_CONF = _FRONTEND / "security-headers.conf"
_VITE_CONF = _FRONTEND / "vite.config.js"

# Directives that MUST include blob: so blob: URLs render / are fetchable.
# frame-src is required because Chrome renders an <embed> PDF in an internal
# viewer frame, so the blob load is checked against frame-src (not just object-src).
_BLOB_REQUIRED = ("connect-src", "media-src", "object-src", "frame-src", "img-src")


def _parse_csp(csp: str) -> dict:
    """Parse a CSP header value into {directive: {sources}}."""
    directives: dict = {}
    for part in csp.split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0]] = set(tokens[1:])
    return directives


def _nginx_csp() -> str:
    text = _NGINX_CONF.read_text()
    m = re.search(r'add_header\s+Content-Security-Policy\s+"([^"]+)"', text)
    assert m, "no Content-Security-Policy add_header found in security-headers.conf"
    return m.group(1)


def _vite_csp() -> str:
    text = _VITE_CONF.read_text()
    # The CSP is a concatenation of "..." string literals assigned to the
    # 'Content-Security-Policy' key, up to the end of the devSecurityHeaders object.
    m = re.search(r"'Content-Security-Policy':(.*?)\n\}", text, re.DOTALL)
    assert m, "no Content-Security-Policy key found in vite.config.js"
    fragments = re.findall(r'"([^"]*)"', m.group(1))
    assert fragments, "Content-Security-Policy value has no string literals"
    return "".join(fragments)


@pytest.mark.parametrize("loader", [_nginx_csp, _vite_csp], ids=["nginx", "vite"])
def test_csp_directives_allow_blob(loader):
    directives = _parse_csp(loader())
    for directive in _BLOB_REQUIRED:
        assert directive in directives, (
            f"{directive} missing from CSP ({loader.__name__}) — required for "
            f"Files-tab preview (#1400)"
        )
        assert "blob:" in directives[directive], (
            f"{directive} must include blob: for Files-tab preview/download "
            f"(#1400) — got {sorted(directives[directive])}"
        )


def test_csp_sources_in_sync_for_blob_directives():
    """The dev (vite) and prod (nginx) CSPs must agree on the blob: directives.

    script-src legitimately differs (dev needs 'unsafe-inline'/'unsafe-eval' for
    HMR), so only the preview-relevant directives are compared.
    """
    nginx = _parse_csp(_nginx_csp())
    vite = _parse_csp(_vite_csp())
    for directive in _BLOB_REQUIRED:
        assert nginx.get(directive) == vite.get(directive), (
            f"{directive} differs between security-headers.conf and vite.config.js "
            f"— they must mirror each other. nginx={sorted(nginx.get(directive) or [])} "
            f"vite={sorted(vite.get(directive) or [])}"
        )
