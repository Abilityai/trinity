"""
Agent HTTP Client Service.

Provides a clean interface for communicating with agent containers.
Centralizes URL construction, timeout handling, error handling,
circuit breaking, and retry logic (RELIABILITY-001).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Any, Dict

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

logger = logging.getLogger(__name__)


# ============================================================================
# Circuit Breaker (per-agent, hand-rolled for async compatibility)
# ============================================================================

@dataclass
class CircuitState:
    """Per-agent circuit breaker state."""
    failure_count: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"  # closed, open, half-open

    # Thresholds
    failure_threshold: int = 3
    cooldown_seconds: float = 30.0

    # Set by _get_circuit after creation
    agent_name: str = ""

    def record_failure(self):
        self.failure_count += 1
        # Don't reset the cooldown clock once open — continuous probe failures
        # would otherwise keep last_failure_time near zero, preventing the
        # half-open transition (root cause of #687).
        if self.state != "open":
            self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            if self.state != "open":
                logger.warning(
                    "Circuit OPENED for agent %s after %d failures",
                    self.agent_name, self.failure_count,
                )
            self.state = "open"

    def record_success(self):
        if self.state != "closed":
            logger.info(
                "Circuit CLOSED for agent %s (recovered)",
                self.agent_name,
            )
        self.failure_count = 0
        self.state = "closed"

    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= self.cooldown_seconds:
            self.state = "half-open"
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "cooldown_remaining": max(
                0.0,
                self.cooldown_seconds - (time.monotonic() - self.last_failure_time)
            ) if self.state == "open" else 0.0,
        }


# Module-level registry: agent_name → CircuitState
_circuit_registry: Dict[str, CircuitState] = {}


def _get_circuit(agent_name: str) -> CircuitState:
    """Get or create circuit breaker for an agent (TOCTOU-safe via setdefault)."""
    circuit = _circuit_registry.setdefault(agent_name, CircuitState(agent_name=agent_name))
    return circuit


def get_all_circuit_states() -> Dict[str, dict]:
    """Return circuit breaker states for all tracked agents."""
    return {name: state.to_dict() for name, state in _circuit_registry.items()}


# ============================================================================
# Connection Pool (shared httpx.AsyncClient per agent)
# ============================================================================

_client_pool: Dict[str, httpx.AsyncClient] = {}


def _get_http_client(base_url: str) -> httpx.AsyncClient:
    """Get or create a persistent HTTP client for a base URL."""
    client = _client_pool.get(base_url)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            base_url=base_url,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=30.0,
            ),
        )
        _client_pool[base_url] = client
    return client


async def close_all_clients():
    """Close all pooled HTTP clients. Call on app shutdown."""
    for client in _client_pool.values():
        await client.aclose()
    _client_pool.clear()


# ============================================================================
# Response Models
# ============================================================================

@dataclass
class AgentChatMetrics:
    """Observability data extracted from agent chat response."""
    context_used: int
    context_max: int
    context_percent: float
    cost_usd: Optional[float]
    tool_calls_json: Optional[str]
    execution_log_json: Optional[str]


@dataclass
class AgentChatResponse:
    """Parsed response from agent chat endpoint."""
    response_text: str
    metrics: AgentChatMetrics
    raw_response: Dict[str, Any]


@dataclass
class AgentSessionInfo:
    """Agent context/session information."""
    context_tokens: int
    context_window: int
    context_percent: float
    total_cost_usd: Optional[float] = None


# ============================================================================
# Exceptions
# ============================================================================

class AgentClientError(Exception):
    """Base exception for agent client errors."""
    pass


class AgentNotReachableError(AgentClientError):
    """Agent container is not responding."""
    pass


class AgentCircuitOpenError(AgentClientError):
    """Circuit breaker is open — agent is known to be unhealthy."""
    pass


class AgentRequestError(AgentClientError):
    """Agent returned an error response."""
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


# ============================================================================
# Agent Client
# ============================================================================

class AgentClient:
    """
    HTTP client for agent container communication.

    Centralizes:
    - URL construction
    - Timeout handling
    - Error handling
    - Response parsing
    """

    # Default timeouts
    CHAT_TIMEOUT = 900.0      # 15 minutes for chat
    SESSION_TIMEOUT = 5.0     # 5 seconds for session info
    DEFAULT_TIMEOUT = 30.0    # 30 seconds default

    def __init__(self, agent_name: str):
        """
        Initialize client for a specific agent.

        Args:
            agent_name: Name of the agent (without 'agent-' prefix)
        """
        self.agent_name = agent_name
        self.base_url = f"http://agent-{agent_name}:8000"
        self._circuit = _get_circuit(agent_name)

    # ========================================================================
    # Core HTTP Methods
    # ========================================================================

    async def _request(
        self,
        method: str,
        path: str,
        timeout: float = None,
        **kwargs
    ) -> httpx.Response:
        """
        Make an HTTP request to the agent.

        Checks circuit breaker before sending. Records success/failure
        to the per-agent circuit state.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: URL path (e.g., "/api/chat")
            timeout: Request timeout in seconds
            **kwargs: Additional arguments for httpx request

        Returns:
            httpx.Response

        Raises:
            AgentCircuitOpenError: If circuit breaker is open
            AgentNotReachableError: If connection fails
            AgentRequestError: If request fails with error status
        """
        if not self._circuit.allow_request():
            raise AgentCircuitOpenError(
                f"Circuit open for agent {self.agent_name} "
                f"(failures={self._circuit.failure_count})"
            )

        timeout = timeout or self.DEFAULT_TIMEOUT
        client = _get_http_client(self.base_url)

        try:
            response = await client.request(
                method, path, timeout=timeout, **kwargs
            )
            self._circuit.record_success()
            return response
        except httpx.ConnectError as e:
            self._circuit.record_failure()
            raise AgentNotReachableError(
                f"Cannot connect to agent {self.agent_name}: {e}"
            )
        except httpx.TimeoutException as e:
            self._circuit.record_failure()
            raise AgentNotReachableError(
                f"Request to agent {self.agent_name} timed out after {timeout}s"
            )

    async def get(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a GET request to the agent."""
        return await self._request("GET", path, timeout, **kwargs)

    async def post(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a POST request to the agent."""
        return await self._request("POST", path, timeout, **kwargs)

    async def put(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a PUT request to the agent."""
        return await self._request("PUT", path, timeout, **kwargs)

    async def delete(self, path: str, timeout: float = None, **kwargs) -> httpx.Response:
        """Make a DELETE request to the agent."""
        return await self._request("DELETE", path, timeout, **kwargs)

    # ========================================================================
    # Chat Operations
    # ========================================================================

    async def chat(
        self,
        message: str,
        stream: bool = False,
        timeout: float = None
    ) -> AgentChatResponse:
        """
        Send a chat message to the agent.

        Args:
            message: Message to send
            stream: Whether to stream the response
            timeout: Request timeout (default: 5 minutes)

        Returns:
            AgentChatResponse with parsed metrics

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        response = await self.post(
            "/api/chat",
            json={"message": message, "stream": stream},
            timeout=timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_chat_response(result)

    async def task(
        self,
        message: str,
        timeout: float = None,
        execution_id: Optional[str] = None
    ) -> AgentChatResponse:
        """
        Execute a stateless task on the agent (no conversation context).

        Unlike chat(), this endpoint:
        - Does NOT maintain conversation history
        - Each call is independent (no --continue flag)
        - Returns raw Claude Code execution log (full transcript)

        Use this for scheduled executions and independent tasks.

        Args:
            message: Task prompt to execute
            timeout: Request timeout (default: 15 minutes)
            execution_id: Optional execution ID for process registry (enables termination and live streaming)

        Returns:
            AgentChatResponse with parsed metrics and raw execution log

        Raises:
            AgentNotReachableError: If agent is not reachable
            AgentRequestError: If request fails
        """
        timeout = timeout or self.CHAT_TIMEOUT

        payload = {"message": message, "timeout_seconds": int(timeout)}
        if execution_id:
            payload["execution_id"] = execution_id

        response = await self.post(
            "/api/task",
            json=payload,
            timeout=timeout + 10  # Add buffer to agent timeout
        )

        # Check for error response and extract detailed error message
        if response.status_code >= 400:
            error_msg = self._extract_error_detail(response)
            raise AgentRequestError(error_msg, status_code=response.status_code)

        result = response.json()
        return self._parse_task_response(result)

    def _parse_task_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent task response into structured data.

        Similar to _parse_chat_response but handles /api/task format
        which returns raw Claude Code execution log.
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data (task response has metadata but no session)
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage from metadata
        context_used = metadata.get("input_tokens", 0)
        context_max = metadata.get("context_window", 200000)
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd")

        # Execution log - raw Claude Code transcript
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    def _extract_error_detail(self, response: httpx.Response) -> str:
        """Extract detailed error message from agent HTTP response."""
        try:
            error_data = response.json()
            if "detail" in error_data:
                return error_data["detail"]
        except Exception:
            pass
        # Fall back to response text if JSON parsing fails
        if response.text:
            return response.text[:500]
        return f"HTTP {response.status_code} error"

    def _parse_chat_response(self, result: Dict[str, Any]) -> AgentChatResponse:
        """
        Parse agent chat response into structured data.

        Extracts:
        - Response text (truncated if > 10000 chars)
        - Context usage (tokens, window, percentage)
        - Cost
        - Tool calls / execution log
        """
        # Extract response text
        response_text = result.get("response", str(result))
        if len(response_text) > 10000:
            response_text = response_text[:10000] + "... (truncated)"

        # Extract observability data
        session_data = result.get("session", {})
        metadata = result.get("metadata", {})
        execution_log = result.get("execution_log")

        # Context usage
        # NOTE: cache_creation_tokens and cache_read_tokens are SUBSETS of input_tokens
        # for billing purposes, NOT additional tokens. Do NOT sum them.
        context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
        context_max = session_data.get("context_window") or metadata.get("context_window", 200000)
        context_percent = round(context_used / max(context_max, 1) * 100, 1)

        # Cost
        cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

        # Tool calls / execution log
        # Note: Check is not None, not truthiness - empty list [] is valid log
        tool_calls_json = None
        execution_log_json = None
        if execution_log is not None:
            execution_log_json = json.dumps(execution_log)
            tool_calls_json = execution_log_json  # Backwards compatibility

        metrics = AgentChatMetrics(
            context_used=context_used,
            context_max=context_max,
            context_percent=context_percent,
            cost_usd=cost,
            tool_calls_json=tool_calls_json,
            execution_log_json=execution_log_json
        )

        return AgentChatResponse(
            response_text=response_text,
            metrics=metrics,
            raw_response=result
        )

    # ========================================================================
    # Session / Context Operations
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def get_session(self, timeout: float = None) -> Optional[AgentSessionInfo]:
        """
        Get current session/context information.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            AgentSessionInfo or None if request fails
        """
        timeout = timeout or self.SESSION_TIMEOUT

        try:
            response = await self.get("/api/chat/session", timeout=timeout)
            if response.status_code == 200:
                session = response.json()
                context_tokens = session.get("context_tokens", 0)
                context_window = session.get("context_window", 200000)
                return AgentSessionInfo(
                    context_tokens=context_tokens,
                    context_window=context_window,
                    context_percent=round(
                        context_tokens / max(context_window, 1) * 100, 1
                    ),
                    total_cost_usd=session.get("total_cost_usd")
                )
        except AgentClientError:
            pass
        return None

    # ========================================================================
    # File Operations
    # ========================================================================

    async def read_file(
        self,
        path: str,
        timeout: float = 30.0
    ) -> dict:
        """
        Read content from a file in the agent's workspace.

        Args:
            path: File path within /home/developer
            timeout: Request timeout

        Returns:
            dict with success status and content
        """
        try:
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            response = await self.get(
                f"/api/files/download?path={encoded_path}",
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, "content": response.text}
            elif response.status_code == 404:
                return {"success": True, "content": None, "not_found": True}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    async def write_file(
        self,
        path: str,
        content: str,
        timeout: float = 30.0,
        platform: bool = False
    ) -> dict:
        """
        Write content to a file in the agent's workspace.
        Creates parent directories if they don't exist.

        Args:
            path: File path within /home/developer
            content: File content to write
            timeout: Request timeout
            platform: If True, allows writes to .trinity directory (platform-initiated)

        Returns:
            dict with success status and file info
        """
        try:
            # URL encode the path for query parameter
            import urllib.parse
            encoded_path = urllib.parse.quote(path, safe='')

            # Add platform flag if needed
            query = f"path={encoded_path}"
            if platform:
                query += "&platform=true"

            response = await self.put(
                f"/api/files?{query}",
                json={"content": content},
                timeout=timeout
            )

            if response.status_code == 200:
                return {"success": True, **response.json()}
            else:
                return {
                    "success": False,
                    "error": response.text,
                    "status_code": response.status_code
                }

        except AgentClientError as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # Health Check
    # ========================================================================

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        retry=retry_if_exception_type(AgentNotReachableError),
        reraise=True,
    )
    async def health_check(self, timeout: float = 5.0) -> bool:
        """
        Check if agent is healthy and responding.
        Retries up to 3x with exponential backoff on transient errors.

        Returns:
            True if agent responds to health check
        """
        try:
            response = await self.get("/api/health", timeout=timeout)
            return response.status_code == 200
        except AgentCircuitOpenError:
            return False
        except AgentClientError:
            return False


# ============================================================================
# Factory Function
# ============================================================================

def get_agent_client(agent_name: str) -> AgentClient:
    """
    Factory function to create an AgentClient.

    Args:
        agent_name: Name of the agent

    Returns:
        AgentClient instance
    """
    return AgentClient(agent_name)
