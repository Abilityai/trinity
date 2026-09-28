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
from services import ask_service
from services.operator_queue_choices import ResponseNotOfferedError
from utils.helpers import iso_cutoff, utc_now_iso

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

# trinity-enterprise#611: how long an ask that ended stays listed, so the person
# it was addressed to sees how it ended instead of watching it vanish (#606).
ENDED_WINDOW_DAYS = 7


def _status_of(item: dict) -> str:
    """`pending` | `answered` | `cancelled` | `expired`.

    The ending first (trinity-enterprise#611): `disposition` when the row carries
    the ledger, else its terminal `status` (a row that ended before the ledger).
    The clock is consulted only for a row still `pending` — one past its deadline
    that the poller has not swept yet reads as expired, which is what it is.

    Answered is checked BEFORE expiry: an answer that landed is a fact, and an
    `expires_at` that has since passed does not un-answer it (ent#430 review).
    """
    disposition = item.get("disposition")
    status = item.get("status") or ""
    if disposition == "answered" or status in _ANSWERED_STATUSES:
        return "answered"
    if disposition == "cancelled" or status == "cancelled":
        return "cancelled"
    if disposition == "expired" or status == "expired":
        return "expired"
    return "expired" if _is_expired(item) else "pending"


def _ending_of(item: dict, viewer_email: Optional[str]) -> tuple:
    """`(ended_at, ended_by)` for a client — COARSE on purpose.

    `ended_by` is `you` (the viewer answered), `operator` (another person
    answered or cancelled) or `timeout`; never an email and never the cancel
    reason (both are the operator's, not the client's). `ended_at` is the
    ledger's time, or a legacy answer's time — never `created_at`, which is when
    the ask was filed, not when it ended.
    """
    status = _status_of(item)
    if status in ("pending",) or (status == "expired" and item.get("status") == "pending"):
        return None, None
    if status == "expired":
        return item.get("disposed_at"), "timeout"
    by = item.get("disposed_by_email") or (item.get("responded_by_email") if status == "answered" else None)
    who = "you" if by and viewer_email and by.lower() == viewer_email.lower() else "operator"
    at = item.get("disposed_at") or (item.get("responded_at") if status == "answered" else None)
    return at, who


def _project(item: dict, *, viewer_email: Optional[str] = None,
             resume_requested: Optional[bool] = None) -> WorkspaceAsk:
    """The explicit client-facing projection (see `WorkspaceAsk`).

    `chat_id` comes from platform-written context only — enforced since ent#429,
    which strips any agent-authored `workspace_session_id` at the ingestion
    boundary before writing the real one. Until then this docstring described an
    intention rather than a property. `context` is otherwise agent-authored and
    never forwarded.
    """
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    chat_id = context.get("workspace_session_id")
    from services.operator_queue_service import is_aged
    ended_at, ended_by = _ending_of(item, viewer_email)
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
        ended_at=ended_at,
        ended_by=ended_by,
        chat_id=chat_id if isinstance(chat_id, str) else None,
        resume_requested=resume_requested,
        sync=_coarse_sync(item),
        aging=bool(is_aged(item)),
    )


def _coarse_sync(item: dict) -> str:
    """The client-facing sync state (#2915): `confirmed | changed | closed |
    unconfirmed`. `missing` and `stale_id` collapse to `unconfirmed` and the
    reason never crosses — `agent_not_running` / `file_missing` describe the
    operator's infrastructure, not the ask.

    A row outside the file contract (raised over MCP, trinity-enterprise#611)
    has no agent file to be out of sync with: `confirmed`, never the NULL
    sync state's `unconfirmed`."""
    if item.get("channel") not in (None, "file"):
        return "confirmed"
    state = item.get("sync_state")
    if state == "confirmed":
        return "confirmed"
    if state == "changed":
        return "changed"
    if state == "closed_by_filer":
        return "closed"
    return "unconfirmed"


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


