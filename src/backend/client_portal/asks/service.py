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
from services.operator_queue_choices import (
    ResponseNotOfferedError,
    validate_response_choice,
)
from utils.helpers import utc_now_iso

from .models import WorkspaceAsk

logger = logging.getLogger(__name__)

# Only these reach a client. `alert` is included because an informational update
# is one of the three things ent#364 asks for; anything else an agent invents is
# not rendered rather than rendered as an unknown kind.
_VISIBLE_KINDS = ("question", "approval", "alert")


class AskError(Exception):
    """A named, actionable refusal — never a bare 422 from a validator."""

    def __init__(self, status_code: int, code: str, detail: str,
                 data: Optional[dict] = None):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        # #2376: extra machine-readable fields for the refusals that have them
        # (today: the options an approval actually offered). A client cannot act
        # on "that is not a valid choice" without being told what the choices
        # were, and re-deriving them by parsing the message is the thing that
        # breaks the day the wording changes.
        self.data = data or {}


def _is_expired(item: dict) -> bool:
    expires_at = item.get("expires_at")
    return bool(expires_at) and expires_at <= utc_now_iso()


# Queue statuses that mean "this has been answered". `acknowledged` is the
# operator-side terminal for the same thing; both read as answered to a client,
# which has no vocabulary for the distinction and no surface that uses it.
_ANSWERED_STATUSES = frozenset({"responded", "acknowledged"})


def _status_of(item: dict) -> str:
    """`pending` | `expired` | `answered` (ent#430 review).

    The listing only ever carries pending and expired rows, so `answered` is
    reachable from ONE place: the response to an answer that was just recorded.
    That response used to read `status: "pending"` beside
    `resume_requested: true` — a row simultaneously reporting that nobody has
    answered it and that answering it started work. Harmless while the second
    field did not exist; actively contradictory once it did.

    Answered is checked BEFORE expiry: an answer that landed is a fact, and an
    `expires_at` that has since passed does not un-answer it.
    """
    if (item.get("status") or "") in _ANSWERED_STATUSES:
        return "answered"
    return "expired" if _is_expired(item) else "pending"


