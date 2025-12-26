"""
Trinity API Test Client

Provides a wrapper around httpx for making authenticated API requests.
"""

import httpx
import os
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass

# Token refresh threshold - refresh if token is older than this many seconds
TOKEN_REFRESH_THRESHOLD_SECONDS = 25 * 60  # 25 minutes (tokens expire at 30)


@dataclass
class ApiConfig:
    """API configuration from environment variables."""
    base_url: str
    username: str
    password: str
    mcp_api_key: Optional[str] = None
    test_agent_name: Optional[str] = None

    @classmethod
    def from_env(cls) -> "ApiConfig":
        """Load configuration from environment variables."""
        return cls(
            base_url=os.getenv("TRINITY_API_URL", "http://localhost:8000"),
            username=os.getenv("TRINITY_TEST_USERNAME", "admin"),
            password=os.getenv("TRINITY_TEST_PASSWORD", "password"),
            mcp_api_key=os.getenv("TRINITY_MCP_API_KEY"),
            test_agent_name=os.getenv("TEST_AGENT_NAME"),
        )


class TrinityApiClient:
    """HTTP client for Trinity API with authentication support."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self.token: Optional[str] = None
        self._token_time: Optional[float] = None
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def authenticate(self) -> str:
        """Authenticate and get JWT token."""
        response = self._client.post(
            "/token",
            data={
                "username": self.config.username,
                "password": self.config.password,
            },
        )
        response.raise_for_status()
        data = response.json()
        self.token = data["access_token"]
        self._token_time = time.time()
        return self.token

    def _ensure_fresh_token(self) -> None:
        """Re-authenticate if token is close to expiration."""
        if self._token_time is None:
            return
        elapsed = time.time() - self._token_time
        if elapsed > TOKEN_REFRESH_THRESHOLD_SECONDS:
            self.authenticate()

    def _get_headers(self, auth: bool = True, mcp_auth: bool = False) -> Dict[str, str]:
        """Get request headers with optional authentication."""
        headers = {"Content-Type": "application/json"}
        if mcp_auth and self.config.mcp_api_key:
            headers["Authorization"] = f"Bearer {self.config.mcp_api_key}"
        elif auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated GET request."""
        if auth:
            self._ensure_fresh_token()
        return self._client.get(
            path,
            params=params,
            headers=self._get_headers(auth),
            **kwargs
        )

    def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        timeout: Optional[float] = None,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated POST request."""
        if auth:
            self._ensure_fresh_token()
        request_timeout = timeout if timeout else self._client.timeout
        return self._client.post(
            path,
            json=json,
            data=data,
            headers=self._get_headers(auth),
            timeout=request_timeout,
            **kwargs
        )

    def put(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated PUT request."""
        if auth:
            self._ensure_fresh_token()
        return self._client.put(
            path,
            json=json,
            headers=self._get_headers(auth),
            **kwargs
        )

    def delete(
        self,
        path: str,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated DELETE request."""
        if auth:
            self._ensure_fresh_token()
        return self._client.delete(
            path,
            headers=self._get_headers(auth),
            **kwargs
        )

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AsyncTrinityApiClient:
    """Async HTTP client for Trinity API."""

    def __init__(self, config: ApiConfig):
        self.config = config
        self.token: Optional[str] = None
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def authenticate(self) -> str:
        """Authenticate and get JWT token."""
        response = await self._client.post(
            "/token",
            data={
                "username": self.config.username,
                "password": self.config.password,
            },
        )
        response.raise_for_status()
        data = response.json()
        self.token = data["access_token"]
        return self.token

    def _get_headers(self, auth: bool = True) -> Dict[str, str]:
        """Get request headers with optional authentication."""
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated GET request."""
        return await self._client.get(
            path,
            params=params,
            headers=self._get_headers(auth),
            **kwargs
        )

    async def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        timeout: Optional[float] = None,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated POST request."""
        request_timeout = timeout if timeout else self._client.timeout
        return await self._client.post(
            path,
            json=json,
            data=data,
            headers=self._get_headers(auth),
            timeout=request_timeout,
            **kwargs
        )

    async def put(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated PUT request."""
        return await self._client.put(
            path,
            json=json,
            headers=self._get_headers(auth),
            **kwargs
        )

    async def delete(
        self,
        path: str,
        auth: bool = True,
        **kwargs
    ) -> httpx.Response:
        """Make authenticated DELETE request."""
        return await self._client.delete(
            path,
            headers=self._get_headers(auth),
            **kwargs
        )

    async def close(self):
        """Close the HTTP client."""
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
