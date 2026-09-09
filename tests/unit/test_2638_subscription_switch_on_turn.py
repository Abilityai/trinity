"""#2638 — a turn on a rate-limited subscription completes on another one.

Reported from the Workspace: sending a message to an agent whose Claude
subscription was rate-limited failed outright, the message was lost to a FAILED
execution, and the client was told the failure was not retryable. SUB-003 has
switched on the first 429 since #441 and re-issues the turn once since #792, so
the machinery existed — these are the four gaps that left the turn failing
anyway.

  1. NO ALTERNATIVE ⇒ HARD FAIL. The candidate filter drops every subscription
     with ANY failure event in a flat 2h window, so on a two-subscription
     install one stale event means "no viable alternative" while an alternative
     the provider would happily serve sits there. Fixed by READMITTING a
     skip-listed candidate on positive evidence only — `recovery_verdict`.

  2. The skip-list ignored the reset the sampler already caches. A 5-hour window
     that rolled over 20 minutes ago is still excluded for the full two hours.

  3. Reactive only: the first message after a wall always burned a failed
     attempt. `ensure_serviceable_subscription` moves the agent BEFORE dispatch
     when the assigned subscription is already known-refused.

  4. After a switch the client was still told "not retryable", while the agent
     sat on a fresh subscription. The switch is now carried on the result and
     the portal says so.

Pure functions are tested pure; the wiring is tested through the real modules
with `db` and the headroom service monkeypatched as ATTRIBUTES (the #2409/ent#434
harness shape — never a `sys.modules` stub, which is how a truthy MagicMock
silently inverts a fail-closed default).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# A frozen instant for the PURE tests, which inject it explicitly.
NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _real_now() -> datetime:
    """The wall clock, for the wiring tests. `recovery_verdict` takes `now` as
    an injectable only so the pure tests can be deterministic; the production
    callers do not pass it, so a test that drives them through the real
    selector has to speak in real time."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _svc():
    import services.subscription_headroom_service as headroom
    return headroom


def _auto_switch():
    import services.subscription_auto_switch as auto_switch
    return auto_switch


def _sub(sid, *, agents=0, name=None):
    return SimpleNamespace(id=sid, name=name or sid, agent_count=agents)


def _reading(*, five=None, seven=None, status="ok", age=30):
    """A `HeadroomReading` built directly — these tests are about the POLICY
    over a reading, not about the gate that produces one (#2409 owns that)."""
    svc = _svc()
    mk = lambda w: None if w is None else svc.WindowReading(**w)
    return svc.HeadroomReading(
        age_seconds=age, provider_status=status,
        five_hour=mk(five), seven_day=mk(seven),
    )


# =============================================================================
# A — `recovery_verdict`: when may the 2h skip-list be overridden?
# =============================================================================

