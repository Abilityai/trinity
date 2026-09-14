"""
#2396 — the provider's own warning tier is not a rate limit.

`_headroom_indicates_limited` judged a window by `status not in ("allowed",)`.
That catch-all is deliberate — it exists so an unknown *blocking* status is
caught without anyone enumerating a vocabulary the provider does not publish
(pinned by `test_a_blocked_window_under_an_ok_status_still_reads_as_limited` in
test_447) — but it cannot distinguish a NON-blocking status whose name is not
the literal `"allowed"`. `allowed_warning` is exactly that, so every
subscription approaching its weekly window was reported rate-limited while the
provider was still serving it.

Verified on a live instance before the fix: a stored snapshot recording
`seven_day: 90.0 / allowed_warning` with `overage_status: allowed` sat beside 47
successful executions in the same two hours and ZERO
`subscription_rate_limit_events` — the provider said allowed and meant it.

The fix narrows the catch-all to a named allowlist rather than inverting it to a
blocklist. The direction matters: a deny-check silently admits every future
value (the #848 lesson in docs/memory/learnings.md), and here the safe default
runs the other way — an unrecognised status must still read as blocking.

Test hygiene per learnings 2026-08-12/13 (#2114 stub-leak class): no bare
`sys.modules` stubs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_FRESH = 10  # comfortably inside FRESHNESS_SECONDS


def _headroom(**kw):
    from db_models import HeadroomWindow, SubscriptionHeadroom

    win = kw.pop("five_hour", None)
    seven = kw.pop("seven_day", None)
    return SubscriptionHeadroom(
        five_hour=HeadroomWindow(**win) if isinstance(win, dict) else win,
        seven_day=HeadroomWindow(**seven) if isinstance(seven, dict) else seven,
        **kw,
    )


def _window(status, pct=78.0):
    return {"utilization_pct": pct, "resets_at": "2026-09-01T00:00:00Z", "status": status}


# =============================================================================
# A — the defect: a warning tier read as a limit
# =============================================================================

class TestWarningTierIsNotALimit:
    def test_allowed_warning_does_not_indicate_limited(self):
        """The bug, stated directly."""
        from services.subscription_headroom_service import _headroom_indicates_limited

        h = _headroom(
            seven_day=_window("allowed_warning", 90.0),
            snapshot_age_seconds=_FRESH,
            status="ok",
        )
        assert _headroom_indicates_limited(h) is False

    def test_allowed_warning_is_positive_proof_of_headroom(self):
        """The deliberate half (#2396): a warning tier CLEARS a stale db limit.

        The provider reached the quota and answered "yes". `_headroom_indicates_
        healthy` excludes stale / rejected-token / transport-error because none
        of those is evidence the subscription is usable — a served request is.
        Excluding it would keep a `LIMIT` badge on a working subscription for
        the db predicate's full two hours: #447 returning in a narrower window.
        """
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy,
            resolve_rate_limited_now,
        )

        h = _headroom(
            seven_day=_window("allowed_warning", 90.0),
            snapshot_age_seconds=_FRESH,
            status="ok",
        )
        assert _headroom_indicates_healthy(h) is True
        assert resolve_rate_limited_now(db_says_limited=True, headroom=h) is False

    def test_end_to_end_resolution_for_a_warning_tier_subscription(self):
        from services.subscription_headroom_service import resolve_rate_limited_now

        h = _headroom(
            five_hour=_window("allowed", 41.0),
            seven_day=_window("allowed_warning", 78.0),
            snapshot_age_seconds=_FRESH,
            status="ok",
        )
        assert resolve_rate_limited_now(db_says_limited=False, headroom=h) is False


# =============================================================================
# B — the fail-safe the fix must NOT trade away
# =============================================================================

class TestUnknownStatusesStillFailSafe:
    @pytest.mark.parametrize(
        "status",
        ["blocked", "rejected", "queueing", "throttled", "allowed_but_typo", "ALLOWED"],
    )
    def test_any_unrecognised_status_still_reads_as_limited(self, status):
        """An allowlist, never a blocklist — including case sensitivity.

        `ALLOWED` is in the list on purpose: the provider sends lowercase, so an
        uppercase variant is an unrecognised value and must fail safe rather
        than being quietly case-folded into the allowed set.
        """
        from services.subscription_headroom_service import _headroom_indicates_limited

        h = _headroom(
            seven_day=_window(status), snapshot_age_seconds=_FRESH, status="ok"
        )
        assert _headroom_indicates_limited(h) is True

    @pytest.mark.parametrize("status", ["blocked", "rejected", "ALLOWED"])
    def test_an_unrecognised_status_is_never_proof_of_health(self, status):
        from services.subscription_headroom_service import _headroom_indicates_healthy

        h = _headroom(
            seven_day=_window(status), snapshot_age_seconds=_FRESH, status="ok"
        )
        assert _headroom_indicates_healthy(h) is False

    def test_a_real_429_still_wins_over_any_window_status(self):
        """The window arm is the WEAKEST of three detectors, and stays that way.

        An actual HTTP 429 sets the top-level snapshot status, checked before
        any window is inspected — so even a fully non-blocking window set cannot
        mask a real block.
        """
        from services.subscription_headroom_service import _headroom_indicates_limited

        h = _headroom(
            five_hour=_window("allowed", 2.0),
            seven_day=_window("allowed_warning", 80.0),
            snapshot_age_seconds=_FRESH,
            status="rate_limited",
        )
        assert _headroom_indicates_limited(h) is True

    def test_a_stale_warning_snapshot_falls_through_to_the_db_predicate(self):
        """Freshness still dominates: an old reading proves nothing either way."""
        from services.subscription_headroom_service import (
            _headroom_indicates_healthy,
            _headroom_indicates_limited,
            resolve_rate_limited_now,
        )

        h = _headroom(
            seven_day=_window("allowed_warning", 90.0),
            snapshot_age_seconds=10_000,
            status="ok",
        )
        assert _headroom_indicates_limited(h) is False
        assert _headroom_indicates_healthy(h) is False
        assert resolve_rate_limited_now(db_says_limited=True, headroom=h) is True
        assert resolve_rate_limited_now(db_says_limited=False, headroom=h) is False

    def test_a_warning_status_with_no_windows_is_not_proof_of_health(self):
        """Unchanged from #447: no windows ⇒ nothing was actually measured."""
        from services.subscription_headroom_service import _headroom_indicates_healthy

        assert _headroom_indicates_healthy(
            _headroom(snapshot_age_seconds=_FRESH, status="ok")
        ) is False


# =============================================================================
# C — one vocabulary, two languages
# =============================================================================

class TestVocabularyParity:
    """Two homes for one status vocabulary is how this bug happened.

    The backend decides `rate_limited_now`; the frontend's `bindingWindow`
    independently asks the same question to pick the binding window. They cannot
    share code across the language boundary, so they share a pinned vocabulary
    instead — the `model_catalog` (#2086) / `mcp_validator` (#2007) pattern at
    the smallest scale it is worth applying.
    """

    def _js_vocabulary(self) -> list:
        src = (
            _REPO / "src" / "frontend" / "src" / "utils" / "subscriptionPressure.js"
        ).read_text()
        m = re.search(
            r"export const NON_BLOCKING_WINDOW_STATUSES = Object\.freeze\(\[(.*?)\]\)",
            src,
            re.S,
        )
        assert m, "frontend NON_BLOCKING_WINDOW_STATUSES not found or reshaped"
        return re.findall(r"'([^']+)'", m.group(1))

    def test_frontend_and_backend_agree_on_the_vocabulary(self):
        from services.subscription_headroom_service import NON_BLOCKING_WINDOW_STATUSES

        assert set(self._js_vocabulary()) == set(NON_BLOCKING_WINDOW_STATUSES)

    def test_the_vocabulary_contains_the_status_this_issue_is_about(self):
        """Guards the regex above: an empty match set would pass equality."""
        from services.subscription_headroom_service import NON_BLOCKING_WINDOW_STATUSES

        assert "allowed_warning" in NON_BLOCKING_WINDOW_STATUSES
        assert "allowed" in NON_BLOCKING_WINDOW_STATUSES
        assert "allowed_warning" in self._js_vocabulary()

    def test_both_predicates_read_the_shared_constant_not_a_literal(self):
        """A future edit must not re-inline `("allowed",)` into one of them."""
        src = (
            _BACKEND / "services" / "subscription_headroom_service.py"
        ).read_text()
        for fn in ("_headroom_indicates_limited", "_headroom_indicates_healthy"):
            body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
            assert "NON_BLOCKING_WINDOW_STATUSES" in body, (
                f"{fn} must judge window status through the shared constant"
            )
            assert '"allowed"' not in body, (
                f"{fn} re-inlines a status literal — the two homes will drift"
            )
