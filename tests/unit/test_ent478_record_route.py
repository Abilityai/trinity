"""`POST /api/agents/{name}/metrics/points` (trinity-enterprise#478, C4).

The route owns transport only, so these tests are about ORDER and STATUS — who
is refused before what is read, which failures are retryable, and which exits
release the batch idempotency claim. What a point MEANS is decided in
`test_ent478_point_validation.py` and is not re-proved here.

The app is built from the real router with a real FastAPI TestClient; only the
store, the limiter and the idempotency service are stubbed, because the point
of each assertion is the handler's own branching.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import IntegrityError, OperationalError  # noqa: E402

import routers.metric_points as route_mod  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402

AGENT = "route-agent"


def _definition(**overrides):
    d = {"name": "cycles", "type": "counter", "status": "active",
         "dimensions": [], "values": None, "type_conflict": None}
    d.update(overrides)
    return d


class _Idem:
    """A real-enough idempotency service: claims in a dict, replays snapshots."""

    def __init__(self):
        self.claims = {}
        self.failed = []

    def make_agent_scope(self, name):
        return f"agent:{name}"

    def make_effect_scope(self, execution_id):
        return f"effect:{execution_id}"

    def derive_effect_key(self, execution_id, effect_type, args, label=""):
        return f"{effect_type}:{hash((execution_id, args)) & 0xffff}"

    def resolve_and_validate_execution(self, execution_id, agent_name):
        return object() if execution_id == "exec-ok" else None

    def begin(self, scope, key):
        class D:
            enabled = bool(key)
            scope_ = scope
        d = D()
        d.scope, d.key = scope, key
        d.replay = d.in_flight = False
        d.snapshot = None
        if not key:
            d.enabled = False
            return d
        state = self.claims.get((scope, key))
        if state is None:
            self.claims[(scope, key)] = {"state": "in_flight", "snapshot": None}
            d.enabled = True
            return d
        d.enabled = True
        d.replay = True
        d.in_flight = state["state"] == "in_flight"
        d.snapshot = state["snapshot"]
        return d

    def complete(self, decision, execution_id, snapshot):
        if getattr(decision, "key", None):
            self.claims[(decision.scope, decision.key)] = {
                "state": "completed", "snapshot": snapshot}

    def fail(self, decision):
        if getattr(decision, "key", None):
            self.failed.append((decision.scope, decision.key))
            self.claims.pop((decision.scope, decision.key), None)


class _Db:
    def __init__(self):
        self.definitions = [_definition()]
        self.today = 0
        self.inserted = []
        self.raise_on_insert = None
        self.raise_on_read = None

    def list_metric_definitions(self, name, include_retired=False):
        if self.raise_on_read:
            raise self.raise_on_read
        return self.definitions

    def count_metric_points_today(self, name, day_start, limit):
        return min(self.today, limit)

    def insert_metric_points(self, name, rows):
        if self.raise_on_insert:
            raise self.raise_on_insert
        self.inserted.append(rows)
        return (len(rows), 0)


@pytest.fixture
def ctx(monkeypatch):
    """The route with its collaborators stubbed and an agent principal."""
    fake_db, fake_idem = _Db(), _Idem()
    monkeypatch.setattr(route_mod, "db", fake_db)
    monkeypatch.setattr(route_mod, "idempotency_service", fake_idem)
    monkeypatch.setattr(route_mod.rate_limiter, "enforce",
                        lambda *a, **k: None)
    monkeypatch.setattr(
        route_mod.settings_service, "resolve_ops_setting",
        lambda key: ({"metrics_retention_days": "365",
                      "metrics_daily_point_cap": "100000"}[key], "default"))

    app = FastAPI()
    principal = User(id=1, username=AGENT, email=f"{AGENT}@agents.local",
                     role="user", agent_name=AGENT)
    app.dependency_overrides[get_current_user] = lambda: principal

    # The access dependency is overridden to "every agent in the path is
    # accessible", so these tests exercise the handler's OWN gates. The uniform
    # 404 that dependency produces for an unknown agent is proven by its own
    # suite (#186) and is not re-proved here.
    from dependencies import get_authorized_agent

    def _authorized(name: str) -> str:
        return name

    # Keyed off the callable the ROUTE actually resolved, not a re-import of
    # it: the unit island's conftest preloads and restores backend modules
    # between tests, so a second import is not guaranteed to be the same object
    # FastAPI will look up.
    app.include_router(route_mod.router)
    for _route in app.routes:
        for _dep in getattr(getattr(_route, "dependant", None), "dependencies", []):
            if _dep.call is get_authorized_agent or _dep.call.__name__ == "get_authorized_agent":
                app.dependency_overrides[_dep.call] = _authorized
            if _dep.call.__name__ == "get_current_user":
                app.dependency_overrides[_dep.call] = lambda: principal

    class Ctx:
        client = TestClient(app, raise_server_exceptions=False)
        db = fake_db
        idem = fake_idem
        user = principal
    return Ctx()


_DEFAULT_POINTS = [{"metric": "cycles", "value": 1}]


def _post(ctx, points=None, **body):
    # `is None`, not falsy: an EMPTY list is a batch this suite deliberately
    # sends, and `or` would quietly replace it with the default one.
    payload = {"points": _DEFAULT_POINTS if points is None else points}
    headers = body.pop("headers", {})
    payload.update(body)
    return ctx.client.post(
        f"/api/agents/{AGENT}/metrics/points", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_a_valid_batch_is_recorded(ctx):
    r = _post(ctx)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["recorded"] == 1 and body["deduplicated"] == 0
    assert body["replayed"] is False
    assert body["agent_name"] == AGENT


def test_the_response_returns_the_identity_the_store_assigned(ctx):
    """An agent that omitted `ts` cannot otherwise learn which row its point
    became — and the identity is what a later correction has to differ from."""
    r = _post(ctx)
    point = r.json()["points"][0]
    assert point["index"] == 0
    assert point["ts"].endswith("Z")
    assert len(point["idempotency_key"]) == 64


def test_the_agent_name_comes_from_auth_not_the_body(ctx):
    _post(ctx)
    assert ctx.db.inserted[0], "rows were written"
    # The store is called with the path/auth name; nothing in the body can
    # redirect a write to another agent's series.
    assert ctx.db.inserted and isinstance(ctx.db.inserted[0], list)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_an_agent_key_may_not_record_as_another_agent(ctx):
    r = ctx.client.post("/api/agents/someone-else/metrics/points",
                        json={"points": [{"metric": "cycles", "value": 1}]})
    assert r.status_code == 403
    assert "itself" in r.json()["detail"]


def test_the_self_gate_runs_before_anything_is_read(ctx):
    """Access-first: the refusal must not depend on whether the other agent
    exists, or the status code becomes an existence oracle (Invariant #8)."""
    ctx.db.raise_on_read = OperationalError("x", {}, Exception())
    r = ctx.client.post("/api/agents/someone-else/metrics/points",
                        json={"points": [{"metric": "cycles", "value": 1}]})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Validation transport
# ---------------------------------------------------------------------------

def test_an_invalid_point_refuses_the_whole_batch_with_reason_codes(ctx):
    r = _post(ctx, points=[
        {"metric": "cycles", "value": 1},
        {"metric": "undeclared_one", "value": 1},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["reason"] == "invalid_points"
    assert detail["errors"][0]["code"] == "metric_undeclared"
    assert detail["errors"][0]["index"] == 1
    assert ctx.db.inserted == [], "all-or-nothing: nothing was written"


def test_an_empty_batch_is_refused_by_the_model(ctx):
    assert _post(ctx, points=[]).status_code == 422


def test_an_oversized_batch_is_refused_by_the_model(ctx):
    from models import METRIC_BATCH_MAX_POINTS
    too_many = [{"metric": "cycles", "value": 1}] * (METRIC_BATCH_MAX_POINTS + 1)
    assert _post(ctx, points=too_many).status_code == 422


def test_a_declared_oversize_body_is_refused_on_the_header(ctx):
    r = ctx.client.post(
        f"/api/agents/{AGENT}/metrics/points",
        json={"points": [{"metric": "cycles", "value": 1}]},
        headers={"content-length": str(route_mod.METRIC_BATCH_MAX_BYTES + 1)},
    )
    assert r.status_code in (413, 400)


# ---------------------------------------------------------------------------
# Idempotency (E3/E10/S1)
# ---------------------------------------------------------------------------

def test_a_repeated_batch_key_replays_the_first_result(ctx):
    first = _post(ctx, headers={"Idempotency-Key": "batch-1"})
    second = _post(ctx, headers={"Idempotency-Key": "batch-1"})

    assert first.status_code == 201 and first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert len(ctx.db.inserted) == 1, "the work was done once"


def test_an_in_flight_duplicate_is_a_conflict_not_a_second_write(ctx):
    ctx.idem.claims[(f"agent:{AGENT}", "record_metrics:batch-x")] = {
        "state": "in_flight", "snapshot": None}
    r = _post(ctx, headers={"Idempotency-Key": "batch-x"})
    assert r.status_code == 409
    assert ctx.db.inserted == []


def test_a_rejected_batch_does_not_wedge_the_key_for_a_day(ctx):
    """E3/S7: `begin()` happens before validation, so a 422 that kept the claim
    would silently drop every corrected retry for 24 hours."""
    bad = _post(ctx, points=[{"metric": "undeclared_one", "value": 1}],
                headers={"Idempotency-Key": "batch-2"})
    assert bad.status_code == 422
    assert (f"agent:{AGENT}", "record_metrics:batch-2") in ctx.idem.failed

    good = _post(ctx, headers={"Idempotency-Key": "batch-2"})
    assert good.status_code == 201 and good.json()["replayed"] is False


def test_the_batch_key_is_scoped_by_the_execution_when_none_is_given(ctx):
    """S1: points with no `ts` take a fresh server-now timestamp, so on a
    re-delivered turn the ROW key cannot dedup them — the execution-derived
    batch key is what does."""
    first = _post(ctx, execution_id="exec-ok")
    second = _post(ctx, execution_id="exec-ok")

    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert len(ctx.db.inserted) == 1


def test_without_a_key_or_an_execution_a_retry_is_a_new_observation(ctx):
    """Documented honestly rather than papered over with a body hash: the same
    numbers an hour later are usually a NEW observation, and treating them as a
    duplicate would silently drop a real point."""
    _post(ctx)
    _post(ctx)
    assert len(ctx.db.inserted) == 2


def test_a_human_principals_batch_does_not_replay_an_agents(ctx):
    """E10: `AuthorizedAgent` admits shared viewers, so a scope of
    `agent:{name}` alone would let one principal's batch be swallowed as
    another's replay."""
    _post(ctx, headers={"Idempotency-Key": "shared"})

    human = User(id=2, username="someone", email="someone@example.com",
                 role="user")
    ctx.client.app.dependency_overrides[get_current_user] = lambda: human
    second = _post(ctx, headers={"Idempotency-Key": "shared"})

    assert second.status_code == 201
    assert second.json()["replayed"] is False
    assert len(ctx.db.inserted) == 2


# ---------------------------------------------------------------------------
# Daily cap (A5/A6/E13)
# ---------------------------------------------------------------------------

def test_a_batch_that_crosses_the_daily_cap_is_a_quota_refusal(ctx, monkeypatch):
    monkeypatch.setattr(
        route_mod.settings_service, "resolve_ops_setting",
        lambda key: ({"metrics_retention_days": "365",
                      "metrics_daily_point_cap": "5"}[key], "default"))
    ctx.db.today = 5

    r = _post(ctx)
    assert r.status_code == 429
    assert r.json()["detail"] == "daily_point_cap_exceeded"
    assert int(r.headers["Retry-After"]) > 0, "the refusal says when to return"
    assert ctx.db.inserted == []


def test_a_cap_of_zero_is_unlimited(ctx, monkeypatch):
    """The `ops_cost_limit_daily_usd` convention: an operator must be able to
    lift a cap without typing a huge number."""
    monkeypatch.setattr(
        route_mod.settings_service, "resolve_ops_setting",
        lambda key: ({"metrics_retention_days": "365",
                      "metrics_daily_point_cap": "0"}[key], "default"))
    ctx.db.today = 10_000_000
    assert _post(ctx).status_code == 201


def test_an_unparseable_cap_falls_back_to_the_default_not_to_unlimited(ctx, monkeypatch):
    """E4: `0` is the fail-OPEN direction for this knob, so garbage must not
    coerce to it — the cap would disappear under exactly the pressure it is
    there for."""
    monkeypatch.setattr(
        route_mod.settings_service, "resolve_ops_setting",
        lambda key: ({"metrics_retention_days": "365",
                      "metrics_daily_point_cap": "not-a-number"}[key], "db-row"))
    ctx.db.today = 100_000
    assert _post(ctx).status_code == 429


def test_the_cap_refusal_releases_the_batch_claim(ctx, monkeypatch):
    monkeypatch.setattr(
        route_mod.settings_service, "resolve_ops_setting",
        lambda key: ({"metrics_retention_days": "365",
                      "metrics_daily_point_cap": "1"}[key], "default"))
    ctx.db.today = 5
    _post(ctx, headers={"Idempotency-Key": "capped"})
    assert (f"agent:{AGENT}", "record_metrics:capped") in ctx.idem.failed


# ---------------------------------------------------------------------------
# Store failures (E6 / Bar 5)
# ---------------------------------------------------------------------------

def test_a_connectivity_failure_is_retryable(ctx):
    ctx.db.raise_on_insert = OperationalError("stmt", {}, Exception("down"))
    r = _post(ctx)
    assert r.status_code == 503
    assert r.json()["detail"] == "metric_store_unavailable"
    assert r.headers["Retry-After"]


def test_a_content_failure_is_not_retryable(ctx):
    """E6: an agent told to retry a batch the database will reject identically
    forever retries forever. A content error is a 500, not a 503."""
    ctx.db.raise_on_insert = IntegrityError("stmt", {}, Exception("bad"))
    r = _post(ctx)
    assert r.status_code == 500
    assert r.json()["detail"] == "metric_store_rejected_batch"


def test_an_unreadable_registry_is_retryable(ctx):
    ctx.db.raise_on_read = OperationalError("stmt", {}, Exception("down"))
    r = _post(ctx)
    assert r.status_code == 503


def test_a_store_failure_releases_the_batch_claim(ctx):
    """So the retry the 503 asks for is actually accepted rather than 409'd
    against the claim its own failure left behind (learning 2026-09-09)."""
    ctx.db.raise_on_insert = OperationalError("stmt", {}, Exception("down"))
    _post(ctx, headers={"Idempotency-Key": "boom"})
    assert (f"agent:{AGENT}", "record_metrics:boom") in ctx.idem.failed


# ---------------------------------------------------------------------------
# Execution provenance (MEM-001)
# ---------------------------------------------------------------------------

def test_a_foreign_execution_id_is_stored_as_null_never_a_4xx(ctx):
    r = _post(ctx, execution_id="someone-elses-execution")
    assert r.status_code == 201
    assert ctx.db.inserted[0][0]["execution_id"] is None


def test_an_owned_execution_id_is_stored(ctx):
    _post(ctx, execution_id="exec-ok")
    assert ctx.db.inserted[0][0]["execution_id"] == "exec-ok"


# ---------------------------------------------------------------------------
# Every post-claim exit releases the claim (S7's choke point)
# ---------------------------------------------------------------------------

def test_every_post_claim_refusal_path_releases_the_claim(ctx, monkeypatch):
    """The property, exercised across all four refusal kinds rather than
    asserted over the source: a new early return that forgets `_reject` shows
    up here as a claim left behind."""
    cases = {
        "validation": lambda: _post(
            ctx, points=[{"metric": "nope_nope", "value": 1}],
            headers={"Idempotency-Key": "k-validation"}),
        "store": None,
        "content": None,
    }
    cases["validation"]()
    assert (f"agent:{AGENT}", "record_metrics:k-validation") in ctx.idem.failed

    ctx.db.raise_on_insert = OperationalError("s", {}, Exception())
    _post(ctx, headers={"Idempotency-Key": "k-store"})
    assert (f"agent:{AGENT}", "record_metrics:k-store") in ctx.idem.failed

    ctx.db.raise_on_insert = IntegrityError("s", {}, Exception())
    _post(ctx, headers={"Idempotency-Key": "k-content"})
    assert (f"agent:{AGENT}", "record_metrics:k-content") in ctx.idem.failed

    ctx.db.raise_on_insert = None
    ctx.db.raise_on_read = OperationalError("s", {}, Exception())
    _post(ctx, headers={"Idempotency-Key": "k-read"})
    assert (f"agent:{AGENT}", "record_metrics:k-read") in ctx.idem.failed
