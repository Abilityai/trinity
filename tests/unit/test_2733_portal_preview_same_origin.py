"""Portal file preview must not depend on the portal base URL's origin (#2733).

A deployment may front its agents on a dedicated public hostname while the
portal page is served from the app hostname. ``portal_documents`` then builds
every ``download_url`` off that other origin, and the preview ``fetch()`` was
blocked before it left the browser: CSP ``connect-src`` lists ``'self'`` plus
two fixed hosts, and a per-deployment origin cannot be baked into a static
nginx or Vite header.

The fix is a same-origin rewrite in the loader, so what has to stay true is
structural rather than textual:

  - the preview fetch goes through ``sharePreviewPath``, which reduces an
    ``/api/`` url to a path, instead of fetching ``download_url`` directly;
  - Download keeps the absolute ``download_url``, an anchor navigation that
    ``connect-src`` does not govern, so the shareable link is unaffected;
  - the CSP stays static: no directive names a deployment-specific origin, so
    nothing re-introduces the dependency this issue removed.

Sibling of ``test_1400_csp_blob_preview.py``, which pins the ``blob:`` half of
the same preview path.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_FRONTEND = _ROOT / "src" / "frontend"
_RAIL = _FRONTEND / "src" / "components" / "portal" / "PortalRailFiles.vue"
_HELPERS = _FRONTEND / "src" / "components" / "portal" / "portalFiles.js"
_NGINX_CONF = _FRONTEND / "security-headers.conf"
_VITE_CONF = _FRONTEND / "vite.config.js"


def _strip_comments(text: str) -> str:
    """Drop /* */ and // runs so a prose mention cannot satisfy an assertion."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _nginx_csp() -> str:
    m = re.search(
        r'add_header\s+Content-Security-Policy\s+"([^"]+)"', _NGINX_CONF.read_text()
    )
    assert m, "no Content-Security-Policy add_header in security-headers.conf"
    return m.group(1)


def _vite_csp() -> str:
    m = re.search(
        r"'Content-Security-Policy':(.*?)\n\}", _VITE_CONF.read_text(), re.DOTALL
    )
    assert m, "no Content-Security-Policy key in vite.config.js"
    fragments = re.findall(r'"([^"]*)"', m.group(1))
    assert fragments, "Content-Security-Policy value has no string literals"
    return "".join(fragments)


def test_preview_fetch_is_rewritten_to_the_page_origin():
    """The rail previews through the rewrite, never straight off download_url."""
    rail = _strip_comments(_RAIL.read_text())

    assert "sharePreviewPath(" in rail, (
        "the Files rail no longer previews through sharePreviewPath — a direct "
        "fetch of an absolute download_url is what CSP connect-src blocks (#2733)"
    )
    # No parenthesis between `fetch(` and `download_url`: a call in between is
    # the rewrite doing its job, a bare read is the bug this issue is about.
    direct = re.findall(r"fetch\(\s*[^()]*download_url", rail)
    assert direct == [], (
        f"the preview fetch reads download_url directly: {direct}. On a "
        "deployment whose portal base URL is another origin that request is "
        "refused by connect-src before it is sent"
    )


def test_api_urls_are_reduced_to_a_path_whatever_origin_they_carry():
    """The rewrite is bound to the backend's own routes, not to the origin."""
    helpers = _strip_comments(_HELPERS.read_text())

    assert "BACKEND_API_PREFIX" in helpers, (
        "sameOriginPath no longer distinguishes the backend's own routes; an "
        "/api/ url from another origin must still be reduced to a path (#2733)"
    )
    assert re.search(r"startsWith\(\s*BACKEND_API_PREFIX\s*\)", helpers), (
        "the /api/ exemption in sameOriginPath is gone, so a cross-origin "
        "download_url would be fetched cross-origin again"
    )


def test_download_keeps_the_absolute_url():
    """Download is an anchor navigation, so the shareable link stays absolute."""
    rail = _strip_comments(_RAIL.read_text())
    assert re.search(r"\.href\s*=\s*row\.item\.download_url", rail), (
        "the Download path no longer uses the absolute download_url — it is the "
        "user-shareable link and saves natively through the anchor (#2733 AC 3)"
    )


@pytest.mark.parametrize("loader", [_nginx_csp, _vite_csp], ids=["nginx", "vite"])
def test_csp_names_no_deployment_specific_origin(loader):
    """The CSP stays a build-time constant.

    The portal base URL is a runtime setting, so a template placeholder or an
    interpolated origin in the header would be the wrong lever for preview and
    would silently re-couple it to the deployment's hostname.
    """
    csp = loader()
    for marker in ("${", "{{", "<PORTAL", "PORTAL_BASE_URL", "public_chat_url"):
        assert marker not in csp, (
            f"the CSP interpolates {marker!r} ({loader.__name__}): preview must "
            "not depend on the portal base URL's origin being allowlisted (#2733)"
        )
    directives = {
        part.split()[0]: set(part.split()[1:]) for part in csp.split(";") if part.split()
    }
    assert "'self'" in directives.get("connect-src", set()), (
        f"connect-src no longer allows 'self' ({loader.__name__}), which is the "
        "origin the preview fetch now targets"
    )
