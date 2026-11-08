"""`POST` / `GET /api/agents/{name}/guardrail-events` (trinity-enterprise#165, S1).

The app is built from the real router over a real database (`db_backend`) and a
fakeredis reporter store. Only the MCP-key lookup is stubbed — the point of the
POST assertions is the handler's own ordering and statuses, which the agent-side
forwarder (S2) branches on:

- only the agent's OWN key is accepted (sibling, user, connector, none -> 403);
- 413 for more than 500 events or a body over 256 KiB (the forwarder halves);
- 422 only when the envelope itself is malformed (the forwarder drops it);
- one bad event never fails the batch: it is counted in `rejected`;
- 429 + Retry-After past the per-agent cap.

The GET is owner tier with the uniform 404 of Invariant #8 (#186).
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("fastapi", reason="backend venv required")
fakeredis = pytest.importorskip("fakeredis")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Bound at import, as in test_ent478_record_route: the override key and the
# router's Depends must be the SAME function object.
import routers.guardrail_events as route_mod  # noqa: E402
from database import db  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from models import User  # noqa: E402
from services import guardrail_event_service as svc  # noqa: E402
from services import rate_limiter  # noqa: E402

AGENT = "guard-agent"
OTHER = "guard-other"
OWNER = "guard-owner"
STRANGER = "guard-stranger"
ADMIN = "guard-admin"

_KEYS = {
    "own": {"agent_name": AGENT, "scope": "agent"},
    "sibling": {"agent_name": OTHER, "scope": "agent"},
    "user": {"agent_name": None, "scope": "user"},
    "connector": {"agent_name": AGENT, "scope": "connector"},
}


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from db_models import UserCreate

    db.create_user(UserCreate(username=OWNER, password="x", role="creator", email="o@example.com"))
    db.create_user(UserCreate(username=STRANGER, password="x", role="creator", email="s@example.com"))
    db.create_user(UserCreate(username=ADMIN, password="x", role="admin", email="a@example.com"))
    db.register_agent_owner(AGENT, OWNER)
    db.register_agent_owner(OTHER, OWNER)

    monkeypatch.setattr(db, "validate_mcp_api_key",
                        lambda token, track_usage=True: _KEYS.get(token))
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(svc, "_get_redis", lambda: client)
    # The request cap runs in-process here (no Redis for the limiter); cleared so
    # one test's hits never count against the next.
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    yield db, svc
    rate_limiter.clear_inprocess()


def _app(principal=None):
    app = FastAPI()
    app.include_router(route_mod.router)
    if principal is not None:
        app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app)


def _user(username, role="creator", **kw):
    row = db.get_user_by_username(username)
    return User(id=row["id"], username=username, role=role, **kw)


def _ev(event_id="e-1", **kw):
    ev = {"event_id": event_id, "ts": "2026-11-08T10:00:00.000000Z", "kind": "deny",
          "hook": "bash_guardrail", "tool": "Bash", "code": "destructive_rm",
          "execution_id": "exec-1"}
    ev.update(kw)
    return ev


def _post(client, body, key="own", agent=AGENT, raw=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if raw is not None:
        headers["Content-Type"] = "application/json"
        return client.post(f"/api/agents/{agent}/guardrail-events", content=raw, headers=headers)
    return client.post(f"/api/agents/{agent}/guardrail-events", json=body, headers=headers)


def _batch(events, **kw):
    body = {"schema": 1, "runtime": "claude-code", "events": events, "dropped": 0}
    body.update(kw)
    return body


# ---------------------------------------------------------------------------
# POST — auth
# ---------------------------------------------------------------------------

class TestPostAuth:
    def test_the_agents_own_key_is_accepted(self, world):
        r = _post(_app(), _batch([_ev()]))
        assert r.status_code == 200, r.text
        assert r.json() == {"accepted": 1, "duplicates": 0, "rejected": 0}

    @pytest.mark.parametrize("key", ["sibling", "user", "connector", "unknown-key", None])
    def test_any_other_key_is_refused_and_nothing_is_stored(self, world, key):
        db, svc = world
        r = _post(_app(), _batch([_ev()]), key=key)
        assert r.status_code == 403
        assert svc.list_events(AGENT)["events"] == []
        assert svc.reporting_state(AGENT)["state"] == "never"

    def test_a_non_bearer_header_is_refused(self, world):
        r = _app().post(f"/api/agents/{AGENT}/guardrail-events", json=_batch([]),
                        headers={"Authorization": "Basic own"})
        assert r.status_code == 403

    def test_the_system_agents_own_system_key_is_accepted(self, world, monkeypatch):
        from db.agents import SYSTEM_AGENT_NAME

        _KEYS["sys"] = {"agent_name": SYSTEM_AGENT_NAME, "scope": "system"}
        try:
            r = _post(_app(), _batch([]), key="sys", agent=SYSTEM_AGENT_NAME)
            assert r.status_code == 200, r.text
            r = _post(_app(), _batch([]), key="sys", agent=AGENT)
            assert r.status_code == 403
        finally:
            _KEYS.pop("sys")


# ---------------------------------------------------------------------------
# POST — envelope, caps, tolerance
# ---------------------------------------------------------------------------

class TestPostEnvelope:
    def test_one_bad_event_never_fails_the_batch(self, world):
        r = _post(_app(), _batch([_ev(), "garbage", _ev("e-2", kind="future_kind", hook="future_hook")]))
        assert r.status_code == 200
        assert r.json() == {"accepted": 2, "duplicates": 0, "rejected": 1}

    def test_a_resend_is_counted_as_duplicates(self, world):
        app = _app()
        _post(app, _batch([_ev()]))
        r = _post(app, _batch([_ev(), _ev("e-2")]))
        assert r.json() == {"accepted": 1, "duplicates": 1, "rejected": 0}

    def test_more_than_500_events_is_413_not_422(self, world):
        db, svc = world
        r = _post(_app(), _batch([_ev(f"e-{i}") for i in range(501)]))
        assert r.status_code == 413
        assert svc.list_events(AGENT)["events"] == []

    def test_exactly_500_events_is_accepted(self, world):
        r = _post(_app(), _batch([_ev(f"e-{i}") for i in range(500)]))
        assert r.status_code == 200
        assert r.json()["accepted"] == 500

    def test_a_body_over_256_kib_is_413(self, world):
        big = _batch([_ev("e-1", path="p" * 300_000)])
        r = _post(_app(), None, raw=json.dumps(big))
        assert r.status_code == 413

    @pytest.mark.parametrize("raw", [
        "not json",
        "[]",
        json.dumps({"runtime": "claude-code", "events": []}),            # no schema
        json.dumps({"schema": "one", "runtime": "claude-code", "events": []}),
        json.dumps({"schema": 0, "runtime": "claude-code", "events": []}),
        json.dumps({"schema": 1, "runtime": "claude-code", "events": {}}),
        json.dumps({"schema": 1, "runtime": "claude-code"}),             # no events
        json.dumps({"schema": 1, "runtime": "claude-code", "events": [], "dropped": -1}),
    ])
    def test_a_malformed_envelope_is_422(self, world, raw):
        r = _post(_app(), None, raw=raw)
        assert r.status_code == 422, r.text

    def test_a_newer_schema_number_is_not_refused(self, world):
        """Version skew: a newer image's envelope is read event by event."""
        r = _post(_app(), _batch([_ev()], schema=2, extra_field="ignored"))
        assert r.status_code == 200
        assert r.json()["accepted"] == 1

    def test_a_keepalive_marks_the_agent_reporting(self, world):
        db, svc = world
        r = _post(_app(), _batch([], runtime="gemini", dropped=4))
        assert r.status_code == 200
        state = svc.reporting_state(AGENT)
        assert (state["state"], state["runtime"], state["dropped_total"]) == ("reporting", "gemini", 4)

    def test_past_the_per_agent_cap_is_429_with_retry_after(self, world, monkeypatch):
        db, svc = world
        monkeypatch.setattr(svc, "RATE_LIMIT_REQUESTS", 2)
        app = _app()
        assert _post(app, _batch([])).status_code == 200
        assert _post(app, _batch([])).status_code == 200
        r = _post(app, _batch([_ev()]))
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) >= 1
        assert svc.list_events(AGENT)["events"] == []
        # The cap is per agent: a sibling posting with its own key is unaffected.
        assert _post(app, _batch([]), key="sibling", agent=OTHER).status_code == 200


