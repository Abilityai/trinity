"""#2973 (CSO Finding 3) — an hourly cap on event-subscription dispatches from
one source agent into one subscriber agent.

Chain depth counts RUNNING rows and the loopback `/task` is async, so an agent
that emits at the end of every turn to its own subscription reads depth 0 on
every hop. It is keyed on the (source, subscriber) PAIR: an agent's own key
can create subscriptions on itself, so a per-subscription key multiplies, and a
per-subscriber key lets one noisy source starve every other source.

Driven through the real `trigger_subscription` with fakeredis; the HTTP
loopback and the alert are the doubles.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.event_dispatch_service as _EDS  # noqa: E402

pytestmark = pytest.mark.unit

LIMIT = 3
SUB_AGENT = "budget-sub"


class _Http:
    """httpx.AsyncClient double; records every loopback POST."""

    def __init__(self):
        self.posts = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.posts.append(url)
        return SimpleNamespace(status_code=202, text="")


@pytest.fixture
def env(monkeypatch):
    import httpx

    redis = fakeredis.FakeRedis(decode_responses=True)
    http = _Http()
    alert = AsyncMock()
    monkeypatch.setattr(_EDS, "_fire_budget_redis", lambda: redis)
    monkeypatch.setattr(_EDS, "_fire_budget_limit", lambda: LIMIT)
    monkeypatch.setattr(httpx, "AsyncClient", http)
    import services.monitoring_alerts as _MA

    monkeypatch.setattr(
        _MA, "get_alert_service",
        lambda: SimpleNamespace(alert_event_dispatch_budget_exhausted=alert),
    )
    return SimpleNamespace(redis=redis, http=http, alert=alert)


def _sub(sub_id: str, agent: str = SUB_AGENT):
    return SimpleNamespace(id=sub_id, subscriber_agent=agent, target_message="go")


SOURCE = "budget-src"


def _event(source=SOURCE):
    return SimpleNamespace(id="ev1", source_agent=source, event_type="work.done", payload=None)


def _fire(sub, n=1, source=SOURCE):
    for _ in range(n):
        asyncio.run(
            _EDS.trigger_subscription(sub, _event(source), agent_originated=True, chain_depth=1)
        )


def test_2973_dispatches_up_to_the_cap_then_skips(env):
    _fire(_sub("s1"), LIMIT + 2)
    assert len(env.http.posts) == LIMIT


def test_2973_k_subscriptions_between_one_pair_share_one_budget(env):
    """K self-subscriptions must not buy K times the budget."""
    for i in range(LIMIT + 1):
        _fire(_sub(f"s{i}", agent=SOURCE), source=SOURCE)
    assert len(env.http.posts) == LIMIT


def test_2973_one_noisy_source_does_not_starve_the_others(env):
    _fire(_sub("s1"), LIMIT + 2, source="noisy")
    assert len(env.http.posts) == LIMIT
    _fire(_sub("s2"), source="quiet")
    assert len(env.http.posts) == LIMIT + 1
    _fire(_sub("s3", agent="budget-other"), source="noisy")
    assert len(env.http.posts) == LIMIT + 2


def test_2973_the_alert_fires_once_per_window(env):
    _fire(_sub("s1"), LIMIT + 3)
    env.alert.assert_awaited_once_with(SUB_AGENT, SOURCE, LIMIT)


def test_2973_a_cap_lowered_mid_window_still_alerts_once(env, monkeypatch):
    _fire(_sub("s1"), LIMIT)
    monkeypatch.setattr(_EDS, "_fire_budget_limit", lambda: 1)  # count is already past 1 + 1
    _fire(_sub("s1"), 3)
    env.alert.assert_awaited_once_with(SUB_AGENT, SOURCE, 1)


def test_2973_the_counter_always_carries_a_ttl(env):
    _fire(_sub("s1"))
    ttl = env.redis.ttl(f"{_EDS._FIRE_KEY_PREFIX}{SOURCE}:{SUB_AGENT}")
    assert 0 < ttl <= _EDS._FIRE_WINDOW_SECONDS


def test_2973_a_later_fire_does_not_extend_the_window(env):
    key = f"{_EDS._FIRE_KEY_PREFIX}{SOURCE}:{SUB_AGENT}"
    _fire(_sub("s1"))
    env.redis.expire(key, 10)
    _fire(_sub("s1"))
    assert env.redis.ttl(key) <= 10


def test_2973_redis_unavailable_fails_open(env, monkeypatch):
    monkeypatch.setattr(_EDS, "_fire_budget_redis", lambda: None)
    _fire(_sub("s1"), LIMIT + 2)
    assert len(env.http.posts) == LIMIT + 2


def test_2973_redis_error_fails_open(env, monkeypatch):
    broken = MagicMock()
    broken.pipeline.side_effect = ConnectionError("redis down")
    monkeypatch.setattr(_EDS, "_fire_budget_redis", lambda: broken)
    _fire(_sub("s1"), LIMIT + 2)
    assert len(env.http.posts) == LIMIT + 2


def test_2973_a_failing_alert_does_not_undo_the_skip(env):
    env.alert.side_effect = RuntimeError("notifications down")
    _fire(_sub("s1"), LIMIT + 1)
    assert len(env.http.posts) == LIMIT


def test_2973_the_limit_is_the_ops_setting_clamped(monkeypatch):
    # Resolve the instance the way the code does at call time (sys.modules),
    # so a sibling test that swapped the module cannot detach the patch.
    import services.settings_service  # noqa: F401 — ensure it is loaded

    svc = sys.modules["services.settings_service"].settings_service
    monkeypatch.setattr(svc, "get_ops_setting", lambda k, t: 50_000)
    assert _EDS._fire_budget_limit() == 10_000
    monkeypatch.setattr(svc, "get_ops_setting", MagicMock(side_effect=ValueError("junk")))
    assert _EDS._fire_budget_limit() == 120
