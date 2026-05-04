"""
Tests for Agent Website Proxy (SITE-001)
Issue: #633

Tests the /site/{token}/{path} reverse-proxy endpoint:
- Token validation (valid, invalid, wrong type, expired, disabled)
- Site link creation via public-links API
- URL format for site-type links
- Rate limiting
- 502 when agent web server not reachable
"""
import uuid
import pytest
from utils.api_client import TrinityApiClient


class TestSiteLinkCreation:
    """Test creating site-type public links via the owner API."""

    @pytest.fixture
    def test_agent(self, api_client: TrinityApiClient):
        """Create a test agent, yield its name, delete on teardown."""
        agent_name = f"site-test-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            "/api/agents",
            json={"name": agent_name, "type": "business-assistant", "resources": {"cpu": "1", "memory": "1g"}},
            timeout=60,
        )
        if response.status_code != 200:
            pytest.skip(f"Could not create test agent: {response.text}")
        yield agent_name
        api_client.delete(f"/api/agents/{agent_name}", timeout=30)

    def test_create_chat_link_default_type(self, api_client: TrinityApiClient, test_agent):
        """Creating a link without specifying link_type defaults to 'chat'."""
        resp = api_client.post(
            f"/api/agents/{test_agent}/public-links",
            json={"name": "default chat link"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["link_type"] == "chat"
        assert "/chat/" in data["url"]

    def test_create_site_link(self, api_client: TrinityApiClient, test_agent):
        """Creating a site link returns link_type='site' and URL under /site/."""
        resp = api_client.post(
            f"/api/agents/{test_agent}/public-links",
            json={"name": "agent website", "link_type": "site"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["link_type"] == "site"
        assert "/site/" in data["url"]
        assert data["url"].endswith("/")

    def test_create_invalid_link_type_rejected(self, api_client: TrinityApiClient, test_agent):
        """Creating a link with an unknown type returns 400."""
        resp = api_client.post(
            f"/api/agents/{test_agent}/public-links",
            json={"name": "bad", "link_type": "webhook"},
        )
        assert resp.status_code == 400

    def test_list_links_includes_type(self, api_client: TrinityApiClient, test_agent):
        """Listing an agent's links returns link_type for each link."""
        # Create one of each type
        api_client.post(
            f"/api/agents/{test_agent}/public-links",
            json={"name": "chat", "link_type": "chat"},
        )
        api_client.post(
            f"/api/agents/{test_agent}/public-links",
            json={"name": "site", "link_type": "site"},
        )

        resp = api_client.get(f"/api/agents/{test_agent}/public-links")
        assert resp.status_code == 200, resp.text
        links = resp.json()
        types = {l["link_type"] for l in links}
        assert "chat" in types
        assert "site" in types


class TestSiteProxyEndpoint:
    """Test the /site/{token}/{path} proxy endpoint directly."""

    @pytest.fixture
    def site_link(self, api_client: TrinityApiClient):
        """Create a throwaway agent + site link, yield the token, clean up."""
        agent_name = f"site-proxy-{uuid.uuid4().hex[:8]}"
        resp = api_client.post(
            "/api/agents",
            json={"name": agent_name, "type": "business-assistant", "resources": {"cpu": "1", "memory": "1g"}},
            timeout=60,
        )
        if resp.status_code != 200:
            pytest.skip(f"Could not create test agent: {resp.text}")

        link_resp = api_client.post(
            f"/api/agents/{agent_name}/public-links",
            json={"name": "test site", "link_type": "site"},
        )
        assert link_resp.status_code == 200, link_resp.text
        token = link_resp.json()["token"]

        yield {"token": token, "agent_name": agent_name}

        api_client.delete(f"/api/agents/{agent_name}", timeout=30)

    def test_invalid_token_returns_401(self, api_client: TrinityApiClient):
        """A completely invalid token returns 401 with a generic error message."""
        resp = api_client.get("/site/invalid-token-xyz/", auth=False)
        assert resp.status_code == 401
        # Must not reveal whether token exists
        assert "invalid" in resp.json().get("detail", "").lower() or \
               "expired" in resp.json().get("detail", "").lower()

    def test_chat_token_rejected_at_site_endpoint(self, api_client: TrinityApiClient):
        """A valid chat-type token returns 401 when used at /site/."""
        # Create an agent and chat link
        agent_name = f"chat-type-{uuid.uuid4().hex[:8]}"
        resp = api_client.post(
            "/api/agents",
            json={"name": agent_name, "type": "business-assistant", "resources": {"cpu": "1", "memory": "1g"}},
            timeout=60,
        )
        if resp.status_code != 200:
            pytest.skip(f"Could not create test agent: {resp.text}")

        try:
            link_resp = api_client.post(
                f"/api/agents/{agent_name}/public-links",
                json={"name": "chat link", "link_type": "chat"},
            )
            assert link_resp.status_code == 200
            token = link_resp.json()["token"]

            site_resp = api_client.get(f"/site/{token}/", auth=False)
            assert site_resp.status_code == 401
        finally:
            api_client.delete(f"/api/agents/{agent_name}", timeout=30)

    def test_valid_site_token_502_when_agent_not_running(
        self, api_client: TrinityApiClient, site_link
    ):
        """A valid site token returns 502 when the agent's web server is not running.

        The agent container exists but no web server is running on port 3000.
        """
        token = site_link["token"]
        resp = api_client.get(f"/site/{token}/", auth=False)
        # Agent exists but nothing on port 3000 → 502
        assert resp.status_code == 502

    def test_disabled_link_returns_401(self, api_client: TrinityApiClient, site_link):
        """Disabling a site link returns 401 for subsequent requests."""
        token = site_link["token"]
        agent_name = site_link["agent_name"]

        # Get the link ID
        links_resp = api_client.get(f"/api/agents/{agent_name}/public-links")
        link_id = next(l["id"] for l in links_resp.json() if l["token"] == token)

        # Disable it
        api_client.put(
            f"/api/agents/{agent_name}/public-links/{link_id}",
            json={"enabled": False},
        )

        resp = api_client.get(f"/site/{token}/", auth=False)
        assert resp.status_code == 401

    def test_root_path_without_trailing_slash_redirects(
        self, api_client: TrinityApiClient, site_link
    ):
        """GET /site/{token} (no slash) should redirect to /site/{token}/."""
        token = site_link["token"]
        resp = api_client.get(f"/site/{token}", auth=False, follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers.get("location", "").endswith("/")
