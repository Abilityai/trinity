"""
The delegation gate never starves the paid-chat facilitator gate (ent#966 C2/§9).

A panel polling eight agents' Nevermined spending at once fills the delegation
gate (default 4) and queues the rest; the paid-chat verify/settle path's
`facilitator_slot()` stays free the whole time. Drives the real
`read_delegation` with a blocking mock Nevermined — the service layer the GET
route hands off to.
"""
import ast
import asyncio
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_MAIN = Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"

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


def test_main_registers_the_delegation_router_once():
    """The composition root is the wiring under test (ent#966 R12, the #3120
    class): `main.py` must import `routers.nevermined_delegation` and pass its
    router to exactly one `app.include_router(...)` call. Importing `main` is
    too heavy for the unit island (~14 s plus Docker side effects), so the pin
    is an AST walk of the file the production process executes — the same
    mechanism `test_3325_credential_error_types.py` uses for its handler."""
    tree = ast.parse(_MAIN.read_text())
    imports = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "routers.nevermined_delegation"
    ]
    assert imports, "main.py no longer imports routers.nevermined_delegation"
    (bound,) = [alias.asname or alias.name for imp in imports for alias in imp.names if alias.name == "router"]
    registrations = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "include_router"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "app"
        and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == bound
    ]
    assert len(registrations) == 1, (
        f"expected exactly one app.include_router({bound}) in main.py, found {len(registrations)}"
    )
