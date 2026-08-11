"""enable/disable/trigger must actually reach their handler (#2094).

The sibling `test_2094_dependency_path_param_pairing.py` proves the *annotation*
is right. This proves the *route* is: it drives the real `schedules.router`
through a `TestClient` and asserts the handler runs.

Both are needed. The static guard generalises to all 146 uses across the routers
but only ever reads source; this one exercises FastAPI's actual dependency
resolution, which is where the failure lived — a 422 raised during request
validation, before any handler code ran. A rule about annotations is a proxy for
"the route works"; this is the thing itself.

The assertion is deliberately `!= 422 with a missing-path-param detail` rather
than `== 200`. The handlers touch the DB and the scheduler, so pinning 200 would
mean mocking the world and would couple this test to schedule-lookup behaviour
it is not about. Reaching the handler at all is exactly what #2081 broke.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# --- self-sufficient env + path (before importing the router) --------------
# config.py raises at import without Redis creds; database.py opens a SQLite DB
# at import; log_archive_service mkdirs LOG_ARCHIVE_PATH. Same shape as
# tests/unit/test_1483_route_order.py, pinned to throwaway values.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2094-routes.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2094-logs")
)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "probe-agent"
CONTROL_ROUTES = ("enable", "disable", "trigger")


def _walk(dependant):
    for sub in dependant.dependencies:
        yield sub
        yield from _walk(sub)


def _resolved_dependency_calls(router):
    """Every callable in every route's dependency graph, as the ROUTE holds it.

    This is the identity that matters: `dependency_overrides` is keyed by object,
    and these are the exact objects FastAPI will look up at request time.
    """
    from fastapi.routing import APIRoute

    calls = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in _walk(route.dependant):
            if callable(dep.call) and hasattr(dep.call, "__qualname__"):
                calls.append(dep.call)
    return calls


@pytest.fixture(scope="module")
def client():
    """The real schedules router, with only the auth gates stubbed.

    The gates are overridden — NOT bypassed. The point is to prove the route
    *binds* its dependency; the owner check itself is `dependencies.py`'s and is
    covered there.

    Overrides are keyed on the function objects the ROUTES actually resolve,
    discovered by walking each route's dependant graph — not on the objects a
    fresh `import dependencies` hands back. `dependencies` is on conftest's #762
    `_SYS_MODULES_INVARIANT_KEYS` list, so its `sys.modules` entry is restored to
    a baseline between tests; a re-import here can therefore yield a *different*
    module object, with different function objects, than `routers.schedules`
    bound at its own import. `dependency_overrides` is an identity-keyed dict, so
    the override then silently does not apply, the real auth chain runs, and
    every request 401s.

    That is not hypothetical: the first version of this fixture keyed on
    `dependencies.get_owned_agent` and passed standalone while failing in the
    full-suite run with `401 {"detail":"Not authenticated"}` — a *test* failure
    that reads exactly like a product auth bug.
    """
    from models import User
    from routers import schedules

    app = FastAPI()
    # No prefix here: `schedules.router` already carries prefix="/api/agents"
    # and main.py mounts it bare. Passing one again double-prefixes, every
    # request 404s on no-route-matched, and these tests pass vacuously —
    # which is exactly what the first draft did.
    app.include_router(schedules.router)

    owner = User(id=1, username="owner", role="admin", email="owner@example.com")

    stubs = {
        "dependencies.get_owned_agent": lambda: AGENT,
        "dependencies.get_authorized_agent": lambda: AGENT,
        "dependencies.get_current_user": lambda: owner,
    }
    for call in _resolved_dependency_calls(schedules.router):
        key = f"{call.__module__}.{call.__qualname__}"
        if key in stubs:
            app.dependency_overrides[call] = stubs[key]

    # A stub that matched nothing would leave the real chain in place and every
    # assertion below would fail confusingly. Fail here instead, where the cause
    # is visible.
    assert len(app.dependency_overrides) >= 2, (
        f"only {len(app.dependency_overrides)} auth dependencies matched by name "
        "— the router no longer resolves the expected gates, or they were renamed"
    )

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _missing_path_param(response) -> list[str]:
    """Path params FastAPI reported as unsatisfiable, if any."""
    if response.status_code != 422:
        return []
    try:
        detail = response.json().get("detail", [])
    except ValueError:
        return []
    return [
        ".".join(str(p) for p in d.get("loc", []))
        for d in detail
        if isinstance(d, dict) and d.get("type") == "missing" and "path" in d.get("loc", [])
    ]


@pytest.mark.parametrize("action", CONTROL_ROUTES)
def test_the_route_reaches_its_handler(client, action):
    """The regression: every caller got 422 before the handler ran."""
    r = client.post(f"/api/agents/{AGENT}/schedules/sched-123/{action}")

    missing = _missing_path_param(r)
    assert not missing, (
        f"POST .../{action} returned 422 for an unsatisfiable path param "
        f"{missing} — the agent dependency reads a path param this route does "
        "not declare, so the handler never runs (#2094 / regression from #2081)"
    )
    assert r.status_code != 422, f"unexpected 422 from .../{action}: {r.text[:300]}"


@pytest.mark.parametrize("action", CONTROL_ROUTES)
def test_the_handler_actually_ran(client, action):
    """Reaching the handler is provable, not inferred from a non-422.

    With a schedule id that does not exist the handlers raise 404 "Schedule not
    found" — a response only reachable from *inside* the handler body. Anything
    else (401/403/422) would mean the request died in the dependency layer.
    """
    r = client.post(f"/api/agents/{AGENT}/schedules/no-such-schedule/{action}")
    assert r.status_code == 404, (
        f".../{action} returned {r.status_code}, expected the handler's own 404; "
        f"body: {r.text[:300]}"
    )
    # The handler's EXACT detail. Starlette's no-route-matched body is
    # {"detail": "Not Found"} — which a loose `"not found" in text.lower()`
    # also satisfies, so that assertion could not distinguish "the handler
    # ran and rejected the id" from "the route does not exist".
    assert r.json().get("detail") == "Schedule not found", (
        f"404 did not come from the handler (body: {r.text[:200]}) — a generic "
        "Not Found means the request never reached it"
    )


def test_the_owner_gate_is_still_wired(client):
    """#2081's intent must survive the fix.

    The three routes were moved to owner-tier deliberately so a shared accessor
    cannot flip owner-intent schedule state. Fixing the 422 by dropping to
    `AuthorizedAgent` would have made the symptom disappear while re-opening the
    hole the security review closed — so assert the OWNER dependency is the one
    these routes resolve.
    """
    from fastapi.routing import APIRoute
    from routers import schedules

    # Compare by qualified NAME, not by function identity. `dependencies` is on
    # conftest's #762 `_SYS_MODULES_INVARIANT_KEYS` list, whose autouse fixture
    # restores that key to a baseline module object between tests — so a fresh
    # `import dependencies` here can hand back a *different* module object than
    # the one `routers.schedules` bound at ITS import, and `is`-comparison fails
    # against a perfectly correct route. (Observed: two `get_owned_agent`
    # objects at different addresses in this very test.) The name is stable
    # across module identity; the object is not.
    for action in CONTROL_ROUTES:
        route = next(
            r for r in schedules.router.routes
            if isinstance(r, APIRoute) and r.path.endswith(f"/schedules/{{schedule_id}}/{action}")
        )
        names = {
            f"{d.call.__module__}.{d.call.__qualname__}"
            for d in _walk(route.dependant)
            if callable(d.call) and hasattr(d.call, "__qualname__")
        }
        assert "dependencies.get_owned_agent" in names, (
            f"{route.path} no longer resolves the OWNER gate (resolves: "
            f"{sorted(n for n in names if 'agent' in n)}) — #2081 moved these "
            "routes to owner-tier so a shared accessor cannot flip schedule state"
        )
        assert "dependencies.get_authorized_agent" not in names, (
            f"{route.path} resolves the accessor-tier gate; that is the tier "
            "#2081 moved away from"
        )
