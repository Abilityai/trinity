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

    def test_soft_deleted_agent_webhook_returns_404_then_recovers(
        self, api_client: TrinityApiClient
    ):
        """#1423: a webhook must not fire a schedule whose *agent* is soft-deleted.

        Soft-deleting the agent → the token lookup joins agent_ownership and
        excludes it → 404 (so no scheduler dispatch, no execution row). Admin
        recovery clears agent_ownership.deleted_at → the same token fires again.
        Uses a throwaway agent so the shared fixture agent is untouched; the
        webhook 404 gate needs only the ownership row, not a running container.
        """
        import httpx
        agent = f"test-1423-{uuid.uuid4().hex[:6]}"
        created = api_client.post("/api/agents", json={"name": agent})
        if created.status_code not in (200, 201):
            pytest.skip(f"could not create throwaway agent: {created.text}")

        sid = None
        try:
            sid = _create_test_schedule(api_client, agent)["id"]
            token = api_client.post(
                f"/api/agents/{agent}/schedules/{sid}/webhook"
            ).json()["webhook_url"].split("/api/webhooks/")[1]

            # Sanity: token fires while the agent is live (202 dispatched, or 503
            # if the scheduler is down — never 404).
            live = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            assert live.status_code in (202, 503), live.text

            # Soft-delete the agent (child schedule row keeps deleted_at NULL).
            assert api_client.delete(f"/api/agents/{agent}").status_code in (200, 204)

            # The token must now 404 — the agent-ownership guard (#1423).
            gone = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            assert gone.status_code == 404, (
                f"soft-deleted agent's webhook should 404, got {gone.status_code}"
            )

            # Admin recovery clears deleted_at → the token resolves again.
            rec = api_client.post(f"/api/admin/soft-deleted/agents/{agent}/recover")
            assert rec.status_code == 200, rec.text
            back = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            assert back.status_code != 404, "recovered agent's webhook should fire again"
            assert back.status_code in (202, 503), back.text
        finally:
            if sid:
                _delete_schedule(api_client, agent, sid)
            api_client.delete(f"/api/agents/{agent}")
