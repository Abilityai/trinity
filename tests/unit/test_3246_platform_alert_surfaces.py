"""
#3246 — a row the PLATFORM ended reads as ended by the platform on the
client-facing projection, never as a person's answer and never as a timeout.

`client_portal/asks/service.py::_ending_of` is the coarse Workspace projection
of the endings ledger. Before #3246 it read only `disposed_by_email`, so a
platform cancel (`disposed_by = 'platform'`, no email) fell through to
`operator` — a person who never acted. The canary's L-03 orphan scan must also
know the `_skills-sync` alarm host, whose rows now arrive through the seam.

Related flow: docs/memory/feature-flows/operating-room.md (Platform alerts).
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

ENDED_AT = "2026-10-05T10:00:00Z"


def _ended(**over):
    row = {
        "id": "r1", "status": "cancelled", "disposition": "cancelled",
        "disposed_at": ENDED_AT, "disposed_by": "person",
        "disposed_by_email": None, "disposition_reason": None,
    }
    row.update(over)
    return row


class TestEndingOfPlatform:
    @pytest.mark.parametrize("reason", ["condition_cleared", "superseded"])
    def test_platform_ending_reads_platform(self, reason):
        from client_portal.asks.service import _ending_of

        item = _ended(disposed_by="platform", disposition_reason=reason)
        assert _ending_of(item, "client@example.com") == (ENDED_AT, "platform")

    def test_platform_ending_is_not_a_viewer_answer_even_when_viewer_unknown(self):
        from client_portal.asks.service import _ending_of

        assert _ending_of(_ended(disposed_by="platform"), None) == (ENDED_AT, "platform")

    def test_person_cancel_still_reads_operator(self):
        from client_portal.asks.service import _ending_of

        item = _ended(disposed_by_email="op@example.com")
        assert _ending_of(item, "client@example.com") == (ENDED_AT, "operator")

    def test_viewer_answer_still_reads_you(self):
        from client_portal.asks.service import _ending_of

        item = _ended(status="responded", disposition="answered",
                      disposed_by_email="client@example.com")
        assert _ending_of(item, "Client@Example.com") == (ENDED_AT, "you")

    def test_expiry_still_reads_timeout(self):
        from client_portal.asks.service import _ending_of

        item = _ended(status="expired", disposition="expired", disposed_by="timeout")
        assert _ending_of(item, "client@example.com") == (ENDED_AT, "timeout")


class TestCanarySentinel:
    def test_skills_sync_host_is_a_platform_alarm_sentinel(self):
        from canary.snapshot import _PLATFORM_ALARM_SENTINELS, _SENTINEL_SQL_LIST
        from services.skill_service import RECONCILE_ALARM_AGENT_NAME

        assert RECONCILE_ALARM_AGENT_NAME in _PLATFORM_ALARM_SENTINELS
        assert f"'{RECONCILE_ALARM_AGENT_NAME}'" in _SENTINEL_SQL_LIST
