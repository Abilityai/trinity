"""
#3583 — a public link's `expires_at` must be a real ISO timestamp.

The create and update bodies took `expires_at` as a free string and stored it.
`_link_to_response` then parsed it per row, so one bad value made every list of
that agent's links 500 and the owner panel showed "No public links".

Pinned here: both request models reject an unparseable value (FastAPI answers
422 before the handler runs, so nothing is stored), and a row that already holds
a bad value still renders in the list. A stored value that does not parse
counts as already expired on both the public check and the owner list, so a
link meant to expire never grants indefinite access.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("model_name", ["PublicLinkCreate", "PublicLinkUpdate"])
@pytest.mark.parametrize("bad", ["not-a-date", "2026-13-01T00:00:00Z"])
def test_invalid_expires_at_rejected(model_name, bad):
    import db_models

    with pytest.raises(ValidationError):
        getattr(db_models, model_name)(expires_at=bad)


@pytest.mark.parametrize("model_name", ["PublicLinkCreate", "PublicLinkUpdate"])
@pytest.mark.parametrize("good", ["2026-12-01T00:00:00.000Z", "2026-12-01T00:00:00+02:00", "2026-12-01T00:00:00", "", None])
def test_valid_expires_at_accepted_unchanged(model_name, good):
    import db_models

    assert getattr(db_models, model_name)(expires_at=good).expires_at == good


def test_stored_bad_expiry_does_not_break_the_list(monkeypatch):
    import routers.public_links as pl

    monkeypatch.setattr(pl, "get_public_chat_url", lambda: None)
    row = {
        "id": "l1",
        "agent_name": "a",
        "token": "t",
        "created_by": "u",
        "created_at": "2026-10-01T00:00:00Z",
        "expires_at": "not-a-date",
        "enabled": True,
        "name": "x",
        "type": "chat",
    }
    resp = pl._link_to_response(row, include_usage=False)
    assert resp.id == "l1"
    # Shown as already expired, matching the public check below.
    assert resp.expires_at is not None
    assert resp.expires_at < datetime.now(timezone.utc)


def test_stored_bad_expiry_fails_closed_on_the_public_check():
    from db.public_links import PublicLinkOperations

    ops = PublicLinkOperations.__new__(PublicLinkOperations)
    ops.get_public_link_by_token = lambda token: {"enabled": True, "expires_at": "not-a-date"}
    assert ops.is_link_valid("t") == (False, "expired", None)
