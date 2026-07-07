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
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def webhook_agent(api_client: TrinityApiClient):
    """A real agent with a live `agent_ownership` row, for the whole module.

    #1445: schedule/webhook creation is now gated on a live ownership row, so
    every test that calls `_create_test_schedule` (asserts 201) needs a real
    agent, not the name-only `test_agent_name` fixture (which mints no
    ownership row → orphan schedules whose tokens 404). Mirrors the #1423
    throwaway-agent pattern: `POST /api/agents` and use it immediately — a
    webhook 202 needs only the ownership row + scheduler, NOT a running
    container, so there is no flaky running-wait. One agent per module.
    """
    agent = f"test-webhook-{uuid.uuid4().hex[:6]}"
    created = api_client.post("/api/agents", json={"name": agent})
    if created.status_code not in (200, 201):
        pytest.skip(f"could not create webhook test agent: {created.text}")
    try:
        yield agent
    finally:
        api_client.delete(f"/api/agents/{agent}")


# ---------------------------------------------------------------------------
# Webhook CRUD (authenticated)
# ---------------------------------------------------------------------------

class TestWebhookGeneration:
    """Webhook token generation, status, and revocation endpoints."""

    def test_generate_webhook_creates_token(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
            )
            assert_status(resp, 200)
            data = assert_json_response(resp)
            assert_has_fields(data, ["schedule_id", "has_token", "webhook_enabled", "webhook_url"])
            assert data["has_token"] is True
            assert data["webhook_enabled"] is True
            assert "/api/webhooks/" in data["webhook_url"]
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_generate_webhook_twice_changes_url(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            r1 = api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            r2 = api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            assert_status(r1, 200)
            assert_status(r2, 200)
            assert r1.json()["webhook_url"] != r2.json()["webhook_url"]
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_get_webhook_status_no_token(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            resp = api_client.get(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            assert_status(resp, 200)
            data = resp.json()
            assert data["has_token"] is False
            assert data["webhook_enabled"] is False
            assert data["webhook_url"] is None
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_get_webhook_status_after_generate(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            resp = api_client.get(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            assert_status(resp, 200)
            data = resp.json()
            assert data["has_token"] is True
            assert data["webhook_enabled"] is True
            assert data["webhook_url"] is not None
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_revoke_webhook_clears_token(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            resp_del = api_client.delete(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
            )
            assert_status(resp_del, 204)
            resp_get = api_client.get(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
            )
            data = resp_get.json()
            assert data["has_token"] is False
            assert data["webhook_url"] is None
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_generate_webhook_unknown_schedule_404(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        resp = api_client.post(
            f"/api/agents/{webhook_agent}/schedules/nonexistent-id/webhook"
        )
        assert_status(resp, 404)

    def test_webhook_endpoints_require_auth(self, webhook_agent: str):
        import httpx
        base = "http://localhost:8000"
        r = httpx.post(
            f"{base}/api/agents/{webhook_agent}/schedules/any-id/webhook"
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
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
            )
            webhook_url = gen_resp.json()["webhook_url"]
            token = webhook_url.split("/api/webhooks/")[1]

            # Revoke → disabled
            api_client.delete(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")

            resp = httpx.post(f"http://localhost:8000/api/webhooks/{token}")
            assert resp.status_code == 404  # token no longer exists in DB
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_valid_webhook_returns_202(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
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
            _delete_schedule(api_client, webhook_agent, sid)

    def test_webhook_with_context_body(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
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
            _delete_schedule(api_client, webhook_agent, sid)

    def test_webhook_context_too_long_rejected(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
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
            _delete_schedule(api_client, webhook_agent, sid)

    def test_oversized_body_returns_413(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        """A body over the size cap is rejected (413) before it is parsed (#1424)."""
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            gen_resp = api_client.post(
                f"/api/agents/{webhook_agent}/schedules/{sid}/webhook"
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
            _delete_schedule(api_client, webhook_agent, sid)

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
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            r1 = api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")
            old_url = r1.json()["webhook_url"]
            old_token = old_url.split("/api/webhooks/")[1]

            # Regenerate
            api_client.post(f"/api/agents/{webhook_agent}/schedules/{sid}/webhook")

            resp = httpx.post(f"http://localhost:8000/api/webhooks/{old_token}")
            assert resp.status_code == 404
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def _fresh_webhook_token(self, api_client, agent, sid):
        return api_client.post(
            f"/api/agents/{agent}/schedules/{sid}/webhook"
        ).json()["webhook_url"].split("/api/webhooks/")[1]

    def test_bodyless_repeats_are_not_deduped(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        """#1422 (AC a): two body-less pings seconds apart each fire — no silent
        24h replay. Pre-fix the 2nd carried X-Idempotent-Replay and did nothing."""
        import httpx
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            token = self._fresh_webhook_token(api_client, webhook_agent, sid)
            r1 = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            r2 = httpx.post(f"http://localhost:8000/api/webhooks/{token}", timeout=15.0)
            assert r1.status_code in (202, 503) and r2.status_code in (202, 503)
            # Neither key-less delivery is a replay — each is a fresh trigger.
            assert "x-idempotent-replay" not in {k.lower() for k in r1.headers}
            assert "x-idempotent-replay" not in {k.lower() for k in r2.headers}
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

    def test_explicit_key_resend_is_deduped(
        self, api_client: TrinityApiClient, webhook_agent: str
    ):
        """#1422 (AC b): a resend with the same explicit Idempotency-Key replays
        the first result (X-Idempotent-Replay) — anti-double-fire preserved."""
        import httpx
        import uuid as _uuid
        schedule = _create_test_schedule(api_client, webhook_agent)
        sid = schedule["id"]
        try:
            token = self._fresh_webhook_token(api_client, webhook_agent, sid)
            key = f"ci-{_uuid.uuid4().hex}"
            r1 = httpx.post(
                f"http://localhost:8000/api/webhooks/{token}",
                headers={"Idempotency-Key": key}, timeout=15.0,
            )
            # Only assert dedup when the first call actually fired (202); if the
            # scheduler is down (503) no claim completes and there's nothing to replay.
            if r1.status_code == 202:
                r2 = httpx.post(
                    f"http://localhost:8000/api/webhooks/{token}",
                    headers={"Idempotency-Key": key}, timeout=15.0,
                )
                assert r2.headers.get("X-Idempotent-Replay") == "true", (
                    f"explicit-key resend should replay, got {r2.status_code} {dict(r2.headers)}"
                )
            else:
                assert r1.status_code == 503
        finally:
            _delete_schedule(api_client, webhook_agent, sid)

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


# ---------------------------------------------------------------------------
# Creation gate — no orphan schedules (#1445)
# ---------------------------------------------------------------------------

class TestWebhookCreationGate:
    """#1445: schedule/webhook creation is gated on a live owning agent.

    An admin's `can_user_access_agent` returns True unconditionally, so before
    this gate an admin could mint a schedule (and a real webhook token) on a
    never-created agent — a token that then 404s deterministically at the
    #1423 INNER-JOIN token lookup. The gate rejects creation up front, so a
    webhook token always resolves to a schedule of a live agent.
    """

    def test_create_schedule_on_missing_agent_returns_404(
        self, api_client: TrinityApiClient
    ):
        """Creating a schedule on a never-created agent now 404s (was 201 →
        orphan). This is the exact fixture artifact behind the #1445 flaky
        webhook 404s."""
        ghost = f"test-1445-ghost-{uuid.uuid4().hex[:6]}"
        data = {
            "name": f"gate-{uuid.uuid4().hex[:8]}",
            "cron_expression": "0 0 1 1 *",
            "message": "should never be created",
            "enabled": True,
            "timezone": "UTC",
        }
        resp = api_client.post(f"/api/agents/{ghost}/schedules", json=data)
        assert resp.status_code == 404, (
            f"schedule on a non-existent agent must 404, got {resp.status_code}"
        )

    def test_generate_webhook_on_soft_deleted_agent_returns_404(
        self, api_client: TrinityApiClient
    ):
        """Defense-in-depth: minting a token for a schedule whose agent is no
        longer live 404s (a token that would 404 at trigger time is never
        issued). Exercised via soft-delete since the gate prevents creating an
        orphan schedule directly."""
        agent = f"test-1445-del-{uuid.uuid4().hex[:6]}"
        created = api_client.post("/api/agents", json={"name": agent})
        if created.status_code not in (200, 201):
            pytest.skip(f"could not create throwaway agent: {created.text}")

        sid = None
        try:
            sid = _create_test_schedule(api_client, agent)["id"]
            # Soft-delete the agent → is_agent_live(name) is now False.
            assert api_client.delete(f"/api/agents/{agent}").status_code in (200, 204)

            resp = api_client.post(
                f"/api/agents/{agent}/schedules/{sid}/webhook"
            )
            assert resp.status_code == 404, (
                f"generate_webhook on a soft-deleted agent must 404, got {resp.status_code}"
            )
        finally:
            api_client.delete(f"/api/agents/{agent}")