class TestRecoveryVerdict:
    """The whole of gap 1+2, as a pure function.

    The skip-list is the ONLY guard against #444's ping-pong, so it is
    overridden on POSITIVE evidence and never on the absence of it.
    """

    def test_no_evidence_at_all_readmits_nothing(self):
        assert _svc().recovery_verdict(None, None, _iso(NOW), now=NOW) is None

    def test_a_fresh_serving_reading_readmits(self):
        """Ground truth about NOW beats an inference from a past failure —
        the #447 rule, applied to candidate selection."""
        svc = _svc()
        fresh = _reading(seven={"utilization_pct": 40.0, "blocked": False, "resets_at": None})
        assert svc.recovery_verdict(fresh, fresh, _iso(NOW), now=NOW) == svc.RECOVERY_SERVING_NOW

    def test_a_fresh_refusing_reading_readmits_nothing(self):
        """And it must not fall through to the weaker instant arm — the
        strongest evidence available says no."""
        svc = _svc()
        fresh = _reading(
            five={"utilization_pct": 100.0, "blocked": True,
                  "resets_at": _iso(NOW - timedelta(hours=1))},
        )
        assert svc.recovery_verdict(fresh, fresh, _iso(NOW - timedelta(hours=2)), now=NOW) is None

    def test_an_elapsed_reset_after_the_failure_readmits(self):
        """Gap 2: the 5h window rolled over after the failure, so the quota the
        failure was about no longer exists."""
        svc = _svc()
        aged = _reading(
            five={"utilization_pct": 100.0, "blocked": True,
                  "resets_at": _iso(NOW - timedelta(minutes=20))},
            age=9000,
        )
        failed_at = _iso(NOW - timedelta(minutes=90))
        assert svc.recovery_verdict(None, aged, failed_at, now=NOW) == svc.RECOVERY_WINDOW_RESET

    def test_a_reset_that_has_not_elapsed_readmits_nothing(self):
        svc = _svc()
        aged = _reading(
            five={"utilization_pct": 100.0, "blocked": True,
                  "resets_at": _iso(NOW + timedelta(minutes=20))},
            age=9000,
        )
        assert svc.recovery_verdict(None, aged, _iso(NOW - timedelta(hours=1)), now=NOW) is None

    def test_a_failure_AFTER_the_reset_readmits_nothing(self):
        """The load-bearing ordering. Without it a subscription that 429'd one
        minute after its window rolled over — i.e. one that is exhausted
        AGAIN — would be readmitted on a reset it had already consumed."""
        svc = _svc()
        reset = NOW - timedelta(minutes=40)
        aged = _reading(
            five={"utilization_pct": 100.0, "blocked": True, "resets_at": _iso(reset)},
            age=9000,
        )
        failed_after = _iso(reset + timedelta(minutes=1))
        assert svc.recovery_verdict(None, aged, failed_after, now=NOW) is None

    def test_an_unblocked_window_is_not_an_instant_to_reason_about(self):
        svc = _svc()
        aged = _reading(
            five={"utilization_pct": 12.0, "blocked": False,
                  "resets_at": _iso(NOW - timedelta(hours=3))},
            age=9000,
        )
        assert svc.recovery_verdict(None, aged, _iso(NOW - timedelta(hours=4)), now=NOW) is None

    @pytest.mark.parametrize("bad", ["", "not-a-time", "2026-13-45T99:99:99Z", None])
    def test_unreadable_instants_fail_closed(self, bad):
        """Anything we cannot read leaves the skip-list standing — this decides
        whether to override a safety guard, so 'we could not tell' is 'no'."""
        svc = _svc()
        aged = _reading(
            five={"utilization_pct": 100.0, "blocked": True, "resets_at": bad}, age=9000,
        )
        assert svc.recovery_verdict(None, aged, _iso(NOW - timedelta(hours=4)), now=NOW) is None
        assert svc.recovery_verdict(None, aged, bad, now=NOW) is None


# =============================================================================
# B — the selector readmits, and only with evidence
# =============================================================================

@pytest.fixture
def selector(monkeypatch):
    auto_switch = _auto_switch()
    svc = _svc()
    db = MagicMock(name="db")
    db.list_viable_alternative_subscriptions.return_value = []
    db.list_recently_failed_alternatives.return_value = []
    db.last_failure_at_by_subscription.return_value = {}
    monkeypatch.setattr(auto_switch, "db", db)
    monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: True)
    return auto_switch, svc, db


class TestReadmission:

    def test_a_recovered_candidate_rescues_a_turn_that_had_no_alternative(self, selector, monkeypatch):
        """The reported bug, at the selector: zero survivors, one skip-listed
        subscription the provider is serving. Before #2638 this returned None
        and the user's message failed."""
        auto_switch, svc, db = selector
        db.list_recently_failed_alternatives.return_value = [_sub("b")]
        monkeypatch.setattr(
            svc, "cached_headroom_readings",
            lambda ids, **k: {i: _reading(seven={"utilization_pct": 30.0, "blocked": False,
                                                 "resets_at": None}) for i in ids},
        )
        picked = auto_switch.select_best_alternative_subscription("a")
        assert picked is not None
        sub, why = picked
        assert sub.id == "b"
        assert why["readmitted"] == svc.RECOVERY_SERVING_NOW

    def test_no_evidence_leaves_the_skip_list_standing(self, selector, monkeypatch):
        auto_switch, svc, db = selector
        db.list_recently_failed_alternatives.return_value = [_sub("b")]
        monkeypatch.setattr(svc, "cached_headroom_readings", lambda ids, **k: {i: None for i in ids})
        assert auto_switch.select_best_alternative_subscription("a") is None

    def test_a_never_failed_survivor_is_not_marked_readmitted(self, selector, monkeypatch):
        auto_switch, svc, db = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b")]
        monkeypatch.setattr(svc, "cached_headroom_readings", lambda ids, **k: {i: None for i in ids})
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b"
        assert why["readmitted"] is None

    def test_the_fail_open_path_never_readmits(self, selector, monkeypatch):
        """The ranking half is where the evidence lives. When it cannot be read
        at all, readmission has nothing to stand on — the pick degrades to the
        pre-#2409 load-balance order over SURVIVORS ONLY."""
        auto_switch, svc, db = selector
        db.list_viable_alternative_subscriptions.return_value = [_sub("b")]
        db.list_recently_failed_alternatives.return_value = [_sub("c")]

        def _boom(*a, **k):
            raise RuntimeError("redis is gone")
        monkeypatch.setattr(svc, "cached_headroom_readings", _boom)
        sub, why = auto_switch.select_best_alternative_subscription("a")
        assert sub.id == "b"
        assert why["tier"] == auto_switch.SELECTION_UNRANKED

    def test_the_fail_open_path_with_no_survivors_is_none(self, selector, monkeypatch):
        """And it must not index into an empty list — the pre-#2638 code
        returned early on `not survivors` before the try block existed."""
        auto_switch, svc, db = selector
        db.list_recently_failed_alternatives.return_value = [_sub("c")]

        def _boom(*a, **k):
            raise RuntimeError("redis is gone")
        monkeypatch.setattr(svc, "cached_headroom_readings", _boom)
        assert auto_switch.select_best_alternative_subscription("a") is None


