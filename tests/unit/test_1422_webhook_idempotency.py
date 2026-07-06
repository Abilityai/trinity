"""Webhook idempotency only dedupes on an explicit key (#1422).

The public webhook trigger used to auto-derive an idempotency key from
sha256(token, raw_body). A completed claim lives 24h, so repeated identical /
body-less "ping to trigger" calls (CI hooks, monitors, IFTTT, external cron)
were silently deduped — only the first fired while every later 202 lied. The
fix dedupes ONLY when the caller sends an explicit `Idempotency-Key`; a key-less
call fires a fresh execution every time.

Runnable in-process (FastAPI TestClient + real idempotency_service over a temp
SQLite DB); the scheduler dispatch is faked and counted, so "fired vs replayed"
is observable without a live backend.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TRINITY_DB_PATH", os.path.join(tempfile.gettempdir(), "trinity_1422_test.db"))

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers import webhooks  # noqa: E402


class _FakeSchedule:
    id = "sched-1422"
    agent_name = "agent-x"
    name = "nightly"
    message = "do the thing"
    webhook_enabled = True
    # ent#77: signature auth off by default (matches the real Schedule model);
    # these idempotency tests exercise the unauthenticated path.
    webhook_auth_enabled = False
    webhook_secret_encrypted = None


class _FakeResp:
    status_code = 202


class _FakeAsyncClient:
    """Counts scheduler dispatches so 'fired' vs 'replayed' is observable."""
    calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        _FakeAsyncClient.calls += 1
        return _FakeResp()


_TOKEN = "W" * 43


@pytest.fixture
def client(monkeypatch):
    _FakeAsyncClient.calls = 0
    monkeypatch.setattr(webhooks.db, "get_schedule_by_webhook_token", lambda t: _FakeSchedule())
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeAsyncClient)
    # Roomy limits so rate limiting never interferes with the dispatch counts.
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 10_000)
    monkeypatch.setattr(webhooks, "WEBHOOK_RATE_LIMIT", 10_000)
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app)


def test_bodyless_repeats_each_fire_a_fresh_execution(client):
    """AC(a): two body-less calls seconds apart both fire — no silent replay."""
    r1 = client.post(f"/api/webhooks/{_TOKEN}")
    r2 = client.post(f"/api/webhooks/{_TOKEN}")
    assert r1.status_code == 202 and r2.status_code == 202
    assert "x-idempotent-replay" not in {k.lower() for k in r1.headers}
    assert "x-idempotent-replay" not in {k.lower() for k in r2.headers}
    assert _FakeAsyncClient.calls == 2, "both key-less deliveries must dispatch"


def test_identical_body_repeats_each_fire(client):
    """AC(a): identical non-empty bodies (no key) also both fire."""
    body = {"context": "same every time"}
    client.post(f"/api/webhooks/{_TOKEN}", json=body)
    client.post(f"/api/webhooks/{_TOKEN}", json=body)
    assert _FakeAsyncClient.calls == 2


def test_explicit_key_resend_dedupes(client):
    """AC(b): same explicit Idempotency-Key → one execution + replay header."""
    key = f"ci-{uuid.uuid4().hex}"
    r1 = client.post(f"/api/webhooks/{_TOKEN}", headers={"Idempotency-Key": key})
    r2 = client.post(f"/api/webhooks/{_TOKEN}", headers={"Idempotency-Key": key})
    assert r1.status_code == 202 and r2.status_code == 202
    assert "x-idempotent-replay" not in {k.lower() for k in r1.headers}
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert _FakeAsyncClient.calls == 1, "the resend must not dispatch a second time"


def test_distinct_explicit_keys_each_fire(client):
    """Different explicit keys are distinct intentional triggers."""
    client.post(f"/api/webhooks/{_TOKEN}", headers={"Idempotency-Key": f"a-{uuid.uuid4().hex}"})
    client.post(f"/api/webhooks/{_TOKEN}", headers={"Idempotency-Key": f"b-{uuid.uuid4().hex}"})
    assert _FakeAsyncClient.calls == 2
