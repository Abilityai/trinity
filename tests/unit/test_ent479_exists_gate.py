"""`GET /api/agent-dashboard/{name}/exists` is gated (trinity-enterprise#479, TD-9).

The probe answers two flags the Dashboard tab needs — `has_dashboard` and
`has_declared_metrics` — and before this issue it ran on a bare
`get_current_user`. That made it a **fleet-wide existence oracle**: any logged
in principal could walk names and learn from the status code alone which agents
exist on the instance, the Invariant #8 / #186 disclosure class.

The fix routes it through `AuthorizedAgentByName`, so an agent that does not
exist and an agent this caller cannot reach are **byte-identical** 404s. That
is the property under test here, and it has to be tested against the REAL
dependency: the sibling route suites override the dependency away in order to
reach the handlers' own gates, which by construction cannot see this.
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = _Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dependencies as deps_mod  # noqa: E402
import routers.agent_dashboard as route_mod  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402

OWNED = "owned-agent"
SOMEONE_ELSES = "someone-elses-agent"
ABSENT = "no-such-agent"

# Who owns what, on the instance this caller is NOT the admin of.
_OWNERS = {OWNED: "operator", SOMEONE_ELSES: "stranger"}


class _Db:
    """Only what the route and the gate read."""

    def __init__(self):
        self.declared = {OWNED: [{"name": "revenue"}]}
        self.cached = set()
        self.raise_on_definitions = None

    # --- read by `get_authorized_agent_by_name` ---------------------------
    def get_agent_owner(self, name):
        return _OWNERS.get(name)

    def can_user_access_agent(self, username, name):
        return _OWNERS.get(name) == username

    # --- read by the handler ---------------------------------------------
    def has_cached_dashboard(self, name):
        return name in self.cached

    def list_metric_definitions(self, name, include_retired=False):
        if self.raise_on_definitions:
            raise self.raise_on_definitions
        return list(self.declared.get(name, []))


@pytest.fixture
def ctx(monkeypatch):
    fake = _Db()
    # The gate reads `dependencies.db`; the handler reads its own module-level
    # `db`. Both are the same object in production and must be here too, or
    # the test proves the gate against a store the handler never sees.
    monkeypatch.setattr(deps_mod, "db", fake)
    monkeypatch.setattr(route_mod, "db", fake)

    app = FastAPI()
    app.include_router(route_mod.router)
    holder = {"user": User(id=1, username="operator", email="op@example.com",
                           role="user")}
    app.dependency_overrides[get_current_user] = lambda: holder["user"]

    class Ctx:
        client = TestClient(app, raise_server_exceptions=False)
        db = fake
        users = holder
    return Ctx()


def _exists(ctx, name):
    return ctx.client.get(f"/api/agent-dashboard/{name}/exists")


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_the_owner_gets_both_flags(ctx):
    """The gate must not cost a legitimate caller anything — including the
    half that made this worth gating: `has_declared_metrics` answers for an
    agent with no `dashboard.yaml` at all."""
    response = _exists(ctx, OWNED)
    assert response.status_code == 200
    assert response.json() == {"has_dashboard": False,
                               "has_declared_metrics": True}


def test_an_absent_and_an_inaccessible_agent_are_INDISTINGUISHABLE(ctx):
    """The #186 property, stated as one assertion because the defect IS the
    difference between the two responses. A 403 here (or a differing body)
    would restore the oracle in a new spelling."""
    absent = _exists(ctx, ABSENT)
    inaccessible = _exists(ctx, SOMEONE_ELSES)

    assert absent.status_code == 404
    assert inaccessible.status_code == 404
    assert absent.json() == inaccessible.json()


def test_the_probe_does_not_leak_through_the_declared_metrics_half(ctx):
    """An agent this caller cannot reach is refused BEFORE the registry is
    read, so the answer cannot vary with whether that agent declares metrics
    — a 404 whose latency or body moved with the store would be the same
    oracle one layer down."""
    ctx.db.declared[SOMEONE_ELSES] = [{"name": "revenue"}, {"name": "leads"}]
    ctx.db.cached.add(SOMEONE_ELSES)
    assert _exists(ctx, SOMEONE_ELSES).json() == _exists(ctx, ABSENT).json()


def test_a_registry_failure_is_not_a_500_for_a_caller_who_may_ask(ctx):
    """The tab gate is not worth a 500: a store blip degrades to "no declared
    metrics" and the `dashboard.yaml` half still answers."""
    ctx.db.cached.add(OWNED)
    ctx.db.raise_on_definitions = RuntimeError("store down")
    response = _exists(ctx, OWNED)
    assert response.status_code == 200
    assert response.json() == {"has_dashboard": True,
                               "has_declared_metrics": False}
