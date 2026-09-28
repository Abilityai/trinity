"""
Agent report service (#918).

Persists an agent-published structured report and broadcasts a **thin trigger**
over WebSocket. Data flow:

    agent --MCP report tool--> POST /api/agents/{name}/reports (self-gated)
        --> report_service.create_report
            --> db.create_report (SQLite/PG)
            --> thin WS event {agent_name, report_id, report_type, created_at}
                |                                   |
                v                                   v
            /ws (SCOPE_ALL, all UI clients)    /ws/events (SCOPE_SCOPED, access-filtered)
                |
                v
        frontend store REFETCHES via the access-controlled REST endpoint

CRITICAL (review A1): ``/ws`` is SCOPE_ALL and unfiltered — every logged-in
browser receives a SCOPE_ALL event. So the broadcast carries NO ``title`` and NO
``payload`` (which can hold sensitive domain data); only the trigger metadata.
The frontend fetches the actual content through the access-gated REST endpoint.
"""

import json
import logging
from typing import Dict, Optional

from database import db

logger = logging.getLogger(__name__)

# WebSocket managers, injected from main.py at startup (same pattern as
# routers/notifications.py). ``broadcast`` → /ws (SCOPE_ALL);
# ``broadcast_filtered`` → /ws/events (SCOPE_SCOPED, access-filtered).
_websocket_manager = None
_filtered_websocket_manager = None


def set_websocket_manager(manager) -> None:
    global _websocket_manager
    _websocket_manager = manager


def set_filtered_websocket_manager(manager) -> None:
    global _filtered_websocket_manager
    _filtered_websocket_manager = manager


def _resolve_portal_session(execution_id: str, agent_name: str) -> Optional[str]:
    """The Workspace chat a publishing turn belongs to, or None (ent#365).

    Moved here from `routers/reports.py` by ent#610 (Invariant #1: the router
    holds no business logic, and the resolution grew a second half).

    Two gates, in this order: the execution must belong to THIS agent
    (`resolve_and_validate_execution`, the MEM-001 rule — the agent supplies an
    id, never its own identity), and the id must be the turn currently in flight
    for a portal session, which is what the ent#286 reverse marker answers.

    Fail-soft to None everywhere: a report with no chat still lists on the agent
    page, whereas a 5xx here would fail a publish over a card placement. The
    marker is Redis-backed with a TTL sized to the turn.
    """
    try:
        from services.idempotency_service import resolve_and_validate_execution
        if resolve_and_validate_execution(execution_id, agent_name) is None:
            return None
        from client_portal import service as portal_service
        return portal_service.get_inflight_session_for_execution(execution_id)
    except Exception as e:  # noqa: BLE001
        # WARNING, not debug (caught in review on #2383). Fail-soft is right —
        # a card placement must never fail a publish — but a Redis outage, an
        # import error or a renamed marker key would otherwise make every card
        # silently stop appearing in the turn's chat.
        logger.warning(
            "portal session resolution failed for execution %s (%s) — the "
            "report will fall back to the addressee's Main",
            execution_id, type(e).__name__,
        )
        return None