def list_asks(email: str, is_platform: bool, agent_name: Optional[str] = None,
              include_ended: bool = False) -> List[WorkspaceAsk]:
    """Asks addressed to `email`: open ones first, then — with `include_ended` —
    the ones that ended in the last `ENDED_WINDOW_DAYS`, most recent ending
    first (trinity-enterprise#611). Never raises."""
    try:
        # The addressee is a SQL condition (ent#428), NOT something filtered out
        # of the result here. `list_items` orders by status, then priority, then
        # age and applies `limit` before this code sees a row — so filtering
        # afterwards would mean "the newest 200 pending items in the FLEET, of
        # which some are yours", and one client's low-priority ask would drop
        # out of their sidebar as soon as the fleet got busy while still sitting
        # pending in the queue. Nobody else may answer it, so nobody would.
        items = db.list_operator_queue_items(
            status=None if include_ended else "pending",
            hide_ended_before=iso_cutoff(hours=ENDED_WINDOW_DAYS * 24) if include_ended else None,
            # Clear All is the OPERATOR's list hygiene (#1017): it must not make
            # an ended ask vanish from the person it was addressed to inside the
            # window — the agent's own readback ignores it for the same reason.
            include_cleared=include_ended,
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
        out.append(_project(item, viewer_email=email))
    return out


def answer_ask(item_id: str, email: str, is_platform: bool,
               response: Optional[str], response_text: Optional[str],
               acknowledge_divergence: bool = False) -> WorkspaceAsk:
    """Answer one ask as the addressee. Raises `AskError` with a named code."""
    # #2375: the decision is REQUIRED. `response` is the field the agent reads
    # (the write-back copies it to the queue file verbatim; the ent#329 resume
    # frames it as "the answer"), so a note-only body would clear the ask while
    # handing the agent an empty answer — exactly the bug this gate closes. The
    # operator route's model (`OperatorResponse.response: str`) already requires
    # it; this aligns the two contracts.
    if not (response and response.strip()):
        raise AskError(422, "empty_answer",
                       "An answer needs a decision in `response` — that is the field "
                       "the agent reads; `response_text` is only a note and cannot "
                       "stand alone.")

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
    # #2915: the agent rewrote or closed its own copy of this ask after the
    # platform ingested it. Answering the version the person read would hand the
    # agent a decision about a different question, so it is refused until the
    # person has seen that and answers anyway (the operator route mirrors this).
    if (item.get("sync_state") in ("changed", "closed_by_filer")
            and not acknowledge_divergence):
        raise AskError(409, "item_diverged",
                       "The agent changed this ask after you opened it. Review it and answer again.",
                       {"sync": _coarse_sync(item)})

    # The ask sink (trinity-enterprise#611) — the one writer of an answer, shared
    # with the operator route: the #2376 check that the answer is one the agent
    # offered, the compare-and-set with its endings ledger, the audit row, the
    # thin broadcast, and the ent#329 resume (ent#430 — ONE dispatch surface).
    # A workspace-specific write would fork all of them.
    #
    # `responded_by_id=None` is deliberate: it is a `users` FK and a workspace
    # client has no row there. Writing one would be a lie in the audit trail, so
    # the responder KIND is recorded instead — "answered by a client" must stay
    # distinguishable from "answered by an operator whose account was deleted".
    #
    # A plain `def` on a worker thread, and the sink is synchronous for exactly
    # this caller: its audit and broadcast hop to the loop, never awaited here.
    try:
        ending = ask_service.answer(
            item,
            response=response,
            response_text=response_text,
            actor=ask_service.Actor(email=email),
            responded_by_id=None,
            divergence_acknowledged=bool(
                acknowledge_divergence and item.get("sync_state") in ("changed", "closed_by_filer")
            ),
        )
    except ResponseNotOfferedError as e:
        raise AskError(422, e.code, str(e), {"offered_options": e.options})
    except ask_service.AskNotFound:
        raise AskError(409, "already_resolved", "This ask was just answered elsewhere.")
    except ask_service.AskConflict as conflict:
        # A lost race writes nothing and dispatches nothing — the sink only
        # reaches its observers on a compare-and-set it WON. Still pending means
        # the deadline refused it before the poller swept the row.
        if conflict.code == "expired":
            raise AskError(409, "expired", "This ask expired before it was answered.")
        raise AskError(409, "already_resolved", "This ask was just answered elsewhere.")
    updated = ending.rows[0]

    logger.info(
        "[WorkspaceAsks] %s answered by %s (client=%s)",
        item_id, email, not is_platform,
    )

    # ent#430 AC #5: report what was actually SCHEDULED, not what the flag
    # permits. The resume is the sink's default observer; `observers_ok` is False
    # when one of them raised, so a spawn that failed reports false. Reading the
    # opt-in here is a report of intent — see `_resume_requested`.
    agent = updated.get("agent_name") or item.get("agent_name") or ""
    dispatched = bool(ending.observers_ok and _resume_requested(agent))
    return _project(updated, viewer_email=email, resume_requested=dispatched)


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
