"""Issue #589 regression test for webhooks.py Redis client switch.

The fix in src/backend/routers/webhooks.py replaced
    redis.Redis(host="redis", port=6379)
with
    redis.from_url(REDIS_URL)
so the credentials embedded in REDIS_URL are actually used. Without this
test, a regression would silently fail-open and rate limiting would be
disabled.

Self-contained: creates an agent + schedule + webhook token inline so a
fresh token is used (no pre-existing rate-limit state).

Marked `integration` (not `smoke`) because it needs the full stack —
backend + Redis with auth + scheduler service. The smoke runner targets
~30s and excludes Docker-dependent tests; this goes through
tests/run-integration.sh.
"""

import uuid

import httpx
import pytest

from utils.api_client import TrinityApiClient


WEBHOOK_RATE_LIMIT = 10  # matches WEBHOOK_RATE_LIMIT default in webhooks.py


@pytest.mark.integration
def test_webhook_rate_limit_returns_429_after_threshold(api_client: TrinityApiClient):
    """11th call within the window must return 429."""
    agent_name = f"test-589-webhook-{uuid.uuid4().hex[:8]}"

    create_resp = api_client.post("/api/agents", json={"name": agent_name})
    if create_resp.status_code not in (200, 201):
        pytest.skip(f"Cannot create test agent: {create_resp.text}")

    try:
        sched_resp = api_client.post(
            f"/api/agents/{agent_name}/schedules",
            json={
                "name": f"wh-{uuid.uuid4().hex[:6]}",
                "cron_expression": "0 0 1 1 *",  # never fires during tests
                "message": "noop",
                "enabled": True,
                "timezone": "UTC",
            },
        )
        assert sched_resp.status_code == 201, sched_resp.text
        sid = sched_resp.json()["id"]

        gen_resp = api_client.post(
            f"/api/agents/{agent_name}/schedules/{sid}/webhook"
        )
        assert gen_resp.status_code == 200, gen_resp.text
        webhook_url = gen_resp.json()["webhook_url"]
        token = webhook_url.split("/api/webhooks/")[1]

        # Fire WEBHOOK_RATE_LIMIT calls — all must succeed (or 503 if scheduler
        # is down, which is also acceptable as a non-429 response).
        url = f"http://localhost:8000/api/webhooks/{token}"
        for i in range(WEBHOOK_RATE_LIMIT):
            r = httpx.post(url, timeout=5.0)
            assert r.status_code in (202, 503), (
                f"call {i+1} returned {r.status_code} (expected 202 or 503): {r.text}"
            )

        # The 11th call must be rate-limited. If Redis auth is broken,
        # _get_redis() returns None and rate limiting silently fails-open;
        # this assertion catches that regression.
        r = httpx.post(url, timeout=5.0)
        assert r.status_code == 429, (
            f"11th call returned {r.status_code} — rate limiter silently disabled? "
            f"Body: {r.text}"
        )
    finally:
        api_client.delete(f"/api/agents/{agent_name}")
