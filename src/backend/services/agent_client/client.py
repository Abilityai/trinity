"""The AgentClient itself — request plumbing, response parsing, and the typed error ladder.

Carved out of the 1,294-line `services/agent_client.py` (#1028); the package
`__init__` re-exports the public surface unchanged. Cross-module calls go
through the sibling module object so a patch on the owning module reaches
every caller (the git_service rule).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Any, Dict, Tuple

import httpx
import redis as _redis
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

# Shared Redis plumbing (#526 D4). Top-level module (NOT services/) so this
# import stays clean when agent_client is loaded standalone in its unit +
# integration suites — see redis_breaker_util's module docstring.
from redis_breaker_util import (
    ScriptCache,
    decode_pair,
    get_breaker_redis,
    reset_breaker_redis_client,
)
from services.agent_auth import merge_auth_headers
from services.model_context import DEFAULT_CONTEXT_WINDOW

logger = logging.getLogger(__name__)


# ============================================================================
# Circuit Breaker (per-agent, Redis-backed for cross-worker coordination, #631)
# ============================================================================
#
# Why Redis: backend runs with N uvicorn workers. Per-process state means
# each worker probed independently, doubled DB writes, doubled log noise.
# Single Redis hash + Lua scripts give atomic state machine transitions and
# the "only one worker probes at a time" semantics for free.
#
# Redis layout (per agent):
#     agent:circuit:{name}             HASH  state, failures, last_failure_ts,
#                                            next_probe_at, probe_count_since_open
#     agent:circuit:{name}:probe-lock  STRING (NX EX 10) — short-lived probe permit
#
# State machine:
#     closed                    — happy path; every request goes through.
#     open                      — failure_threshold hit; only one half-open probe
#                                 per cooldown window (per cluster, not per worker).
#     dormant                   — too many consecutive failed probes; stops probing
#                                 entirely until the agent container restarts or an
#                                 operator manually triggers a health check.


from . import circuit, http_pool

logger = logging.getLogger(__name__)

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


class AgentClientError(Exception):
    """Base exception for agent client errors."""
    pass


class AgentNotReachableError(AgentClientError):
    """Agent container is not responding."""
    pass


class AgentConnectionDroppedError(AgentNotReachableError):
    """Connection dropped mid-flight (transport-level disconnect).

    Distinguishes "in-flight transport broke" from "agent unreachable from the
    start" so the circuit breaker can stay neutral. Inherits from
    AgentNotReachableError so tenacity retries (`retry_if_exception_type`) and
    callers catching `AgentNotReachableError` handle it correctly. (#474.)
    """
    pass


class AgentCircuitOpenError(AgentClientError):
    """Circuit breaker is open — agent is known to be unhealthy."""
    pass


class AgentRequestError(AgentClientError):
    """Agent returned an error response."""
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


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
        self._circuit = circuit._get_circuit(agent_name)

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
        # #1159: stamp the per-agent auth token. merge_auth_headers overrides any
        # caller-supplied X-Trinity-Agent-Token (case-insensitively) so a stale
        # or forged value can never slip past the agent middleware.
        kwargs["headers"] = merge_auth_headers(self.agent_name, kwargs.get("headers"))
        client, is_pooled = http_pool._acquire_client(self.base_url)

        try:
            response = await client.request(
                method, path, timeout=timeout, **kwargs
            )
            self._circuit.record_success()
            return response

        except asyncio.CancelledError:
            # Cancellation (e.g. MCP client drop propagating through FastAPI)
            # is not an agent-health signal. Explicit re-raise so a future
            # maintainer can't shadow it with a broader catch. (#798.)
            raise

        except circuit.CIRCUIT_FAILURE_EXCEPTIONS as e:
            # ConnectError / ConnectTimeout — agent is genuinely unreachable.
            # ConnectTimeout is a TimeoutException subclass, so this branch
            # must come before any TimeoutException catch. (#798.)
            #
            # Drop-grace override (#474 sibling-collapse fix): if a peer
            # caller on the same base_url tripped a transport drop within
            # the last _DROP_GRACE_SEC, this ConnectError is collateral —
            # the pool was evicted under us and the fresh client failed
            # to reconnect mid-burst on a still-healthy agent. Raise the
            # AgentNotReachableError subclass instead of record_failure()
            # so siblings of a single drop don't poison the circuit.
            if http_pool._is_within_drop_grace(self.base_url):
                raise AgentConnectionDroppedError(
                    f"Connection to agent {self.agent_name} dropped mid-burst "
                    f"(collateral {type(e).__name__}): {e}"
                )
            self._circuit.record_failure()
            raise AgentNotReachableError(
                f"Cannot reach agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

        except (
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            BrokenPipeError,
            ConnectionResetError,
        ) as e:
            # Transport drop mid-flight. NOT a circuit-health signal —
            # the agent itself may be fine and the connection just died
            # (upstream MCP-sync cancellation, transient socket reset,
            # broken keepalive). Do NOT record_failure(). (#474.)
            #
            # This branch is layered ABOVE circuit.TRANSIENT_TRANSPORT_EXCEPTIONS
            # so the stamp+evict side effects always run on the genuine
            # drop signals (httpx.ReadError / WriteError / RemoteProtocolError
            # — which also appear in the tuple below — plus the raw
            # OSError subclasses, which #798 deliberately let propagate).
            # The tuple-based handler below is then left only with the
            # timeout / pool-exhaustion members of the contract.
            #
            # Pool eviction: a pooled client must be removed so the next
            # call doesn't reuse a broken keepalive socket. The `is
            # client` identity check guarantees only the worker that
            # still owns the pool entry closes it; siblings in a
            # concurrent burst see the pool empty and skip. Non-pooled
            # (fresh-during-grace) clients are not in the pool, so the
            # eviction step is skipped — the outer `finally` closes
            # them either way.
            http_pool._stamp_drop(self.base_url)
            if is_pooled:
                evicted = http_pool._client_pool.pop(self.base_url, None)
                if evicted is client:
                    try:
                        await client.aclose()
                    except Exception:
                        pass
            raise AgentConnectionDroppedError(
                f"Connection to agent {self.agent_name} dropped mid-flight: "
                f"{type(e).__name__}: {e}"
            )

        except circuit.TRANSIENT_TRANSPORT_EXCEPTIONS as e:
            # Read/Write timeouts and pool exhaustion. Surface to the
            # caller as the existing typed error so `except AgentClientError`
            # blocks keep working, but DO NOT count toward the circuit
            # threshold (#474 / #798). The httpx.ReadError / WriteError /
            # RemoteProtocolError members of this tuple are intercepted
            # by the stamp+evict handler above so this branch sees only
            # the *Timeout / PoolTimeout members in practice.
            #
            # Drop-grace override (#474 sibling-collapse fix): if a peer
            # caller on the same base_url tripped a transport drop within
            # the last _DROP_GRACE_SEC, surface as AgentConnectionDroppedError
            # so tenacity / catch chains see a consistent "in-burst" signal
            # rather than a plain transient.
            if http_pool._is_within_drop_grace(self.base_url):
                raise AgentConnectionDroppedError(
                    f"Connection to agent {self.agent_name} dropped mid-burst "
                    f"(collateral {type(e).__name__}): {e}"
                )
            raise AgentNotReachableError(
                f"Transient transport error to agent {self.agent_name}: "
                f"{type(e).__name__}: {e}"[:200]
            )

        finally:
            # Non-pooled clients are single-use (returned by `_acquire_client`
            # while a drop-grace window is active so the pool isn't
            # repopulated with transient sockets). The body has already
            # buffered any successful response (default httpx is non-
            # streaming), so closing here is safe on every exit path and
            # prevents the connection-handle leak that would otherwise
            # accumulate during sustained drop bursts. (#474.)
            if not is_pooled:
                try:
                    await client.aclose()
                except Exception:
                    pass

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
        context_max = metadata.get("context_window") or DEFAULT_CONTEXT_WINDOW
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
        context_max = session_data.get("context_window") or metadata.get("context_window") or DEFAULT_CONTEXT_WINDOW
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
                context_window = session.get("context_window") or DEFAULT_CONTEXT_WINDOW
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


def get_agent_client(agent_name: str) -> AgentClient:
    """
    Factory function to create an AgentClient.

    Args:
        agent_name: Name of the agent

    Returns:
        AgentClient instance
    """
    return AgentClient(agent_name)


