"""The webhook consumer of the scheduler's new 409 (#1968).

#1968 made `POST /api/schedules/{id}/trigger` answer **409 already_running**
when the schedule lock is held. Three consumers were updated; the fourth —
`routers/webhooks.py`, the unauthenticated public trigger — was not, and it
regressed a healthy state into an error:

* `if response.status_code not in (200, 202)` caught the 409, logged at ERROR,
  and answered the caller `503 "Trigger failed — try again later"` — advice
  that only hits the same lock;
* the `except HTTPException` arm then called `idempotency_service.fail(idem)`,
  releasing the #525 dedup claim for a delivery that never failed;
* before #1968 that same delivery returned **202**.

`tests/test_webhook_triggers.py` asserts `status_code in (202, 503)` throughout,
so CI accepted the regression silently — which is why it needed a test that
names the status rather than tolerating a set.

Runs in-process (FastAPI TestClient, faked scheduler) like #1422's suite.
"""

from __future__ import annotations

import logging
import os
import tempfile

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault(
    "TRINITY_DB_PATH", os.path.join(tempfile.gettempdir(), "trinity_1968_test.db")
)

pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from routers import webhooks  # noqa: E402


class _FakeSchedule:
    id = "sched-1968"
    agent_name = "agent-x"
    name = "nightly"
    message = "do the thing"
    webhook_enabled = True
    webhook_auth_enabled = False
    webhook_secret_encrypted = None


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _client_returning(monkeypatch, resp):
    class _FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return resp

    monkeypatch.setattr(webhooks.db, "get_schedule_by_webhook_token", lambda t: _FakeSchedule())
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(webhooks, "WEBHOOK_IP_RATE_LIMIT", 10_000)
    monkeypatch.setattr(webhooks, "WEBHOOK_RATE_LIMIT", 10_000)
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app)


_TOKEN = "R" * 43


_ALREADY_RUNNING = _Resp(409, {
    "status": "already_running",
    "schedule_id": _FakeSchedule.id,
    "message": "Schedule is already executing",
})


def test_a_busy_schedule_is_not_reported_as_a_failure(monkeypatch):
    """A healthy, busy schedule must not answer an unauthenticated caller 503."""
    client = _client_returning(monkeypatch, _ALREADY_RUNNING)
    r = client.post(f"/api/webhooks/{_TOKEN}")

    assert r.status_code == 202, (
        f"a busy schedule answered {r.status_code} — before #1968 this delivery "
        "was a 202, and 'try again later' only hits the same lock"
    )
    assert r.json()["status"] == "already_running", (
        "the response must say the delivery was coalesced, not claim a fresh "
        "execution started"
    )


def test_the_busy_case_is_not_logged_as_an_error(monkeypatch, caplog):
    """ERROR is for faults an operator should act on; a held lock is not one."""
    client = _client_returning(monkeypatch, _ALREADY_RUNNING)
    with caplog.at_level(logging.INFO, logger=webhooks.logger.name):
        client.post(f"/api/webhooks/{_TOKEN}")

    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert not errors, f"busy schedule logged at ERROR: {[r.getMessage() for r in errors]}"
    assert any("already executing" in rec.getMessage() for rec in caplog.records), (
        "the coalesced delivery should still be visible in the log at INFO"
    )


def test_the_idempotency_claim_is_completed_not_released(monkeypatch):
    """`fail()` is for 'nothing dispatched, retry is legitimate'.

    A held lock means the work IS running, so releasing the claim invites a
    duplicate delivery to fire a second execution the moment the lock clears.
    """
    released, completed = [], []
    monkeypatch.setattr(webhooks.idempotency_service, "fail", lambda i: released.append(i))
    monkeypatch.setattr(
        webhooks.idempotency_service,
        "complete",
        lambda i, e, s: completed.append(s),
    )
    client = _client_returning(monkeypatch, _ALREADY_RUNNING)
    r = client.post(f"/api/webhooks/{_TOKEN}", headers={"Idempotency-Key": "k-1968"})

    assert r.status_code == 202
    assert not released, "the dedup claim was released for a delivery that did not fail"
    assert completed and completed[0]["status"] == "already_running", (
        "the stored snapshot must record the coalesced outcome, so a replay of "
        "the same key reports what actually happened"
    )


@pytest.mark.parametrize("code", [500, 502, 400])
def test_genuine_scheduler_errors_still_fail(monkeypatch, code):
    """The 409 carve-out must not widen into 'any non-2xx is fine'."""
    client = _client_returning(monkeypatch, _Resp(code))
    r = client.post(f"/api/webhooks/{_TOKEN}")
    assert r.status_code == 503, f"scheduler {code} should still surface as 503"
