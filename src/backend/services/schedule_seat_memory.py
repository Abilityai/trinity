"""A scheduled run that names a user runs as that seat (trinity-enterprise#637).

A role companion's proactive brief is a scheduled run, and a scheduled run could
not touch an individual user's memory: `write_user_memory` refused every
`triggered_by='schedule'` execution, so a brief could not carry the open loops
and commitments that make the next brief better than the last. That mechanical
gap is what forced "one agent per seat" as the safe pattern; the framework's
rule is that separate agents are for separate *work*, not separate people
(operator ruling R26).

**The seat is the delivery address, read off the execution row.** ent#498's
`resolve_and_stamp` already writes `source_channel='portal'` +
`source_channel_client=<email>` onto the pre-created row before dispatch, after
the roster and block checks. This module reads that back — it never accepts an
email from the agent, which keeps MEM-001's rule intact (the caller never names
the user). `source_user_email` is deliberately NOT stamped for a seat run:
`client_portal/work` reads it as "work I started" and two stream-ownership
checks key on it, so a brief would surface as the person's own work.

**The run reads before it writes.** `write_user_memory` is whole-blob replace
(read → update → write), so a run that could write but not see the current
notes would erase them on its first call. `build_seat_caller_prompt` composes
the MEM-001 memory block plus a short seat instruction, passed as
`execute_task(system_prompt=...)`. The public-channel persona prompt (#1205) is
NOT folded in — a brief is not a public surface.

HTTP-free (Invariant #1); every function here is fail-soft or pure.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - config is always importable in the app
    from config import PORTAL_SOURCE_CHANNEL
except Exception:  # pragma: no cover
    PORTAL_SOURCE_CHANNEL = "portal"

SCHEDULE_TRIGGER = "schedule"
#: The three ways a SCHEDULE fires (`src/scheduler/main.py::_trigger_handler`):
#: cron, "Run now", and a webhook. All three address the same seat — the
#: operator who presses Run now is not the addressee — so all three run as it.
SCHEDULE_FIRE_TRIGGERS = frozenset({SCHEDULE_TRIGGER, "manual", "webhook"})
#: `schedule_executions.schedule_id` for a run that came from no schedule
#: (`/task`, chat) — mirrors `client_portal.agent_page.NO_SCHEDULE_ID`.
NO_SCHEDULE_ID = "__manual__"

SEAT_MEMORY_INSTRUCTION = """### This run serves one person

This scheduled run is addressed to a specific person, and their per-person memory is
available to you exactly as it is in a chat with them: the **"What you know about this
user"** block above is theirs, and `mcp__trinity__write_user_memory` (with this run's
`execution_id`) writes it back. Use it to carry forward what the next run needs — open
loops, commitments, context — and write the **complete** updated blob (read → update →
write); it replaces the previous notes. Every write is recorded and the person can see
and undo it."""


def seat_for_execution(execution: Any) -> Optional[str]:
    """The seat email a schedule's run serves, or None.

    Three things must all hold: the row came from a schedule (a real
    `schedule_id`, fired by cron / Run now / webhook), ent#498 stamped it with a
    Workspace destination (`source_channel='portal'`), and that stamp names a
    client. Every other row answers None — the user-facing chat triggers
    resolve their user through `source_user_email` as before, and nothing else
    may write per-user memory. A manual fire's `source_user_email` (the
    operator who pressed Run now, #1970) is deliberately not consulted: the
    brief is addressed to the seat, not to whoever fired it.
    """
    if execution is None:
        return None
    if (getattr(execution, "triggered_by", "") or "").lower() not in SCHEDULE_FIRE_TRIGGERS:
        return None
    schedule_id = (getattr(execution, "schedule_id", "") or "").strip()
    if not schedule_id or schedule_id == NO_SCHEDULE_ID:
        return None
    if (getattr(execution, "source_channel", "") or "").lower() != PORTAL_SOURCE_CHANNEL:
        return None
    client = (getattr(execution, "source_channel_client", "") or "").strip().lower()
    return client or None


def build_seat_caller_prompt(agent_name: str, email: str) -> Optional[str]:
    """The caller prompt for a seat run: the seat's memory block + the seat note.

    Returns None only when the memory could not be read at all — the seat note
    alone would invite a blind whole-blob write, which is the one thing this
    module exists to prevent. An empty memory (new seat) still yields the note,
    because there is nothing to overwrite.
    """
    from database import db
    from services.platform_prompt_service import format_user_memory_block

    try:
        record = db.get_or_create_public_user_memory(agent_name, email)
    except Exception as e:  # noqa: BLE001 — never block a scheduled run on memory
        logger.warning("[ent#637] seat memory read failed for %s/%s: %s", agent_name, email, e)
        return None
    block = format_user_memory_block(record)
    parts = [p for p in (block, SEAT_MEMORY_INSTRUCTION) if p and p.strip()]
    return "\n\n".join(parts)
