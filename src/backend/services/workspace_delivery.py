"""Where a schedule's output lands in someone's Workspace (ent#498).

A schedule can name ONE Workspace user as its delivery target
(``agent_schedules.deliver_to_workspace_email``). When it fires, the
execution's terminal is filed as a new turn in that person's conversation with
the agent, through the completion-report machinery that already exists
(``channel_completion_report._resolve_portal`` → ``ensure_thread_for_ask`` →
``report_completion``). This module is the ONE place that answers two questions
for that path:

  1. **May this agent deliver to this person at all?** — ``validate_target``,
     called at schedule create/update so a misconfiguration is a named 400 at
     config time, and again at fire time so a share revoked in between is a
     visible failure rather than a silent no-op (AC #5).
  2. **Which thread?** — ``resolve_delivery_session``.

**The named seam (dispatch note, 2026-09-06).** ent#523 rules that the
destination is the user's **Main chat** with the agent. That surface is ruled
but not built: nothing in the platform can yet identify which of a person's
threads is "the Main one". So ``resolve_delivery_session`` resolves to the
user's most recent thread with the agent — exactly what ``ensure_thread_for_ask``
(#429) already does for an addressed operator-queue ask, deliberately reusing
that function rather than re-deriving "which thread", because two definitions of
that is how the ask surface and this one would start disagreeing about where a
person's work lands.

When ent#523 ships, the ONLY edit is the body of ``resolve_delivery_session``.
Nothing downstream of it knows how the session was chosen: the internal dispatch
path receives a session id, stamps it on the row, and the delivery leg reads the
row. That is what "behind one named seam" buys — the resolution can change
without touching the delivery path.

**Why validation is split from resolution.** Resolving CREATES a thread when the
person has never chatted with the agent (`_resolve_session_id`'s own behaviour).
That is right at fire time — a brief for someone who has never opened the chat
still has to land — and wrong at config time, where a typo'd address would
silently mint an empty thread for a stranger. So the create/update path calls
``validate_target`` only, which reads and never writes.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class WorkspaceDeliveryError(Exception):
    """The named target cannot be delivered to.

    Carries a machine-readable ``code`` so both callers can be specific: the
    schedule router turns it into a 400 the operator can act on, and the
    internal dispatch path writes it onto the execution row (AC #5 — "never a
    silent no-op").
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_target_email(email: Optional[str]) -> Optional[str]:
    """Lowercase + trim, or raise ``WorkspaceDeliveryError``. ``None``/blank in
    means ``None`` out — an absent target is not an invalid one.

    Normalising HERE is what makes the stored value comparable to
    ``enterprise_portal_sessions.client_email`` and to the roster, both of which
    key on the lowercased address. A schedule storing ``Ada@Example.com`` while
    the share reads ``ada@example.com`` would validate at config time and then
    fail closed at delivery — the worst possible split.
    """
    if email is None:
        return None
    candidate = email.strip()
    if not candidate:
        return None
    from client_portal.service import ClientPortalError, normalize_client_email

    try:
        return normalize_client_email(candidate)
    except ClientPortalError as exc:
        raise WorkspaceDeliveryError(
            "workspace_delivery_invalid_email",
            f"'{candidate[:80]}' is not a valid email address.",
        ) from exc


def validate_target(agent_name: str, email: str) -> str:
    """The address, normalized, iff ``agent_name`` is reachable in that person's
    Workspace. Raises ``WorkspaceDeliveryError`` otherwise. Reads only.

    ``include_owned=True`` because the recipient may be either kind of Workspace
    principal: an external client (the agent was SHARED with them) or a signed-in
    platform user reaching the same surface in one click (ent#357 — where their
    own agents are on the roster and are never in the shared set, since Trinity
    refuses a self-share). Answering "is this agent on this person's roster"
    without the owned half would refuse a schedule delivering to the agent's own
    owner, which is the single most obvious use of this feature.

    Fails CLOSED on a lookup error: an unverifiable target must not be accepted
    at config time, and at fire time it is exactly the case that must not deliver.
    """
    from client_portal.service import agent_on_roster

    normalized = normalize_target_email(email)
    if not normalized:
        raise WorkspaceDeliveryError(
            "workspace_delivery_invalid_email",
            "A Workspace delivery target needs an email address.",
        )
    try:
        on_roster = agent_on_roster(agent_name, normalized, include_owned=True)
    except Exception as exc:  # noqa: BLE001 — fail closed, never accept unverified
        logger.warning(
            "[ent#498] roster lookup failed for %s/%s: %s", agent_name, normalized, exc
        )
        raise WorkspaceDeliveryError(
            "workspace_delivery_target_unverifiable",
            "Could not verify Workspace access for this address. Try again.",
        ) from exc
    if not on_roster:
        raise WorkspaceDeliveryError(
            "workspace_delivery_no_access",
            f"'{normalized}' has no Workspace access to agent '{agent_name}'. "
            f"Share the agent with that address first.",
        )
    return normalized


def resolve_delivery_session(agent_name: str, email: str) -> str:
    """The session id this schedule's output belongs in. **The ent#523 seam.**

    Today: the person's most recent thread with the agent, opening one only if
    they have never chatted (``ensure_thread_for_ask``). When the Main chat
    (ent#523) exists, this function resolves to it and nothing else changes.

    Re-validates the target rather than trusting the stored column: the column
    was checked when it was written, and a share can be revoked at any point
    between then and the fire. Access is a live fact, not a stored one.
    """
    normalized = validate_target(agent_name, email)
    from client_portal.service import ensure_thread_for_ask

    try:
        session_id = ensure_thread_for_ask(agent_name, normalized)
    except Exception as exc:  # noqa: BLE001 — surfaced on the row, never swallowed
        logger.warning(
            "[ent#498] thread resolution failed for %s/%s: %s",
            agent_name, normalized, exc,
        )
        raise WorkspaceDeliveryError(
            "workspace_delivery_thread_unresolved",
            "Could not resolve a Workspace conversation for this address.",
        ) from exc
    if not session_id:
        raise WorkspaceDeliveryError(
            "workspace_delivery_thread_unresolved",
            "Could not resolve a Workspace conversation for this address.",
        )
    return session_id
