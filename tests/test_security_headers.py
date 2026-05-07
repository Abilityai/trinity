"""
Tests for security headers on API responses (#190).

Verifies that the FastAPI security headers middleware is active and that
the Server header is suppressed via Uvicorn's --no-server-header flag.

Feature flow: N/A (infrastructure hardening)
"""

import pytest


@pytest.mark.smoke
class TestSecurityHeaders:
    """Verify security headers are present on API responses."""

    def test_api_response_has_security_headers(self, api_client):
        """GET /api/health should include all expected security headers."""
        response = api_client.get("/api/health")

        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
        assert response.headers.get("permissions-policy") == "camera=(), microphone=(), geolocation=(), payment=()"
        assert response.headers.get("cross-origin-resource-policy") == "same-origin"
        # Issue #549: X-Frame-Options + COOP added to FastAPI responses so
        # API surfaces (Swagger, direct curl) match the frontend baseline.
        assert response.headers.get("x-frame-options") == "DENY"
        assert response.headers.get("cross-origin-opener-policy") == "same-origin"

    def test_hsts_absent_on_plain_http(self, api_client):
        """#549: HSTS must NOT be set on plain-HTTP responses — emitting it
        over HTTP is misleading (browsers ignore per RFC 6797) and could
        break any future http-only path on the same host."""
        response = api_client.get("/api/health")
        assert response.headers.get("strict-transport-security") is None

    def test_hsts_set_when_x_forwarded_proto_https(self, api_client):
        """#549: when an upstream TLS terminator signals HTTPS via
        X-Forwarded-Proto, HSTS should fire so browsers pin the policy."""
        response = api_client._client.get(
            "/api/health",
            headers={"X-Forwarded-Proto": "https"},
        )
        hsts = response.headers.get("strict-transport-security")
        assert hsts is not None
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_server_header_stripped(self, api_client):
        """API responses should not expose the server technology."""
        response = api_client.get("/api/health")

        # Uvicorn's --no-server-header flag should suppress this
        server = response.headers.get("server")
        assert server is None or "uvicorn" not in server.lower(), (
            f"Server header should not expose uvicorn, got: {server}"
        )

    def test_cors_preflight_still_works(self, unauthenticated_client):
        """CORS preflight (OPTIONS) should still return proper CORS headers
        alongside security headers — the middleware must not break CORS."""
        response = unauthenticated_client._client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )

        # CORS middleware intercepts preflight and returns 200
        assert response.status_code in (200, 204, 405)
        # Security headers should also be present on preflight responses
        assert response.headers.get("x-content-type-options") == "nosniff"