# =============================================================================
# C — the pre-dispatch switch (gap 3)
# =============================================================================

class TestPreDispatchSwitch:

    @pytest.fixture
    def wired(self, monkeypatch):
        auto_switch = _auto_switch()
        db = MagicMock(name="db")
        db.get_setting_value.return_value = "true"
        db.get_agent_subscription_id.return_value = "sub-a"
        db.get_subscription.return_value = _sub("sub-a", name="A")
        db.is_subscription_rate_limited.return_value = False
        monkeypatch.setattr(auto_switch, "db", db)
        perform = AsyncMock(return_value={"switched": True, "new_subscription": "B"})
        monkeypatch.setattr(auto_switch, "_perform_auto_switch", perform)
        return auto_switch, db, perform

    @pytest.mark.asyncio
    async def test_no_evidence_dispatches_unchanged(self, wired, monkeypatch):
        auto_switch, db, perform = wired
        monkeypatch.setattr(
            auto_switch, "select_best_alternative_subscription",
            lambda sid: (_sub("sub-b", name="B"), {}),
        )
        assert await auto_switch.ensure_serviceable_subscription("scribe") is None
        perform.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_recent_429_switches_before_the_first_attempt(self, wired, monkeypatch):
        auto_switch, db, perform = wired
        db.is_subscription_rate_limited.return_value = True
        monkeypatch.setattr(
            auto_switch, "select_best_alternative_subscription",
            lambda sid: (_sub("sub-b", name="B"), {"tier": "measured"}),
        )
        result = await auto_switch.ensure_serviceable_subscription("scribe")
        assert result and result["switched"] is True
        assert result["evidence"] == "recent_rate_limit"
        # AC#6: the SAME switch the reactive path performs, so the activity,
        # the notification and the hot-reload all still happen.
        perform.assert_awaited_once()
        assert perform.await_args.kwargs["pre_dispatch"] is True

    @pytest.mark.asyncio
    async def test_it_records_no_failure_event(self, wired, monkeypatch):
        """Nothing failed — that is the point. A synthetic event would poison
        the very skip-list that decides where the agent may move next."""
        auto_switch, db, perform = wired
        db.is_subscription_rate_limited.return_value = True
        monkeypatch.setattr(
            auto_switch, "select_best_alternative_subscription",
            lambda sid: (_sub("sub-b", name="B"), {}),
        )
        await auto_switch.ensure_serviceable_subscription("scribe")
        db.record_rate_limit_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_alternative_dispatches_anyway(self, wired, monkeypatch):
        """Refusing to dispatch would turn a probably-failing turn into a
        certainly-failing one, and the provider's answer is better evidence
        than ours."""
        auto_switch, db, perform = wired
        db.is_subscription_rate_limited.return_value = True
        monkeypatch.setattr(
            auto_switch, "select_best_alternative_subscription", lambda sid: None
        )
        assert await auto_switch.ensure_serviceable_subscription("scribe") is None
        perform.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auto_switch_disabled_is_honoured(self, wired, monkeypatch):
        auto_switch, db, perform = wired
        db.get_setting_value.return_value = "false"
        db.is_subscription_rate_limited.return_value = True
        assert await auto_switch.ensure_serviceable_subscription("scribe") is None
        perform.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_never_raises(self, wired, monkeypatch):
        """A pre-flight optimisation must not be able to fail a turn that
        would otherwise have run."""
        auto_switch, db, perform = wired
        db.get_agent_subscription_id.side_effect = RuntimeError("db is down")
        assert await auto_switch.ensure_serviceable_subscription("scribe") is None