# ---------------------------------------------------------------------------
# GET — owner tier, uniform 404
# ---------------------------------------------------------------------------

class TestGet:
    def _seed(self):
        _post(_app(), _batch([_ev("e-1"), _ev("e-2", kind="credential_redacted",
                                              hook="output_scanner", code=None)]))

    def test_the_owner_reads_events_and_reporting(self, world):
        self._seed()
        r = _app(_user(OWNER)).get(f"/api/agents/{AGENT}/guardrail-events")
        assert r.status_code == 200, r.text
        body = r.json()
        assert {e["event_id"] for e in body["events"]} == {"e-1", "e-2"}
        assert body["next_cursor"] is None
        assert body["reporting"]["state"] == "reporting"
        assert body["reporting"]["runtime"] == "claude-code"

    def test_an_admin_reads_any_agent(self, world):
        self._seed()
        r = _app(_user(ADMIN, role="admin")).get(f"/api/agents/{AGENT}/guardrail-events")
        assert r.status_code == 200
        assert len(r.json()["events"]) == 2

    def test_a_fresh_agent_reads_empty_and_never(self, world):
        r = _app(_user(OWNER)).get(f"/api/agents/{AGENT}/guardrail-events")
        assert r.status_code == 200
        assert r.json() == {"events": [], "next_cursor": None,
                            "reporting": {"state": "never", "last_report_at": None,
                                          "runtime": None, "dropped_total": 0}}

    def test_a_non_owner_and_a_missing_agent_get_the_same_404(self, world):
        self._seed()
        app = _app(_user(STRANGER))
        unowned = app.get(f"/api/agents/{AGENT}/guardrail-events")
        missing = app.get("/api/agents/no-such-agent/guardrail-events")
        assert unowned.status_code == missing.status_code == 404
        assert unowned.json() == missing.json()

    def test_a_shared_person_is_not_owner_tier(self, world):
        db, svc = world
        self._seed()
        db.share_agent(AGENT, OWNER, "s@example.com")
        r = _app(_user(STRANGER)).get(f"/api/agents/{AGENT}/guardrail-events")
        assert r.status_code == 404

    def test_a_bad_query_is_a_named_422(self, world):
        app = _app(_user(OWNER))
        assert app.get(f"/api/agents/{AGENT}/guardrail-events?kind=everything").status_code == 422
        assert app.get(f"/api/agents/{AGENT}/guardrail-events?before=garbage").status_code == 422
        assert app.get(f"/api/agents/{AGENT}/guardrail-events?limit=201").status_code == 422
        assert app.get(f"/api/agents/{AGENT}/guardrail-events?since=yesterday").status_code == 422

    def test_filters_reach_the_store(self, world):
        self._seed()
        r = _app(_user(OWNER)).get(f"/api/agents/{AGENT}/guardrail-events?kind=deny&limit=1")
        assert [e["event_id"] for e in r.json()["events"]] == ["e-1"]
