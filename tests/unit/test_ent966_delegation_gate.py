"""
The delegation gate never starves the paid-chat facilitator gate (ent#966 C2/§9).

A panel polling eight agents' Nevermined spending at once fills the delegation
gate (default 4) and queues the rest; the paid-chat verify/settle path's
`facilitator_slot()` stays free the whole time. Drives the real
`read_delegation` with a blocking mock Nevermined — the service layer the GET
route hands off to.
"""
import asyncio

import httpx
import pytest

pytestmark = pytest.mark.unit

LIVE = {"status": "active", "remainingBudgetCents": 4200, "spendingLimitCents": 5000}


@pytest.mark.asyncio
async def test_paid_verify_slot_free_under_eight_gets(monkeypatch):
    from services import nevermined_delegation_service as svc
    from services import nevermined_payment_service as pay

    agents = [f"ent966-gate-{i}" for i in range(8)]
    monkeypatch.setattr(svc.db, "get_nevermined_organization",
                        lambda: {"org_id": "org-1", "environment": "sandbox"})
    monkeypatch.setattr(svc.db, "get_nevermined_organization_secrets",
                        lambda: {"widget_secret": "w", "agent_api_key": "sandbox:k"})
    # distinct delegation ids, so the per-id single-flight does not collapse them
    monkeypatch.setattr(svc.db, "get_active_agent_delegation", lambda name: {
        "delegation_id": f"dlg-{name}", "payment_method_id": None, "provider": None,
        "environment": "sandbox", "created_by": "ada", "created_at": "now"})
    monkeypatch.setattr(svc.db, "list_agent_delegation_history", lambda name, limit=10: [])

    release = asyncio.Event()
    in_flight = []

    async def handler(request):
        in_flight.append(request.url.path)
        await release.wait()
        return httpx.Response(200, json=LIVE)

    monkeypatch.setattr(svc, "_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    reads = [asyncio.create_task(svc.read_delegation(a)) for a in agents]
    for _ in range(100):
        if len(in_flight) >= svc.NEVERMINED_DELEGATION_MAX_INFLIGHT:
            break
        await asyncio.sleep(0.01)
    # the delegation gate is full and the rest are queued behind it...
    assert len(in_flight) == svc.NEVERMINED_DELEGATION_MAX_INFLIGHT < len(agents)
    # ...and the paid-chat gate is untouched: every one of its slots is free
    held = []
    for _ in range(pay.NEVERMINED_MAX_INFLIGHT):
        cm = pay.facilitator_slot()
        await asyncio.wait_for(cm.__aenter__(), timeout=0.5)
        held.append(cm)
    for cm in held:
        await cm.__aexit__(None, None, None)

    release.set()
    states = await asyncio.gather(*reads)
    assert len(in_flight) == len(agents)
    assert all(s["live"]["status"] == "active" and s["live"]["remaining_cents"] == 4200 for s in states)
