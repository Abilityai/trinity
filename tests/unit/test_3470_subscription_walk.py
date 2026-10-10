"""#3470 — a turn walks EVERY subscription (and the API key) before it fails.

Reported from the Workspace: a turn on a rate-limited subscription came back
*"The agent has reached its usage limit"* while the install had other
subscriptions with headroom. SUB-003 remediated at most once per turn (#792:
one switch + one re-issue; #2638: or one API-key fallback) and several surfaces
handed the second retry back to the person. Part 2: the API-key fallback
CLEARED the assignment and RESTARTED the agent — a key with no credit made a
few-hour limit a permanent outage.

The fix is a credential WALK: each refused attempt asks for the next rung
(another subscription, then the platform key) and the same turn is re-issued
with that credential as a request-scoped `auth_override` the agent applies to
ONE spawn. Nothing moves until a trial serves; the serving subscription is then
committed once. Attribution is exact (the walk knows what it sent), liveness is
the walk's own property (rung cap + "already tried ⇒ exhausted"), and the
budget is the turn's remaining wall clock (#2789) over a 30 s floor.

Harness rules (learnings 2026-08-12): `db` and sibling modules are patched as
ATTRIBUTES, never `sys.modules` stubs — a truthy MagicMock standing in for a
module silently inverts a fail-closed default. The Redis markers go through a
single `_marker_redis` seam, replaced here with an in-memory fake.

Modules under test:
    src/backend/services/subscription_auto_switch.py
    src/backend/services/task_execution_service.py
    src/backend/services/chat_execution_service.py
    src/backend/client_portal/service.py
    docker/base-image/agent_server/services/execution_env.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

HDR = "X-Trinity-Auth-Override"
_NO_EXPECTATION = object()


def _auto_switch():
    # sys.modules truth, like a production `from services.X import f` — see the
    # note on `_auto_switch` in test_2638_subscription_switch_on_turn.py.
    import importlib
    return importlib.import_module("services.subscription_auto_switch")


def _headroom():
    import importlib
    return importlib.import_module("services.subscription_headroom_service")


def _await(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sub(sid, name=None, agents=0):
    return SimpleNamespace(id=sid, name=name or sid.upper(), agent_count=agents)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeRedis:
    """Enough of redis-py for the markers: SET (nx/ex), GET, DELETE."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


def _switch_db(*, current="sub-a", subs=None, viable=None, refused=None,
               last_failed=None, use_platform_key=True):
    """The switcher's db: a MUTABLE assignment (assign updates it), named
    subscriptions by id, and the two candidate lists."""
    subs = subs or [_sub("sub-a"), _sub("sub-b"), _sub("sub-c")]
    by_id = {s.id: s for s in subs}
    state = {"current": current}
    db = MagicMock(name="switch_db")
    db.get_agent_subscription_id.side_effect = lambda name: state["current"]
    def _assign(name, sid, expected_subscription_id=_NO_EXPECTATION):
        # The #3470 commit is a compare-and-set on the assignment this turn
        # started from; a plain assign (the switch performer) is unconditional.
        if expected_subscription_id is not _NO_EXPECTATION and state["current"] != expected_subscription_id:
            return False
        state["current"] = sid
        return True
    db.assign_subscription_to_agent.side_effect = _assign
    db.get_subscription.side_effect = lambda sid: by_id.get(sid)
    db.get_subscription_token.side_effect = lambda sid: f"tok-{sid}"
    db.list_subscriptions.return_value = list(subs)
    db.get_setting_value.return_value = "true"
    db.record_rate_limit_event.return_value = 1
    db.get_use_platform_api_key.return_value = use_platform_key
    # Explicit: the pre-dispatch arm reads this, and an unset MagicMock is
    # truthy — it would evacuate the agent BEFORE the first attempt and reshape
    # every walk below. Tests that want the pre-dispatch arm flip it on.
    db.is_subscription_rate_limited.return_value = False
    db.list_viable_alternative_subscriptions.side_effect = (
        lambda cur: [s for s in (viable or []) if s.id != cur]
    )
    db.list_recently_failed_alternatives.side_effect = (
        lambda cur: [s for s in (refused or []) if s.id != cur]
    )
    db.last_failure_at_by_subscription.side_effect = (
        lambda ids: {i: (last_failed or {}).get(i) for i in ids}
    )
    db._state = state
    return db


@pytest.fixture
def wired(monkeypatch):
    """Real walk, patched collaborators: db, headroom readings (none), the
    switch performer (spy that mutates the assignment), Redis (fake), the
    API-key rung's three environment reads."""
    m, svc = _auto_switch(), _headroom()
    m._reset_locks_for_test()
    redis = FakeRedis()
    monkeypatch.setattr(m, "_marker_redis", lambda: redis)
    monkeypatch.setattr(svc, "cached_headroom_readings",
                        lambda ids, **k: {i: None for i in ids})
    monkeypatch.setattr(svc, "is_auto_refresh_enabled", lambda: True)
    performed = []

    async def _perform(*, agent_name, old_subscription_name, new_subscription,
                       failure_kind, event_count, destination_headroom=None,
                       pre_dispatch=False, old_subscription_id=None):
        performed.append(new_subscription.id)
        m.db.assign_subscription_to_agent(agent_name, new_subscription.id)
        m.db.create_notification(agent_name=agent_name, data=SimpleNamespace(
            title=f"Subscription auto-switched to '{new_subscription.name}'"))
        return {"switched": True, "agent_name": agent_name,
                "old_subscription": old_subscription_name,
                "new_subscription": new_subscription.name,
                "old_subscription_id": old_subscription_id,
                "new_subscription_id": new_subscription.id,
                "restart_result": "hot_reloaded", "pre_dispatch": pre_dispatch}
    monkeypatch.setattr(m, "_perform_auto_switch", _perform)
    # The API-key rung's environment: Claude runtime, a key configured. Patched
    # as ATTRIBUTES of the real modules (`api_key_rung` imports the names at
    # call time), never as `sys.modules` stand-ins.
    import services.docker_service as docker_service
    import services.settings_service as settings_service
    monkeypatch.setattr(docker_service, "get_agent_container", lambda n: None)
    monkeypatch.setattr(settings_service, "get_anthropic_api_key", lambda: "sk-ant-test-key")
    return SimpleNamespace(m=m, redis=redis, performed=performed, monkeypatch=monkeypatch,
                           settings=settings_service)


