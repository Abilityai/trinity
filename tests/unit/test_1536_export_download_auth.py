"""Export downloads must carry the Bearer token (#1536).

The shipped version used `<a :href="/api/reports/{id}/export?format=xlsx" download>`
with a comment asserting that "the session cookie/JWT interceptor is not involved
in a binary body". That reasoning was wrong about this platform: Trinity holds
its JWT in localStorage and attaches it via the `api.js` request interceptor, so
a raw browser navigation sends NO Authorization header and the endpoint answers
**401**. The export button was unusable from the UI — which is the entire point
of #1536 — while `curl -H "Authorization: Bearer ..."` against the same URL
worked, so endpoint-level testing could not see it.

This is a static guard, deliberately: the failure is in markup, not in a
function, so there is nothing to call. It asserts the anchors are gone and the
authed store helper is used instead.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parents[2] / "src" / "frontend" / "src"
_PANELS = [
    _FRONTEND / "components" / "ReportsPanel.vue",
    _FRONTEND / "components" / "ReportsPanelFleet.vue",
]


@pytest.mark.parametrize("panel", _PANELS, ids=lambda p: p.name)
def test_no_unauthenticated_anchor_download(panel: Path):
    src = panel.read_text()
    anchors = re.findall(r'<a\b[^>]*href[^>]*?/export\b', src, flags=re.S)
    assert not anchors, (
        f"{panel.name} downloads the export via a raw <a href>, which sends no "
        "Authorization header (JWT lives in localStorage + the api.js "
        "interceptor) and gets 401. Use downloadReportExport() from the store."
    )


@pytest.mark.parametrize("panel", _PANELS, ids=lambda p: p.name)
def test_export_goes_through_the_authed_store_helper(panel: Path):
    src = panel.read_text()
    assert "downloadReportExport" in src, (
        f"{panel.name} must call the store's downloadReportExport(), which "
        "fetches through the shared api client so the auth interceptor runs "
        "(Invariant #7: one API client, no raw fetch)."
    )


def test_store_helper_requests_a_blob_through_the_shared_client():
    src = (_FRONTEND / "stores" / "reports.js").read_text()
    i = src.index("export async function downloadReportExport")
    body = src[i : i + 1200]
    assert "api.get(" in body, "must use the shared api.js client, not raw fetch/axios"
    assert "responseType: 'blob'" in body, "a binary body needs responseType: 'blob'"
    # The backend already names the file; rebuilding it here would drift.
    assert "content-disposition" in body.lower()
