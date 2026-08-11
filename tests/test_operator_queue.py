"""
Operator Queue Tests (test_operator_queue.py)

Tests for the Operating Room / Operator Queue API endpoints (OPS-001).
Covers list, get, respond, cancel, stats, agent-specific queries,
authentication, input validation, and cross-user access isolation (#470).

Feature Flow: operating-room.md
"""

import os
import subprocess
import pytest
import uuid
from datetime import datetime, timezone

from testkit.api_client import TrinityApiClient, ApiConfig
from testkit.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
)


# ============================================================================
# Helpers
# ============================================================================

def _insert_queue_item(api_client: TrinityApiClient, **overrides) -> dict:
    """Insert a queue item directly via the database for testing.

    Since there's no public POST endpoint for creating queue items (they come
    from agent containers via the sync service), we insert via a direct DB
    call exposed through a test-support endpoint, or we use the list/respond
    flow with pre-seeded data.

    For now, we use the internal admin endpoint pattern.
    """
    item_id = overrides.get("id", f"test-{uuid.uuid4().hex[:12]}")
    now = datetime.now(timezone.utc).isoformat()

    defaults = {
        "id": item_id,
        "agent_name": "test-agent",
        "type": "approval",
        "status": "pending",
        "priority": "medium",
        "title": "Test approval request",
        "question": "Should we proceed with this test?",
        "options": ["approve", "reject"],
        "context": {"test": True},
        "created_at": now,
    }
    defaults.update(overrides)
    return defaults


# ============================================================================
# Authentication Tests
# ============================================================================

class TestOperatorQueueAuthentication:
    """Tests for operator queue endpoint authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_list_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/operator-queue requires authentication."""
        response = unauthenticated_client.get("/api/operator-queue", auth=False)
        assert_status(response, 401)

    def test_stats_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/operator-queue/stats requires authentication."""
        response = unauthenticated_client.get("/api/operator-queue/stats", auth=False)
        assert_status(response, 401)

    def test_get_item_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/operator-queue/{id} requires authentication."""
        response = unauthenticated_client.get(
            "/api/operator-queue/test-item-123", auth=False
        )
        assert_status(response, 401)

    def test_respond_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/operator-queue/{id}/respond requires authentication."""
        response = unauthenticated_client.post(
            "/api/operator-queue/test-item-123/respond",
            json={"response": "approve"},
            auth=False,
        )
        assert_status(response, 401)

    def test_cancel_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """POST /api/operator-queue/{id}/cancel requires authentication."""
        response = unauthenticated_client.post(
            "/api/operator-queue/test-item-123/cancel",
            auth=False,
        )
        assert_status(response, 401)

    def test_agent_items_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/operator-queue/agents/{name} requires authentication."""
        response = unauthenticated_client.get(
            "/api/operator-queue/agents/test-agent", auth=False
        )
        assert_status(response, 401)


# ============================================================================
# List Queue Items Tests
# ============================================================================