def _no_key(w):
    w.monkeypatch.setattr(w.settings, "get_anthropic_api_key", lambda: "")


def _wire_db(w, db):
    w.monkeypatch.setattr(w.m, "db", db)
    return db


# =============================================================================
# A — the walk state machine (pure, through the real functions)
# =============================================================================

class TestTheWalkOrdersAndBoundsItself:

    def test_two_of_three_refused_reaches_the_third(self, wired):
        """AC#2/#8(a): A refuses, B refuses, C is picked — each tried once."""
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        r1 = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert r1.kind == "subscription" and r1.subscription_id == "sub-b"
        assert r1.credential == "tok-sub-b" and r1.payload() == {"oauth_token": "tok-sub-b"}
        r2 = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert r2.subscription_id == "sub-c"
        assert walk.tried_subscription_ids == {"sub-a", "sub-b"}
        # Each refusal was recorded against the credential it RAN on.
        recorded = [c.kwargs["subscription_id"] for c in db.record_rate_limit_event.call_args_list]
        assert recorded == ["sub-a", "sub-b"]
        # Nothing was assigned: a trial moves no assignment.
        db.assign_subscription_to_agent.assert_not_called()

    def test_exhausted_pool_ends_the_walk_honestly(self, wired):
        """AC#8(b)/(c): no candidate, no key ⇒ None, `exhausted`, attempts
        never exceed the candidates."""
        _no_key(wired)
        _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")).subscription_id == "sub-b"
        assert _await(m.advance_subscription_walk(walk, failure_kind="auth")) is None
        assert walk.stop_reason == "exhausted"
        assert len(walk.attempts) == 2 <= walk.max_rungs
        assert "Tried: SUB-A (rate limit) -> SUB-B (auth); every candidate refused." in walk.trail_text()
        s = walk.summary()
        assert s["switched"] is False and s["exhausted"] is True and s["retried"] is True

    def test_budget_stop_still_records_the_refusal(self, wired):
        """#471: a refusal the turn cannot act on is still a refusal."""
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit", budget_ok=False)) is None
        assert walk.stop_reason == "budget"
        db.record_rate_limit_event.assert_called_once()
        assert walk.summary()["budget_exhausted"] is True

    def test_an_already_tried_pick_is_exhausted_not_a_loop(self, wired):
        """Liveness is the walk's, not the selector's: a selector that hands
        back a tried id ends the walk."""
        _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        wired.monkeypatch.setattr(
            m, "select_best_alternative_subscription",
            lambda cur, **k: (_sub("sub-a"), {}),  # the origin, already tried
        )
        _no_key(wired)
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None
        assert walk.stop_reason == "exhausted"

    def test_the_rung_cap_is_a_hard_stop(self, wired):
        _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        m._resolve_walk_lazies(walk)
        assert walk.max_rungs == 3 + 2
        walk.attempts = [{"attempt": i} for i in range(walk.max_rungs)]
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None
        assert walk.stop_reason == "cap"

    def test_a_concurrent_switch_is_rerun_not_switched_again(self, wired):
        """AC#7: another turn moved the agent while this attempt ran on the old
        assignment — re-run on the new one; no second switch, no event against
        the destination that never ran."""
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        db._state["current"] = "sub-c"  # a concurrent turn committed C meanwhile
        rung = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert rung.kind == "concurrent_switch" and rung.subscription_id == "sub-c"
        assert rung.payload() is None  # nothing to override — the baseline IS C now
        assert walk.assigned_subscription_id == "sub-c"
        recorded = [c.kwargs["subscription_id"] for c in db.record_rate_limit_event.call_args_list]
        assert recorded == ["sub-a"]

    def test_auto_switch_disabled_records_but_does_not_walk(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        db.get_setting_value.return_value = "false"
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None
        assert walk.stop_reason == "disabled"
        db.record_rate_limit_event.assert_called_once()


class TestTheLastResortRung:
    """AC#2: the 2h skip-list must not block the last untried candidate."""

    def _pick(self, wired, *, interactive, failed_minutes_ago):
        now = datetime.now(timezone.utc)
        _wire_db(wired, _switch_db(
            viable=[], refused=[_sub("sub-b")],
            last_failed={"sub-b": _iso(now - timedelta(minutes=failed_minutes_ago))},
        ))
        m = wired.m
        age = 0 if interactive else m.LAST_RESORT_MIN_AGE_AUTONOMOUS_SECONDS
        return m.select_best_alternative_subscription(
            "sub-a", exclude_ids={"sub-a"}, last_resort_min_age_seconds=age,
        )

    def test_interactive_turns_try_the_skip_listed_candidate(self, wired):
        picked = self._pick(wired, interactive=True, failed_minutes_ago=5)
        assert picked is not None
        sub, why = picked
        assert sub.id == "sub-b" and why["last_resort"] is True

    def test_autonomous_turns_wait_for_the_age_gate(self, wired):
        assert self._pick(wired, interactive=False, failed_minutes_ago=5) is None
        picked = self._pick(wired, interactive=False, failed_minutes_ago=45)
        assert picked is not None and picked[0].id == "sub-b"

    def test_legacy_callers_keep_the_skip_list_absolute(self, wired):
        _wire_db(wired, _switch_db(viable=[], refused=[_sub("sub-b")]))
        assert wired.m.select_best_alternative_subscription("sub-a") is None

    def test_a_fresh_provider_refusal_still_excludes(self, wired):
        """The skip-list is an inference; a fresh reading is live evidence."""
        svc = _headroom()
        refusing = svc.HeadroomReading(
            age_seconds=30, provider_status="ok",
            five_hour=svc.WindowReading(utilization_pct=100.0, blocked=True,
                                        resets_at=_iso(datetime.now(timezone.utc) + timedelta(hours=1))),
            seven_day=None,
        )
        wired.monkeypatch.setattr(svc, "cached_headroom_readings",
                                  lambda ids, **k: {i: refusing for i in ids})
        assert self._pick(wired, interactive=True, failed_minutes_ago=5) is None

    def test_a_blocked_window_already_reset_ranks_unknown_not_refused(self, wired):
        svc = _headroom()
        stale = svc.HeadroomReading(
            age_seconds=30, provider_status="ok",
            five_hour=svc.WindowReading(utilization_pct=100.0, blocked=True,
                                        resets_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=10))),
            seven_day=None,
        )
        wired.monkeypatch.setattr(svc, "cached_headroom_readings",
                                  lambda ids, **k: {i: stale for i in ids})
        picked = self._pick(wired, interactive=True, failed_minutes_ago=5)
        assert picked is not None and picked[0].id == "sub-b"


