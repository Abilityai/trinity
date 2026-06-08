"""Webhook rate-limit regression tests.

Two scenarios covered against the live stack:

1. **Sequential** (#589) — verifies the Redis client uses the credentialed
   `REDIS_URL` so rate limiting is actually engaged (the historic regression
   was a silent fail-open on bad auth).
2. **Concurrent** (#644) — verifies the limiter is TOCTOU-safe: firing
   `WEBHOOK_RATE_LIMIT + 5` simultaneous requests must not let more than
   `WEBHOOK_RATE_LIMIT` slip through. The previous read-then-INCR path
   allowed each concurrent caller to observe `count < limit` before any
   of them incremented — so the actual call rate exceeded the budget by
   the concurrency factor.

Both tests build their own agent + schedule + webhook token so they don't
share rate-limit state with each other.

Marked `integration` (not `smoke`) because they need the full stack —
backend + Redis with auth + scheduler service. The smoke runner targets
~30s and excludes Docker-dependent tests; these go through
tests/run-integration.sh.
"""

import asyncio
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

        # The 11th call must be rate-limited. If Redis auth is broken, the
        # shared limiter (services/rate_limiter.py, #1023) fails open to its
        # per-worker in-process fallback; this assertion catches a regression
        # where rate limiting silently disappears.
        r = httpx.post(url, timeout=5.0)
        assert r.status_code == 429, (
            f"11th call returned {r.status_code} — rate limiter silently disabled? "
            f"Body: {r.text}"
        )
    finally:
        api_client.delete(f"/api/agents/{agent_name}")


@pytest.mark.integration
def test_webhook_rate_limit_holds_under_concurrency(api_client: TrinityApiClient):
    """Concurrent regression for #644.

    Fire `WEBHOOK_RATE_LIMIT + 5` requests simultaneously. The pre-fix
    read-then-INCR path could let all N callers observe `count < limit`
    before any incremented, exceeding the limit by N. After the
    INCR-then-compare fix, at most `WEBHOOK_RATE_LIMIT` calls get a
    non-429 response.
    """
    agent_name = f"test-644-webhook-{uuid.uuid4().hex[:8]}"

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

        url = f"http://localhost:8000/api/webhooks/{token}"
        n_concurrent = WEBHOOK_RATE_LIMIT + 5

        async def fire_all():
            async with httpx.AsyncClient(timeout=10.0) as client:
                tasks = [client.post(url) for _ in range(n_concurrent)]
                return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(fire_all())

        statuses = []
        for r in results:
            if isinstance(r, Exception):
                pytest.fail(f"Concurrent webhook call raised: {r!r}")
            statuses.append(r.status_code)

        accepted = sum(1 for s in statuses if s in (202, 503))
        rate_limited = sum(1 for s in statuses if s == 429)
        other = [s for s in statuses if s not in (202, 503, 429)]

        assert not other, f"Unexpected statuses: {other} (full set: {statuses})"
        assert accepted <= WEBHOOK_RATE_LIMIT, (
            f"{accepted} calls succeeded under {n_concurrent}-way concurrency, "
            f"limit is {WEBHOOK_RATE_LIMIT}. TOCTOU race regressed: {statuses}"
        )
        # Sanity: at least some made it through (otherwise the test isn't
        # exercising the limiter — e.g., backend down).
        assert accepted >= 1, (
            f"No requests accepted ({statuses}) — limiter or backend broken"
        )
        # And at least one was rate-limited (proves the limiter ran).
        assert rate_limited >= 1, (
            f"No 429 in {n_concurrent}-way burst with limit {WEBHOOK_RATE_LIMIT} — "
            f"limiter not engaging: {statuses}"
        )
    finally:
        api_client.delete(f"/api/agents/{agent_name}")