class TestListQueueItems:
    """Tests for GET /api/operator-queue endpoint."""

    pytestmark = pytest.mark.smoke

    def test_list_items_structure(self, api_client: TrinityApiClient):
        """List queue items returns expected structure."""
        response = api_client.get("/api/operator-queue")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["items", "count"])
        assert isinstance(data["items"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["items"])

    def test_list_items_respects_limit(self, api_client: TrinityApiClient):
        """List respects limit parameter."""
        response = api_client.get("/api/operator-queue?limit=2")
        assert_status(response, 200)
        data = response.json()
        assert len(data["items"]) <= 2

    def test_list_items_respects_offset(self, api_client: TrinityApiClient):
        """List respects offset parameter."""
        response = api_client.get("/api/operator-queue?offset=0&limit=500")
        assert_status(response, 200)
        all_items = response.json()["items"]

        if len(all_items) > 1:
            response2 = api_client.get("/api/operator-queue?offset=1&limit=500")
            assert_status(response2, 200)
            offset_items = response2.json()["items"]
            assert len(offset_items) == len(all_items) - 1

    def test_list_items_filter_by_status(self, api_client: TrinityApiClient):
        """List can filter by status parameter."""
        response = api_client.get("/api/operator-queue?status=pending")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["status"] == "pending"

    def test_list_items_filter_by_type(self, api_client: TrinityApiClient):
        """List can filter by type parameter."""
        response = api_client.get("/api/operator-queue?type=approval")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["type"] == "approval"

    def test_list_items_filter_by_priority(self, api_client: TrinityApiClient):
        """List can filter by priority parameter."""
        response = api_client.get("/api/operator-queue?priority=high")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["priority"] == "high"

    def test_list_items_filter_by_agent_name(self, api_client: TrinityApiClient):
        """List can filter by agent_name parameter."""
        response = api_client.get("/api/operator-queue?agent_name=oracle-1")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["agent_name"] == "oracle-1"

    def test_list_items_item_structure(self, api_client: TrinityApiClient):
        """Each item in list has expected fields."""
        response = api_client.get("/api/operator-queue?limit=5")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert_has_fields(item, [
                "id", "agent_name", "type", "status", "priority",
                "title", "question", "created_at"
            ])

    def test_list_items_invalid_limit(self, api_client: TrinityApiClient):
        """List with invalid limit returns 422."""
        response = api_client.get("/api/operator-queue?limit=0")
        assert_status(response, 422)

    def test_list_items_limit_too_large(self, api_client: TrinityApiClient):
        """List with limit > 500 returns 422."""
        response = api_client.get("/api/operator-queue?limit=501")
        assert_status(response, 422)


# ============================================================================
# Get Queue Item Tests
# ============================================================================

class TestGetQueueItem:
    """Tests for GET /api/operator-queue/{item_id} endpoint."""

    pytestmark = pytest.mark.smoke

    def test_get_item_not_found(self, api_client: TrinityApiClient):
        """Get non-existent queue item returns 404."""
        response = api_client.get("/api/operator-queue/nonexistent-item-id")
        assert_status(response, 404)

    def test_get_existing_item(self, api_client: TrinityApiClient):
        """Get existing queue item returns full item data."""
        # List items first to find one
        list_response = api_client.get("/api/operator-queue?limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.get(f"/api/operator-queue/{item_id}")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "id", "agent_name", "type", "status", "priority",
            "title", "question", "created_at"
        ])
        assert data["id"] == item_id


# ============================================================================
# Queue Stats Tests
# ============================================================================

class TestQueueStats:
    """Tests for GET /api/operator-queue/stats endpoint."""

    pytestmark = pytest.mark.smoke

    def test_stats_structure(self, api_client: TrinityApiClient):
        """Stats endpoint returns expected structure."""
        response = api_client.get("/api/operator-queue/stats")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, [
            "by_status", "by_type", "by_priority", "by_agent",
            "avg_response_seconds", "responded_today"
        ])

    def test_stats_by_status_is_dict(self, api_client: TrinityApiClient):
        """Stats by_status is a dict with count values."""
        response = api_client.get("/api/operator-queue/stats")
        data = response.json()
        assert isinstance(data["by_status"], dict)
        for key, value in data["by_status"].items():
            assert isinstance(value, int)

    def test_stats_by_type_is_dict(self, api_client: TrinityApiClient):
        """Stats by_type is a dict with count values."""
        response = api_client.get("/api/operator-queue/stats")
        data = response.json()
        assert isinstance(data["by_type"], dict)

    def test_stats_by_priority_is_dict(self, api_client: TrinityApiClient):
        """Stats by_priority is a dict with count values."""
        response = api_client.get("/api/operator-queue/stats")
        data = response.json()
        assert isinstance(data["by_priority"], dict)

    def test_stats_by_agent_is_dict(self, api_client: TrinityApiClient):
        """Stats by_agent is a dict with count values."""
        response = api_client.get("/api/operator-queue/stats")
        data = response.json()
        assert isinstance(data["by_agent"], dict)

    def test_stats_responded_today_is_int(self, api_client: TrinityApiClient):
        """Stats responded_today is an integer."""
        response = api_client.get("/api/operator-queue/stats")
        data = response.json()
        assert isinstance(data["responded_today"], int)
        assert data["responded_today"] >= 0


