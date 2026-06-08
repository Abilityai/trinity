"""Shared builder for the unified per-agent circuit-breaker view (#526).

Composes the transport breaker (#631, ``services/agent_client.CircuitState``) and
the dispatch breaker (#526, ``services/dispatch_breaker.DispatchBreaker``) into the
single ``{dispatch, transport, open, config}`` block returned by every
breaker-reporting surface:

  * ``GET /api/agents/{name}/circuit-breaker``  (the dedicated breaker endpoint)
  * ``GET /api/agents/{name}``                  (#3 fold — saves a 2nd round-trip)
  * agent-health detail (``routers/monitoring.py``)

Extracted so the block shape is defined ONCE instead of copy-pasted at each call
site (DRY). Imports are lazy inside the function so this stays a cheap leaf
module and never drags ``agent_client`` / ``db`` into an import-time graph.
"""
from __future__ import annotations

from typing import Any, Dict


def build_circuit_breaker_block(agent_name: str) -> Dict[str, Any]:
    """Build the unified breaker block for ``agent_name``.

    Always returns a full dict — both breakers are fail-open, so this never
    raises. ``open`` is true when EITHER the dispatch breaker is open OR the
    transport breaker is open/dormant. ``config.enabled`` is the per-agent
    opt-in; ``config.global_enabled`` is the dispatch master switch.

    The transport breaker (#631) is live regardless of the dispatch master
    switch, so this helper does NOT short-circuit on ``DISPATCH_BREAKER_ENABLED``
    — a caller on a hot path (e.g. ``GET /agents/{name}``) gates the CALL itself
    when it only cares about the dispatch badge.
    """
    from config import DISPATCH_BREAKER_ENABLED
    from database import db
    from services.agent_client import CircuitState
    from services.dispatch_breaker import DispatchBreaker

    transport = CircuitState(agent_name).to_dict()
    dispatch = DispatchBreaker(agent_name).to_dict()
    return {
        "dispatch": dispatch,
        "transport": transport,
        "open": bool(
            dispatch.get("state") == "open"
            or transport.get("state") in ("open", "dormant")
        ),
        "config": {
            "enabled": bool(db.get_circuit_breaker_enabled(agent_name)),
            "global_enabled": bool(DISPATCH_BREAKER_ENABLED),
        },
    }
