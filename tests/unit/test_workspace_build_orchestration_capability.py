"""The Workspace roster says whether orchestrators can be designed on the Build page.

The roster is the Workspace's only capability channel (#2128). The bit reads an
instance setting, is true only for a platform user, and fails closed when the
setting is absent or cannot be read.
"""
from __future__ import annotations

import pytest

from unit.test_2128_workspace_rooms_capability import (  # noqa: F401 — fixtures
    _install, _pin_container_state, _seed, entitlements, roster_db,
)

pytestmark = pytest.mark.unit

SETTING = "workspace_build_orchestrators"


def _set(monkeypatch, value):
    from services import settings_service as module
    monkeypatch.setattr(
        module.settings_service, "get_setting",
        lambda key, default=None: value if key == SETTING else default,
    )


@pytest.mark.asyncio
async def test_on_for_a_platform_user_when_the_instance_enables_it(roster_db, entitlements, monkeypatch):
    _set(monkeypatch, "true")
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=True)
    assert roster.orchestration_available is True


@pytest.mark.asyncio
async def test_an_outside_client_never_gets_it(roster_db, entitlements, monkeypatch):
    _set(monkeypatch, "true")
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=False)
    assert roster.orchestration_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "false", "yes", "1"])
async def test_anything_but_true_reads_as_off(roster_db, entitlements, monkeypatch, value):
    _set(monkeypatch, value)
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=True)
    assert roster.orchestration_available is False


@pytest.mark.asyncio
async def test_an_unreadable_setting_never_breaks_the_roster(roster_db, entitlements, monkeypatch):
    from services import settings_service as module

    # Only this key raises: the roster reads other settings it handles itself.
    def boom(key, default=None):
        if key == SETTING:
            raise RuntimeError("database unavailable")
        return default

    monkeypatch.setattr(module.settings_service, "get_setting", boom)
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=True)
    assert roster.orchestration_available is False


def test_the_model_defaults_closed():
    from client_portal.models import PortalRoster
    assert PortalRoster(agents=[]).orchestration_available is False
