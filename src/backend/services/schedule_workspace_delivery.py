"""Deliver a scheduled run's output into a Workspace conversation (ent#498).

A role companion's daily brief is a scheduled playbook call whose output has to
reach one primary human where they already work. Until now every scheduled run
terminated in an execution row — the operator's surface — and a person who is
not the operator never saw it.

**Almost all of this already existed.** `channel_completion_report` has resolved,
persisted and effect-guarded a portal-bound completion since ent#457, and
`report_completion` is trigger-agnostic: `schedule` is deliberately NOT in
`INLINE_CHANNEL_TRIGGERS`, because a scheduled run has no surface that already
answered. What was missing was only that a scheduled execution row never carried
`source_channel='portal'`. This module is that stamp, and nothing else — no new
delivery path, no second applier, no change to any terminal writer.

**Why the resolution happens here and not in the scheduler.** The scheduler is a
separate process that cannot import the portal package, so it cannot answer
"which session". It also creates the execution row itself and always sends
`execution_id`, so `execute_task`'s channel-persisting branch can never run for a
cron fire and passing the columns as kwargs would be silently inert (#2426). So
the scheduler carries the ADDRESS and the backend resolves and stamps.

HTTP-free by design (Invariant #1): it raises `WorkspaceDeliveryRefused` and the
router maps it. Nothing here writes a terminal.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: The `source_channel` value the portal resolver dispatches on. Imported rather
#: than spelled, so this cannot drift from the resolver that reads it.
try:  # pragma: no cover - config is always importable in the app
    from config import PORTAL_SOURCE_CHANNEL
except Exception:  # pragma: no cover
    PORTAL_SOURCE_CHANNEL = "portal"


class WorkspaceDeliveryRefused(Exception):
    """The named person cannot receive this schedule's output.

    Carries a stable machine `reason` beside the sentence, because this becomes
    the `error` on a FAILED execution row that an operator reads days later and
    a future UI will want to branch on.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def resolve_and_stamp(execution_id: str, agent_name: str, email: str) -> str:
    """Point ``execution_id`` at ``email``'s Main chat with ``agent_name``.

    Returns the resolved session id. Raises `WorkspaceDeliveryRefused` when the
    address cannot receive the brief — the caller turns that into a **visible
    failure on the execution row**, never a silent no-op (AC 5). Refusing before
    the turn runs is both cheaper and louder than running it and dropping the
    answer somewhere nobody can read.

    Access is checked against **where the message will land**:
    `agent_on_roster(..., include_owned=True)` is the Workspace's own roster
    (shared ∪ owned). `email_has_agent_access` was rejected for this: it admits
    any admin, and an admin who neither owns the agent nor is shared it cannot
    open that thread, so a brief delivered there would be invisible.

    The destination is **Main** (ruled 2026-09-06), through the same
    `ensure_main_session` an agent-initiated message and an ask outside a chat
    already use — a brief is not a fourth thing that decides where to land.
    """
    from client_portal import db as portal_db
    from client_portal.service import agent_on_roster, ensure_main_session
    from database import db

    address = (email or "").strip().lower()
    if not address:
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_no_target",
            "The schedule names no Workspace delivery address.",
        )

    # Fail CLOSED on an unreadable block list: `is_client_blocked` deliberately
    # does not swallow its own errors, and a block that evaporates on a bad read
    # is not a block.
    try:
        blocked = portal_db.is_client_blocked(address)
    except Exception as e:  # noqa: BLE001
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_target_unverifiable",
            f"Could not check whether {address} may receive messages ({type(e).__name__}).",
        ) from e
    if blocked:
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_target_blocked",
            f"{address} is blocked from this Workspace, so the output was not delivered.",
        )

    try:
        on_roster = agent_on_roster(agent_name, address, True)
    except Exception as e:  # noqa: BLE001
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_target_unverifiable",
            f"Could not check whether {address} can reach {agent_name} "
            f"({type(e).__name__}).",
        ) from e
    if not on_roster:
        # One message for "never had access" and "no longer has access": telling
        # them apart would need a second lookup and would say, to whoever reads
        # the execution row, whether an address is known to this instance.
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_target_unreachable",
            f"{address} cannot reach {agent_name} in the Workspace — the agent is "
            f"neither shared with them nor owned by them, so there is nowhere to "
            f"deliver this run's output.",
        )

    try:
        session_id = ensure_main_session(agent_name, address)
    except Exception as e:  # noqa: BLE001
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_session_unavailable",
            f"Could not open {address}'s main conversation with {agent_name} "
            f"({type(e).__name__}).",
        ) from e

    stamped = db.stamp_execution_channel_context(
        execution_id,
        source_channel=PORTAL_SOURCE_CHANNEL,
        source_channel_chat_id=session_id,
        # The portal resolver matches this against the SESSION's own
        # `client_email` and fails closed when they differ — so this is not
        # decoration, it is the recipient check's other half.
        source_channel_client=address,
    )
    if not stamped:
        # The row already carries a destination, or is gone. Either way this is
        # not ours to repoint: an inbound channel turn's adapter is waiting on
        # that answer.
        raise WorkspaceDeliveryRefused(
            "workspace_delivery_row_not_stampable",
            f"Execution {execution_id} already has a delivery destination, or no "
            f"longer exists — the Workspace delivery was not attached.",
        )

    logger.info(
        "[ent#498] execution %s will deliver to %s's main chat with %s (session %s)",
        execution_id, address, agent_name, session_id,
    )
    return session_id