# =============================================================================
# D — the API-key fallback and the reset the client is told about (gap 1/4)
# =============================================================================

class TestEarliestKnownReset:

    def test_it_picks_the_soonest_blocked_window(self, monkeypatch):
        auto_switch, svc = _auto_switch(), _svc()
        soon, late = NOW + timedelta(hours=1), NOW + timedelta(days=3)
        monkeypatch.setattr(
            svc, "cached_headroom_readings",
            lambda ids, **k: {
                "s1": _reading(
                    five={"utilization_pct": 100.0, "blocked": True, "resets_at": _iso(late)},
                    seven={"utilization_pct": 100.0, "blocked": True, "resets_at": _iso(soon)},
                ),
            },
        )
        assert auto_switch.earliest_known_reset(["s1"]) == _iso(soon)

    def test_an_unblocked_window_contributes_no_instant(self, monkeypatch):
        auto_switch, svc = _auto_switch(), _svc()
        monkeypatch.setattr(
            svc, "cached_headroom_readings",
            lambda ids, **k: {"s1": _reading(
                five={"utilization_pct": 4.0, "blocked": False, "resets_at": _iso(NOW)},
            )},
        )
        assert auto_switch.earliest_known_reset(["s1"]) is None

    def test_nothing_known_says_nothing(self, monkeypatch):
        auto_switch, svc = _auto_switch(), _svc()
        monkeypatch.setattr(svc, "cached_headroom_readings", lambda ids, **k: {"s1": None})
        assert auto_switch.earliest_known_reset(["s1"]) is None


class TestApiKeyFallback:

    @pytest.fixture
    def wired(self, monkeypatch):
        auto_switch = _auto_switch()
        db = MagicMock(name="db")
        db.get_setting_value.return_value = "true"
        db.get_agent_subscription_id.return_value = "sub-a"
        db.get_subscription.return_value = _sub("sub-a", name="A")
        monkeypatch.setattr(auto_switch, "db", db)
        monkeypatch.setattr(auto_switch, "_restart_agent", AsyncMock(return_value="success"))
        return auto_switch, db

    @pytest.mark.asyncio
    async def test_disabled_setting_does_nothing(self, wired, monkeypatch):
        auto_switch, db = wired
        db.get_setting_value.return_value = "false"
        assert await auto_switch.fallback_to_api_key("scribe") is None
        db.clear_agent_subscription.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_never_raises(self, wired, monkeypatch):
        auto_switch, db = wired
        db.get_setting_value.side_effect = RuntimeError("boom")
        assert await auto_switch.fallback_to_api_key("scribe") is None

    def test_the_setting_defaults_on_and_fails_open(self, monkeypatch):
        """A read error must not silently disable the remedy — the failure this
        guards is a user's turn dying with a usable key sitting in settings."""
        auto_switch = _auto_switch()
        db = MagicMock(name="db")
        db.get_setting_value.side_effect = RuntimeError("settings unreadable")
        monkeypatch.setattr(auto_switch, "db", db)
        assert auto_switch.is_api_key_fallback_enabled() is True


# =============================================================================
# E — the result carries the switch, and the portal reads it (gap 4)
# =============================================================================

class TestTheResultCarriesTheSwitch:

    def test_the_envelope_has_the_field(self):
        from services.execution_envelope import TaskExecutionResult
        r = TaskExecutionResult(execution_id="e", status="failed", response="")
        assert r.subscription_switch is None

    def test_stamping_is_a_no_op_without_a_switch(self):
        from services.execution_envelope import TaskExecutionResult
        from services.task_execution_service import _with_switch, _AttemptState
        state = _AttemptState(start_time=NOW)
        r = _with_switch(TaskExecutionResult(execution_id="e", status="failed", response=""), state)
        assert r.subscription_switch is None

    def test_stamping_carries_the_switch(self):
        from services.execution_envelope import TaskExecutionResult
        from services.task_execution_service import _with_switch, _AttemptState
        state = _AttemptState(start_time=NOW)
        state.subscription_switch = {"switched": True, "new_subscription": "B"}
        r = _with_switch(TaskExecutionResult(execution_id="e", status="failed", response=""), state)
        assert r.subscription_switch["new_subscription"] == "B"


