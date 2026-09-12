"""The CSP<->preview-loader contract for the Files tab (#1400, #2733).

One bug class, two halves, kept in one file because the person who next edits
``connect-src`` needs to meet both: *a CSP directive silently breaks Files-tab
preview*. #1400 was a missing source (``blob:``); #2733 was a source that can
never be added (the portal base URL's origin, a per-deployment runtime setting),
whose only real fix is to stop needing it — the preview fetch is rewritten
same-origin in ``portalFiles.js::sharePreviewPath``.

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

The #2733 half guards the *contract*, never JavaScript syntax (a regex over a
function body rejects a functionally identical edit and accepts a dead branch):

  - ``connect-src`` is a FROZEN SET in both sources. Adding a host to make
    portal file preview work is the move #2733 rejects, and it defeats every
    weaker guard: a literal origin carries no wildcard and no placeholder, and
    adding it to both files keeps the in-sync test green.
  - the share-route literal in ``portalFiles.js`` must equal the one
    ``client_portal/service.py`` builds ``download_url`` from. This is what makes
    a backend route living in the frontend safe: version it to ``/api/v1/files/``
    and CI fails instead of preview silently re-breaking.
  - the loader carries a stable ``@csp-coupled:`` marker comment, so the reason
    sits where the next editor reads it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent.parent / "src"
_FRONTEND = _SRC / "frontend"
_NGINX_CONF = _FRONTEND / "security-headers.conf"
_VITE_CONF = _FRONTEND / "vite.config.js"
_PORTAL_FILES_JS = _FRONTEND / "src" / "components" / "portal" / "portalFiles.js"
_PORTAL_SERVICE_PY = _SRC / "backend" / "client_portal" / "service.py"

# #2733 — the connect-src sources, frozen. A new entry here is a deliberate,
# reviewed decision in its own commit, never a silent one inside a bug fix.
_CONNECT_SRC_FROZEN = frozenset({
    "'self'",
    "blob:",
    "ws:",
    "wss:",
    "https://us-central1-mcp-server-project-455215.cloudfunctions.net",
    "https://intake.abilityai.dev",
})

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


# ---------------------------------------------------------------------------
# #2733 — the CSP cannot carry a runtime origin, so the loader must not need one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", [_nginx_csp, _vite_csp], ids=["nginx", "vite"])
def test_connect_src_is_a_frozen_set(loader):
    """connect-src must equal the frozen set exactly — additions are reviewed.

    Set equality rather than a shape check ("no wildcard, no placeholder"),
    because the lever a developer actually pulls is a literal host, which has
    neither and would sail through.
    """
    sources = _parse_csp(loader())["connect-src"]
    assert sources == set(_CONNECT_SRC_FROZEN), (
        "connect-src changed in "
        f"{loader.__name__}: added={sorted(sources - _CONNECT_SRC_FROZEN)} "
        f"removed={sorted(_CONNECT_SRC_FROZEN - sources)}. If you are adding an "
        "origin here to make portal file preview work, that is #2733 — the portal "
        "base URL is a per-deployment runtime setting that no static header can "
        "carry (and CORS would refuse it a second time). Make the fetch "
        "same-origin in portalFiles.js::sharePreviewPath instead. A legitimate "
        "new host is added to _CONNECT_SRC_FROZEN in its own commit."
    )


def test_share_route_literal_matches_the_backend():
    """The frontend's share-route constant must equal the backend's own f-string.

    `sharePreviewPath` slices the path from `/api/files/` onward, which is the
    exact inverse of the url `portal_documents` composes. Nothing else couples
    the two, so version the route and preview re-breaks silently — unless this
    fails first.
    """
    backend = re.search(r'f"(/api/files/)\{', _PORTAL_SERVICE_PY.read_text())
    assert backend, (
        "client_portal/service.py no longer builds a download_url from an "
        'f"/api/files/{...}" literal — #2733\'s same-origin preview rewrite in '
        "portalFiles.js slices on that exact route. Re-point both sides together."
    )
    # Whole file, not a function-body slice: the constant is module-level.
    frontend = re.search(
        r"const SHARED_FILE_ROUTE\s*=\s*'(/api/files/)'", _PORTAL_FILES_JS.read_text()
    )
    assert frontend, (
        "portalFiles.js must declare `const SHARED_FILE_ROUTE = "
        f"'{backend.group(1)}'` — the route the backend builds download_url from "
        "(#2733). The preview fetch is rewritten onto the portal page's own "
        "origin by slicing from it."
    )
    assert frontend.group(1) == backend.group(1)


def test_preview_loader_declares_the_csp_coupling():
    """The loader carries the marker comment that names this coupling.

    A comment token, not an AST assertion: it pins the REASON where the next
    editor reads it, and reformatting cannot break it.
    """
    assert "@csp-coupled:" in _PORTAL_FILES_JS.read_text(), (
        "portalFiles.js lost its `@csp-coupled:` marker — the note saying "
        "connect-src cannot carry portal_base_url's origin, which is why the "
        "preview fetch must be same-origin (#2733)."
    )