# ============================================================================
# Respond to Queue Item Tests
# ============================================================================

class TestRespondToQueueItem:
    """Tests for POST /api/operator-queue/{item_id}/respond endpoint."""

    pytestmark = pytest.mark.smoke

    def test_respond_not_found(self, api_client: TrinityApiClient):
        """Responding to non-existent item returns 404."""
        response = api_client.post(
            "/api/operator-queue/nonexistent-id/respond",
            json={"response": "approve"}
        )
        assert_status(response, 404)

    def test_respond_missing_body(self, api_client: TrinityApiClient):
        """Responding without body returns 422."""
        response = api_client.post(
            "/api/operator-queue/some-id/respond",
            json={}
        )
        assert_status(response, 422)

    def test_respond_to_pending_item(self, api_client: TrinityApiClient):
        """Respond to a pending item transitions it to responded."""
        # Find a pending item
        list_response = api_client.get("/api/operator-queue?status=pending&limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No pending queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.post(
            f"/api/operator-queue/{item_id}/respond",
            json={"response": "approve", "response_text": "Looks good"}
        )
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == item_id
        assert data["status"] == "responded"
        assert data["response"] == "approve"
        assert data["response_text"] == "Looks good"
        assert data["responded_at"] is not None
        assert data["responded_by_email"] is not None

    def test_respond_to_already_responded_item(self, api_client: TrinityApiClient):
        """Responding to an already-responded item returns 400."""
        # Find a responded item
        list_response = api_client.get("/api/operator-queue?status=responded&limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No responded queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.post(
            f"/api/operator-queue/{item_id}/respond",
            json={"response": "approve"}
        )
        assert_status(response, 400)

    def test_respond_with_response_text(self, api_client: TrinityApiClient):
        """Respond with optional response_text field."""
        list_response = api_client.get("/api/operator-queue?status=pending&limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No pending queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.post(
            f"/api/operator-queue/{item_id}/respond",
            json={
                "response": "reject",
                "response_text": "Not ready yet, needs more testing"
            }
        )
        assert_status(response, 200)
        data = response.json()
        assert data["response_text"] == "Not ready yet, needs more testing"


# ============================================================================
# Cancel Queue Item Tests
# ============================================================================

class TestCancelQueueItem:
    """Tests for POST /api/operator-queue/{item_id}/cancel endpoint."""

    pytestmark = pytest.mark.smoke

    def test_cancel_not_found(self, api_client: TrinityApiClient):
        """Cancelling non-existent item returns 404."""
        response = api_client.post(
            "/api/operator-queue/nonexistent-id/cancel"
        )
        assert_status(response, 404)

    def test_cancel_pending_item(self, api_client: TrinityApiClient):
        """Cancel a pending item transitions it to cancelled."""
        list_response = api_client.get("/api/operator-queue?status=pending&limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No pending queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.post(f"/api/operator-queue/{item_id}/cancel")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == item_id
        assert data["status"] == "cancelled"

    def test_cancel_already_responded_item(self, api_client: TrinityApiClient):
        """Cancelling an already-responded item returns 400."""
        list_response = api_client.get("/api/operator-queue?status=responded&limit=1")
        items = list_response.json().get("items", [])

        if not items:
            pytest.skip("No responded queue items available for testing")

        item_id = items[0]["id"]
        response = api_client.post(f"/api/operator-queue/{item_id}/cancel")
        assert_status(response, 400)


# ============================================================================
# Agent-Specific Queue Items Tests
# ============================================================================

class TestAgentQueueItems:
    """Tests for GET /api/operator-queue/agents/{agent_name} endpoint."""

    pytestmark = pytest.mark.smoke

    def test_agent_items_structure(self, api_client: TrinityApiClient):
        """Agent queue items returns expected structure."""
        response = api_client.get("/api/operator-queue/agents/oracle-1")
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["agent_name", "items", "count"])
        assert data["agent_name"] == "oracle-1"
        assert isinstance(data["items"], list)
        assert data["count"] == len(data["items"])

    def test_agent_items_filter_agent(self, api_client: TrinityApiClient):
        """All returned items belong to the requested agent."""
        response = api_client.get("/api/operator-queue/agents/oracle-1")
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["agent_name"] == "oracle-1"

    def test_agent_items_with_status_filter(self, api_client: TrinityApiClient):
        """Agent items support status filter."""
        response = api_client.get(
            "/api/operator-queue/agents/oracle-1?status=pending"
        )
        assert_status(response, 200)
        data = response.json()
        for item in data["items"]:
            assert item["status"] == "pending"
            assert item["agent_name"] == "oracle-1"

    def test_agent_items_nonexistent_agent(self, api_client: TrinityApiClient):
        """Querying items for non-existent agent returns empty list (not 404)."""
        response = api_client.get(
            "/api/operator-queue/agents/nonexistent-agent-xyz"
        )
        assert_status(response, 200)
        data = response.json()
        assert data["count"] == 0
        assert data["items"] == []

    def test_agent_items_respects_limit(self, api_client: TrinityApiClient):
        """Agent items respects limit parameter."""
        response = api_client.get(
            "/api/operator-queue/agents/oracle-1?limit=1"
        )
        assert_status(response, 200)
        data = response.json()
        assert len(data["items"]) <= 1