def _project(item: dict, *, resume_requested: Optional[bool] = None) -> WorkspaceAsk:
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
        status=_status_of(item),
        chat_id=chat_id if isinstance(chat_id, str) else None,
        resume_requested=resume_requested,
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
    # #2376: same rule as the operator route, from the one shared validator —
    # a copy per entry point is how the two drift, and this path answers the
    # same rows.
    try:
        validate_response_choice(item, response)
    except ResponseNotOfferedError as e:
        raise AskError(422, e.code, str(e), {"offered_options": e.options})

    updated = db.respond_to_operator_queue_item(
        item_id=item_id,
        response=response or "",
        response_text=response_text,
        responded_by_id=None,
        responded_by_email=email,
    )
    # A lost race has TWO shapes and only one of them is falsy. `respond_to_
    # operator_queue_item` returns None when the row does not exist, but when the
    # row exists and has already left `pending` — the race that actually happens
    # — it returns a TRUTHY dict carrying `_status_conflict`, having written
    # nothing. Checking `if not updated` alone therefore falls through on the
    # real race, and this path then spends money dispatching a resume for an
    # answer that is not in the database, under an idempotency key derived from
    # the LOSING text — so the two answers hash differently and one queue item
    # produces two paid executions.
    #
    # `routers/operator_queue.py` pops the same flag before its own spawn; this
    # is that rule, not a new one. Popped rather than read so the sentinel never
    # reaches `_project` and becomes a client-visible field.
    if not updated or updated.pop("_status_conflict", False):
        raise AskError(409, "already_resolved", "This ask was just answered elsewhere.")

    logger.info(
        "[WorkspaceAsks] %s answered by %s (client=%s)",
        item_id, email, not is_platform,
    )

    # ent#430 — the gate. Until this, an answer given here was recorded, reached
    # the agent's queue file in about three seconds, and re-triggered nothing:
    # the operator route dispatched, the client route returned. So ent#428/#429
    # hosted a surface for answers nobody acted on, which is exactly what that
    # issue says the flag defaulting OFF was standing in for.
    #
    # Hung off the CAS WIN only, like the operator route: the 409 above already
    # returned for a lost race, so reaching here means THIS answer is the one
    # that landed. Two people answering at once produce one resume.
    #
    # Nothing about the mechanism is re-decided here — the per-agent opt-in, the
    # idempotency key, the audit row and the failure handling all live inside
    # `maybe_dispatch_resume`. ent#430's body is explicit that a second dispatch
    # surface "is how the cost, trigger-label and loop-prevention questions get
    # answered twice, differently", so this path only reaches the first one.
    #
    # `updated`, never `item`: the pre-answer read still says `pending`, and a
    # resume handed that row acts on an ask that does not yet carry its answer.
    #
    # Belt-and-braces on the raise: the spawn is fire-and-forget, but a failure
    # ON THIS LINE would still propagate, and a 500 here would tell the client
    # their answer failed when it is committed and already on its way to the
    # agent. The answer is the thing that must not be lost.
    # `updated` first, `item` as the fallback: both name the same agent, and the
    # CAS result is the row this answer actually landed on.
    agent = (updated.get("agent_name") or item.get("agent_name") or "")

    dispatched = False
    if _resume_requested(agent):
        try:
            from services import operator_resume_service

            operator_resume_service.spawn_resume_dispatch(
                updated,
                response=response or "",
                response_text=response_text,
                responded_by_email=email,
            )
            dispatched = True
        except Exception:  # noqa: BLE001 — never lose a committed answer
            logger.exception(
                "[WorkspaceAsks] resume dispatch could not be started for %s", item_id
            )

    # AC #5: report what was actually SCHEDULED, not what the flag permits.
    # This previously read the opt-in a second time after the swallowed spawn, so
    # a spawn that raised still answered `resume_requested: true` — the exact
    # over-claim ("an ask that reads as acted upon while nothing happened") the
    # docstring below says it fails closed against. `dispatched` is set only on
    # the line after the spawn returns, so the failure path reports false.
    return _project(updated, resume_requested=dispatched)


def _resume_requested(agent_name: str) -> bool:
    """Whether answering this agent's ask sets work in motion (ent#430 AC #5).

    A report of INTENT, not a promise of success: the dispatch is backgrounded,
    so at this point the only honest thing to say is whether it will be
    attempted. A failure after this lands as a FAILED execution row plus an
    `operator_resume_dispatch` audit entry (ent#329) — operator-visible, which
    is the surface that can act on it.

    This is the SAME accessor `maybe_dispatch_resume` gates on, but it is a
    SECOND read of it, taken a task hop earlier — and the earlier draft of this
    docstring claimed the two "cannot disagree", which is true of the accessor
    and false of the instant. An owner who disables the opt-in between this line
    and the dispatch gets `resume_requested: true` and no resume. That window is
    accepted rather than closed, and the reason it cannot simply be one read is
    that the two reads answer different questions: this one is synchronous and
    must produce a value for THIS response, while the dispatch's own read is the
    authority at the moment it would spend. Collapsing them either makes the
    response wait for a background task or lets a stale verdict authorise a
    spend — both worse than a rare over-report that AC #5's own remedy (the
    FAILED row plus the `operator_resume_dispatch` audit entry) already covers.

    Fails CLOSED: an unreadable flag claims nothing, because over-claiming is
    precisely the failure AC #5 names — an ask that reads as acted upon while
    nothing happened.
    """
    try:
        return bool(db.get_operator_resume_enabled(agent_name))
    except Exception:  # noqa: BLE001
        logger.warning(
            "[WorkspaceAsks] could not read the resume opt-in for %s; "
            "reporting no resume", agent_name, exc_info=True,
        )
        return False
