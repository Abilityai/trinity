"""
Tests for broken access control fix (Issue #174).

Verifies that non-admin users cannot access admin-only endpoints,
and that non-owner users cannot access owner-only agent endpoints.

These tests create a temporary non-admin user in the database,
authenticate as that user, and verify 403 responses on hardened endpoints.
"""

import os
import uuid
import subprocess
import pytest
import httpx

from utils.api_client import TrinityApiClient, ApiConfig
from utils.assertions import assert_status, assert_status_in


# =============================================================================
# Fixtures
# =============================================================================

BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
TEST_USER_USERNAME = f"testuser-{uuid.uuid4().hex[:8]}"
TEST_USER_PASSWORD = "test-password-174"
TEST_USER_EMAIL = f"{TEST_USER_USERNAME}@test.example.com"


def _run_in_backend(python_code: str) -> str:
    """Execute Python code inside the backend container and return stdout."""
    result = subprocess.run(
        ["docker", "exec", BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    return result.stdout.strip()


def _create_test_user(username: str, password: str, email: str):
    """Create a non-admin user in the backend database via docker exec."""
    # Single Python script that hashes password and inserts the user
    code = f"""
import sqlite3, os
from pathlib import Path
from passlib.context import CryptContext

ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
password_hash = ctx.hash("{password}")
db_path = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))

conn = sqlite3.connect(db_path)
try:
    conn.execute(
        "INSERT OR IGNORE INTO users (username, password_hash, role, email, created_at, updated_at) "
        "VALUES (?, ?, 'user', ?, datetime('now'), datetime('now'))",
        ("{username}", password_hash, "{email}"),
    )
    conn.commit()
    print("OK")
finally:
    conn.close()
"""
    output = _run_in_backend(code)
    if "OK" not in output:
        raise RuntimeError(f"Failed to create test user: {output}")


def _delete_test_user(username: str):
    """Delete the test user from the backend database."""
    code = f"""
import sqlite3, os
from pathlib import Path

db_path = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db_path)
try:
    conn.execute("DELETE FROM users WHERE username = ?", ("{username}",))
    conn.commit()
    print("OK")
finally:
    conn.close()
"""
    try:
        _run_in_backend(code)
    except Exception:
        pass  # Best-effort cleanup


@pytest.fixture(scope="module")
def regular_user_credentials():
    """Create a non-admin user directly in the database.

    Returns (username, password) tuple.
    Cleans up after all tests in this module.
    """
    _create_test_user(TEST_USER_USERNAME, TEST_USER_PASSWORD, TEST_USER_EMAIL)

    yield (TEST_USER_USERNAME, TEST_USER_PASSWORD)

    _delete_test_user(TEST_USER_USERNAME)


@pytest.fixture(scope="module")
def regular_user_client(regular_user_credentials):
    """Create an authenticated API client for the non-admin user."""
    username, password = regular_user_credentials
    config = ApiConfig(
        base_url=os.getenv("TRINITY_API_URL", "http://localhost:8000"),
        username=username,
        password=password,
    )
    client = TrinityApiClient(config)
    client.authenticate()
    yield client
    client.close()


@pytest.fixture(scope="module")
def admin_client():
    """Create an authenticated admin API client."""
    config = ApiConfig.from_env()
    client = TrinityApiClient(config)
    client.authenticate()
    yield client
    client.close()


@pytest.fixture(scope="module")
def admin_owned_agent(admin_client):
    """Create an agent owned by admin (not accessible to regular user).

    The regular user is NOT shared on this agent, so they should get 403.
    """
    agent_name = f"test-acl-{uuid.uuid4().hex[:6]}"
    response = admin_client.post("/api/agents", json={"name": agent_name})
    if response.status_code not in [200, 201]:
        pytest.skip(f"Failed to create test agent: {response.text}")

    # Wait for it to exist (no need to wait for running)
    import time
    time.sleep(5)

    yield agent_name

    # Cleanup
    try:
        admin_client.post(f"/api/agents/{agent_name}/stop")
        time.sleep(2)
    except Exception:
        pass
    admin_client.delete(f"/api/agents/{agent_name}")


# =============================================================================
# Test: Non-admin user gets 403 on admin-only endpoints
# =============================================================================


class TestAdminOnlyEndpoints:
    """Verify non-admin users get 403 on admin-only endpoints."""

    # -- ops.py endpoints --

    def test_fleet_status_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/fleet/status")
        assert_status(response, 403)

    def test_fleet_health_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/fleet/health")
        assert_status(response, 403)

    def test_schedules_list_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/schedules")
        assert_status(response, 403)

    def test_ops_costs_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/costs")
        assert_status(response, 403)

    def test_auth_report_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/auth-report")
        assert_status(response, 403)

    def test_ops_alerts_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/ops/alerts")
        assert_status(response, 403)

    def test_ops_alert_acknowledge_requires_admin(self, regular_user_client):
        response = regular_user_client.post("/api/ops/alerts/fake-id/acknowledge")
        assert_status(response, 403)

    # -- observability.py endpoints --

    def test_observability_metrics_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/observability/metrics")
        assert_status(response, 403)

    def test_observability_status_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/observability/status")
        assert_status(response, 403)

    # -- system_agent.py endpoints --

    def test_system_agent_status_requires_admin(self, regular_user_client):
        response = regular_user_client.get("/api/system-agent/status")
        assert_status(response, 403)

    # -- alerts.py threshold endpoints --

    @pytest.mark.skip(reason="Process Engine archived — /api/alerts/thresholds endpoints removed")
    def test_set_threshold_requires_admin(self, regular_user_client):
        response = regular_user_client.put(
            "/api/alerts/thresholds/fake-process-id",
            json={"threshold_type": "daily", "amount": 10.0},
        )
        assert_status(response, 403)

    @pytest.mark.skip(reason="Process Engine archived — /api/alerts/thresholds endpoints removed")
    def test_delete_threshold_requires_admin(self, regular_user_client):
        response = regular_user_client.delete(
            "/api/alerts/thresholds/fake-process-id/daily"
        )
        assert_status(response, 403)


# =============================================================================
# Test: Non-owner user gets 403 on owner-only agent endpoints
# =============================================================================


class TestOwnerOnlyEndpoints:
    """Verify non-owner users are denied on owner-only agent endpoints.

    Uses an agent created by admin that the regular user does NOT own
    and is NOT shared with.

    #186: these endpoints route through the OwnedAgent(_by_name) dependency,
    which now returns a uniform 404 for both a non-existent and an existing-but-
    unowned agent (was 403) so agent existence can't be enumerated.
    """

    # -- credentials.py (inject/export/import) --

    def test_credential_inject_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/credentials/inject",
            json={"files": {".env": "TEST=1"}},
        )
        assert_status(response, 404)

    def test_credential_export_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/credentials/export",
        )
        assert_status(response, 404)

    def test_credential_import_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/credentials/import",
        )
        assert_status(response, 404)

    # -- chat.py (delete history) --

    def test_delete_chat_history_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.delete(
            f"/api/agents/{admin_owned_agent}/chat/history"
        )
        assert_status(response, 404)

    # -- agents.py (queue mutations) --

    def test_queue_clear_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/queue/clear"
        )
        assert_status(response, 404)

    def test_queue_release_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/queue/release"
        )
        assert_status(response, 404)

    # -- skills.py (write operations) --

    def test_update_skills_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.put(
            f"/api/agents/{admin_owned_agent}/skills",
            json={"skills": []},
        )
        assert_status(response, 404)

    def test_assign_skill_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/skills/some-skill",
        )
        assert_status(response, 404)

    def test_unassign_skill_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.delete(
            f"/api/agents/{admin_owned_agent}/skills/some-skill",
        )
        assert_status(response, 404)

    def test_inject_skills_requires_owner(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.post(
            f"/api/agents/{admin_owned_agent}/skills/inject",
        )
        assert_status(response, 404)


# =============================================================================
# Test: Non-owner read access denied on agent-scoped read endpoints
# =============================================================================


class TestAgentAccessCheckEndpoints:
    """Verify non-owner/non-shared users are denied on agent-scoped read endpoints.

    #186: these route through the AuthorizedAgent(_by_name) dependency, which now
    returns a uniform 404 for both a non-existent and an inaccessible agent (was
    403) so agent existence can't be enumerated.
    """

    def test_agent_stats_requires_access(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.get(
            f"/api/agents/{admin_owned_agent}/stats"
        )
        assert_status(response, 404)

    def test_agent_queue_requires_access(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.get(
            f"/api/agents/{admin_owned_agent}/queue"
        )
        assert_status(response, 404)

    def test_get_agent_skills_requires_access(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.get(
            f"/api/agents/{admin_owned_agent}/skills"
        )
        assert_status(response, 404)

    def test_get_agent_detail_requires_access(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.get(
            f"/api/agents/{admin_owned_agent}"
        )
        assert_status(response, 404)

    def test_agent_logs_requires_access(
        self, regular_user_client, admin_owned_agent
    ):
        response = regular_user_client.get(
            f"/api/agents/{admin_owned_agent}/logs"
        )
        assert_status(response, 404)


# =============================================================================
# Test: Non-admin user CAN still access their own data
# =============================================================================


class TestRegularUserCanAccessOwnResources:
    """Verify a non-admin user can still list their own agents and use non-admin endpoints."""

    def test_regular_user_can_list_agents(self, regular_user_client):
        """Non-admin user can list agents (returns only their own)."""
        response = regular_user_client.get("/api/agents")
        assert_status(response, 200)

    def test_regular_user_can_list_skills_library(self, regular_user_client):
        """Skills library listing is not admin-only."""
        response = regular_user_client.get("/api/skills/library")
        assert_status(response, 200)

    def test_regular_user_can_list_notifications(self, regular_user_client):
        """Notifications listing works but returns only accessible agents' notifications."""
        response = regular_user_client.get("/api/notifications")
        assert_status(response, 200)


# =============================================================================
# Test: Unauthenticated access denied on approvals endpoints (Issue #173)
# =============================================================================


@pytest.mark.skip(reason="Process Engine archived — /api/approvals endpoints removed")
class TestApprovalsRequireAuth:
    """Verify unauthenticated users get 401 on all approval endpoints."""

    def test_list_approvals_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get("/api/approvals", auth=False)
        assert_status(response, 401)

    def test_get_approval_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get("/api/approvals/fake-id", auth=False)
        assert_status(response, 401)

    def test_get_approval_by_step_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get(
            "/api/approvals/execution/fake-exec/step/fake-step", auth=False
        )
        assert_status(response, 401)

    def test_approve_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.post(
            "/api/approvals/fake-id/approve",
            json={"comment": "malicious approval"},
            auth=False,
        )
        assert_status(response, 401)

    def test_reject_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.post(
            "/api/approvals/fake-id/reject",
            json={"comment": "malicious rejection"},
            auth=False,
        )
        assert_status(response, 401)


# =============================================================================
# Test: Unauthenticated access denied on trigger endpoints (Issue #173)
# =============================================================================


@pytest.mark.skip(reason="Process Engine archived — /api/triggers endpoints removed")
class TestTriggersRequireAuth:
    """Verify unauthenticated users get 401 on trigger management endpoints."""

    def test_list_triggers_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get("/api/triggers", auth=False)
        assert_status(response, 401)

    def test_get_trigger_info_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get(
            "/api/triggers/webhook/fake-id/info", auth=False
        )
        assert_status(response, 401)

    def test_list_schedule_triggers_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get("/api/triggers/schedules", auth=False)
        assert_status(response, 401)

    def test_get_schedule_trigger_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get(
            "/api/triggers/schedules/fake-id", auth=False
        )
        assert_status(response, 401)

    def test_list_process_schedules_requires_auth(self, unauthenticated_client):
        response = unauthenticated_client.get(
            "/api/triggers/process/fake-id/schedules", auth=False
        )
        assert_status(response, 401)


# =============================================================================
# Test: Authenticated users CAN access approvals and triggers (Issue #173)
# =============================================================================


@pytest.mark.skip(reason="Process Engine archived — /api/approvals and /api/triggers endpoints removed")
class TestAuthenticatedUsersCanAccessApprovalsAndTriggers:
    """Verify authenticated users get 200 (not 401) on these endpoints."""

    def test_authenticated_can_list_approvals(self, admin_client):
        response = admin_client.get("/api/approvals")
        assert_status(response, 200)

    def test_authenticated_can_list_triggers(self, admin_client):
        response = admin_client.get("/api/triggers")
        assert_status(response, 200)

    def test_authenticated_can_list_schedule_triggers(self, admin_client):
        response = admin_client.get("/api/triggers/schedules")
        assert_status(response, 200)
