"""Issue #589 — Redis lockdown integration tests.

Acceptance criteria covered:
  #3 — agent-network containers cannot reach Redis (network segment separation)
  #1 — requirepass enforced (NOAUTH from unauth client)
  #2 — backend ACL user lacks permissions for FLUSHALL / CONFIG (no admin)

Requires the platform stack to be running. Skipped (via tests/security/conftest.py)
when REDIS_PASSWORD / REDIS_BACKEND_PASSWORD are not available in the environment.
"""

import os

import pytest

docker = pytest.importorskip("docker")


# Network names match docker-compose.yml top-level `name:` field.
AGENT_NETWORK = "trinity-agent-network"
PLATFORM_NETWORK = "trinity-platform-network"


def _client():
    return docker.from_env()


@pytest.mark.integration
def test_agent_network_container_cannot_reach_redis():
    """Acceptance #3: a container on the agent network has no route to Redis."""
    client = _client()
    out = client.containers.run(
        "alpine:3.19",
        # nc -z exits 0 on reach, 1 on refused/no-route. Wrap so output is
        # deterministic even when the connection is refused.
        command=[
            "sh",
            "-c",
            "nc -z -w 2 redis 6379 && echo REACHABLE || echo BLOCKED",
        ],
        network=AGENT_NETWORK,
        remove=True,
    )
    assert b"BLOCKED" in out, f"Redis is reachable from agent network: {out!r}"


@pytest.mark.integration
def test_redis_rejects_unauthenticated_connection():
    """Acceptance #1: even on the platform network, no creds → NOAUTH."""
    client = _client()
    out = client.containers.run(
        "redis:7-alpine",
        command=["redis-cli", "-h", "redis", "PING"],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOAUTH" in out, f"Redis accepted unauth connection: {out!r}"


@pytest.mark.integration
def test_platform_container_can_authenticate():
    """Sanity: the backend connection string works."""
    client = _client()
    pwd = os.environ["REDIS_BACKEND_PASSWORD"]
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", pwd,
            "--no-auth-warning",
            "PING",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"PONG" in out, f"backend ACL user cannot PING: {out!r}"


@pytest.mark.integration
def test_backend_acl_blocks_flushall():
    """Acceptance #2: backend ACL user cannot wipe data."""
    client = _client()
    pwd = os.environ["REDIS_BACKEND_PASSWORD"]
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", pwd,
            "--no-auth-warning",
            "FLUSHALL",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOPERM" in out, f"backend user can run FLUSHALL: {out!r}"


@pytest.mark.integration
def test_backend_acl_blocks_config_get():
    """Acceptance #2: backend ACL user cannot read CONFIG (would leak admin password)."""
    client = _client()
    pwd = os.environ["REDIS_BACKEND_PASSWORD"]
    out = client.containers.run(
        "redis:7-alpine",
        command=[
            "redis-cli",
            "-h", "redis",
            "--user", "backend",
            "-a", pwd,
            "--no-auth-warning",
            "CONFIG", "GET", "requirepass",
        ],
        network=PLATFORM_NETWORK,
        remove=True,
    )
    assert b"NOPERM" in out, f"backend user can read CONFIG: {out!r}"
