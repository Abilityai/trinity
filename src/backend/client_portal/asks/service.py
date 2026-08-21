"""Business logic for Workspace asks (ent#364). OSS core since ent#428.

Reads and answers `operator_queue` rows addressed to the calling workspace user.
Owns no table: the addressee column and its ingestion-time roster validation are
OSS primitives, and answering goes through the OSS respond path so the write-back
to the agent, the audit fields and the WS broadcast all keep working exactly as
they do for an operator.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from database import db
from utils.helpers import utc_now_iso

from .models import WorkspaceAsk

logger = logging.getLogger(__name__)

# Only these reach a client. `alert` is included because an informational update
# is one of the three things ent#364 asks for; anything else an agent invents is
# not rendered rather than rendered as an unknown kind.
_VISIBLE_KINDS = ("question", "approval", "alert")


class AskError(Exception):
    """A named, actionable refusal — never a bare 422 from a validator."""

    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


def _is_expired(item: dict) -> bool:
    expires_at = item.get("expires_at")
    return bool(expires_at) and expires_at <= utc_now_iso()


def _project(item: dict) -> WorkspaceAsk:
    """The explicit client-facing projection (see `WorkspaceAsk`).

    `chat_id` comes from platform-written context only — enforced since ent#429,
    which strips any agent-authored `workspace_session_id` at the ingestion
    boundary before writing the real one. Until then this docstring described an
    intention rather than a property. `context` is otherwise agent-authored and
    never forwarded.
    """
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    chat_id = context.get("workspace_session_id")
    return WorkspaceAsk(
        id=item["id"],
        agent_name=item["agent_name"],
        kind=item.get("type") or "question",
        priority=item.get("priority") or "medium",
        title=item.get("title") or "",
        question=item.get("question") or "",
        options=item.get("options") if isinstance(item.get("options"), list) else None,
        created_at=item.get("created_at") or "",
        expires_at=item.get("expires_at"),
        status="expired" if _is_expired(item) else "pending",
        chat_id=chat_id if isinstance(chat_id, str) else None,
    )


def _on_roster(agent_name: str, email: str, is_platform: bool) -> bool:
    """Read-time roster re-check (ent#364).

    Membership at raise time is not a standing grant: a share revoked afterwards
    must stop showing the ask. Fails CLOSED — an unreadable roster hides the ask
    rather than showing one we cannot justify.
    """
    try:
        from client_portal.service import agent_on_roster

        return bool(agent_on_roster(agent_name, email, include_owned=is_platform))
    except Exception:  # noqa: BLE001
        logger.warning("[WorkspaceAsks] roster re-check failed for %s", agent_name, exc_info=True)
        return False


def list_asks(email: str, is_platform: bool, agent_name: Optional[str] = None) -> List[WorkspaceAsk]:
    """Open asks addressed to `email`, newest first. Never raises."""
    try:
        # The addressee is a SQL condition (ent#428), NOT something filtered out
        # of the result here. `list_items` orders by status, then priority, then
        # age and applies `limit` before this code sees a row — so filtering
        # afterwards would mean "the newest 200 pending items in the FLEET, of
        # which some are yours", and one client's low-priority ask would drop
        # out of their sidebar as soon as the fleet got busy while still sitting
        # pending in the queue. Nobody else may answer it, so nobody would.
        items = db.list_operator_queue_items(
            status="pending",
            agent_name=agent_name,
            addressed_to_email=email,
            limit=200,
        )
    except Exception:  # noqa: BLE001 — a sidebar badge must not break the Workspace
        logger.warning("[WorkspaceAsks] list failed", exc_info=True)
        return []

    # Memoized per REQUEST, not cached across them: `agent_on_roster` is
    # `agent_name in roster_agent_names(...)` and that inner call is one-to-two
    # DB reads, so asking it per item made this O(items) queries for an answer
    # that cannot change inside one request — on an endpoint the Workspace polls
    # every 20s, per signed-in client, per open tab. A client's asks cluster on
    # one or two agents, so in practice this is 1-2 reads instead of N.
    #
    # Deliberately memoizing `_on_roster` rather than hoisting
    # `roster_agent_names` up here: that function IS the access predicate
    # ("the scope of what a caller can DO must equal the scope of what they can
    # SEE"), and re-implementing membership beside it is how the two drift. It
    # also keeps the fail-CLOSED behaviour per agent, unchanged.
    seen: dict[str, bool] = {}

    def _allowed(agent: str) -> bool:
        if agent not in seen:
            seen[agent] = _on_roster(agent, email, is_platform)
        return seen[agent]

    out: List[WorkspaceAsk] = []
    for item in items or []:
        if item.get("type") not in _VISIBLE_KINDS:
            continue
        if not _allowed(item.get("agent_name") or ""):
            continue
        out.append(_project(item))
    return out


def answer_ask(item_id: str, email: str, is_platform: bool,
               response: Optional[str], response_text: Optional[str]) -> WorkspaceAsk:
    """Answer one ask as the addressee. Raises `AskError` with a named code."""
    if not (response or response_text):
        raise AskError(422, "empty_answer",
                       "An answer needs a choice or some text — an empty answer would "
                       "clear the ask while telling the agent nothing.")

    item = db.get_operator_queue_item(item_id)
    # Uniform 404 for missing / not-mine / off-roster: a distinguishable 403 would
    # let any client enumerate which ask ids exist (Invariant #8).
    if (
        not item
        or (item.get("addressed_to_email") or "").lower() != email.lower()
        or not _on_roster(item.get("agent_name") or "", email, is_platform)
    ):
        raise AskError(404, "not_found", "Ask not found")

    if item.get("status") != "pending":
        # 400, matching `POST /api/operator-queue/{id}/respond` exactly (ent#428
        # AC #6). The operator path spends its two codes on two different
        # things: 400 for "it was already resolved when you looked", 409 for
        # "someone resolved it between your read and your write". Collapsing
        # both onto 409 here would make a client's refusal say less than an
        # operator's about the same row — and the lost-race branch below is the
        # one that genuinely is a 409.
        raise AskError(400, "already_resolved",
                       f"This ask is already {item.get('status')}.")
    if _is_expired(item):
        raise AskError(409, "expired",
                       "This ask expired before it was answered.")

    # The OSS respond path, unchanged: it writes the answer back to the agent's
    # queue file (the 5s sync loop), stamps the audit fields and broadcasts. A
    # workspace-specific write would fork all three.
    #
    # `responded_by_id=None` is deliberate: it is a `users` FK and a workspace
    # client has no row there. Writing one would be a lie in the audit trail, so
    # the responder KIND is recorded instead — "answered by a client" must stay
    # distinguishable from "answered by an operator whose account was deleted".
    updated = db.respond_to_operator_queue_item(
        item_id=item_id,
        response=response or "",
        response_text=response_text,
        responded_by_id=None,
        responded_by_email=email,
    )
    if not updated:
        # Lost a race with an operator answering the same row.
        raise AskError(409, "already_resolved", "This ask was just answered elsewhere.")

    logger.info(
        "[WorkspaceAsks] %s answered by %s (client=%s)",
        item_id, email, not is_platform,
    )
    return _project(updated)