# ============================================================================
# Cross-User Access Isolation Tests (#470)
# ============================================================================

_BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
_ISO_USERNAME = f"testuser-opq-{uuid.uuid4().hex[:8]}"
_ISO_PASSWORD = "test-opq-password-470"
_ISO_EMAIL = f"{_ISO_USERNAME}@test.example.com"
_ISO_AGENT = f"test-opq-agent-{uuid.uuid4().hex[:6]}"
_ISO_ITEM_ID = f"test-opq-item-{uuid.uuid4().hex[:12]}"


def _exec_backend(python_code: str) -> str:
    result = subprocess.run(
        ["docker", "exec", _BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    return result.stdout.strip()


@pytest.fixture(scope="module")
def _iso_setup(api_client: TrinityApiClient):
    """Set up cross-user isolation fixtures.

    Creates:
    - A non-admin user (role='user') directly in the DB
    - A fake agent ownership row for a sentinel agent owned by admin
    - A queue item for that sentinel agent
    """
    now = datetime.now(timezone.utc).isoformat()

    # Create non-admin user
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
from passlib.context import CryptContext
ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
pw = ctx.hash("{_ISO_PASSWORD}")
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO users (username, password_hash, role, email, created_at, updated_at) "
    "VALUES (?, ?, 'user', ?, datetime('now'), datetime('now'))",
    ("{_ISO_USERNAME}", pw, "{_ISO_EMAIL}"),
)
conn.commit()
conn.close()
print("OK")
""")

    # Register fake agent ownership under admin's user id
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
conn.execute(
    "INSERT OR IGNORE INTO agent_ownership (agent_name, owner_id, created_at) VALUES (?, ?, datetime('now'))",
    ("{_ISO_AGENT}", admin_id),
)
conn.commit()
conn.close()
print("OK")
""")

    # Insert a queue item belonging to that agent
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO operator_queue "
    "(id, agent_name, type, status, priority, title, question, created_at) "
    "VALUES (?, ?, 'approval', 'pending', 'high', 'Test item 470', 'Proceed?', ?)",
    ("{_ISO_ITEM_ID}", "{_ISO_AGENT}", "{now}"),
)
conn.commit()
conn.close()
print("OK")
""")

    # Build the non-admin client
    cfg = ApiConfig(
        base_url=os.getenv("TRINITY_API_URL", "http://localhost:8000"),
        username=_ISO_USERNAME,
        password=_ISO_PASSWORD,
    )
    non_admin = TrinityApiClient(cfg)
    non_admin.authenticate()

    yield {"admin": api_client, "non_admin": non_admin, "item_id": _ISO_ITEM_ID, "agent": _ISO_AGENT}

    non_admin.close()

    # Teardown: remove queue item, agent ownership, user
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute("DELETE FROM operator_queue WHERE id = ?", ("{_ISO_ITEM_ID}",))
conn.execute("DELETE FROM agent_ownership WHERE agent_name = ?", ("{_ISO_AGENT}",))
conn.execute("DELETE FROM users WHERE username = ?", ("{_ISO_USERNAME}",))
conn.commit()
conn.close()
print("OK")
""")


