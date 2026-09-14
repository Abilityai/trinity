"""HTTP client for the Trinity Backend API."""

import sys
from typing import Any, Optional

import click
import httpx

from .config import get_api_key, get_instance_url


def _profile_from_context() -> Optional[str]:
    """Try to get the --profile value from the current Click context."""
    try:
        ctx = click.get_current_context(silent=True)
        if ctx:
            root = ctx.find_root()
            if root.obj and "profile" in root.obj:
                return root.obj["profile"]
    except RuntimeError:
        pass
    return None


class TrinityAPIError(Exception):
    def __init__(self, status_code: int, detail: str, body: Optional[dict] = None):
        self.status_code = status_code
        self.detail = detail
        # #2322 — the parsed JSON body when there was one. `detail` alone is a
        # human string; a caller that must branch on the failure KIND (a login
        # deferred by a second factor, say) needs the structured fields.
        self.body = body or {}
        super().__init__(f"HTTP {status_code}: {detail}")


class TrinityClient:
    """Thin HTTP wrapper around the Trinity FastAPI backend."""

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None,
                 profile: Optional[str] = None):
        resolved_profile = profile or _profile_from_context()
        self.base_url = base_url or get_instance_url(resolved_profile)
        self.token = token or get_api_key(resolved_profile)
        if not self.base_url:
            print("Error: No Trinity instance configured. Run 'trinity init' or 'trinity login' first.", file=sys.stderr)
            sys.exit(1)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _handle_response(self, resp: httpx.Response) -> Any:
        if resp.status_code == 401:
            print("Error: Authentication failed. Run 'trinity login' to re-authenticate.", file=sys.stderr)
            sys.exit(1)
        if resp.status_code >= 400:
            body = None
            try:
                body = resp.json()
                detail = body.get("detail", resp.text) if isinstance(body, dict) else resp.text
            except Exception:
                detail = resp.text
            raise TrinityAPIError(
                resp.status_code, str(detail),
                body=body if isinstance(body, dict) else None,
            )
        if resp.status_code == 204:
            return None
        return resp.json()

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            return self._handle_response(resp)

    def post(self, path: str, json: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=60) as c:
            resp = c.post(f"{self.base_url}{path}", headers=self._headers(), json=json)
            return self._handle_response(resp)

    def put(self, path: str, json: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.put(f"{self.base_url}{path}", headers=self._headers(), json=json)
            return self._handle_response(resp)

    def delete(self, path: str) -> Any:
        with httpx.Client(timeout=30) as c:
            resp = c.delete(f"{self.base_url}{path}", headers=self._headers())
            return self._handle_response(resp)

    def post_form(self, path: str, data: dict) -> Any:
        """POST with form-encoded body (for OAuth2 token endpoint)."""
        with httpx.Client(timeout=30) as c:
            resp = c.post(f"{self.base_url}{path}", data=data)
            return self._handle_response(resp)

    def post_unauthenticated(self, path: str, json: Optional[dict] = None) -> Any:
        """POST without auth header (for login/registration flows)."""
        with httpx.Client(timeout=30) as c:
            resp = c.post(
                f"{self.base_url}{path}",
                headers={"Content-Type": "application/json"},
                json=json,
            )
            return self._handle_response(resp)

    def get_unauthenticated(self, path: str) -> Any:
        """GET without auth header."""
        with httpx.Client(timeout=30) as c:
            resp = c.get(f"{self.base_url}{path}")
            return self._handle_response(resp)
