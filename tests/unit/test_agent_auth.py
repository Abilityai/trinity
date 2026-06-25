"""Unit tests for the agent-auth token core (#1159).

Pure-function tests for ``services/agent_auth.py``: deterministic per-agent
token derivation from a stable master secret, the header builder, the
case-insensitive merge helper, and the fail-closed-on-empty-secret contract.
"""
import hashlib
import hmac

import httpx
import pytest

from services import agent_auth


SECRET = "a" * 64  # openssl-hex-32 shape, value irrelevant to the math


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_SECRET", SECRET)


def test_derive_is_deterministic():
    assert agent_auth.derive_agent_token("alpha") == agent_auth.derive_agent_token("alpha")


def test_derive_differs_per_agent():
    assert agent_auth.derive_agent_token("alpha") != agent_auth.derive_agent_token("beta")


def test_derive_matches_reference_hmac():
    expected = hmac.new(
        SECRET.encode(),
        b"trinity-agent-auth:v1:alpha",
        hashlib.sha256,
    ).hexdigest()
    assert agent_auth.derive_agent_token("alpha") == expected


def test_derive_raises_on_empty_secret(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_SECRET", "")
    with pytest.raises(RuntimeError):
        agent_auth.derive_agent_token("alpha")


def test_derive_raises_on_missing_secret(monkeypatch):
    monkeypatch.delenv("AGENT_AUTH_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        agent_auth.derive_agent_token("alpha")


def test_build_headers_shape():
    headers = agent_auth.build_agent_auth_headers("alpha")
    assert headers == {"X-Trinity-Agent-Token": agent_auth.derive_agent_token("alpha")}


def test_merge_preserves_other_headers():
    merged = agent_auth.merge_auth_headers("alpha", {"Content-Type": "application/json"})
    assert merged["Content-Type"] == "application/json"
    assert merged["X-Trinity-Agent-Token"] == agent_auth.derive_agent_token("alpha")


def test_merge_overrides_caller_token_case_insensitively():
    # A caller that supplied a stale/forged token (any header casing) must be
    # overridden by the backend-derived value.
    merged = agent_auth.merge_auth_headers(
        "alpha", {"x-trinity-agent-token": "forged-value"}
    )
    token_values = [v for k, v in merged.items() if k.lower() == "x-trinity-agent-token"]
    assert token_values == [agent_auth.derive_agent_token("alpha")]
    assert "forged-value" not in merged.values()


def test_merge_handles_none():
    merged = agent_auth.merge_auth_headers("alpha", None)
    assert merged == {"X-Trinity-Agent-Token": agent_auth.derive_agent_token("alpha")}


@pytest.mark.asyncio
async def test_httpx_client_carries_token():
    client = agent_auth.agent_httpx_client("alpha")
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert client.headers["X-Trinity-Agent-Token"] == agent_auth.derive_agent_token("alpha")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_httpx_client_merges_caller_headers():
    client = agent_auth.agent_httpx_client("alpha", headers={"Content-Type": "application/json"})
    try:
        assert client.headers["Content-Type"] == "application/json"
        assert client.headers["X-Trinity-Agent-Token"] == agent_auth.derive_agent_token("alpha")
    finally:
        await client.aclose()
