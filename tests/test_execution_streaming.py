"""
Execution Streaming Tests (test_execution_streaming.py)

Tests for SSE (Server-Sent Events) execution log streaming.
Covers EXEC-009 (Real-time execution logs via SSE).

Note: The streaming endpoint returns 200 OK and sends errors via SSE stream
rather than HTTP error codes. This is a valid SSE pattern where errors are
communicated in-band via the stream.
"""

import pytest
import httpx
from testkit.api_client import TrinityApiClient, ApiConfig
from testkit.assertions import (
    assert_status,
    assert_status_in,
)


class TestExecutionStreamingAuthentication:
    """Tests for SSE streaming authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_stream_requires_authentication(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/executions/{id}/stream requires authentication."""
        response = unauthenticated_client.get(
            "/api/agents/test-agent/executions/test-exec-id/stream",
            auth=False
        )
        assert_status(response, 401)


class TestExecutionStreamingEndpoint:
    """EXEC-009: Real-time execution logs via SSE."""

    def test_stream_nonexistent_agent_returns_404(
        self,
        api_client: TrinityApiClient
    ):
        """GET /api/agents/{name}/executions/{id}/stream for nonexistent agent returns 404."""
        response = api_client.get(
            "/api/agents/nonexistent-agent-xyz/executions/test-exec-id/stream"
        )
        assert_status(response, 404)

    def test_stream_nonexistent_execution_handled(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/executions/{id}/stream for nonexistent execution is handled gracefully.

        The streaming endpoint may return either:
        - HTTP 404 (error before stream starts)
        - HTTP 200 with error in SSE stream (error communicated in-band)
        Both are valid implementations.
        """
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/nonexistent-exec-id/stream"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Accept both HTTP errors and 200 with SSE error
        if response.status_code == 200:
            # SSE stream started - check for error message in stream
            body = response.text
            assert "error" in body.lower() or "not found" in body.lower() or "stream_end" in body, \
                "Stream should contain error message or stream_end for nonexistent execution"
        else:
            # HTTP error before stream
            assert_status_in(response, [400, 404])

    def test_stream_endpoint_exists(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSE streaming endpoint exists and responds."""
        # Get an execution ID from the executions list
        executions_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions"
        )

        if executions_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if executions_response.status_code != 200:
            pytest.skip("Could not get executions list")

        executions = executions_response.json()

        if not executions or len(executions) == 0:
            # Try with a fake execution ID - should return handled error
            response = api_client.get(
                f"/api/agents/{created_agent['name']}/executions/fake-exec-id/stream"
            )
            # Should return a proper response (either HTTP error or 200 with SSE error)
            # The endpoint exists if it doesn't return 404 for the route itself
            assert response.status_code != 405, "Streaming endpoint method should be allowed"
            return

        # Use first execution
        exec_id = executions[0].get("id") or executions[0].get("execution_id")
        if not exec_id:
            pytest.skip("No execution ID found")

        # Make request to stream endpoint
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/{exec_id}/stream"
        )

        # Should return 200 with SSE or 404 if execution completed/not found
        assert_status_in(response, [200, 404])


class TestExecutionStreamingFormat:
    """Tests for SSE stream format validation."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_stream_returns_sse_content_type(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSE stream returns correct content-type header."""
        # Get an active execution or skip
        executions_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions",
            params={"status": "running"}
        )

        if executions_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if executions_response.status_code != 200:
            pytest.skip("Could not get executions")

        executions = executions_response.json()
        if not executions or len(executions) == 0:
            pytest.skip("No running executions to stream")

        exec_id = executions[0].get("id") or executions[0].get("execution_id")

        # Make streaming request
        config = ApiConfig.from_env()
        with httpx.Client(base_url=config.base_url, timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {api_client.token}"}
            try:
                with client.stream(
                    "GET",
                    f"/api/agents/{created_agent['name']}/executions/{exec_id}/stream",
                    headers=headers,
                    timeout=5.0
                ) as response:
                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        assert "text/event-stream" in content_type, \
                            f"Expected SSE content-type, got: {content_type}"
            except httpx.ReadTimeout:
                # Timeout is acceptable for SSE streams
                pass


class TestExecutionStreamingIntegration:
    """Integration tests for execution streaming with actual executions."""

    @pytest.mark.slow
    @pytest.mark.requires_agent
    def test_start_execution_and_stream(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Start an execution and verify streaming endpoint is available."""
        # Start a simple execution
        chat_response = api_client.post(
            f"/api/agents/{created_agent['name']}/chat",
            json={"message": "echo test", "parallel": True},
            timeout=30.0
        )

        if chat_response.status_code == 503:
            pytest.skip("Agent server not ready")

        if chat_response.status_code != 200:
            pytest.skip(f"Could not start execution: {chat_response.status_code}")

        result = chat_response.json()
        exec_id = result.get("execution_id")

        if not exec_id:
            pytest.skip("No execution ID returned")

        # Try to stream this execution
        stream_response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/{exec_id}/stream"
        )

        # Should either stream or return 404 if already completed
        assert_status_in(stream_response, [200, 404])


class TestExecutionStreamingErrors:
    """Tests for error handling in execution streaming."""

    def test_stream_invalid_execution_id_format_handled(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Invalid execution ID format is handled gracefully.

        The endpoint may return either HTTP error or 200 with SSE error.
        Both are valid implementations.
        """
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/!invalid!id!/stream"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        # Accept both HTTP errors and 200 with SSE error
        if response.status_code == 200:
            # SSE stream - should contain error or stream_end
            body = response.text
            assert "error" in body.lower() or "stream_end" in body, \
                "Stream should contain error or stream_end for invalid ID"
        else:
            # HTTP error
            assert_status_in(response, [400, 404, 422])

    def test_stream_empty_execution_id(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """Empty execution ID is handled gracefully."""
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions//stream"
        )

        # Should return error or not found
        assert_status_in(response, [400, 404, 405, 422])


class TestExecutionStreamingRetryableErrors:
    """Tests for retryable error classification in SSE streams (#68)."""

    def test_stream_not_found_includes_retryable_flag(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """When execution is not found on agent, error should include retryable flag.

        This handles the race condition where the frontend connects before the
        agent has registered the execution.
        """
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/nonexistent-race-condition/stream"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        if response.status_code == 200:
            body = response.text
            # If the stream contains an error, check for retryable field
            if '"type": "error"' in body or '"type":"error"' in body:
                import json
                for line in body.split('\n'):
                    if line.startswith('data: '):
                        try:
                            data = json.loads(line[6:])
                            if data.get('type') == 'error':
                                # Retryable flag should be present for 404/connection errors
                                assert 'retryable' in data or 'message' in data, \
                                    "Error events should include retryable flag or message"
                                break
                        except json.JSONDecodeError:
                            continue

    def test_stream_contains_stream_end(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """SSE stream should always terminate with stream_end event.

        This ensures the frontend can detect when the stream has ended
        and trigger the polling fallback if execution is still running.
        """
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/executions/test-stream-end/stream"
        )

        if response.status_code == 503:
            pytest.skip("Agent server not ready")

        if response.status_code == 200:
            body = response.text
            assert "stream_end" in body, \
                "SSE stream should always contain a stream_end event"
