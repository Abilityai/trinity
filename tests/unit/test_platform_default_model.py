"""
Unit tests for platform default model resolution (#831).

Self-contained: settings_service.get_platform_default_model() — fallback, DB
value, TTL cache — with no live backend. Split out of the original mixed file so
the per-PR unit job (tests/unit) collects it (#1895). The live feature-flags /
settings-API half stays in tests/test_platform_default_model.py.

#1895: these tests patch `services.settings_service.db` and the module-level TTL
cache IN PLACE (monkeypatch, auto-restored) — they do NOT pop + `importlib.reload`
the module. A reload replaces the module-level `settings_service` singleton and
the module-level helper functions with fresh objects; every later unit test that
captured the ORIGINAL (e.g. an instance of ProactiveMessageService, or a
`from services.settings_service import clamp_to_ceiling` consumer) then diverges
from the sys.modules entry a sibling test patches, and that sibling's patch
silently no-ops (test_1609 / test_1081 in the full randomized suite). Patching the
already-imported module in place keeps its identity stable — island-safe.
"""

import types

import services.settings_service as ss


class TestGetPlatformDefaultModelUnit:
    """Tests for settings_service.get_platform_default_model() in isolation."""

    def test_returns_fallback_when_no_db_row(self, monkeypatch):
        """No platform_default_model row → the hardcoded default."""
        fake_db = types.SimpleNamespace(
            get_setting_value=lambda key, default=None: default
        )
        monkeypatch.setattr(ss, "db", fake_db)
        monkeypatch.setattr(ss, "_platform_model_cache", None)
        monkeypatch.setattr(ss, "_platform_model_cache_ts", 0.0)

        assert ss.SettingsService().get_platform_default_model() == "claude-sonnet-4-6"

    def test_returns_db_value_when_set(self, monkeypatch):
        """A platform_default_model row → that value."""
        fake_db = types.SimpleNamespace(
            get_setting_value=lambda key, default=None: (
                "claude-opus-4-7" if key == "platform_default_model" else default
            )
        )
        monkeypatch.setattr(ss, "db", fake_db)
        monkeypatch.setattr(ss, "_platform_model_cache", None)
        monkeypatch.setattr(ss, "_platform_model_cache_ts", 0.0)

        assert ss.SettingsService().get_platform_default_model() == "claude-opus-4-7"

    def test_ttl_cache_returns_cached_value(self, monkeypatch):
        """TTL cache returns the cached value within 60s without a new DB read."""
        call_count = [0]

        def counting_get(key, default=None):
            if key == "platform_default_model":
                call_count[0] += 1
            return "claude-sonnet-4-6" if key == "platform_default_model" else default

        fake_db = types.SimpleNamespace(get_setting_value=counting_get)
        monkeypatch.setattr(ss, "db", fake_db)
        monkeypatch.setattr(ss, "_platform_model_cache", None)
        monkeypatch.setattr(ss, "_platform_model_cache_ts", 0.0)

        svc = ss.SettingsService()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        # Only one DB read due to TTL cache.
        assert call_count[0] == 1


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
