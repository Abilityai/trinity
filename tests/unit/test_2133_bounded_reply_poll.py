"""The reply poll is bounded, and the bound covers a cold retry (#2133).

Two failures sit on opposite sides of one number, which is why it is worth
pinning rather than eyeballing:

* **Too large** — the marker's original 1-hour TTL. A hard backend kill skips
  the `finally` that clears it (and `except Exception` never catches
  `CancelledError`), so the client polls a dead turn ~5,100 times from a single
  tab, and the UI never says the reply is not coming.

* **Too small** — and this is the one that costs money. `run_resumable_turn`
  re-runs the WHOLE turn when a resume finds no JSONL, so a legitimate turn can
  take two full timeouts. A bound of one timeout expires the marker while the
  retry is still running, the client is told "nothing is running", and a live,
  already-billed turn is declared not delivered with a Retry beside it. That is
  precisely the double-billing #2120 fixed, reappearing on exactly the
  cold-retry path that fix existed for.

So the bound is `2 × turn timeout + slack`, the marker and the client share it,
and the server sends it to the client rather than the client guessing.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2133.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2133-logs"))

import pytest

pytestmark = pytest.mark.unit


def test_the_bound_covers_two_full_turns():
    """One timeout is not enough: the cold retry re-runs the whole turn."""
    from client_portal import service as svc

    assert svc.PORTAL_MAX_TURN_SECONDS > 2 * svc.PORTAL_TURN_TIMEOUT_SECONDS


def test_one_attempt_is_not_bounded_by_timeout_seconds():
    """The first version of this fix assumed it was, and was therefore still too
    small. `execute_task` dispatches with `timeout_seconds + 10`, and the #678
    reader-race auto-retry adds a whole second HTTP call on top of whatever
    attempt 1 already burned — capped at `_AUTO_RETRY_MAX_TIMEOUT_S`, not at the
    remaining budget.

    Read from the REAL constants, so raising either one fails here rather than
    silently shrinking the marker below a live turn.
    """
    from client_portal import service as svc
    from services import task_execution_service as tes

    worst_attempt = svc.PORTAL_TURN_TIMEOUT_SECONDS + 10 + tes._AUTO_RETRY_MAX_TIMEOUT_S
    assert svc.PORTAL_ATTEMPT_CEILING_SECONDS >= worst_attempt
    assert svc.PORTAL_MAX_TURN_SECONDS >= 2 * worst_attempt


def test_the_marker_ttl_is_that_bound():
    """The client waits for as long as the marker claims a turn is running, so a
    TTL shorter than the bound cuts off a live turn no matter what the client
    does."""
    from client_portal import service as svc

    assert svc.PORTAL_INFLIGHT_TTL_SECONDS == svc.PORTAL_MAX_TURN_SECONDS


def test_the_bound_is_nowhere_near_the_old_hour():
    """The other failure direction. An hour-long marker outlives the work, so a
    backend restart mid-turn leaves the composer disabled for the remainder."""
    from client_portal import service as svc

    assert svc.PORTAL_INFLIGHT_TTL_SECONDS < 3600


def test_the_turn_is_dispatched_with_the_timeout_the_bound_is_built_from():
    """The bound is derived from `PORTAL_TURN_TIMEOUT_SECONDS`, so the dispatch
    must actually use that constant. A literal `300` here would let the two
    drift silently — which is how the bound stops covering the retry."""
    import inspect
    from client_portal import service as svc

    src = inspect.getsource(svc.portal_chat)
    assert "timeout_seconds=PORTAL_TURN_TIMEOUT_SECONDS" in src, (
        "portal_chat must dispatch with the constant the bound is computed from"
    )


def test_the_202_carries_the_budget_to_the_client():
    """The client must not invent its own ceiling: the server owns the timeout.
    A frontend constant drifts the next time it changes."""
    from client_portal.models import PortalTurnStarted

    assert "wait_budget_seconds" in PortalTurnStarted.model_fields


def test_the_budget_field_is_optional_so_an_older_client_is_unaffected():
    from client_portal.models import PortalTurnStarted

    started = PortalTurnStarted(execution_id="e1")
    assert started.wait_budget_seconds is None
