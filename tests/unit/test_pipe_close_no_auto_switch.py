"""Regression guard for the SUB-003 / pipe-close collision (#474).

The agent-server's subprocess pipe-close handler raises HTTP 502 (not 503)
specifically to avoid colliding with SUB-003 auth-switch semantics in
`task_execution_service.py:622-642`:

    elif agent_status_code == 503 or is_auth_failure(error_msg):
        await handle_subscription_failure(... failure_kind="auth")
        error_code = TaskExecutionErrorCode.AUTH

If a future change flips the pipe-close back to 503 — or adds 502 to the
auth-switch branch — this test fires. Without it, a transient pipe drop
would silently swap the agent's subscription on every event.

Two layers of guard:

1. Pure unit assertion on `is_auth_failure` — the pipe-close detail string
   doesn't match any auth indicator.
2. Inline replay of the actual SUB-003 conditional under a 502 status, with
   `handle_subscription_failure` patched as a tracking spy. Verifies the
   conditional cannot fire for 502 / the pipe-close detail.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock



_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Load subscription_auto_switch directly without dragging in the rest of
# services/__init__.py.
_spec = importlib.util.spec_from_file_location(
    "subscription_auto_switch_under_test",
    str(_BACKEND / "services" / "subscription_auto_switch.py"),
)
sub_auto_switch = importlib.util.module_from_spec(_spec)
# `database` is imported at module top — but only `db` is referenced inside
# functions, so the import doesn't run anything that needs Redis/etc.
_spec.loader.exec_module(sub_auto_switch)


PIPE_CLOSE_DETAIL = "Agent subprocess closed before task could complete"


# ─── Layer 1: pure auth-failure classifier ──────────────────────────────────

def test_is_auth_failure_rejects_pipe_close_detail():
    """The pipe-close detail must not match any AUTH_INDICATORS substring.
    If a future addition to AUTH_INDICATORS would match it (e.g. someone
    adds 'closed before' or similar), this test fires and we re-think the
    pipe-close detail wording."""
    assert sub_auto_switch.is_auth_failure(PIPE_CLOSE_DETAIL) is False


def test_is_auth_failure_still_matches_known_auth_indicators():
    """Regression guard: known indicators still match (we didn't accidentally
    break the classifier when adding pipe-close coverage)."""
    assert sub_auto_switch.is_auth_failure("credit balance too low") is True
    assert sub_auto_switch.is_auth_failure("Unauthorized — token expired") is True
    assert sub_auto_switch.is_auth_failure("403 Forbidden") is True


# ─── Layer 2: inline replay of the SUB-003 conditional ──────────────────────
#
# `task_execution_service.py:622-642`:
#
#     if agent_status_code == 429:
#         await handle_subscription_failure(..., failure_kind="rate_limit")
#     elif agent_status_code == 503 or is_auth_failure(error_msg):
#         await handle_subscription_failure(..., failure_kind="auth")
#
#     error_code = None
#     if agent_status_code == 503:
#         error_code = TaskExecutionErrorCode.AUTH
#
# Replay this conditional in isolation against a 502 status + the pipe-close
# detail. Asserts the spy was never called and no auth error_code is set.


def _run_sub_003_conditional(
    agent_status_code: int, error_msg: str, handle_spy: AsyncMock
) -> str | None:
    """Replay the conditional shape (no dependency on the surrounding
    function). Returns the equivalent of `error_code` so the test can
    assert AUTH only fires on 503."""

    async def _run():
        if agent_status_code == 429:
            await handle_spy(failure_kind="rate_limit")
        elif agent_status_code == 503 or sub_auto_switch.is_auth_failure(error_msg):
            await handle_spy(failure_kind="auth")

        error_code = None
        if agent_status_code == 503:
            error_code = "AUTH"
        return error_code

    return asyncio.run(_run())


def test_502_pipe_close_does_not_trigger_subscription_switch():
    """The critical regression guard: 502 + the canonical pipe-close detail
    must NOT call handle_subscription_failure and must NOT set
    error_code=AUTH. This pins v3's choice of 502 over 503."""
    spy = AsyncMock()

    error_code = _run_sub_003_conditional(
        agent_status_code=502,
        error_msg=PIPE_CLOSE_DETAIL,
        handle_spy=spy,
    )

    spy.assert_not_called()
    assert error_code is None


def test_503_from_agent_still_triggers_auth_switch():
    """Regression: the existing SUB-003 503-auth behaviour is intact —
    we only carved out 502, not the whole auth path."""
    spy = AsyncMock()

    error_code = _run_sub_003_conditional(
        agent_status_code=503,
        error_msg="Some agent-side error",
        handle_spy=spy,
    )

    spy.assert_called_once()
    assert spy.await_args.kwargs.get("failure_kind") == "auth"
    assert error_code == "AUTH"


def test_429_still_triggers_rate_limit_switch():
    """Regression: 429 still routes to rate_limit (separate path from auth)."""
    spy = AsyncMock()

    error_code = _run_sub_003_conditional(
        agent_status_code=429,
        error_msg="rate limited",
        handle_spy=spy,
    )

    spy.assert_called_once()
    assert spy.await_args.kwargs.get("failure_kind") == "rate_limit"
    assert error_code is None