def resolve_report_session(execution_id: Optional[str], agent_name: str,
                           audience: Optional[str], *,
                           allow_main: bool) -> Optional[str]:
    """The Workspace chat an addressed report is stamped to (ent#365, ent#610).

    * No `audience` → None: an unaddressed report is operator-only.
    * The publishing turn's in-flight chat, **only if the addressee owns it**.
      A report addressed to X during Y's turn used to be stamped into Y's chat,
      where X's unread arm never counted it and Y's inline read (audience = Y)
      never showed it — the card was in nobody's chat.
    * Otherwise, **only when `allow_main`**, the addressee's **Main**
      (`ensure_main_session`, race-safe by
      the partial unique index). Resolving mints but never TOUCHES: the caller
      touches the stamped chat with `touch_report_session` only after the
      report row is written (/review, ent#610), so a failed insert cannot leave
      an empty Main listed in the sidebar.

    This is the ent#523 landing rule ("an agent-initiated thing lands in Main")
    applied to deliverables, with the same move as
    `schedule_workspace_delivery.resolve_and_stamp`. It gives the report a chat,
    an inline card, an anchor and an unread count. The audience was already
    roster-validated (`include_owned=False`) by the caller, so this is a bounded
    push channel to people the agent is shared with — never an arbitrary email.

    **`allow_main` is the publisher gate, and it has no default on purpose** —
    every call site states it. The publish route is gated by `AuthorizedAgent`,
    so every human the agent is shared with can publish AS the agent; the Main
    fallback is for the agent's OWN publish (an agent-scoped key for this
    agent) only. Otherwise one sharer could address a report to another person
    on the roster and mint and touch that person's Main — a badge, an excerpt
    and a card in their Inbox the agent never produced (/cso, ent#610). A
    human publish keeps the addressee-owned in-flight chat, else None.

    Fails soft to None with a WARNING, like its sibling: a card placement must
    never fail a publish.
    """
    if not audience:
        return None
    from client_portal import db as portal_db
    from client_portal import service as portal_service

    if execution_id:
        sid = _resolve_portal_session(execution_id, agent_name)
        if sid:
            try:
                if portal_db.get_portal_session(sid, agent_name, audience):
                    return sid
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "report session ownership check failed for %s (%s) — "
                    "falling back to the addressee's Main", agent_name, type(e).__name__,
                )
    if not allow_main:
        return None
    try:
        return portal_service.ensure_main_session(agent_name, audience)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "could not stamp a report for %s to the addressee's Main (%s) — it "
            "will publish without an in-chat card", agent_name, type(e).__name__,
        )
        return None


def touch_report_session(session_id: str) -> None:
    """Advance a stamped chat's `last_message_at` once its report is WRITTEN.

    The touch keeps a report-only Main visible to the sidebar
    (`is_main && !last_message_at` is hidden) and moves the chat in the list;
    `added=0` because a report is not a message, so Reset's "untouched Main"
    test (`message_count == 0`) holds. Called after the insert, never before:
    a failed insert must not list an empty Main. Fails soft with a WARNING — a
    card placement never fails a publish that has already been written.
    """
    from client_portal import db as portal_db
    from utils.helpers import utc_now_iso

    try:
        portal_db.touch_portal_session(session_id, utc_now_iso(), added=0)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not touch the chat a report was stamped to (%s)",
                       type(e).__name__)


async def _broadcast_report(report: Dict) -> None:
    """Broadcast a THIN report trigger — never title/payload (review A1)."""
    event = {
        "type": "agent_report",
        "event": "agent_report",
        "agent_name": report["agent_name"],
        "report_id": report["id"],
        "report_type": report["report_type"],
        "created_at": report["created_at"],
    }
    if _websocket_manager:
        await _websocket_manager.broadcast(json.dumps(event))
    if _filtered_websocket_manager:
        await _filtered_websocket_manager.broadcast_filtered(event)


async def create_report(
    agent_name: str,
    user_id: Optional[int],
    report_type: str,
    title: str,
    payload: Dict,
    display_hint: Optional[str] = None,
    schema_version: int = 1,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    addressed_to_email: Optional[str] = None,
    portal_session_id: Optional[str] = None,
) -> Dict:
    """Persist a report and broadcast its thin trigger. Returns the full report.

    ent#365: the audience and the producing chat arrive already decided — the
    router validates the addressee against the agent's roster and resolves the
    session from the publishing turn. They are deliberately NOT added to the
    broadcast: `/ws` is SCOPE_ALL and unfiltered (#918), so the trigger stays
    `{agent_name, report_id, report_type, created_at}` and an addressee's email
    never rides a fleet-wide channel.
    """
    report = db.create_report(
        agent_name=agent_name,
        user_id=user_id,
        report_type=report_type,
        title=title,
        payload=payload,
        display_hint=display_hint,
        schema_version=schema_version,
        period_start=period_start,
        period_end=period_end,
        addressed_to_email=addressed_to_email,
        portal_session_id=portal_session_id,
    )
    await _broadcast_report(report)
    return report
