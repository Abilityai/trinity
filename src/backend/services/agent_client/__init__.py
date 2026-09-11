"""HTTP client for agent-container communication, and the transport circuit breaker.

#1028: this was one 1,294-line module; it is now three — `circuit` (the #631
breaker), `http_pool` (the per-agent httpx pool), `client` (the AgentClient
and its typed errors) — with the public surface re-exported here, so
`from services.agent_client import get_agent_client` and every sibling import
are unchanged.

Collaborators are not mirrored on the package (the git_service rule): a
private function that exists both here and on its owning module can be
monkeypatched on the wrong one and silently detach, so a stale patch must
raise instead.
"""
from .circuit import (  # noqa: F401
    CIRCUIT_BASE_COOLDOWN_SECONDS,
    CIRCUIT_DORMANT_AFTER_OPEN_PROBES,
    CIRCUIT_DORMANT_COOLDOWN_SECONDS,
    CIRCUIT_FAILURE_EXCEPTIONS,
    CIRCUIT_FAILURE_THRESHOLD,
    CIRCUIT_MAX_COOLDOWN_SECONDS,
    CircuitState,
    force_circuit_dormant,
    get_all_circuit_states,
    is_circuit_failure,
    reset_circuit,
)
from .client import (  # noqa: F401
    AgentChatMetrics,
    AgentChatResponse,
    AgentCircuitOpenError,
    AgentClient,
    AgentClientError,
    AgentConnectionDroppedError,
    AgentNotReachableError,
    AgentRequestError,
    AgentSessionInfo,
    get_agent_client,
)
from .http_pool import close_all_clients  # noqa: F401
