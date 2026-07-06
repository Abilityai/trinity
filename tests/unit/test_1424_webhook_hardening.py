"""Public-webhook pre-auth hardening (#1424).

The public, unauthenticated ``POST /api/webhooks/{token}`` had no per-IP /
pre-auth rate limit (a flood of well-formed-but-unknown tokens hit the DB
unthrottled) and no request-body size cap. This exercises the fix with an
in-process FastAPI ``TestClient`` — no live backend, no Redis (the shared
limiter falls back to its in-process window) — so it runs anywhere the backend
package imports.

Guarded checks:
  * a per-IP limit fires BEFORE the token DB lookup (flood of unknown tokens is
    throttled, and the DB is not consulted once throttled);
  * the per-token limit is preserved for a valid token;
  * an over-sized body is rejected (413) before it is parsed.
"""

from __future__ import annotations

import os
import tempfile

import pytest

# Backend config raises at import without these; keep DB side effects in a temp
# file so importing `database` can't touch a real /data volume.
os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TRINITY_DB_PATH", os.path.join(tempfile.gettempdir(), "trinity_1424_test.db"))

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers import webhooks  # noqa: E402
from services import rate_limiter  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeSchedule:
    def __init__(self):
        self.id = "sched-1"
        self.agent_name = "agent-x"
        self.name = "nightly"
        self.message = "do the thing"
        self.webhook_enabled = True
        # ent#77: signature auth off by default (matches the real Schedule model);
        # these hardening tests exercise the unauthenticated path.
        self.webhook_auth_enabled = False
        self.webhook_secret_encrypted = None


_VALID_TOKEN = "V" * 43
_UNKNOWN_TOKEN = "U" * 43


@pytest.fixture
def client(monkeypatch):
    # Isolate the limiter window per test (in-process fallback, no Redis).
    rate_limiter.clear_inprocess()
    rate_limiter.reset_redis_client()
    # Never actually reach Redis — force the deterministic in-process path.
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)

    lookups = {"count": 0}

    def _fake_lookup(token):
        lookups["count"] += 1
        return _FakeSchedule() if token == _VALID_TOKEN else None

    monkeypatch.setattr(webhooks.db, "get_schedule_by_webhook_token", _fake_lookup)

    app = FastAPI()
    app.include_router(webhooks.router)
    c = TestClient(app)
    c.lookups = lookups
    return c


def test_ip_flood_of_unknown_tokens_is_throttled_before_db(client, monkeypatch):
    """A flood of well-formed-but-unknown tokens 429s on the per-IP limit, and
    once throttled the token DB lookup is not consulted (limit is pre-auth)."""
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 3)

    statuses = []
    for _ in range(6):
        r = client.post(f"/api/webhooks/{_UNKNOWN_TOKEN}")
        statuses.append(r.status_code)

    # First few pass the IP gate (→404 unknown token), then the gate trips.
    assert 429 in statuses, statuses
    first_429 = statuses.index(429)
    assert first_429 <= 3
    # Every request after the first 429 is also 429 (window stays saturated).
    assert all(s == 429 for s in statuses[first_429:]), statuses
    # The DB was consulted only for the requests that passed the IP gate — never
    # for a throttled one (proves the limit sits BEFORE the lookup).
    assert client.lookups["count"] == first_429
    # 429 carries Retry-After.
    r = client.post(f"/api/webhooks/{_UNKNOWN_TOKEN}")
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers}


class _InFlightReplay:
    """A benign idempotency decision → the handler returns 409 without dispatching
    to the scheduler (lets us probe the gates before dispatch)."""
    replay = True
    in_flight = True
    snapshot = None


def _short_circuit_dispatch(monkeypatch):
    async def _noop_log(*a, **k):
        return None

    monkeypatch.setattr(webhooks.platform_audit_service, "log", _noop_log)
    monkeypatch.setattr(webhooks.idempotency_service, "begin", lambda *a, **k: _InFlightReplay())


def test_per_token_limit_preserved_for_valid_token(client, monkeypatch):
    """The existing per-token limit still engages for a resolved token."""
    # Roomy IP budget; tight per-token budget so the token gate is what trips.
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 1000)
    monkeypatch.setattr(webhooks, "WEBHOOK_RATE_LIMIT", 2)
    _short_circuit_dispatch(monkeypatch)

    statuses = [client.post(f"/api/webhooks/{_VALID_TOKEN}").status_code for _ in range(5)]
    # Allowed calls short-circuit to 409 (in-flight replay); once the per-token
    # window saturates the gate returns 429.
    assert 429 in statuses, statuses
    assert statuses.index(429) <= 2, statuses


def test_oversized_body_rejected_with_413(client, monkeypatch):
    """A body over the cap is rejected (413) before parsing."""
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 1000)
    monkeypatch.setattr(webhooks, "WEBHOOK_MAX_BODY_BYTES", 100)
    r = client.post(
        f"/api/webhooks/{_VALID_TOKEN}",
        content=b'{"context": "' + b"x" * 500 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413, r.text


def test_small_valid_body_passes_the_size_gate(client, monkeypatch):
    """A within-cap body clears the 413 gate (reaches dispatch → benign 409).

    Sends an explicit Idempotency-Key: since #1422 the idempotency gate engages
    only for keyed calls, and the key-gated in-flight replay (409) is the benign
    short-circuit this test uses to prove the request got past the size gate.
    """
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 1000)
    _short_circuit_dispatch(monkeypatch)
    r = client.post(
        f"/api/webhooks/{_VALID_TOKEN}",
        json={"context": "small"},
        headers={"Idempotency-Key": "k-1424-small-body"},
    )
    # Past the 413 gate → hits the idempotency short-circuit, not a size rejection.
    assert r.status_code == 409, r.text
