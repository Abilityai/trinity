"""
Unit tests for platform default model resolution (#831).

Self-contained: settings_service.get_platform_default_model() — fallback, DB
value, TTL cache — with no live backend. Split out of the original mixed file so
the per-PR unit job (tests/unit) collects it (#1895). The live feature-flags /
settings-API half stays in tests/test_platform_default_model.py.
"""


class TestGetPlatformDefaultModelUnit:
    """Tests for settings_service.get_platform_default_model() in isolation."""

    def test_returns_fallback_when_no_db_row(self, monkeypatch):
        """When system_settings has no platform_default_model row, return hardcoded default."""
        import sys
        import types

        # Provide a minimal stub for the `database` module so we can import
        # settings_service without a running database.
        if "database" not in sys.modules:
            db_stub = types.ModuleType("database")
            db_stub.db = types.SimpleNamespace(
                get_setting_value=lambda key, default=None: default
            )
            sys.modules["database"] = db_stub

        # Clear the module cache so our monkeypatched db takes effect.
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        result = svc.get_platform_default_model()
        assert result == "claude-sonnet-4-6"

    def test_returns_db_value_when_set(self, monkeypatch):
        """When system_settings has a platform_default_model row, return that value."""
        import sys
        import types

        db_stub = types.ModuleType("database")
        db_stub.db = types.SimpleNamespace(
            get_setting_value=lambda key, default=None: (
                "claude-opus-4-7" if key == "platform_default_model" else default
            )
        )
        sys.modules["database"] = db_stub
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        result = svc.get_platform_default_model()
        assert result == "claude-opus-4-7"

    def test_ttl_cache_returns_cached_value(self, monkeypatch):
        """TTL cache returns the cached value within 60s without a new DB read."""
        import sys
        import types

        call_count = [0]

        def counting_get(key, default=None):
            if key == "platform_default_model":
                call_count[0] += 1
            return "claude-sonnet-4-6" if key == "platform_default_model" else default

        db_stub = types.ModuleType("database")
        db_stub.db = types.SimpleNamespace(get_setting_value=counting_get)
        sys.modules["database"] = db_stub
        sys.modules.pop("services.settings_service", None)

        import importlib
        import services.settings_service as svc_module
        importlib.reload(svc_module)

        svc = svc_module.SettingsService()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        svc.get_platform_default_model()
        # Only one DB read due to TTL cache
        assert call_count[0] == 1
