"""Unit tests for the #1609 configurable proactive-message rate-limit getter.

Covers `settings_service.get_proactive_rate_limit`: shipped-default fallback,
parse/garbage/negative fallback, the `0 = unlimited` pass-through, the upper
clamp, and fail-open on a settings-read error — all isolated from the DB by
monkeypatching `get_setting`. (`src/backend` on sys.path via
tests/unit/conftest.py.)
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def ss():
    from services import settings_service as _ss
    return _ss


def _patch_raw(monkeypatch, ss, value):
    monkeypatch.setattr(ss.settings_service, "get_setting", lambda *_a, **_kw: value)


class TestProactiveRateLimitGetter:
    def test_shipped_defaults(self, ss, monkeypatch):
        # Absent value → the pre-#1609 hardcoded default per key.
        _patch_raw(monkeypatch, ss, None)
        assert ss.get_proactive_rate_limit("slack_proactive_per_channel") == 10
        assert ss.get_proactive_rate_limit("slack_proactive_per_agent") == 100
        assert ss.get_proactive_rate_limit("telegram_proactive_per_group") == 10
        assert ss.get_proactive_rate_limit("telegram_proactive_per_agent") == 100
        assert ss.get_proactive_rate_limit("proactive_dm_per_recipient") == 10

    def test_configured_value(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "42")
        assert ss.get_proactive_rate_limit("slack_proactive_per_channel") == 42

    def test_zero_is_unlimited_passthrough(self, ss, monkeypatch):
        # 0 must survive (callers treat it as "skip the limiter"), not fall back.
        _patch_raw(monkeypatch, ss, "0")
        assert ss.get_proactive_rate_limit("slack_proactive_per_agent") == 0

    def test_garbage_falls_back_to_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "not-a-number")
        assert ss.get_proactive_rate_limit("telegram_proactive_per_group") == 10

    def test_negative_falls_back_to_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "-5")
        assert ss.get_proactive_rate_limit("proactive_dm_per_recipient") == 10

    def test_over_max_is_clamped(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, str(ss.PROACTIVE_RATE_LIMIT_MAX + 999))
        assert ss.get_proactive_rate_limit("slack_proactive_per_channel") == ss.PROACTIVE_RATE_LIMIT_MAX

    def test_fail_open_on_read_error(self, ss, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("settings DB down")
        monkeypatch.setattr(ss.settings_service, "get_setting", _boom)
        # A read failure must degrade to the shipped default, never raise.
        assert ss.get_proactive_rate_limit("slack_proactive_per_agent") == 100

    def test_unknown_key_defaults_to_zero(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, None)
        assert ss.get_proactive_rate_limit("nonexistent_key") == 0

    def test_defaults_map_matches_shipped_constants(self, ss):
        # Guards the "zero behavior change on upgrade" AC: the registry defaults
        # are exactly the pre-#1609 hardcoded values.
        assert ss.PROACTIVE_RATE_LIMIT_DEFAULTS == {
            "slack_proactive_per_channel": 10,
            "slack_proactive_per_agent": 100,
            "telegram_proactive_per_group": 10,
            "telegram_proactive_per_agent": 100,
            "proactive_dm_per_recipient": 10,
        }


class _FakeRedis:
    """Minimal redis stub — a per-key integer counter for the DM limiter."""
    def __init__(self, counts=None):
        self._counts = dict(counts or {})
    def get(self, key):
        v = self._counts.get(key)
        return None if v is None else str(v)


class TestProactiveDmEnforcement:
    """The proactive-DM per-recipient cap now sources its limit from settings
    (#1609) and treats 0 as unlimited. Exercises the real `_check_rate_limit`."""

    @pytest.fixture
    def svc(self):
        from services.proactive_message_service import ProactiveMessageService
        return ProactiveMessageService()

    def _set_limit(self, monkeypatch, value):
        # Patch the name as bound in the CONSUMER module (proactive_message_service
        # imports get_proactive_rate_limit at module level), which is robust to
        # sys.modules identity quirks in the full suite.
        import services.proactive_message_service as pms
        monkeypatch.setattr(pms, "get_proactive_rate_limit", lambda *_a, **_kw: value)

    def test_zero_limit_is_unlimited_and_skips_redis(self, svc, monkeypatch):
        self._set_limit(monkeypatch, 0)
        # Redis must not even be consulted when the cap is disabled.
        def _boom():
            raise AssertionError("redis should not be touched when unlimited")
        monkeypatch.setattr(svc, "_get_redis", _boom)
        assert svc._check_rate_limit("atlas", "bob@example.com") is True

    def test_under_configured_limit_allows(self, svc, monkeypatch):
        self._set_limit(monkeypatch, 10)
        key = svc._rate_limit_key("atlas", "bob@example.com")
        monkeypatch.setattr(svc, "_get_redis", lambda: _FakeRedis({key: 9}))
        assert svc._check_rate_limit("atlas", "bob@example.com") is True

    def test_at_configured_limit_blocks(self, svc, monkeypatch):
        self._set_limit(monkeypatch, 10)
        key = svc._rate_limit_key("atlas", "bob@example.com")
        monkeypatch.setattr(svc, "_get_redis", lambda: _FakeRedis({key: 10}))
        assert svc._check_rate_limit("atlas", "bob@example.com") is False

    def test_limit_is_sourced_from_settings_not_the_constant(self, svc, monkeypatch):
        # Configured cap of 3: 2 allows, 3 blocks — proving the enforce reads the
        # setting, not the hardcoded RATE_LIMIT_MAX_PER_HOUR (10).
        self._set_limit(monkeypatch, 3)
        key = svc._rate_limit_key("atlas", "bob@example.com")
        monkeypatch.setattr(svc, "_get_redis", lambda: _FakeRedis({key: 2}))
        assert svc._check_rate_limit("atlas", "bob@example.com") is True
        monkeypatch.setattr(svc, "_get_redis", lambda: _FakeRedis({key: 3}))
        assert svc._check_rate_limit("atlas", "bob@example.com") is False

    def test_redis_down_fails_open(self, svc, monkeypatch):
        self._set_limit(monkeypatch, 10)
        monkeypatch.setattr(svc, "_get_redis", lambda: None)
        assert svc._check_rate_limit("atlas", "bob@example.com") is True
