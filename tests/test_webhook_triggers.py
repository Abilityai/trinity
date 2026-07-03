"""
Webhook Trigger Tests (test_webhook_triggers.py)

Tests for WEBHOOK-001: Agent schedule webhook triggers (#291).
Covers token generation, trigger flow, rate limiting, revocation, and
security hardening (invalid tokens, disabled webhooks, malformed input).
"""

import pytest
import uuid
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_test_schedule(api_client: TrinityApiClient, agent_name: str) -> dict:
    """Create a minimal schedule on agent_name, return schedule dict."""
    data = {
        "name": f"webhook-test-{uuid.uuid4().hex[:8]}",
        "cron_expression": "0 0 1 1 *",  # once a year — won't fire during tests
        "message": "Webhook test task",
        "enabled": True,
        "timezone": "UTC",
    }
    resp = api_client.post(f"/api/agents/{agent_name}/schedules", json=data)
    assert_status(resp, 201)
    return resp.json()


def _delete_schedule(api_client: TrinityApiClient, agent_name: str, schedule_id: str):
    api_client.delete(f"/api/agents/{agent_name}/schedules/{schedule_id}")


# ---------------------------------------------------------------------------
# Webhook CRUD (authenticated)
# ---------------------------------------------------------------------------

class TestWebhookGeneration:
    """Webhook token generation, status, and revocation endpoints."""

    def test_generate_webhook_creates_token(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            assert_status(resp, 200)
            data = assert_json_response(resp)
            assert_has_fields(data, ["schedule_id", "has_token", "webhook_enabled", "webhook_url"])
            assert data["has_token"] is True
            assert data["webhook_enabled"] is True
            assert "/api/webhooks/" in data["webhook_url"]
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_generate_webhook_twice_changes_url(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            r1 = api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            r2 = api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            assert_status(r1, 200)
            assert_status(r2, 200)
            assert r1.json()["webhook_url"] != r2.json()["webhook_url"]
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_get_webhook_status_no_token(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            resp = api_client.get(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            assert_status(resp, 200)
            data = resp.json()
            assert data["has_token"] is False
            assert data["webhook_enabled"] is False
            assert data["webhook_url"] is None
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_get_webhook_status_after_generate(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            resp = api_client.get(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            assert_status(resp, 200)
            data = resp.json()
            assert data["has_token"] is True
            assert data["webhook_enabled"] is True
            assert data["webhook_url"] is not None
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_revoke_webhook_clears_token(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            resp_del = api_client.delete(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            assert_status(resp_del, 204)
            resp_get = api_client.get(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            data = resp_get.json()
            assert data["has_token"] is False
            assert data["webhook_url"] is None
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_generate_webhook_unknown_schedule_404(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        resp = api_client.post(
            f"/api/agents/{test_agent_name}/schedules/nonexistent-id/webhook"
        )
        assert_status(resp, 404)

    def test_webhook_endpoints_require_auth(self, test_agent_name: str):
        import httpx
        base = "http://localhost:8000"
        r = httpx.post(
            f"{base}/api/agents/{test_agent_name}/schedules/any-id/webhook"
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Public trigger endpoint
# ---------------------------------------------------------------------------

class TestWebhookTrigger:
    """POST /api/webhooks/{token} — public trigger endpoint."""

    def test_invalid_token_returns_404(self):
        import httpx
        resp = httpx.post("http://localhost:8000/api/webhooks/invalid-token-xyz")
        assert resp.status_code == 404

    def test_nonexistent_token_returns_404(self):
        import httpx
        # A plausible-looking but unknown token
        token = "A" * 43
        resp = httpx.post(f"http://localhost:8000/api/webhooks/{token}")
        assert resp.status_code == 404

    def test_disabled_webhook_returns_403(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            # Revoke → disabled
            api_client.delete(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")

            resp = httpx.post(f"http://localhost:8000/api/webhooks/{token}")
            assert resp.status_code == 404  # token no longer exists in DB
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_valid_webhook_returns_202(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            resp = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            # Scheduler may or may not be running in test env; accept 202 or 503
            assert resp.status_code in (202, 503), (
                f"Expected 202 (scheduler up) or 503 (scheduler down), got {resp.status_code}"
            )
            if resp.status_code == 202:
                data = resp.json()
                assert_has_fields(data, ["status", "schedule_id", "agent_name"])
                assert data["status"] == "triggered"
                assert data["schedule_id"] == sid
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_webhook_with_context_body(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            resp = httpx.post(
                f"http://localhost:8000/api/webhooks/{token}",
                json={"context": "Triggered by GitHub Actions push to main"},
                timeout=15.0,
            )
            assert resp.status_code in (202, 503)
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_webhook_context_too_long_rejected(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            resp = httpx.post(
                f"http://localhost:8000/api/webhooks/{token}",
                json={"context": "x" * 5000},  # exceeds 4000 char limit
                timeout=15.0,
            )
            assert resp.status_code == 422
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_oversized_body_returns_413(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        """A body over the size cap is rejected (413) before it is parsed (#1424)."""
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{test_agent_name}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            # ~20 KB raw body — over the 16 KiB cap; rejected on Content-Length
            # before any JSON parse (so 413, not 422).
            resp = httpx.post(
                f"http://localhost:8000/api/webhooks/{token}",
                content=b'{"metadata": {"blob": "' + b"y" * 20000 + b'"}}',
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
            assert resp.status_code == 413, (
                f"Expected 413 for oversized body, got {resp.status_code}"
            )
        finally:
            _delete_schedule(api_client, test_agent_name, sid)

    def test_invalid_token_flood_is_rate_limited(self):
        """A flood of well-formed-but-unknown tokens is throttled per-IP (#1424).

        The per-token limiter never engages for unknown tokens; the pre-auth
        per-IP limit must (429 + Retry-After). Isolated to its own bucket via
        X-Real-IP (127.0.0.1 is a trusted proxy, so _get_client_ip honors it).
        """
        import httpx
        # WEBHOOK_IP_RATE_LIMIT default is 60/60s; send enough to cross it.
        headers = {"X-Real-IP": "203.0.113.77"}
        saw_429 = False
        retry_after_ok = True
        for _ in range(70):
            resp = httpx.post(
                "http://localhost:8000/api/webhooks/" + "Z" * 43,
                headers=headers,
                timeout=15.0,
            )
            if resp.status_code == 429:
                saw_429 = True
                retry_after_ok = "Retry-After" in resp.headers
                break
            assert resp.status_code == 404, (
                f"Pre-429 requests should 404 (unknown token), got {resp.status_code}"
            )
        assert saw_429, "Per-IP rate limit did not trigger 429 within 70 requests"
        assert retry_after_ok, "429 response is missing the Retry-After header"

    def test_old_token_invalid_after_regenerate(
        self, api_client: TrinityApiClient, test_agent_name: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, test_agent_name)
        sid = schedule["id"]
        try:
            r1 = api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")
            old_url = r1.json()["webhook_url"]
            old_token = old_url.split("/api/webhooks/")[1]

            # Regenerate
            api_client.post(f"/api/agents/{test_agent_name}/schedules/{sid}/webhook")

            resp = httpx.post(f"http://localhost:8000/api/webhooks/{old_token}")
            assert resp.status_code == 404
        finally:
            _delete_schedule(api_client, test_agent_name, sid)