class TestThePortalReportsTheSwitch:
    """Gap 4 stated where the client sees it.

    #2320 mapped AUTH/BILLING to `retryable=False` on the reasoning that
    "re-sending re-fails". That reasoning holds only while nothing changed
    underneath — and a switch is exactly something changing underneath.
    """

    def _outcome(self, switch):
        import inspect
        from client_portal import service as portal
        src = inspect.getsource(portal)
        return src

    def test_the_auth_branch_consults_the_switch_before_refusing(self):
        import inspect
        from client_portal import service as portal
        src = inspect.getsource(portal)
        i = src.index('if code in ("AUTH", "BILLING"):')
        branch = src[i:i + 2000]
        assert "subscription_switch" in branch, (
            "the AUTH branch must read the switch before declaring the turn "
            "not retryable"
        )
        assert 'category="auth_switched", retryable=True' in branch

    def test_the_unswitched_branch_names_the_reset(self):
        import inspect
        from client_portal import service as portal
        src = inspect.getsource(portal._usage_limit_detail)
        assert "earliest_known_reset" in src
        assert "resets at" in src

    def test_the_reset_lookup_degrades_to_the_original_sentence(self, monkeypatch):
        """It runs on the path where things are ALREADY going wrong, so a nicer
        message is never worth a 500."""
        from client_portal import service as portal
        import database
        monkeypatch.setattr(
            database.db, "get_agent_subscription_id",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        assert "Please try again later." in portal._usage_limit_detail("scribe")


# =============================================================================
# F — AC#1 end to end: the turn COMPLETES on the other subscription
# =============================================================================

class TestTheTurnCompletesOnAnotherSubscription:
    """AC#1, driven through the real `execute_task` and the real switcher.

    Two subscriptions, the assigned one refuses with a 429, and the turn has to
    come back SUCCESS with the switch recorded — not FAILED. The #792 harness
    shape (`tests/unit/test_792_subscription_retry.py`), but with the REAL
    `handle_subscription_failure` in the loop so the notification and the
    activity this feature promises (AC#6) are asserted rather than mocked away.

    Not an integration test against a live instance: a real 429 cannot be
    provoked on demand from a provider, so the seam that is actually under test
    — refusal in, completed turn plus switch out — is exercised here where it
    can be deterministic.
    """

    def _run_turn(self, monkeypatch, *, viable, refused=None, readings=None):
        import asyncio
        import json
        from unittest.mock import patch

        auto_switch = _auto_switch()
        svc = _svc()

        def _resp(status, body):
            import httpx
            r = MagicMock()
            r.status_code = status
            r.text = json.dumps(body)
            r.json.return_value = body
            if status >= 400:
                r.raise_for_status.side_effect = httpx.HTTPStatusError(
                    f"HTTP {status}", request=MagicMock(), response=r,
                )
            else:
                r.raise_for_status.return_value = None
            return r

        responses = [
            _resp(429, {"detail": "rate_limit_error: usage limit reached"}),
            _resp(200, {
                "success": True, "response": "here you go", "session_id": "s1",
                "metadata": {"cost_usd": 0.01, "context_window": 200000},
                "execution_log": [],
            }),
        ]

        # --- the switcher's own world -------------------------------------
        sw_db = MagicMock(name="switch_db")
        sw_db.get_agent_subscription_id.return_value = "sub-a"
        sw_db.record_rate_limit_event.return_value = 1
        sw_db.get_setting_value.return_value = "true"
        sw_db.get_subscription.return_value = _sub("sub-a", name="A")
        sw_db.is_subscription_rate_limited.return_value = False
        sw_db.list_viable_alternative_subscriptions.return_value = viable
        sw_db.list_recently_failed_alternatives.return_value = refused or []
        # Real-clock-relative, deliberately: `_readmit_recovered` calls
        # `recovery_verdict` without injecting `now` (production has no clock to
        # inject), so instants pinned to this file's frozen NOW would sit in the
        # future or the distant past depending on when the suite runs — and the
        # readmission would silently never fire.
        sw_db.last_failure_at_by_subscription.return_value = {
            s.id: _iso(_real_now() - timedelta(minutes=90)) for s in (refused or [])
        }
        monkeypatch.setattr(auto_switch, "db", sw_db)
        monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token",
                            AsyncMock(return_value="success"))
        monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: True)
        # Model the AGE GATE, not just the lookup: `cached_headroom_readings`
        # returns None for a snapshot older than the bound it was asked for,
        # and that distinction IS the `window_reset` path — a stale reading
        # keeps its instants and loses its verdict. A fake that ignores
        # `max_age_seconds` hands the selector a "fresh refusal" and the
        # readmission it is meant to exercise never happens.
        def _fake_readings(ids, *, max_age_seconds=None, **_k):
            limit = max_age_seconds if max_age_seconds is not None else svc.MAX_READING_AGE_SECONDS
            out = {}
            for i in ids:
                r = (readings or {}).get(i)
                out[i] = r if (r is not None and r.age_seconds <= limit) else None
            return out
        monkeypatch.setattr(svc, "cached_headroom_readings", _fake_readings)

        activity = MagicMock(track_activity=AsyncMock(return_value="act-1"),
                             complete_activity=AsyncMock())
        import services.activity_service as activity_module
        monkeypatch.setattr(activity_module, "activity_service", activity)

        # --- the execution engine's world ---------------------------------
        from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

        ex_db = MagicMock(name="exec_db")
        ex_db.get_max_parallel_tasks.return_value = 3
        ex_db.get_execution.return_value = MagicMock(id="exec-2638", status="running")
        ex_db.update_execution_status.return_value = True

        capacity = MagicMock()
        admitted = MagicMock()
        admitted.state = "admitted"
        capacity.acquire = AsyncMock(return_value=admitted)
        capacity.release = AsyncMock()
        circuit = MagicMock()
        circuit.allow_request.return_value = True

        async def _agent_post(agent_name, endpoint, payload, **kwargs):
            return responses.pop(0)

        with (
            patch("services.task_execution_service.db", ex_db),
            patch("services.task_execution_service.get_capacity_manager", return_value=capacity),
            patch("services.task_execution_service.activity_service",
                  MagicMock(track_activity=AsyncMock(return_value="act-1"),
                            complete_activity=AsyncMock())),
            patch("services.task_execution_service.CircuitState", return_value=circuit),
            patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
            patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
            patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
            patch("services.task_execution_service.platform_audit_service",
                  MagicMock(log=AsyncMock())),
            patch("services.task_execution_service._SWITCH_RETRY_DELAY_S", 0),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(TaskExecutionService().execute_task(
                    agent_name="scribe", message="hello", triggered_by="public",
                    execution_id="exec-2638", timeout_seconds=300, model="sonnet",
                ))
            finally:
                loop.close()
        return result, sw_db, TaskExecutionStatus

    def test_a_429_completes_on_a_never_failed_alternative(self, monkeypatch):
        result, sw_db, Status = self._run_turn(
            monkeypatch, viable=[_sub("sub-b", name="B")],
        )
        assert result.status == Status.SUCCESS
        assert result.response == "here you go"
        # AC#5: the caller can see WHICH subscription it ended up on.
        assert result.subscription_switch["new_subscription"] == "B"
        # AC#6: the notification the Settings cards and pressure badges read.
        sw_db.create_notification.assert_called_once()
        assert "B" in sw_db.create_notification.call_args.kwargs["data"].title

    def test_a_429_completes_on_a_READMITTED_alternative(self, monkeypatch):
        """The reported bug end to end: the only other subscription is inside
        the 2h skip-list, so before #2638 the switcher found nothing and the
        message failed. Its window has since reset, and the turn completes."""
        recovered = _reading(
            five={"utilization_pct": 100.0, "blocked": True,
                  "resets_at": _iso(_real_now() - timedelta(minutes=30))},
            # Older than the SELECTION bound and younger than the INSTANT bound:
            # exactly the state the `window_reset` arm exists for — the figure
            # has decayed, the reset instant has not.
            age=9000,
        )
        result, sw_db, Status = self._run_turn(
            monkeypatch,
            viable=[],
            refused=[_sub("sub-b", name="B")],
            readings={"sub-b": recovered},
        )
        assert result.status == Status.SUCCESS
        assert result.subscription_switch["new_subscription"] == "B"

    def test_with_nothing_to_switch_to_the_turn_still_fails(self, monkeypatch):
        """The honest negative. #2638 does not claim to invent capacity — with
        no alternative and no evidence, the turn fails as it did before, and
        the portal's job is to say WHEN it can be retried."""
        result, sw_db, Status = self._run_turn(monkeypatch, viable=[], refused=[])
        assert result.status == Status.FAILED
        assert result.subscription_switch is None
