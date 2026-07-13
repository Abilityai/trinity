"""
Test resource cleanup utilities.

Ensures test resources are properly cleaned up after tests.

CRED-002 Note: Credential cleanup functions removed. Credentials are now
files stored in agent containers and are cleaned up when agents are deleted.
"""

import re
import sys
from urllib.parse import urlparse
from typing import List, Set
from .api_client import TrinityApiClient

# Pattern for test resources
TEST_RESOURCE_PATTERN = re.compile(r"^test-api-.*")

# ---------------------------------------------------------------------------
# #1558: agent-deletion safety.
#
# The suite creates ephemeral agents and must be able to reclaim them without
# ever touching a user's real agents. Two guarantees:
#   1. Every agent the suite creates is named with THIS prefix — a namespace no
#      human would use — so a crashed-run sweep can prove an agent is ours by
#      name alone (NOT the broad, collision-prone `test-`).
#   2. Agents created in the current session are registered in a set and torn
#      down explicitly by name.
# ---------------------------------------------------------------------------
EPHEMERAL_AGENT_PREFIX = "pytest-ephemeral-"

# Names of agents this pytest session created (registered at creation time).
_SESSION_CREATED_AGENTS: Set[str] = set()

# Hosts we consider "local" for the opt-in destructive sweep. Anything else
# (staging/prod) is refused so pointing the suite at a real instance can never
# delete its agents.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def register_created_agent(name: str) -> None:
    """Record an agent the session created, so it can be torn down by name."""
    if name:
        _SESSION_CREATED_AGENTS.add(name)


def session_created_agents() -> Set[str]:
    """Snapshot of agents this session created (for end-of-session teardown)."""
    return set(_SESSION_CREATED_AGENTS)


def is_session_created(name: str) -> bool:
    """True if the session registered ``name`` as one it created."""
    return name in _SESSION_CREATED_AGENTS


def is_local_target(base_url: str) -> bool:
    """True only when ``base_url`` points at a local instance.

    The destructive leftover-sweep refuses to run against anything else so the
    suite can never delete agents on staging/production (#1558).
    """
    try:
        host = urlparse(base_url).hostname
    except Exception:
        return False
    return (host or "") in _LOCAL_HOSTS


def is_suite_owned_agent(name: str) -> bool:
    """True if ``name`` was provably created by the suite — either registered
    this session, or carrying the dedicated ephemeral prefix (which no user
    agent would use). Never matches the broad ``test-`` prefix (#1558)."""
    return is_session_created(name) or name.startswith(EPHEMERAL_AGENT_PREFIX)


def select_sweepable_agents(
    agent_names: List[str],
    base_url: str,
    *,
    enabled: bool,
) -> List[str]:
    """Pure policy for the startup leftover-sweep (#1558) — no network.

    Returns the subset of ``agent_names`` the suite may safely delete:
      - empty unless the sweep is explicitly ``enabled`` (opt-in env flag), AND
      - empty unless ``base_url`` is local (never sweep staging/prod), AND
      - only names the suite provably owns (``is_suite_owned_agent``).

    A pre-existing ``test-``-prefixed *user* agent is therefore NEVER returned.
    Unit-tested in tests/unit/test_1558_conftest_agent_sweep_guard.py.
    """
    if not enabled or not is_local_target(base_url):
        return []
    return [n for n in agent_names if is_suite_owned_agent(n)]


def get_test_agents(client: TrinityApiClient) -> List[str]:
    """Get list of test agent names that should be cleaned up."""
    response = client.get("/api/agents")
    if response.status_code != 200:
        return []

    agents = response.json()
    return [
        agent["name"]
        for agent in agents
        if TEST_RESOURCE_PATTERN.match(agent["name"])
    ]


def cleanup_test_agent(
    client: TrinityApiClient, name: str, *, require_suite_owned: bool = False
) -> bool:
    """Delete a test agent. Returns True if successful.

    #1558: pass ``require_suite_owned=True`` (used by any sweep over agents the
    caller did not create by name) to refuse deleting an agent the session
    cannot prove it owns — a guard against destroying a user's real agent.
    Explicit teardown of a just-created agent passes the default (False).
    """
    if require_suite_owned and not is_suite_owned_agent(name):
        print(
            f"[cleanup] refusing to delete '{name}': not created by this "
            f"session and lacks the '{EPHEMERAL_AGENT_PREFIX}' prefix (#1558)",
            file=sys.stderr,
        )
        return False

    # First try to stop if running
    client.post(f"/api/agents/{name}/stop")

    # Then delete
    response = client.delete(f"/api/agents/{name}")
    return response.status_code in [200, 204, 404]


def cleanup_all_test_agents(client: TrinityApiClient) -> int:
    """Clean up all test agents. Returns count of deleted agents."""
    agents = get_test_agents(client)
    count = 0
    for name in agents:
        if cleanup_test_agent(client, name):
            count += 1
    return count


def get_test_mcp_keys(client: TrinityApiClient) -> List[str]:
    """Get list of test MCP API key IDs that should be cleaned up."""
    response = client.get("/api/mcp/keys")
    if response.status_code != 200:
        return []

    keys = response.json()
    return [
        key["id"]
        for key in keys
        if key.get("name", "").startswith("test-api-")
    ]


def cleanup_test_mcp_key(client: TrinityApiClient, key_id: str) -> bool:
    """Delete a test MCP API key. Returns True if successful."""
    response = client.delete(f"/api/mcp/keys/{key_id}")
    return response.status_code in [200, 204, 404]


def cleanup_all_test_resources(client: TrinityApiClient) -> dict:
    """Clean up all test resources. Returns summary of deleted items."""
    return {
        "agents_deleted": cleanup_all_test_agents(client),
        "mcp_keys_deleted": len([
            key_id for key_id in get_test_mcp_keys(client)
            if cleanup_test_mcp_key(client, key_id)
        ]),
    }


class ResourceTracker:
    """Track created resources for cleanup."""

    def __init__(self):
        self.agents: Set[str] = set()
        self.mcp_keys: Set[str] = set()
        self.schedules: Set[tuple] = set()  # (agent_name, schedule_id)

    def track_agent(self, name: str):
        """Track an agent for cleanup."""
        self.agents.add(name)

    def track_credential(self, cred_id: str):
        """
        Legacy method - credentials are now cleaned up with agents.
        Kept for backward compatibility but does nothing.
        """
        pass

    def track_mcp_key(self, key_id: str):
        """Track an MCP key for cleanup."""
        self.mcp_keys.add(key_id)

    def track_schedule(self, agent_name: str, schedule_id: str):
        """Track a schedule for cleanup."""
        self.schedules.add((agent_name, schedule_id))

    def cleanup(self, client: TrinityApiClient) -> dict:
        """Clean up all tracked resources."""
        results = {
            "agents": 0,
            "mcp_keys": 0,
            "schedules": 0,
        }

        # Clean schedules first (they depend on agents)
        for agent_name, schedule_id in self.schedules:
            resp = client.delete(f"/api/agents/{agent_name}/schedules/{schedule_id}")
            if resp.status_code in [200, 204, 404]:
                results["schedules"] += 1

        # Clean agents (also cleans up credential files inside agents)
        for name in self.agents:
            if cleanup_test_agent(client, name):
                results["agents"] += 1

        # Clean MCP keys
        for key_id in self.mcp_keys:
            if cleanup_test_mcp_key(client, key_id):
                results["mcp_keys"] += 1

        # Reset tracking
        self.agents.clear()
        self.mcp_keys.clear()
        self.schedules.clear()

        return results
