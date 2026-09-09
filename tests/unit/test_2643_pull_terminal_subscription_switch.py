"""#2643 — a pull-dispatched terminal triggers SUB-003 like the push path does.

Split out of #2638 (gap 5). `pull_coordination_service.apply_task_result` is a
CAS-won terminal writer, and it had every OTHER terminal hook — the #1578
completion event, the #1804 activity close — but no SUB-003 hook. So a
pull-owned turn that died on a quota or credential failure:

  * recorded no `subscription_rate_limit_events` row, so the subscription was
    never skip-listed and no usage card or pressure badge counted it;
  * never called `handle_subscription_failure`, so the agent stayed pinned to a
    subscription that had just refused it;
  * landed FAILED, where `redelivery_governor` treats `billing` as a
    correlated code — so the re-delivery that might have recovered it is the
    thing most likely to be paused.

Inert today (the pull path is gated on `PULL_MODE_PILOT_AGENTS` and nobody is
piloted), which is exactly why it needs a test rather than a soak: turning pull
on for a subscription-backed agent without this silently removes SUB-003 from
that agent, and nothing would say so.

The layers below are the ACs:

  A. the failure-kind mapping is a pure function, and it is a MAP not a guess —
     an unknown code switches nothing;
  B. the hook fires once, on the CAS-won branch only;
  C. the spawn wrapper is fail-open and needs no `await` (the sink is sync, its
     caller is async — the `spawn_close_execution_activity` shape);
  D. a structural pin: the call site is inside `if won:`, so the CAS gate cannot
     be refactored away without this failing.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _execution(**over):
    base = dict(
        id="exec-2643",
        agent_name="worker-a",
        status="running",
        triggered_by="schedule",
        duration_ms=None,
        cost=None,
        fan_out_id=None,
        loop_id=None,
        claude_session_id="dispatched_async",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ===========================================================================
# Layer A — the mapping from a worker's error_code to a SUB-003 failure_kind
# ===========================================================================
class TestFailureKindMapping:
    """`error_code` is the worker's typed class; `failure_kind` is SUB-003's.

    They are NOT the same vocabulary and the names do not line up: the quota
    class is called `billing` on the wire (`result_callback._STATUS_MAP` maps an
    agent 429 to it) and `rate_limit` in `subscription_rate_limit_events`. A
    pass-through would file every 429 under an unknown kind.
    """

    def _map(self):
        from services.pull_coordination_service import _switch_failure_kind

        return _switch_failure_kind

    def test_billing_is_the_quota_class(self):
        assert self._map()("billing") == "rate_limit"

    def test_auth_keeps_its_name(self):
        assert self._map()("auth") == "auth"

    def test_it_is_forgiving_about_shape_because_the_worker_supplies_it(self):
        # Same normalisation the sink's own `is_auth` guard already applies.
        assert self._map()("  BILLING ") == "rate_limit"
        assert self._map()("Auth") == "auth"

    @pytest.mark.parametrize("code", [None, "", "timeout", "agent_error", "unknown"])
    def test_everything_else_switches_nothing(self, code):
        """An ALLOWLIST, not a blocklist. A future error code that means
        "the agent crashed" must not move the agent to another subscription —
        that would churn the fleet through every credential it owns on a bug."""
        assert self._map()(code) is None


# ===========================================================================
# Layer B — the hook fires once, on the CAS-won branch only
# ===========================================================================
class TestPullSinkTriggersTheSwitch:
    def _run(self, *, error_code, status="failed", cas_won=True, row_status="running"):
        from services import pull_coordination_service as pcs

        mock_db = MagicMock()
        mock_db.get_execution.return_value = _execution(
            agent_name="worker-a", status=row_status
        )
        mock_db.update_execution_status.return_value = cas_won
        mock_sub = MagicMock()
        with (
            patch.object(pcs, "db", mock_db),
            patch.object(pcs, "event_dispatch_service", MagicMock()),
            patch.object(pcs, "activity_service", MagicMock()),
            patch.object(pcs, "subscription_auto_switch", mock_sub),
        ):
            outcome = pcs.apply_task_result(
                "exec-2643",
                "tok",
                status=status,
                content="the provider said no",
                error_code=error_code,
            )
        return outcome, mock_sub

    def test_a_quota_terminal_records_and_switches(self):
        outcome, sub = self._run(error_code="billing")
        assert outcome.kind == "applied"
        sub.spawn_subscription_failure.assert_called_once()
        args, kwargs = sub.spawn_subscription_failure.call_args
        assert args[0] == "worker-a"
        assert kwargs["failure_kind"] == "rate_limit"

    def test_an_auth_terminal_switches_as_auth(self):
        _, sub = self._run(error_code="auth")
        sub.spawn_subscription_failure.assert_called_once()
        assert sub.spawn_subscription_failure.call_args.kwargs["failure_kind"] == "auth"

    def test_the_error_text_reaches_the_audit_row(self):
        """`handle_subscription_failure` persists `error_message` on the event
        and quotes it in the owner notification — an empty string there makes a
        switch unexplainable after the fact."""
        _, sub = self._run(error_code="billing")
        msg = sub.spawn_subscription_failure.call_args.kwargs["error_message"]
        assert "the provider said no" in msg
        # The typed class survives too, since the row has no error_code column.
        assert "billing" in msg

    def test_a_success_terminal_switches_nothing(self):
        _, sub = self._run(error_code=None, status="success")
        sub.spawn_subscription_failure.assert_not_called()

    def test_a_success_carrying_a_failure_code_switches_nothing(self):
        """A worker can report `status=success` with an `error_code` set — the
        result endpoint takes both fields and does not cross-check them. The
        provider served that turn, so the subscription demonstrably works and
        moving off it would be backwards. (It is also the one branch where the
        sink never binds `err_text`, so an ungated hook raises NameError and
        turns a committed terminal into a 500.)"""
        outcome, sub = self._run(error_code="billing", status="success")
        assert outcome.kind == "applied"
        sub.spawn_subscription_failure.assert_not_called()

    def test_a_cancelled_quota_terminal_still_switches(self):
        """The exclusion above is SUCCESS-only, not "anything not FAILED": a
        worker that labels a quota refusal `cancelled` still refused the turn
        for a quota reason, and the push path decides from the failure class
        rather than the label."""
        _, sub = self._run(error_code="billing", status="cancelled")
        sub.spawn_subscription_failure.assert_called_once()

    def test_an_unrelated_failure_switches_nothing(self):
        _, sub = self._run(error_code="timeout")
        sub.spawn_subscription_failure.assert_not_called()

    def test_a_lost_cas_switches_nothing(self):
        """The #1083 rule: every side effect follows the CAS bool. A late
        terminal that lost the race must not spend a second switch."""
        outcome, sub = self._run(error_code="billing", cas_won=False)
        assert outcome.kind in ("replayed", "conflict")
        sub.spawn_subscription_failure.assert_not_called()

    def test_a_replayed_terminal_switches_nothing(self):
        """An already-authoritative row short-circuits above the write — the
        duplicate-report path, and the one most likely to be retried."""
        outcome, sub = self._run(error_code="billing", row_status="success")
        assert outcome.kind == "replayed"
        sub.spawn_subscription_failure.assert_not_called()


# ===========================================================================
# Layer C — the spawn wrapper: sync caller, async switch, fail-open
# ===========================================================================
class TestSpawnWrapper:
    def test_it_awaits_handle_subscription_failure_with_what_it_was_given(self):
        from services import subscription_auto_switch as sas

        handler = AsyncMock(return_value={"switched": True})

        async def _drive():
            with patch.object(sas, "handle_subscription_failure", handler):
                sas.spawn_subscription_failure(
                    "worker-a", error_message="429", failure_kind="rate_limit"
                )
                # Let the spawned task run to completion.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(_drive())
        handler.assert_awaited_once_with(
            agent_name="worker-a", error_message="429", failure_kind="rate_limit"
        )

    def test_a_raising_switch_never_reaches_the_caller(self):
        """It runs after a committed, billed terminal. A switch that explodes
        must not turn a recorded terminal into a 500 on the result endpoint."""
        from services import subscription_auto_switch as sas

        handler = AsyncMock(side_effect=RuntimeError("provider down"))

        async def _drive():
            with patch.object(sas, "handle_subscription_failure", handler):
                sas.spawn_subscription_failure(
                    "worker-a", error_message="x", failure_kind="auth"
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        asyncio.run(_drive())  # must not raise
        handler.assert_awaited_once()

    def test_no_running_loop_is_skipped_not_raised(self):
        """Called from a sync test or a sync entry point with no loop: skip,
        and close the coroutine so it does not warn 'never awaited'."""
        from services import subscription_auto_switch as sas

        import warnings

        handler = AsyncMock()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with patch.object(sas, "handle_subscription_failure", handler):
                sas.spawn_subscription_failure(
                    "worker-a", error_message="x", failure_kind="auth"
                )
        handler.assert_not_awaited()
        # The skip must be CLEAN: an unclosed coroutine warns "never awaited",
        # which on the real path is a leak nobody sees because this whole hook
        # is dark until a pull pilot runs.
        assert not [w for w in caught if "never awaited" in str(w.message)]

    def test_the_task_is_strongly_referenced_until_it_finishes(self):
        """asyncio holds only a weak reference to a bare `create_task`, so a
        fire-and-forget switch can be garbage-collected mid-flight (the #1083
        `_inflight` footgun)."""
        from services import subscription_auto_switch as sas

        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow(**_kw):
            started.set()
            await release.wait()

        async def _drive():
            with patch.object(sas, "handle_subscription_failure", _slow):
                sas.spawn_subscription_failure(
                    "worker-a", error_message="x", failure_kind="auth"
                )
                await started.wait()
                assert sas._inflight_switch_tasks, "in-flight task dropped"
                release.set()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            assert not sas._inflight_switch_tasks, "done task not discarded"

        asyncio.run(_drive())


# ===========================================================================
# Layer D — a structural pin on the CAS gate
# ===========================================================================
class TestTheCallSiteIsUnderTheCasGate:
    """Layer B proves the behaviour with a mocked db; this proves the SHAPE, so
    a later refactor that hoists the hook above `if won:` fails here even if it
    keeps some test green by accident."""

    def _fn(self):
        src = (_BACKEND / "services" / "pull_coordination_service.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "apply_task_result":
                return node
        pytest.fail("apply_task_result not found")

    def _calls(self, node):
        return [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "spawn_subscription_failure"
        ]

    def test_exactly_one_call_site(self):
        assert len(self._calls(self._fn())) == 1

    def test_it_sits_inside_the_won_branch(self):
        fn = self._fn()
        won_branches = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == "won"
        ]
        assert won_branches, "the `if won:` gate is gone"
        assert any(self._calls(b) for b in won_branches), (
            "spawn_subscription_failure escaped the CAS gate"
        )