class TestOperatorQueueAccessControl:
    """Cross-user isolation — fixes the pentest finding in issue #470.

    A non-admin user must not be able to read or act on queue items that
    belong to agents they do not own or have been shared on.
    """

    pytestmark = pytest.mark.smoke

    def test_non_admin_list_excludes_foreign_items(self, _iso_setup):
        """Non-admin list must not include items from unowned agents."""
        non_admin = _iso_setup["non_admin"]
        item_id = _iso_setup["item_id"]

        response = non_admin.get("/api/operator-queue?limit=500")
        assert_status(response, 200)
        data = response.json()
        ids = {item["id"] for item in data["items"]}
        assert item_id not in ids, "Non-admin must not see items from unowned agents"

    def test_non_admin_get_item_returns_403(self, _iso_setup):
        """Non-admin GET /{id} for unowned agent's item returns 403."""
        non_admin = _iso_setup["non_admin"]
        item_id = _iso_setup["item_id"]

        response = non_admin.get(f"/api/operator-queue/{item_id}")
        assert_status(response, 403)

    def test_non_admin_respond_returns_403(self, _iso_setup):
        """Non-admin POST /{id}/respond for unowned agent's item returns 403."""
        non_admin = _iso_setup["non_admin"]
        item_id = _iso_setup["item_id"]

        response = non_admin.post(
            f"/api/operator-queue/{item_id}/respond",
            json={"response": "approve"},
        )
        assert_status(response, 403)

    def test_non_admin_cancel_returns_403(self, _iso_setup):
        """Non-admin POST /{id}/cancel for unowned agent's item returns 403."""
        non_admin = _iso_setup["non_admin"]
        item_id = _iso_setup["item_id"]

        response = non_admin.post(f"/api/operator-queue/{item_id}/cancel")
        assert_status(response, 403)

    def test_non_admin_agent_items_returns_403(self, _iso_setup):
        """Non-admin GET /agents/{name} for unowned agent returns 403."""
        non_admin = _iso_setup["non_admin"]
        agent = _iso_setup["agent"]

        response = non_admin.get(f"/api/operator-queue/agents/{agent}")
        assert_status(response, 403)

    def test_non_admin_stats_excludes_foreign_agents(self, _iso_setup):
        """Non-admin stats by_agent must not reveal unowned agent names."""
        non_admin = _iso_setup["non_admin"]
        agent = _iso_setup["agent"]

        response = non_admin.get("/api/operator-queue/stats")
        assert_status(response, 200)
        data = response.json()
        assert agent not in data.get("by_agent", {}), \
            "Stats by_agent must not leak unowned agent names"

    def test_admin_still_sees_all_items(self, _iso_setup):
        """Admin retains full visibility of all queue items (regression)."""
        admin = _iso_setup["admin"]
        item_id = _iso_setup["item_id"]

        response = admin.get(f"/api/operator-queue/{item_id}")
        assert_status(response, 200)
        data = response.json()
        assert data["id"] == item_id

    def test_admin_stats_includes_all_agents(self, _iso_setup):
        """Admin stats by_agent includes items from all agents (regression)."""
        admin = _iso_setup["admin"]
        agent = _iso_setup["agent"]

        response = admin.get("/api/operator-queue/stats")
        assert_status(response, 200)
        data = response.json()
        assert agent in data.get("by_agent", {}), \
            "Admin stats must include the test agent"
