"""Integration test for reset-to-main-preserve-state (S3, #384).

Requires a running Trinity stack with a git-initialised test agent. Skipped
in the default test run; set these env vars to opt in:

    TRINITY_API_URL             e.g. http://localhost:8000
    TRINITY_TEST_TOKEN          JWT or MCP API key with access to the agent
    TRINITY_GIT_TEST_AGENT      name of an agent with .git initialised

The test exercises one round-trip through the real stack: the endpoint
response is either 200 (happy path — agent idle, repo healthy) or one of
the known 409 variants (agent_busy / no_git_config / no_remote_main).
Any other status indicates a regression.
"""
import os

import httpx
import pytest


TRINITY_API_URL = os.environ.get("TRINITY_API_URL")
TRINITY_TEST_TOKEN = os.environ.get("TRINITY_TEST_TOKEN")
GIT_TEST_AGENT = os.environ.get("TRINITY_GIT_TEST_AGENT")

pytestmark = pytest.mark.skipif(
    not (TRINITY_API_URL and TRINITY_TEST_TOKEN and GIT_TEST_AGENT),
    reason=(
        "integration test: set TRINITY_API_URL / TRINITY_TEST_TOKEN / "
        "TRINITY_GIT_TEST_AGENT to opt in"
    ),
)


_KNOWN_CONFLICT_TYPES = {"agent_busy", "no_git_config", "no_remote_main"}


@pytest.mark.slow
def test_reset_preserve_state_end_to_end():
    """POST the endpoint and assert a known response class."""
    url = (
        f"{TRINITY_API_URL}/api/agents/{GIT_TEST_AGENT}"
        "/git/reset-to-main-preserve-state"
    )
    headers = {"Authorization": f"Bearer {TRINITY_TEST_TOKEN}"}

    response = httpx.post(url, headers=headers, timeout=180.0)

    assert response.status_code in (200, 409), (
        f"Unexpected status {response.status_code}: {response.text[:500]}"
    )

    if response.status_code == 200:
        data = response.json()
        assert data.get("success") is True
        assert "commit_sha" in data
        assert "snapshot_dir" in data
        assert data["working_branch"].startswith("trinity/")
    else:
        conflict = response.headers.get("X-Conflict-Type", "")
        assert conflict in _KNOWN_CONFLICT_TYPES, (
            f"Unknown X-Conflict-Type '{conflict}' in 409 response"
        )


@pytest.mark.slow
def test_reset_preserve_state_rejects_unauth():
    """Without a token the endpoint must refuse before any git work happens."""
    url = (
        f"{TRINITY_API_URL}/api/agents/{GIT_TEST_AGENT}"
        "/git/reset-to-main-preserve-state"
    )
    response = httpx.post(url, timeout=30.0)
    assert response.status_code in (401, 403), response.text
