"""#1159 — agent-server inbound auth isolation (integration).

Acceptance: a compromised / prompt-injected sibling on ``trinity-agent-network``
can no longer read another agent's ``.env`` secrets or run code on its
``:8000`` server without the per-agent token. We prove this the same way the
Redis-lockdown suite proves network segmentation — spin up a throwaway sibling
container on the agent network and have it curl the target agent directly.

Requires a running stack with a token-enabled agent:
  * ``TEST_AGENT_NAME``   — a running agent (created with AGENT_AUTH_SECRET set)
  * ``AGENT_AUTH_SECRET`` — the stack master, so the test can derive the agent's
    token (must match the value the backend injected at create time)
Skipped (not failed) when either is absent — see tests/security/README.md.

These probes cover the HTTP surface (curl can't drive a WS handshake). The
WebSocket code-execution route (``/ws/chat``) is gated by the same pure-ASGI
middleware and guarded at the unit level in
``tests/unit/test_agent_auth_middleware.py`` (the ``test_ws_*`` cases).
"""
import os

import pytest

docker = pytest.importorskip("docker")

# Matches docker-compose.yml top-level `name:`; overridable for the per-worktree
# (env-mode) verify network.
AGENT_NETWORK = os.environ.get("TRINITY_AGENT_NETWORK", "trinity-agent-network")
CURL_IMAGE = "curlimages/curl:latest"


def _agent_name() -> str:
    name = os.environ.get("TEST_AGENT_NAME")
    if not name:
        pytest.skip("TEST_AGENT_NAME not set — needs a running token-enabled agent")
    return name


def _derive_token(name: str) -> str:
    if not os.environ.get("AGENT_AUTH_SECRET"):
        pytest.skip("AGENT_AUTH_SECRET not set — cannot derive the agent's token")
    from services.agent_auth import derive_agent_token

    return derive_agent_token(name)


def _curl_status(path: str, headers: dict | None = None) -> str:
    """Curl the target agent from a throwaway sibling on the agent network.

    Returns the HTTP status code as a string (curl ``%{http_code}``).
    """
    name = _agent_name()
    args = ["-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8"]
    for key, value in (headers or {}).items():
        args += ["-H", f"{key}: {value}"]
    args.append(f"http://agent-{name}:8000{path}")
    out = docker.from_env().containers.run(
        CURL_IMAGE, command=args, network=AGENT_NETWORK, remove=True
    )
    return out.decode().strip()


@pytest.mark.integration
def test_sibling_without_token_is_rejected():
    """The core AC: an unauthenticated sibling gets 401 on a real route, so it
    cannot read /api/credentials/read or drive /api/chat on a peer."""
    assert _curl_status("/api/agent/info") == "401"


@pytest.mark.integration
def test_sibling_with_correct_token_is_accepted():
    name = _agent_name()
    status = _curl_status("/api/agent/info", {"X-Trinity-Agent-Token": _derive_token(name)})
    assert status == "200"


@pytest.mark.integration
def test_sibling_with_wrong_token_is_rejected():
    assert _curl_status("/api/agent/info", {"X-Trinity-Agent-Token": "not-the-token"}) == "401"


@pytest.mark.integration
def test_health_probe_is_exempt():
    """/health must stay reachable without a token (Docker healthcheck path)."""
    assert _curl_status("/health") == "200"
