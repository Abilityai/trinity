"""Who may read another agent's declared metrics (trinity-enterprise#727).

Ruling R40, "narrow read now": if A may call B, A may read B's declared
series. That makes the `agent_permissions` edge `chat_with_agent` checks
(MCP `checkAgentEdge`, backend `db.is_agent_permitted`) the ONE rule for a
cross-agent metric read, and this module the one place it is spelled. The
routes (`/metrics`, `/metrics/definitions`) and the objective join all ask
`can_read_agent_metrics`; ent#80 (the wide data-lake grant) replaces the body
of that predicate rather than adding a second gate beside it.

Reading via the chat grant is the INTERIM policy — "may call" and "may read
revenue" are not separable until ent#80 gives reads their own grant.

The audit half answers "who read whose numbers" without flooding
`audit_log`, which is append-only and undeletable for 365 days: one row per
(reader, target, route) per hour, with every read also a structured log line.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from redis_breaker_util import get_breaker_redis
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

#: One audit row per (reader, target, route, actor) per this many seconds.
CROSS_READ_AUDIT_WINDOW = 3600

_AUDIT_KEY = "metrics_cross_read_audit:{reader}:{target}:{route}:{actor}"


def can_read_agent_metrics(reader_agent: str, target_agent: str) -> bool:
    """True when `reader_agent` may read `target_agent`'s declared metrics.

    Self is always readable. Otherwise the `agent_permissions` edge — the same
    row `chat_with_agent` is gated on. A grant is written only by the source's
    owner, onto a target that owner can access, and is cascaded away with
    either agent (`db/agent_cleanup.py`).
    """
    if not reader_agent or not target_agent:
        return False
    if reader_agent == target_agent:
        return True
    from database import db  # call time, so a test's store stub is honoured
    return bool(db.is_agent_permitted(reader_agent, target_agent))


async def audit_cross_agent_read(
    reader_agent: str,
    target_agent: str,
    route: str,
    *,
    actor_user: Any = None,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Record that `reader_agent` read `target_agent`'s numbers. Never raises.

    Order matters: the dedup marker is set only AFTER the row is written, so a
    failed write does not silence auditing for an hour. If the marker store is
    unreachable the row is written anyway — a duplicate row is a smaller
    failure than a missing one, and the caller's rate limit bounds the rate.

    `reader_agent` is the agent whose grant the read went through; the ACTOR
    is whoever asked. They differ on the objectives route, where a person in
    the UI reads through the path agent's grant — so the row names the person,
    and the grant holder rides in `details.reader_agent`.
    """
    logger.info(
        "[Metrics] cross-agent read: %s -> %s (%s)", reader_agent, target_agent, route)

    actor, actor_id = _actor_kwargs(actor_user, reader_agent)
    key = _AUDIT_KEY.format(reader=reader_agent, target=target_agent,
                            route=route, actor=actor_id)
    client = None
    try:
        client = get_breaker_redis()
        if client is not None and client.exists(key):
            return
    except Exception as e:  # noqa: BLE001 — marker store down: audit anyway
        logger.warning("[Metrics] cross-read audit marker unavailable: %s", e)
        client = None

    try:
        event_id = await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION,
            event_action="metrics_cross_agent_read",
            source="api",
            **actor,
            target_type="agent",
            target_id=target_agent,
            endpoint=endpoint,
            request_id=request_id,
            details={
                "reader_agent": reader_agent,
                "target_agent": target_agent,
                "route": route,
            },
        )
    except Exception as e:  # noqa: BLE001 — an audit failure never fails a read
        logger.error("[Metrics] cross-read audit failed: %s", e)
        return

    if event_id and client is not None:
        try:
            client.set(key, "1", ex=CROSS_READ_AUDIT_WINDOW)
        except Exception as e:  # noqa: BLE001
            logger.warning("[Metrics] cross-read audit marker not set: %s", e)


def _actor_kwargs(actor_user: Any, reader_agent: str) -> tuple:
    """The audit actor for this read, and a stable id for the dedup key.

    An agent key resolves to its OWNER carrying the owner's identity, and
    `platform_audit_service` ranks `actor_user` above `actor_agent_name` — so
    passing the principal would file the agent's cross read as the owner's own
    act. For an agent principal the agent is the actor, the owner rides as
    `actor_email`, and the key is named explicitly (the `capability_refusal`
    spelling in `dependencies.py`). A human is filed as the human.
    """
    if actor_user is None:
        return {"actor_agent_name": reader_agent}, f"agent:{reader_agent}"
    agent = getattr(actor_user, "agent_name", None)
    if agent:
        return {
            "actor_agent_name": agent,
            "actor_email": getattr(actor_user, "email", None),
            "mcp_key_id": getattr(actor_user, "mcp_key_id", None),
            "mcp_key_name": getattr(actor_user, "mcp_key_name", None),
            "mcp_scope": getattr(actor_user, "mcp_scope", None),
        }, f"agent:{agent}"
    return {"actor_user": actor_user}, f"user:{getattr(actor_user, 'id', None)}"
