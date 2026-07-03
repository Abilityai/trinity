"""
Unit tests for Telegram webhook back-fill on public_chat_url save (#309).

Verifies:
- Setting public_chat_url to a non-empty value re-registers webhooks for
  every existing Telegram binding.
- Setting public_chat_url to an empty value does NOT trigger re-registration.
- A failing register_webhook call for one binding does not break others.
- Back-fill handles a missing db.get_all_telegram_bindings (fresh install)
  without raising.

Modules: src/backend/routers/settings.py
Issue: https://github.com/abilityai/trinity/issues/309
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_project_root = Path(__file__).resolve().parents[2]
_settings_py = _project_root / "src" / "backend" / "routers" / "settings.py"

# Modules this test stubs into sys.modules — must be restored after each test
# so other test files (e.g. test_webhook_signature.py) get clean imports.
_STUBBED_MODULE_NAMES = [
    "models",
    "database",
    "dependencies",
    "services",
    "services.settings_service",
    "services.platform_audit_service",
    "services.agent_service",
    "services.agent_service.capabilities",
    "adapters",
    "adapters.transports",
    "adapters.transports.telegram_webhook",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after.

    Prevents stubbed modules from leaking into other test files in the same
    pytest session.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_settings_module():
    """Load routers/settings.py as a standalone module with stubbed deps.

    Avoids triggering the real routers/__init__.py import chain, which pulls
    in the entire backend. We stub the module-level imports that settings.py
    reaches for, then load the source file directly.
    """
    stubs = {}

    # Stub the sibling backend modules that settings.py imports at top level.
    def _stub(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        stubs[name] = mod
        return mod

    from pydantic import BaseModel

    class _SystemSetting(BaseModel):
        key: str
        value: str = ""

    class _SystemSettingUpdate(BaseModel):
        value: str = ""

    # Use the REAL models module rather than a partial fake: settings.py imports
    # its request/response models from `models` (#654 centralization), so a
    # hand-listed stub would need every moved name and break whenever the import
    # surface grows. `models` is a safe leaf (imports only utils.helpers +
    # db_models) and is already preloaded by tests/conftest.py.
    stubs["models"] = importlib.import_module("models")

    db_stub = MagicMock()
    db_stub.set_setting = MagicMock(return_value=_SystemSetting(key="public_chat_url"))
    db_stub.get_all_telegram_bindings = MagicMock(return_value=[])
    _stub(
        "database",
        db=db_stub,
        SystemSetting=_SystemSetting,
        SystemSettingUpdate=_SystemSettingUpdate,
    )

    _stub("dependencies", get_current_user=MagicMock())

    _stub(
        "services.platform_audit_service",
        platform_audit_service=AsyncMock(),
        AuditEventType=MagicMock(),
    )

    _stub(
        "services.settings_service",
        get_anthropic_api_key=MagicMock(),
        get_github_pat=MagicMock(),
        get_google_api_key=MagicMock(),
        get_ops_setting=MagicMock(),
        settings_service=MagicMock(),
        OPS_SETTINGS_DEFAULTS={},
        OPS_SETTINGS_DESCRIPTIONS={},
        AGENT_QUOTA_DEFAULTS={},
        AGENT_QUOTA_DESCRIPTIONS={},
        AGENT_DEFAULT_CPU_KEY="agent_default_cpu",
        AGENT_DEFAULT_MEMORY_KEY="agent_default_memory",
        AGENT_DEFAULT_CPU="1.0",
        AGENT_DEFAULT_MEMORY="1g",
        AGENT_DEFAULT_REQUIRE_EMAIL_KEY="agent_default_require_email",  # #1129
        AGENT_DEFAULT_REQUIRE_EMAIL=True,
        get_agent_default_require_email=MagicMock(return_value=True),
        MAX_PARALLEL_TASKS_CEILING_KEY="max_parallel_tasks_ceiling",  # #506
        MAX_PARALLEL_TASKS_CEILING_DEFAULT=10,
        MAX_PARALLEL_TASKS_CEILING_MIN=1,
        MAX_PARALLEL_TASKS_CEILING_MAX=32,
        get_max_parallel_tasks_ceiling=MagicMock(return_value=10),
    )
    # settings.py module-level imports VALID_CPU/VALID_MEMORY from the
    # container-spec module (#1197); stub it so the standalone load completes.
    services_mod = _stub("services", __path__=[])
    agent_service_mod = _stub("services.agent_service", __path__=[])
    capabilities_mod = _stub(
        "services.agent_service.capabilities",
        VALID_CPU=["1", "2", "4", "8", "16"],
        VALID_MEMORY=["1g", "2g", "4g", "8g", "16g", "32g"],
    )
    services_mod.agent_service = agent_service_mod
    agent_service_mod.capabilities = capabilities_mod

    # Stub adapters.transports.telegram_webhook before load so the function
    # can import it lazily without hitting the real backend.
    adapters = _stub("adapters", __path__=[])
    transports = _stub("adapters.transports", __path__=[])
    telegram_webhook = _stub(
        "adapters.transports.telegram_webhook",
        register_webhook=AsyncMock(return_value=True),
    )
    adapters.transports = transports
    transports.telegram_webhook = telegram_webhook

    # Keep stubs in sys.modules beyond load: _backfill_telegram_webhooks
    # imports adapters.transports.telegram_webhook lazily at call time.
    sys.modules.update(stubs)
    spec = importlib.util.spec_from_file_location(
        "_trinity_settings_under_test", _settings_py
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, db_stub, telegram_webhook


class TestTelegramBackfill:
    """Back-fill behavior when public_chat_url is saved."""

    @pytest.mark.asyncio
    async def test_backfill_registers_all_bindings(self):
        """Every existing binding gets register_webhook called with the new URL."""
        module, db_stub, telegram_webhook = _load_settings_module()

        bindings = [
            {"agent_name": "agent-a", "webhook_url": None},
            {"agent_name": "agent-b", "webhook_url": None},
            {"agent_name": "agent-c", "webhook_url": "https://stale.example.com/..."},
        ]
        db_stub.get_all_telegram_bindings.return_value = bindings

        register_mock = AsyncMock(return_value=True)
        telegram_webhook.register_webhook = register_mock

        await module._backfill_telegram_webhooks("https://new.example.com")

        assert register_mock.await_count == 3
        called_agents = {call.args[0] for call in register_mock.await_args_list}
        assert called_agents == {"agent-a", "agent-b", "agent-c"}
        for call in register_mock.await_args_list:
            assert call.args[1] == "https://new.example.com"

    @pytest.mark.asyncio
    async def test_backfill_no_bindings_is_noop(self):
        """Empty binding list produces no calls and no errors."""
        module, db_stub, telegram_webhook = _load_settings_module()
        db_stub.get_all_telegram_bindings.return_value = []

        register_mock = AsyncMock(return_value=True)
        telegram_webhook.register_webhook = register_mock

        await module._backfill_telegram_webhooks("https://new.example.com")

        assert register_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_backfill_continues_past_failures(self):
        """One failing binding does not block others from being registered."""
        module, db_stub, telegram_webhook = _load_settings_module()

        bindings = [
            {"agent_name": "agent-a", "webhook_url": None},
            {"agent_name": "agent-bad", "webhook_url": None},
            {"agent_name": "agent-c", "webhook_url": None},
        ]
        db_stub.get_all_telegram_bindings.return_value = bindings

        async def flaky(agent_name, public_url):
            if agent_name == "agent-bad":
                raise RuntimeError("Telegram API unreachable")
            return True

        register_mock = AsyncMock(side_effect=flaky)
        telegram_webhook.register_webhook = register_mock

        # Must not raise
        await module._backfill_telegram_webhooks("https://new.example.com")

        # All three were attempted — the bad one did not short-circuit the loop
        assert register_mock.await_count == 3

    @pytest.mark.asyncio
    async def test_backfill_swallows_db_errors(self):
        """DB failure during back-fill is logged but does not raise.

        The setting write has already succeeded; the back-fill is best-effort.
        """
        module, db_stub, telegram_webhook = _load_settings_module()
        db_stub.get_all_telegram_bindings.side_effect = RuntimeError("db down")

        register_mock = AsyncMock(return_value=True)
        telegram_webhook.register_webhook = register_mock

        # Must not raise
        await module._backfill_telegram_webhooks("https://new.example.com")

        assert register_mock.await_count == 0
