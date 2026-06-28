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
from typing import Dict, Optional

from database import db

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
) -> Dict:
    """Persist a report and broadcast its thin trigger. Returns the full report."""
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
    )
    await _broadcast_report(report)
    return report
