"""The Workspace roster says whether the viewer's agent is still being set up
(trinity-enterprise#1201).

The roster is the Workspace's only capability channel (#2128). Whether a
person's agent is still being prepared is known only to a module, so the
roster asks the `services.portal_capabilities` seam once per load, keyed on the
caller's own address. With no provider registered (an OSS build) the bit is
False, and a provider that raises is False too (fail closed).
"""
from __future__ import annotations

import pytest

from unit.test_2128_workspace_rooms_capability import (  # noqa: F401 — fixtures
    _install, _pin_container_state, _seed, entitlements, roster_db,
)

pytestmark = pytest.mark.unit

CAPABILITY = "agent_setup_pending"


@pytest.fixture()
def seam():
    from services import portal_capabilities
    portal_capabilities.clear_providers()
    yield portal_capabilities
    portal_capabilities.clear_providers()


@pytest.mark.asyncio
async def test_the_roster_carries_the_providers_answer_for_the_caller(roster_db, entitlements, seam):
    email = _seed(roster_db)
    asked = []

    def provider(e):
        asked.append(e)
        return e == email
    seam.register_provider(CAPABILITY, provider)
    from client_portal import service

    assert (await service.get_roster(email, include_owned=True)).agent_setup_pending is True
    assert (await service.get_roster(email, include_owned=False)).agent_setup_pending is True
    assert (await service.get_roster("other@example.com", include_owned=False)
            ).agent_setup_pending is False
    assert asked == [email, email, "other@example.com"], "asked once per load, for the caller only"


@pytest.mark.asyncio
async def test_an_oss_build_with_no_provider_says_false(roster_db, entitlements, seam):
    email = _seed(roster_db)
    from client_portal import service
    assert (await service.get_roster(email, include_owned=True)).agent_setup_pending is False


@pytest.mark.asyncio
async def test_a_provider_that_raises_says_false(roster_db, entitlements, seam):
    email = _seed(roster_db)

    def boom(e):
        raise RuntimeError("unreadable")
    seam.register_provider(CAPABILITY, boom)
    from client_portal import service
    assert (await service.get_roster(email, include_owned=True)).agent_setup_pending is False


def test_the_model_defaults_false():
    from client_portal.models import PortalRoster
    assert PortalRoster(agents=[]).agent_setup_pending is False
