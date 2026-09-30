"""The Workspace roster says whether Projects are available (ent#661).

The roster is the Workspace's only capability channel (#2128): a portal
principal cannot read `/api/settings/feature-flags`. Projects are internal-only
in v1, so the bit is true only when the module is entitled AND the principal is
a platform user (`include_owned`, the Workspace's is_platform convention). An
outside client never learns the capability exists, and an older backend that
omits the field fails closed.
"""
from __future__ import annotations

import pytest

from unit.test_2128_workspace_rooms_capability import (  # noqa: F401 — fixtures
    _install, _pin_container_state, _seed, entitlements, roster_db,
)

pytestmark = pytest.mark.unit

FEATURE = "projects"


@pytest.mark.asyncio
async def test_entitled_platform_user_gets_the_capability(roster_db, entitlements, monkeypatch):
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements, FEATURE)
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=True)
    assert roster.projects_available is True


@pytest.mark.asyncio
async def test_an_outside_client_never_gets_it(roster_db, entitlements, monkeypatch):
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements, FEATURE)
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=False)
    assert roster.projects_available is False


@pytest.mark.asyncio
async def test_an_oss_build_does_not_offer_it(roster_db, entitlements, monkeypatch):
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements)
    email = _seed(roster_db)
    from client_portal import service
    roster = await service.get_roster(email, include_owned=True)
    assert roster.projects_available is False


def test_the_model_defaults_closed():
    from client_portal.models import PortalRoster
    assert PortalRoster(agents=[]).projects_available is False
