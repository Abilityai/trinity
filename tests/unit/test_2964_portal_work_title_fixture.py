"""#2964 — the chat card's pre-read title must match the feed's title exactly.

The Workspace chat's live card renders a synthetic item until the Work feed
reads the turn; its title is ``previewTitle(message)`` in
``src/frontend/src/components/portal/portalWork.js``, a mirror of
``client_portal/work/service.py::clean_title``. If the two disagree, a long
message re-wraps when the feed's row replaces the synthetic one and the card
jumps (#2964).

The contract is the shared fixture ``tests/fixtures/portal-work-titles.json``:
titles PROBED from the real ``clean_title``, never hand-typed. This test
re-proves every row against the server (the drift alarm for a change to
``clean_title`` or ``TITLE_MAX``); the vitest spec
``src/frontend/tests/unit/portalWorkTitle.spec.js`` proves the mirror agrees.
If this goes red, re-probe the titles from ``clean_title`` and port the change
into ``previewTitle`` until the vitest spec agrees too.

``masked`` rows hold a secret: the server masks it and the mirror does not (the
accepted gap), so those rows must stay marked — a row that stops being masked
here is the signal that the marker is stale.
"""

import json
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_FIXTURE = _REPO / "tests" / "fixtures" / "portal-work-titles.json"
_ROWS = json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def svc():
    from client_portal.work import service as mod
    return mod


def test_fixture_is_non_empty_and_names_the_bound(svc):
    assert len(_ROWS) > 10
    assert svc.TITLE_MAX == 120


@pytest.mark.parametrize("row", _ROWS, ids=[r["note"] for r in _ROWS])
def test_fixture_row_matches_clean_title(svc, row):
    assert svc.clean_title(row["message"]) == row["title"], (
        f"clean_title drift on {row['note']!r}: re-probe the fixture and port the "
        f"change into previewTitle (see module docstring)."
    )


@pytest.mark.parametrize("row", _ROWS, ids=[r["note"] for r in _ROWS])
def test_masked_marker_is_current(svc, row):
    text = re.sub(r"\s+", " ", row["message"]).strip()
    unmasked = "(no message)" if not text else (
        text if len(text) <= svc.TITLE_MAX else text[: svc.TITLE_MAX - 1].rstrip() + "…"
    )
    assert (svc.clean_title(row["message"]) != unmasked) == bool(row.get("masked"))