class TestTheApiKeyRung:
    """Part 2: the key is a rung, not a reassignment."""

    def test_the_key_is_offered_after_the_subscriptions(self, wired):
        _wire_db(wired, _switch_db(viable=[]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        rung = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert rung.kind == "api_key" and rung.payload() == {"api_key": "sk-ant-test-key"}
        assert rung.label == "platform API key"

    def test_a_key_refusal_is_one_more_exhausted_candidate(self, wired):
        """AC Part2 (a): subscription exhausted + key with no credit ⇒ honest
        failure, assignment UNCHANGED, no flag flipped, no restart anywhere."""
        db = _wire_db(wired, _switch_db(viable=[]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")).kind == "api_key"
        assert _await(m.advance_subscription_walk(walk, failure_kind="auth")) is None
        assert walk.stop_reason == "exhausted" and walk.tried_api_key is True
        assert db._state["current"] == "sub-a"
        db.clear_agent_subscription.assert_not_called()
        db.set_use_platform_api_key.assert_not_called()
        assert "platform API key (auth)" in walk.trail_text()
        # ...and the key is now known-refused for 2h.
        assert wired.redis.get(m.API_KEY_REFUSAL_KEY).startswith("auth@")

    def test_a_recently_refused_key_is_not_offered(self, wired):
        """AC Part2 (c)."""
        _wire_db(wired, _switch_db(viable=[]))
        m = wired.m
        m.record_api_key_refusal("auth")
        walk = m.start_subscription_walk("scribe", "public")
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None

    def test_a_429_on_the_key_is_a_short_skip_not_two_hours(self, wired):
        m = wired.m
        calls = []
        wired.redis.set = lambda key, value, nx=False, ex=None: calls.append(ex) or True
        m.record_api_key_refusal("rate_limit")
        m.record_api_key_refusal("auth")
        assert calls == [300, 2 * 3600]

    def test_an_explicit_never_bill_the_key_is_honoured(self, wired):
        _wire_db(wired, _switch_db(viable=[], use_platform_key=False))
        assert wired.m.api_key_rung("scribe") is None

    def test_the_old_fallback_entry_point_changes_nothing(self, wired):
        """`fallback_to_api_key` used to clear + flag + restart. Now it only
        answers whether the rung exists — credential-free."""
        db = _wire_db(wired, _switch_db(viable=[]))
        out = _await(wired.m.fallback_to_api_key("scribe"))
        assert out == {"switched": False, "fallback": "api_key", "agent_name": "scribe", "route": "api_key"}
        db.clear_agent_subscription.assert_not_called()
        db.set_use_platform_api_key.assert_not_called()
        assert "sk-ant" not in json.dumps(out)

    def test_pre_dispatch_routes_the_first_attempt_to_the_key(self, wired):
        """When the assigned subscription is known-refused and nothing else can
        serve, the first attempt goes to the key by override — no doomed
        attempt, no commit."""
        db = _wire_db(wired, _switch_db(viable=[]))
        db.is_subscription_rate_limited.return_value = True
        m = wired.m
        pre = _await(m.ensure_serviceable_subscription("scribe"))
        assert pre == {"switched": False, "route": "api_key", "evidence": "recent_rate_limit"}
        walk = m.start_subscription_walk("scribe", "public")
        m.apply_pre_dispatch_result(walk, pre)
        assert walk.active.kind == "api_key"
        assert db._state["current"] == "sub-a"


class TestCommitAndNotifications:

    def test_a_served_trial_is_committed_once(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert m.note_override_honoured(walk, {HDR: "oauth_token"}) is True
        result = _await(m.commit_subscription_walk(walk, failure_kind="rate_limit"))
        assert result["new_subscription"] == "SUB-B" and wired.performed == ["sub-b"]
        assert db._state["current"] == "sub-b"
        assert walk.summary()["switched"] is True

    def test_a_concurrent_commit_wins(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        db._state["current"] = "sub-c"  # someone else committed C
        _await(m.commit_subscription_walk(walk, failure_kind="rate_limit"))
        assert wired.performed == [] and db._state["current"] == "sub-c"

    def test_key_route_and_return_are_transition_notifications(self, wired):
        """AC Part2 (b)/(d): served on the key ⇒ one 'switched to the key'
        notice (deduped); a later turn served on the subscription ⇒ one
        'returned' notice, and the marker is gone."""
        db = _wire_db(wired, _switch_db(viable=[]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert walk.active.kind == "api_key"
        _await(m.commit_subscription_walk(walk, failure_kind="rate_limit"))
        _await(m.commit_subscription_walk(walk, failure_kind="rate_limit"))  # a second key-served turn
        titles = [c.kwargs["data"].title for c in db.create_notification.call_args_list]
        assert titles == ["Switched to the platform API key"]
        assert db._state["current"] == "sub-a"  # Part 2 AC#3: assignment preserved
        # Next turn: the subscription serves again on the baseline.
        later = m.start_subscription_walk("scribe", "public")
        _await(m.commit_subscription_walk(later, failure_kind="rate_limit"))
        titles = [c.kwargs["data"].title for c in db.create_notification.call_args_list]
        assert titles[-1] == "Returned to subscription 'SUB-A'"
        assert wired.redis.get(m.API_KEY_ROUTE_NOTICE_KEY.format(name="scribe")) is None

    def test_the_exhausted_alert_is_deduped_per_hour(self, wired):
        db = _wire_db(wired, _switch_db(viable=[]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        walk.attempts.append({"attempt": 1, "on": "A", "failure": "rate_limit"})
        walk.stop_reason = "exhausted"
        m.notify_pool_exhausted(walk, None)
        m.notify_pool_exhausted(walk, None)
        assert db.create_notification.call_count == 1
        assert "every candidate refused" in db.create_notification.call_args.kwargs["data"].message


class TestLegacyMode:
    """An agent image that ignores `auth_override` (no header)."""

    def test_the_override_not_honoured_flips_to_legacy_and_clears_attribution(self, wired):
        _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        rung = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert walk.active is rung
        assert m.note_override_honoured(walk, {}) is False
        assert walk.override_supported is False and walk.active is None

    def test_legacy_apply_commits_now_and_a_restart_is_allowed_once(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m

        async def _restarting(**kw):
            db.assign_subscription_to_agent(kw["agent_name"], kw["new_subscription"].id)
            return {"switched": True, "new_subscription": kw["new_subscription"].name,
                    "new_subscription_id": kw["new_subscription"].id, "restart_result": "success"}
        wired.monkeypatch.setattr(m, "_perform_auto_switch", _restarting)
        walk = m.start_subscription_walk("scribe", "public")
        rung = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        walk.override_supported = False
        assert _await(m.apply_rung_legacy(walk, rung, failure_kind="rate_limit")) is True
        assert db._state["current"] == "sub-b" and walk.legacy_restart_used is True
        # The next refusal stops the walk: one restart per turn, and no key rung
        # in legacy mode (the restart-based key path is the stranding bug).
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None
        assert walk.stop_reason == "legacy_restart"


class TestEarliestResetNeverLiesAboutThePast:

    def test_past_instants_are_dropped(self, wired):
        svc, m = _headroom(), wired.m
        now = datetime.now(timezone.utc)
        reading = svc.HeadroomReading(
            age_seconds=30, provider_status="ok",
            five_hour=svc.WindowReading(utilization_pct=100.0, blocked=True, resets_at=_iso(now - timedelta(hours=1))),
            seven_day=svc.WindowReading(utilization_pct=100.0, blocked=True, resets_at=_iso(now + timedelta(days=2))),
        )
        wired.monkeypatch.setattr(svc, "cached_headroom_readings", lambda ids, **k: {"s": reading})
        assert m.earliest_known_reset(["s"]) == _iso(now + timedelta(days=2))


# =============================================================================
# B — the task dispatcher, end to end through execute_task (real walk)
# =============================================================================

def _resp(status, body, *, override=None):
    import httpx
    r = MagicMock()
    r.status_code = status
    r.text = json.dumps(body)
    r.json.return_value = body
    r.headers = {HDR: override} if override else {}
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status}", request=MagicMock(), response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _ok(text="served"):
    return {"success": True, "response": text, "session_id": "s1",
            "metadata": {"cost_usd": 0.01, "context_window": 200000}, "execution_log": []}


def _run_task(wired, *, responses, viable, triggered_by="public", timeout_seconds=300):
    """Drive the REAL execute_task + REAL walk; only the agent and the
    execution engine's own db/capacity/activity are mocked."""
    from services.task_execution_service import TaskExecutionService, TaskExecutionStatus

    db = _wire_db(wired, _switch_db(viable=viable))
    sent_payloads = []

    async def _agent_post(agent_name, endpoint, payload, **kwargs):
        sent_payloads.append(dict(payload))
        return responses.pop(0)

    ex_db = MagicMock(name="exec_db")
    ex_db.get_max_parallel_tasks.return_value = 3
    ex_db.get_execution.return_value = MagicMock(id="exec-3470", status="running")
    ex_db.update_execution_status.return_value = True
    capacity = MagicMock(acquire=AsyncMock(return_value=MagicMock(state="admitted")), release=AsyncMock())
    with (
        patch("services.task_execution_service.db", ex_db),
        patch("services.task_execution_service.get_capacity_manager", return_value=capacity),
        patch("services.task_execution_service.activity_service",
              MagicMock(track_activity=AsyncMock(return_value="act"), complete_activity=AsyncMock())),
        patch("services.task_execution_service.CircuitState",
              return_value=MagicMock(allow_request=lambda: True)),
        patch("services.task_execution_service.agent_post_with_retry", side_effect=_agent_post),
        patch("services.task_execution_service.dispatch_breaker_active", return_value=False),
        patch("services.task_execution_service._record_dispatch_terminal", AsyncMock()),
        patch("services.task_execution_service.platform_audit_service", MagicMock(log=AsyncMock())),
        patch("services.task_execution_service._SWITCH_RETRY_DELAY_S", 0),
    ):
        result = _await(TaskExecutionService().execute_task(
            agent_name="scribe", message="hello", triggered_by=triggered_by,
            execution_id="exec-3470", timeout_seconds=timeout_seconds, model="sonnet",
        ))
    return result, db, ex_db, sent_payloads, TaskExecutionStatus


class TestTheTaskPathWalksThePool:

    def test_two_of_three_rate_limited_the_turn_succeeds_on_the_third(self, wired):
        """AC#8(a): no user-visible failure; the serving subscription is
        committed once; every attempt carried the right credential."""
        result, db, ex_db, sent, Status = _run_task(
            wired,
            responses=[_resp(429, {"detail": "usage limit"}),
                       _resp(429, {"detail": "usage limit"}, override="oauth_token"),
                       _resp(200, _ok(), override="oauth_token")],
            viable=[_sub("sub-b"), _sub("sub-c")],
        )
        assert result.status == Status.SUCCESS and result.response == "served"
        assert [p.get("auth_override") for p in sent] == [
            None, {"oauth_token": "tok-sub-b"}, {"oauth_token": "tok-sub-c"},
        ]
        assert result.subscription_switch["new_subscription"] == "SUB-C"
        assert result.subscription_switch["switched"] is True
        assert [a["on"] for a in result.subscription_switch["attempts"]] == ["SUB-A", "SUB-B"]
        assert wired.performed == ["sub-c"] and db._state["current"] == "sub-c"
        # SUB-004: the row's spend now belongs to the subscription that served.
        ex_db.set_execution_subscription.assert_called_once_with("exec-3470", "sub-c")
        # The retry count carries both re-issues onto the SUCCESS write.
        ok = [c for c in ex_db.update_execution_status.call_args_list
              if c.kwargs.get("status") == Status.SUCCESS]
        assert ok and ok[0].kwargs["result"].retry_count == 2

    def test_all_exhausted_is_exactly_one_failure_with_the_trail(self, wired):
        """AC#8(b): one FAILED row, the trail on it, one operator alert."""
        _no_key(wired)
        result, db, ex_db, sent, Status = _run_task(
            wired,
            responses=[_resp(429, {"detail": "usage limit"}),
                       _resp(429, {"detail": "usage limit"}, override="oauth_token")],
            viable=[_sub("sub-b")],
        )
        assert result.status == Status.FAILED
        assert result.error_code.value == "billing"
        assert "Tried: SUB-A (rate limit) -> SUB-B (rate limit); every candidate refused." in result.error
        assert len(sent) == 2
        assert result.subscription_switch["exhausted"] is True
        assert result.subscription_switch["tried_subscription_ids"] == ["sub-a", "sub-b"]
        failed = [c for c in ex_db.update_execution_status.call_args_list
                  if c.kwargs.get("status") == Status.FAILED]
        assert len(failed) == 1
        titles = [c.kwargs["data"].title for c in db.create_notification.call_args_list]
        assert titles == ["No subscription could serve a turn"]
        assert db._state["current"] == "sub-a"

    def test_the_key_rung_serves_without_moving_the_assignment(self, wired):
        """AC Part2 (b): subscription exhausted + healthy key ⇒ served; the
        assignment is preserved; the row's spend is not a subscription's."""
        result, db, ex_db, sent, Status = _run_task(
            wired,
            responses=[_resp(429, {"detail": "usage limit"}),
                       _resp(200, _ok(), override="api_key")],
            viable=[],
        )
        assert result.status == Status.SUCCESS
        assert sent[1]["auth_override"] == {"api_key": "sk-ant-test-key"}
        assert db._state["current"] == "sub-a" and wired.performed == []
        assert result.subscription_switch["routed_api_key"] is True
        ex_db.set_execution_subscription.assert_called_once_with("exec-3470", None)
        titles = [c.kwargs["data"].title for c in db.create_notification.call_args_list]
        assert titles == ["Switched to the platform API key"]

    def test_an_old_image_gets_the_legacy_switch_and_still_completes(self, wired):
        """No header ⇒ the override attempt ran on the baseline; the rung is
        applied the #792 way and the turn is re-issued once more."""
        result, db, ex_db, sent, Status = _run_task(
            wired,
            responses=[_resp(429, {"detail": "usage limit"}),
                       _resp(429, {"detail": "usage limit"}),   # override ignored
                       _resp(200, _ok())],
            viable=[_sub("sub-b")],
        )
        assert result.status == Status.SUCCESS
        assert len(sent) == 3 and "auth_override" not in sent[2]
        assert wired.performed == ["sub-b"] and db._state["current"] == "sub-b"
        assert result.subscription_switch["new_subscription"] == "SUB-B"

    def test_the_payload_credential_never_reaches_the_row_or_the_audit(self, wired):
        result, db, ex_db, sent, Status = _run_task(
            wired,
            responses=[_resp(429, {"detail": "usage limit"}),
                       _resp(200, _ok(), override="oauth_token")],
            viable=[_sub("sub-b")],
        )
        assert result.status == Status.SUCCESS
        blob = json.dumps([str(c) for c in ex_db.update_execution_status.call_args_list])
        assert "tok-sub-b" not in blob
        assert "tok-sub-b" not in json.dumps(result.subscription_switch)


# =============================================================================
# C — the /chat path re-runs the message
# =============================================================================

class TestTheChatPathReissues:

    def _run_chat(self, wired, responses, *, chat_timeout=300):
        import services.chat_execution_service as ces
        _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        sent = []

        async def _post(name, endpoint, payload, **kw):
            sent.append(dict(payload))
            return responses.pop(0)
        wired.monkeypatch.setattr(ces, "agent_post_with_retry", _post)
        wired.monkeypatch.setattr(ces, "_SWITCH_RETRY_DELAY_S", 0)
        response, walk, kind = _await(ces._walk_chat_dispatch(
            name="scribe", payload={"message": "hi", "stream": False},
            chat_timeout=chat_timeout, task_execution_id="exec-chat", triggered_by="chat",
        ))
        return ces, response, walk, kind, sent

    def test_a_refused_chat_turn_is_rerun_on_the_next_subscription(self, wired):
        ces, response, walk, kind, sent = self._run_chat(wired, [
            _resp(429, {"detail": "usage limit"}),
            _resp(200, _ok(), override="oauth_token"),
        ])
        assert response.status_code == 200 and kind == "rate_limit"
        assert [p.get("auth_override") for p in sent] == [None, {"oauth_token": "tok-sub-b"}]
        _await(ces._commit_chat_walk("scribe", walk, kind, response, "exec-chat"))
        assert wired.performed == ["sub-b"]

    def test_an_exhausted_chat_turn_says_so_once_with_the_reset(self, wired):
        """AC#5: the 'auto-switched … Please retry' hand-back is gone for an
        exhausted pool; the message names the limit and the trail."""
        _no_key(wired)
        ces, response, walk, kind, sent = self._run_chat(wired, [
            _resp(429, {"detail": "usage limit"}),
            _resp(429, {"detail": "usage limit"}, override="oauth_token"),
        ])
        assert response.status_code == 429 and len(sent) == 2
        with pytest.raises(ces.ChatDispatchError) as exc:
            _await(ces._apply_sub003_autoswitch("scribe", "usage limit", 429, walk=walk))
        detail = exc.value.detail
        assert exc.value.status_code == 429
        assert "Please retry" not in detail["message"]
        assert "no other subscription can serve" in detail["message"]
        assert "Tried:" in detail["message"]
        assert detail["auto_switch"]["exhausted"] is True
        assert exc.value.headers[ces.ERROR_CODE_HEADER] == "billing"

    def test_budget_exhausted_keeps_the_last_resort_please_retry(self, wired):
        import services.chat_execution_service as ces
        m = wired.m
        _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        walk = m.start_subscription_walk("scribe", "chat")
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit", budget_ok=False))
        with pytest.raises(ces.ChatDispatchError) as exc:
            _await(ces._apply_sub003_autoswitch("scribe", "usage limit", 429, walk=walk))
        assert exc.value.detail["retry_after"] == 15
        assert "Please retry" in exc.value.detail["message"]

    def test_without_a_walk_the_pre_3470_behaviour_is_untouched(self, wired):
        """test_2889/test_3012 stub the module with exactly two names; the
        `walk=None` arm must keep importing only those."""
        import services.chat_execution_service as ces
        fake = SimpleNamespace(
            handle_subscription_failure=AsyncMock(return_value={"new_subscription": "B"}),
            is_auth_failure=lambda m: False,
        )
        with patch.dict("sys.modules", {"services.subscription_auto_switch": fake}):
            with pytest.raises(ces.ChatDispatchError) as exc:
                _await(ces._apply_sub003_autoswitch("scribe", "rate limited", 429))
        assert "Please retry" in exc.value.detail["message"]


# =============================================================================
# D — the Workspace ladder
# =============================================================================

class TestThePortalLadder:

    def _raise_for(self, switch, *, monkeypatch, reset=None):
        import inspect
        from client_portal import service as portal
        monkeypatch.setattr(portal, "_usage_limit_detail",
                            lambda agent, subscription_ids=None: f"LIMIT ids={sorted(subscription_ids or [])}")
        # Drive the ladder's AUTH/BILLING branch through the real source: the
        # branch is inline in `portal_chat`, so the honest way to test its
        # decisions is to execute that branch's statements against a result.
        src = inspect.getsource(portal)
        i = src.index('if code in ("AUTH", "BILLING"):')
        j = src.index('if code == "CAPACITY"', i)
        import textwrap
        branch = textwrap.dedent(src[i + len('if code in ("AUTH", "BILLING"):'):j])
        ns = {"ClientPortalError": portal.ClientPortalError,
              "_usage_limit_detail": portal._usage_limit_detail,
              "result": SimpleNamespace(subscription_switch=switch), "agent_name": "scribe"}
        with pytest.raises(portal.ClientPortalError) as exc:
            exec(branch, ns)
        return exc.value

    def test_budget_exhausted_is_the_only_send_again(self, monkeypatch):
        e = self._raise_for({"switched": False, "budget_exhausted": True, "retried": False}, monkeypatch=monkeypatch)
        assert e.category == "auth_switched" and e.retryable is True
        assert "ran out of time" in e.detail

    def test_an_exhausted_walk_is_a_usage_limit_over_every_credential_tried(self, monkeypatch):
        e = self._raise_for({"switched": True, "retried": True, "exhausted": True,
                             "new_subscription": "B", "tried_subscription_ids": ["sub-a", "sub-b"]},
                            monkeypatch=monkeypatch)
        assert e.category == "auth" and e.retryable is False
        assert e.detail == "LIMIT ids=['sub-a', 'sub-b']"

    def test_the_declared_categories_still_cover_the_raise_sites(self):
        from client_portal import service as portal
        assert "auth_switched" in portal.PORTAL_FAILURE_CATEGORIES
        assert "auth" in portal.PORTAL_FAILURE_CATEGORIES

    def test_usage_limit_detail_takes_the_earliest_over_all_ids(self, monkeypatch):
        from client_portal import service as portal
        import services.subscription_auto_switch as m
        seen = {}
        monkeypatch.setattr(m, "earliest_known_reset",
                            lambda ids: seen.setdefault("ids", list(ids)) and
                            _iso(datetime.now(timezone.utc) + timedelta(hours=2)))
        import database
        monkeypatch.setattr(database.db, "get_agent_subscription_id", lambda n: "sub-a", raising=False)
        text = portal._usage_limit_detail("scribe", subscription_ids=["sub-b", "sub-c"])
        assert seen["ids"] == ["sub-a", "sub-b", "sub-c"]
        assert "resets at" in text


# =============================================================================
# E — the agent side: one spawn, one credential
# =============================================================================

class TestTheAgentAppliesTheOverrideToOneSpawn:

    def test_env_layers_for_each_kind(self):
        from agent_server.models import AuthOverride
        from agent_server.services import execution_env as env
        extra, drop = env.auth_override_env_layers(AuthOverride(oauth_token="tok"))
        assert extra == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
        assert set(drop) == set(env.SUBSCRIPTION_SHADOW_KEYS)
        extra, drop = env.auth_override_env_layers(AuthOverride(api_key="sk"))
        assert extra == {"ANTHROPIC_API_KEY": "sk"}
        assert "CLAUDE_CODE_OAUTH_TOKEN" in drop
        assert env.auth_override_env_layers(None) == ({}, ())
        # Both or neither ⇒ unusable ⇒ nothing applied.
        assert AuthOverride(oauth_token="a", api_key="b").kind() is None
        assert AuthOverride().kind() is None

    def test_drop_is_the_top_layer(self, tmp_path, monkeypatch):
        from agent_server.services import execution_env as env
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=from-file\n")
        monkeypatch.setattr(env, "INITIAL_ENV", {"ANTHROPIC_API_KEY": "baseline", "PATH": "/bin"})
        monkeypatch.setattr(env, "_RUNTIME_OVERRIDES", {})
        built = env.build_execution_env({"CLAUDE_CODE_OAUTH_TOKEN": "tok"}, env_file=env_file,
                                        drop=("ANTHROPIC_API_KEY",))
        assert built["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
        assert "ANTHROPIC_API_KEY" not in built
        # Without `drop` the file value still wins, as before #3470.
        assert env.build_execution_env(env_file=env_file)["ANTHROPIC_API_KEY"] == "from-file"

    def test_the_header_names_the_applied_kind(self, monkeypatch):
        from fastapi import HTTPException, Response
        from agent_server.models import AuthOverride
        from agent_server.routers import chat as chat_router
        from agent_server.services.execution_env import AUTH_OVERRIDE_HEADER
        from agent_server.utils import credential_sanitizer as cs

        # Never let this test be the first to populate the agent sanitizer's
        # module-global set from the HOST environment — that value set would
        # then redact every later agent-sanitizer test in the session.
        monkeypatch.setattr(cs, "_credential_values", set())
        runtime = SimpleNamespace(supports_auth_override=True)
        req = SimpleNamespace(auth_override=AuthOverride(api_key="sk-ant-test"))
        override = chat_router._accept_auth_override(req, runtime)
        assert override is req.auth_override
        resp = Response()
        chat_router._stamp_auth_override(resp, override)
        assert resp.headers[AUTH_OVERRIDE_HEADER] == "api_key"
        with pytest.raises(HTTPException) as exc:
            chat_router._reraise_with_auth_override(HTTPException(status_code=429, detail="x"), override)
        assert exc.value.headers[AUTH_OVERRIDE_HEADER] == "api_key"
        # A runtime that cannot take a Claude credential drops it silently.
        assert chat_router._accept_auth_override(req, SimpleNamespace(supports_auth_override=False)) is None
        assert chat_router._accept_auth_override(SimpleNamespace(auth_override=None), runtime) is None

    def test_the_override_value_is_staged_for_redaction(self, monkeypatch):
        from agent_server.models import AuthOverride
        from agent_server.routers import chat as chat_router
        from agent_server.utils import credential_sanitizer as cs
        staged = set()
        monkeypatch.setattr(cs, "_credential_values", staged)
        chat_router._accept_auth_override(
            SimpleNamespace(auth_override=AuthOverride(oauth_token="sk-ant-oat01-secret")),
            SimpleNamespace(supports_auth_override=True),
        )
        assert "sk-ant-oat01-secret" in staged


class TestTheCredentialNeverLeaksThroughRepr:

    def test_rung_and_walk_reprs_mask_the_secret(self, wired):
        m = wired.m
        rung = m.Rung(kind="api_key", credential="sk-ant-SECRET-VALUE")
        walk = m.SubscriptionWalk(agent_name="scribe", interactive=True, active=rung)
        assert "SECRET" not in repr(rung) and "SECRET" not in repr(walk)
        assert "SECRET" not in json.dumps(walk.summary() or {})


class TestReviewHardening:
    """Findings of the pre-landing review, pinned."""

    def test_the_facade_delegates_the_new_db_method(self):
        """`DatabaseManager` delegates BY NAME — a mixin method with no
        delegate AttributeErrors at runtime while every mocked test stays green."""
        import inspect
        import database
        assert hasattr(database.DatabaseManager, "set_execution_subscription")
        src = inspect.getsource(database.DatabaseManager.set_execution_subscription)
        assert "_schedule_ops.set_execution_subscription" in src
        src = inspect.getsource(database.DatabaseManager.assign_subscription_to_agent)
        assert "**kwargs" in src  # the CAS keyword reaches the mixin

    def test_a_container_fault_503_never_walks(self):
        """A 503 that is about the container, not the credential, must not
        record an auth refusal against every subscription and the key."""
        from services.execution_classification import classify_switch_failure
        from services.failure_classifier import CONTAINER_FAULT_MARKERS
        for text in ("Claude Code is not available in this container",
                     "Permission bypass failed: permissionMode=bypassPermissions."):
            assert classify_switch_failure(_resp(503, {"detail": text})) is None, text
        assert classify_switch_failure(_resp(503, {"detail": "service unavailable"})) == "auth"
        # Literals pinned against the agent-server producer (the #3012 shape).
        agent_src = "".join(
            (_REPO / "docker/base-image/agent_server/services" / f).read_text().lower()
            for f in ("headless_executor.py", "claude_code.py")
        )
        for marker in CONTAINER_FAULT_MARKERS:
            assert marker in agent_src, marker

    def test_a_key_only_agent_has_no_pool_to_walk(self, wired):
        _wire_db(wired, _switch_db(current=None, viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        assert walk.assigned_subscription_id is None
        assert _await(m.advance_subscription_walk(walk, failure_kind="rate_limit")) is None
        assert walk.stop_reason == "no_subscription" and walk.attempts == []
        assert walk.summary() is None  # the caller keeps its own wording

    def test_an_undecryptable_token_does_not_end_the_subscription_rungs(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        db.get_subscription_token.side_effect = lambda sid: None if sid == "sub-b" else f"tok-{sid}"
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        rung = _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert rung.kind == "subscription" and rung.subscription_id == "sub-c"
        assert "sub-b" in walk.tried_subscription_ids

    def test_a_sibling_workers_commit_wins_the_cas(self, wired):
        """Two workers, two process-local locks: the assignment write is a
        compare-and-set, so only the first commit hot-reloads and notifies."""
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b"), _sub("sub-c")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        real_get = db.get_agent_subscription_id.side_effect
        # The sibling commits C between this worker's read and its write.
        def _read_then_race(name):
            value = real_get(name)
            db._state["current"] = "sub-c"
            return value
        db.get_agent_subscription_id.side_effect = _read_then_race
        _await(m.commit_subscription_walk(walk, failure_kind="rate_limit"))
        assert wired.performed == [] and db._state["current"] == "sub-c"
        db.create_notification.assert_not_called()

    def test_the_hot_path_pays_one_read_until_a_refusal(self, wired):
        db = _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
        m = wired.m
        walk = m.start_subscription_walk("scribe", "public")
        db.get_subscription.assert_not_called()
        db.list_subscriptions.assert_not_called()
        assert walk.max_rungs is None
        _await(m.advance_subscription_walk(walk, failure_kind="rate_limit"))
        assert walk.max_rungs == 5 and walk.assigned_subscription_name == "SUB-A"

    def test_secret_str_hides_the_credential_from_the_request_repr(self):
        from agent_server.models import AuthOverride, ChatRequest
        req = ChatRequest(message="hi", auth_override=AuthOverride(api_key="sk-ant-HIDDEN"))
        assert "HIDDEN" not in repr(req) and "HIDDEN" not in str(req.model_dump())
        assert req.auth_override.secret() == "sk-ant-HIDDEN"
