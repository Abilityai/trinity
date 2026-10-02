"""Business logic for Workspace asks (ent#364). OSS core since ent#428.

Reads and answers `operator_queue` rows addressed to the calling workspace user.
Owns no table: the addressee column and its ingestion-time roster validation are
OSS primitives, and answering goes through the OSS respond path so the write-back
to the agent, the audit fields and the WS broadcast all keep working exactly as
they do for an operator.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
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


class AsksUnavailable(Exception):
    """The asks list could not be READ (trinity-enterprise#610, PR A0).

    Raised instead of returning `[]`: an empty list is a claim ("nothing needs
    you"), and making it during an outage is the #2915 failure class. The router
    maps it to 503 `asks_unavailable`; the store keeps its last good list.
    """


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
    """`pending` | `answered` | `dismissed` | `cancelled` | `expired`.

    `dismissed` (trinity-enterprise#748) is the addressee's own "no answer"; the
    row's status is `cancelled`, so the ledger is read before the status.

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
    if disposition == "dismissed":
        return "dismissed"
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
    never forwarded; `proposal` is forwarded by name (trinity-enterprise#611),
    because it is the action the addressee is being asked to approve.
    """
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    chat_id = context.get("workspace_session_id")
    # ent#734: platform-written, stripped from agent content at both ingestion
    # boundaries. Only the platform's literal True counts.
    raised_in_turn = context.get("workspace_raised_in_turn") is True
    # ent#747: the chat its addressee opened to discuss it — platform-written,
    # stripped from agent content like the two keys above.
    discussion = context.get("workspace_discussion_id")
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
        proposal=item.get("proposal") if isinstance(item.get("proposal"), dict) else None,
        created_at=item.get("created_at") or "",
        expires_at=item.get("expires_at"),
        status=_status_of(item),
        ended_at=ended_at,
        ended_by=ended_by,
        chat_id=chat_id if isinstance(chat_id, str) else None,
        raised_in_turn=raised_in_turn,
        discussion_chat_id=discussion if isinstance(discussion, str) and discussion else None,
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


def _on_roster(agent_name: str, email: str, is_platform: bool,
               strict: bool = False) -> bool:
    """Read-time roster re-check (ent#364).

    Membership at raise time is not a standing grant: a share revoked afterwards
    must stop showing the ask. Fails CLOSED — an unreadable roster never shows an
    ask we cannot justify.

    `strict` is the LIST mode (trinity-enterprise#610, PR A0): there an
    unreadable roster raises `AsksUnavailable`, because hiding every ask during a
    roster outage is a silent "nothing needs you". A clean "not on the roster"
    still returns False in both modes. The answer path stays non-strict, so its
    refusal is the uniform 404 whatever the cause (Invariant #8).
    """
    try:
        from client_portal.service import agent_on_roster

        return bool(agent_on_roster(agent_name, email, include_owned=is_platform))
    except Exception as e:  # noqa: BLE001
        logger.warning("[WorkspaceAsks] roster re-check failed for %s", agent_name, exc_info=True)
        if strict:
            raise AsksUnavailable("roster unreadable") from e
        return False


# #3059: the page size, and the ceiling on it. The default keeps a plain read
# exactly as big as it always was; what changed is that it now says how many
# there are and how to get the rest, instead of stopping silently.
PAGE_MAX = 200
_CURSOR_RE = re.compile(r"^v1:(\d{1,9})$")


@dataclass
class AsksPage:
    """One page of the viewer's visible asks, the size of the whole visible set,
    and — while more remain — the cursor for the next page."""
    items: List[WorkspaceAsk] = field(default_factory=list)
    total: int = 0
    next_cursor: Optional[str] = None


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: Optional[str]) -> int:
    """Opaque to clients; an offset over a stably ordered set inside. Anything
    unreadable is a named 422, never a silent first page."""
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
    except Exception:  # noqa: BLE001 — every malformed shape is the same refusal
        raw = ""
    m = _CURSOR_RE.match(raw)
    if not m:
        raise AskError(422, "invalid_cursor", "That page cursor is not valid. Reload the list.")
    return int(m.group(1))


def list_asks_page(email: str, is_platform: bool, agent_name: Optional[str] = None,
                   include_ended: bool = False, limit: int = PAGE_MAX,
                   cursor: Optional[str] = None, chat_id: Optional[str] = None) -> AsksPage:
    """Asks addressed to `email`: open ones first, then — with `include_ended` —
    the ones that ended in the last `ENDED_WINDOW_DAYS`, most recent ending
    first (trinity-enterprise#611). Paged (#3059).

    Raises `AskError` for an unreadable cursor, and `AsksUnavailable` when the
    queue or the roster cannot be read (trinity-enterprise#610, PR A0) — an
    outage is never answered with an empty page, which the client would render
    as "nothing is waiting".

    `chat_id` (trinity-enterprise#610, the 09-30 ruling amended) is ONE chat's
    own read: the asks the platform stamped as raised in a turn of that chat
    (ent#734), pending and ended with NO ended window and cleared rows kept —
    an ended chat-turn ask stays in that chat's history for the queue's own
    retention (trinity#1142), not the Inbox's 7 days. `include_ended` is
    implied. The SQL match on the stored context is a prefilter; the parsed,
    top-level platform keys decide (`_in_chat`)."""
    offset = _decode_cursor(cursor)
    limit = max(1, min(int(limit), PAGE_MAX))
    # Every filter the viewer's visibility depends on is a SQL condition,
    # applied BEFORE the limit (#3059). The addressee always was (ent#428, see
    # `list_items`: a post-hoc filter reads "the newest 200 items in the FLEET,
    # some of which are yours"). The visible kinds and the roster re-check used
    # to run here, on the result — so a page of 200 rows could render fewer,
    # and no count taken in SQL could be the viewer's real total.
    filters = dict(
        status=None if include_ended else "pending",
        hide_ended_before=iso_cutoff(hours=ENDED_WINDOW_DAYS * 24) if include_ended else None,
        # Clear All is the OPERATOR's list hygiene (#1017): it must not make
        # an ended ask vanish from the person it was addressed to inside the
        # window — the agent's own readback ignores it for the same reason.
        include_cleared=include_ended,
        agent_name=agent_name,
        addressed_to_email=email,
        types=_VISIBLE_KINDS,
    )
    if chat_id is not None:
        filters.update(status=None, hide_ended_before=None, include_cleared=True,
                       context_contains=_chat_turn_fragments(chat_id))
    try:
        # The roster predicate is unchanged — `_on_roster`, asked once per
        # AGENT, fail-closed per agent (ent#428) — it is just asked of the
        # agents the viewer's asks span, and the answer goes into the SQL as the
        # access set. Re-implementing membership here is how the two drift.
        # ent#747: a chat also holds the ask it was opened to DISCUSS — read
        # here, inside the same 503 boundary, on the first page only.
        discussed = (_discussed_in(chat_id, email, is_platform, agent_name)
                     if chat_id is not None and offset == 0 else [])
        allowed = {a for a in db.list_operator_queue_agent_names(**filters)
                   if _on_roster(a, email, is_platform, strict=True)}
        if allowed:
            total = db.count_operator_queue_items(accessible_agent_names=allowed, **filters)
            items = db.list_operator_queue_items(accessible_agent_names=allowed,
                                                 limit=limit, offset=offset, **filters)
        else:
            total, items = 0, []
    except AsksUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — surfaced as a 503, never as "nothing" (A0)
        logger.warning("[WorkspaceAsks] list failed", exc_info=True)
        raise AsksUnavailable("queue unreadable") from e

    out = [_project(item, viewer_email=email) for item in items or []]
    if chat_id is not None:
        out = [a for a in out if _in_chat(a, chat_id)]
    end = offset + len(items or [])
    next_cursor = _encode_cursor(end) if end < total else None
    seen = {a.id for a in out}
    extra = [a for a in discussed if a.id not in seen]
    return AsksPage(items=out + extra, total=total + len(extra), next_cursor=next_cursor)


def _discussed_in(chat_id: str, email: str, is_platform: bool,
                  agent_name: Optional[str]) -> List[WorkspaceAsk]:
    """The ask (at most one in practice) whose addressee opened `chat_id` to
    discuss it (trinity-enterprise#747) — pending or ended, cleared or not, as
    long as the viewer may still read it. The SQL match on the stored context
    is a prefilter; the parsed platform key decides."""
    from services.operator_queue_service import _WORKSPACE_DISCUSSION_KEY
    fragment = json.dumps({_WORKSPACE_DISCUSSION_KEY: chat_id})[1:-1]
    rows = db.list_operator_queue_items(
        agent_name=agent_name, addressed_to_email=email, types=_VISIBLE_KINDS,
        include_cleared=True, context_contains=(fragment,), limit=5,
    ) or []
    out = []
    for row in rows:
        ask = _project(row, viewer_email=email)
        if (ask.discussion_chat_id == chat_id
                and _on_roster(row.get("agent_name") or "", email, is_platform, strict=True)):
            out.append(ask)
    return out


def _chat_turn_fragments(chat_id: str) -> tuple:
    """The two platform keys as the queue stores them (`json.dumps` of the
    context, `db.operator_queue.create_item`), for the SQL prefilter."""
    from services.operator_queue_service import _WORKSPACE_THREAD_KEY, _WORKSPACE_TURN_KEY
    return (json.dumps({_WORKSPACE_THREAD_KEY: chat_id})[1:-1],
            json.dumps({_WORKSPACE_TURN_KEY: True})[1:-1])


def _in_chat(ask: WorkspaceAsk, chat_id: str) -> bool:
    """The projection's own reading of the platform's TOP-LEVEL keys — a key
    an agent nested deeper in its context matches the prefilter text, not this."""
    return ask.raised_in_turn is True and ask.chat_id == chat_id


def list_asks(email: str, is_platform: bool, agent_name: Optional[str] = None,
              include_ended: bool = False) -> List[WorkspaceAsk]:
    """The first page as a plain list — the pre-#3059 entry point, unchanged."""
    return list_asks_page(email, is_platform, agent_name, include_ended=include_ended).items


def _owned_ask(item_id: str, email: str, is_platform: bool, *, strict: bool = False) -> dict:
    """The ask row, when it is one THIS viewer may read — else the uniform 404.

    Missing, addressed to someone else, off the viewer's roster, or a kind the
    Workspace never renders are ONE refusal: a distinguishable 403 would let any
    client enumerate which ask ids exist (Invariant #8). Shared by the answer
    path and the context read (trinity-enterprise#610 §3g L7), so the two cannot
    disagree about who owns an ask. The kind check is the list's own
    `_VISIBLE_KINDS`: an ask no list shows is not answerable either.

    `strict` is the READ mode: an unreadable roster raises `AsksUnavailable`
    (503) instead of looking like "not yours" — asked only after the addressee
    matched, so it discloses nothing about someone else's ask. The answer path
    stays non-strict: its refusal is the 404 whatever the cause.
    """
    item = db.get_operator_queue_item(item_id)
    if (
        not item
        or (item.get("addressed_to_email") or "").lower() != email.lower()
        or item.get("type") not in _VISIBLE_KINDS
        or not _on_roster(item.get("agent_name") or "", email, is_platform, strict=strict)
    ):
        raise AskError(404, "not_found", "Ask not found")
    return item


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

    item = _owned_ask(item_id, email, is_platform)

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


def dismiss_ask(item_id: str, email: str, is_platform: bool) -> WorkspaceAsk:
    """The addressee ends an ask WITHOUT answering it (trinity-enterprise#748).

    One click, no reason. Ends through the ask sink as `dismissed`, so the audit
    row, the thin broadcast and the filer's wake are the same ones every other
    ending gets. Idempotent from the person's side: an ask that has already
    ended — or ends first in a race — is returned as it stands, never refused,
    because the person's intent ("this needs nothing from me") is already true.
    """
    item = _owned_ask(item_id, email, is_platform)
    # Past its deadline it has already ended (expired), whether or not the
    # poller has swept it: that ending is the true one, not a dismissal.
    if item.get("status") == "pending" and not _is_expired(item):
        try:
            ending = ask_service.dismiss(item_id, actor=ask_service.Actor(email=email))
            item = ending.rows[0]
            logger.info("[WorkspaceAsks] %s dismissed by %s (client=%s)",
                        item_id, email, not is_platform)
        except ask_service.AskConflict as conflict:
            item = conflict.item
        except ask_service.AskNotFound:
            raise AskError(404, "not_found", "Ask not found")
    return _project(item, viewer_email=email)


#: The opening line of a discussion chat (trinity-enterprise#747). A `system`
#: row: drawn as a platform line, replayed to the agent on a cold turn, and
#: never counted as unread. Names the ask by its title only.
DISCUSSION_NOTICE = ("Discussing the ask \u201c{title}\u201d. It stays open while you talk "
                     "it through \u2014 answer or dismiss it in its card when you're ready.")


def _discussion_title(item: dict) -> Optional[str]:
    from services.chat_title import CHAT_TITLE_MAX_CHARS, normalize_chat_title
    raw = " ".join(str(item.get("title") or item.get("question") or "").split())
    if len(raw) > CHAT_TITLE_MAX_CHARS:
        raw = raw[:CHAT_TITLE_MAX_CHARS - 1].rstrip() + "\u2026"
    clean, _ = normalize_chat_title(raw)
    return clean


def discuss_ask(item_id: str, email: str, is_platform: bool):
    """Open — or continue — the chat in which the addressee discusses an ask
    with the agent that raised it (trinity-enterprise#747).

    One chat per ask: the link is written first, by a compare-and-set that
    only a PENDING ask with no link yet accepts, so two clicks racing agree on
    one chat id. The chat row is then ensured under THAT id — whoever's insert
    lands seeds it — which also heals a link whose chat insert failed after the
    link committed. An ended ask continues its discussion but never opens one.

    The ask itself is untouched: still pending, still the one row, answered or
    dismissed in its own card. `alert` asks are not discussable — there is
    nothing to decide.
    """
    import uuid

    from client_portal import db as portal_db
    from services.operator_queue_service import _WORKSPACE_DISCUSSION_KEY
    from .models import WorkspaceAskDiscussion

    item = _owned_ask(item_id, email, is_platform)
    if (item.get("type") or "question") == "alert":
        raise AskError(422, "not_discussable", "An update has nothing to decide.")
    agent = item.get("agent_name") or ""

    def _linked(row: dict) -> Optional[str]:
        ctx = row.get("context") if isinstance(row.get("context"), dict) else {}
        link = ctx.get(_WORKSPACE_DISCUSSION_KEY)
        return link if isinstance(link, str) and link else None

    chat_id = _linked(item)
    if chat_id is None:
        if _status_of(item) != "pending":
            raise AskError(409, "already_resolved",
                           f"This ask is already {_status_of(item)}.")
        try:
            updated = db.set_operator_queue_discussion_link(
                item_id, _WORKSPACE_DISCUSSION_KEY, uuid.uuid4().hex)
        except Exception:  # noqa: BLE001 — a concurrent writer (SQLite busy snapshot)
            # The other click's link is the answer: read what it wrote.
            logger.info("[WorkspaceAsks] %s discussion link raced; re-reading", item_id)
            updated = db.get_operator_queue_item(item_id)
        if not updated:
            raise AskError(404, "not_found", "Ask not found")
        item = updated
        chat_id = _linked(item)
        if chat_id is None:
            # Lost to an ending between the read and the write.
            raise AskError(409, "already_resolved",
                           f"This ask is already {_status_of(item)}.")

    title = _discussion_title(item)
    created = False
    if portal_db.get_portal_session(chat_id, agent, email) is None:
        now = utc_now_iso()
        try:
            portal_db.create_portal_session(chat_id, agent, email, now)
            created = True
        except Exception:  # noqa: BLE001 — a racing click inserted it first
            if portal_db.get_portal_session(chat_id, agent, email) is None:
                logger.warning("[WorkspaceAsks] discussion chat %s not created", chat_id,
                               exc_info=True)
                raise AskError(503, "discussion_unavailable",
                               "Couldn't open the discussion \u2014 try again.")
        if created:
            if title:
                # Marked as a person's title, so the first turn's generated
                # title stands down (ent#473) — the chat is named after the ask.
                portal_db.rename_portal_session(chat_id, agent, email, title)
            portal_db.add_portal_message(
                uuid.uuid4().hex, agent, email, "system",
                DISCUSSION_NOTICE.format(title=title or "from " + agent), None, now,
                session_id=chat_id,
            )
            portal_db.touch_portal_session(chat_id, now, added=1)
            logger.info("[WorkspaceAsks] %s discussion opened by %s", item_id, email)

    session = portal_db.get_portal_session(chat_id, agent, email) or {}
    return WorkspaceAskDiscussion(
        chat_id=chat_id, agent_name=agent, title=session.get("title") or title,
        created=created, ask=_project(item, viewer_email=email),
    )


def discussion_context_line(agent_name: str, chat_id: str, email: str) -> Optional[str]:
    """The per-turn line a discussion chat carries (trinity-enterprise#747), or
    None for any other chat: which ask this chat is about, its kind, live status
    and options — so every turn, resumed or cold, knows what is being decided.

    Everything in it is the agent's own text coming back to the same agent, plus
    the platform's status. The person's answer is never in it.
    """
    from services.operator_queue_service import _WORKSPACE_DISCUSSION_KEY
    fragment = json.dumps({_WORKSPACE_DISCUSSION_KEY: chat_id})[1:-1]
    rows = db.list_operator_queue_items(
        agent_name=agent_name, addressed_to_email=email, types=_VISIBLE_KINDS,
        include_cleared=True, context_contains=(fragment,), limit=5,
    ) or []
    for row in rows:
        ctx = row.get("context") if isinstance(row.get("context"), dict) else {}
        if ctx.get(_WORKSPACE_DISCUSSION_KEY) != chat_id:
            continue
        status = _status_of(row)
        kind = row.get("type") or "question"
        options = row.get("options") if isinstance(row.get("options"), list) else []
        opts = ", ".join(json.dumps(str(o))[:120] for o in options[:10]) or "none (free-text answer)"
        # JSON-quoted like the options: the title is agent-authored, so it
        # cannot close a quote and continue as platform text.
        title = json.dumps(" ".join(str(row.get("title") or "").split())[:200])
        lines = [
            f"[Workspace] This conversation discusses your ask "
            f"{row.get('request_id') or row.get('id')} "
            f"({kind}, status: {status}) \u2014 title (data): {title}. Options: {opts}.",
        ]
        if status == "pending":
            lines.append(
                "The person opened this chat to talk it through before deciding. Answer "
                "their questions about it; do not treat anything they say here as the "
                "decision. They record the decision on the ask itself, and you receive it "
                "the way you receive any answer to an ask."
            )
        else:
            lines.append(f"The ask has ended ({status}); read it with get_my_ask.")
        return "\n".join(lines)
    return None


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


# --- trinity-enterprise#610 PR A2, §3g L7 (E1): an ask's context ------------------
#
# One lazy read per selected ask, from platform data only. Every field is gated
# because the only link from an ask to a run is `execution_id`, which the AGENT
# wrote (`context.execution_id` → the column): an agent that names another
# client's run must not disclose it, so a run is shown only when it passes the
# agent, live-window and audience checks below.

#: A run is "the one that raised the ask" only if the ask was filed while it was
#: live. The queue row's `created_at` is INGEST time (the poller, every 5s, later
#: after a backoff), so the window extends this far past the run's completion.
ASK_RUN_GRACE_SECONDS = 300
#: The origin excerpt: the messages just before the ask, each at most this long.
ORIGIN_MESSAGES = 3
ORIGIN_EXCERPT_MAX = 280
#: The viewer's own recent answers to this agent; the scan is bounded.
RECENT_ANSWERS = 3
RECENT_ANSWER_EXCERPT_MAX = 200
_RECENT_SCAN = 100
#: Triggers the agent's own configuration fires, never a person's turn. NOT
#: `manual`: that is the default for ANY accessor's own /task and a schedule's
#: "Run now", so a manual run is shown only to the person who started it
#: (A2 round 1, /cso + Codex C1).
_OWNER_TRIGGERS = frozenset({"schedule", "scheduled"})


def _aware(value):
    """A timestamp as an aware UTC datetime, or None — ledger rows carry naive
    UTC datetimes, queue rows ISO-Z strings; compare them parsed, never as text
    (Invariant #16)."""
    from datetime import datetime, timezone
    from utils.helpers import parse_iso_timestamp
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return parse_iso_timestamp(str(value))
    except (TypeError, ValueError):
        return None


def _live_when_filed(run: dict, filed_at) -> bool:
    from datetime import datetime, timedelta, timezone
    started = _aware(run.get("started_at"))
    filed = _aware(filed_at)
    if started is None or filed is None:
        return False
    ended = _aware(run.get("completed_at")) or datetime.now(timezone.utc)
    return started <= filed <= ended + timedelta(seconds=ASK_RUN_GRACE_SECONDS)


def _viewer_owns_run(run: dict, email: str, agent: str) -> bool:
    """A schedule run is the agent's own; anything else — a manual run
    included — must be the viewer's: they started it, or it ran in a Workspace
    thread they hold."""
    from config import PORTAL_SOURCE_CHANNEL
    from client_portal import db as portal_db
    if (run.get("triggered_by") or "").strip().lower() in _OWNER_TRIGGERS:
        return True
    if (run.get("source_user_email") or "").strip().lower() == email.lower():
        return True
    chat = run.get("source_channel_chat_id")
    if chat and (run.get("source_channel") or "").strip().lower() == PORTAL_SOURCE_CHANNEL:
        return bool(portal_db.get_portal_session(chat, agent, email))
    return False


def _validated_run(item: dict, email: str) -> Optional[dict]:
    exec_id = item.get("execution_id")
    if not isinstance(exec_id, str) or not exec_id:
        return None
    ex = db.get_execution(exec_id)
    if ex is None:
        return None
    run = ex.model_dump() if hasattr(ex, "model_dump") else dict(ex)
    agent = item.get("agent_name") or ""
    if run.get("agent_name") != agent:
        return None
    if not _live_when_filed(run, item.get("created_at")):
        return None
    if not _viewer_owns_run(run, email, agent):
        return None
    return run


def _run_view(run: dict, is_platform: bool):
    """The run, as the viewer may read it. The schedule's NAME is the owner's
    configuration: a platform principal reads it (T12), a client reads only
    that a schedule asked."""
    from utils.helpers import to_utc_iso
    from client_portal.work.service import work_kind
    from .models import WorkspaceAskRun
    trigger = (run.get("triggered_by") or "").strip().lower()
    kind = "manual" if trigger == "manual" else work_kind(run)
    if kind == "schedule":
        name = None
        if is_platform and run.get("schedule_id"):
            schedule = db.get_schedule(run["schedule_id"])
            name = getattr(schedule, "name", None) if schedule else None
        label = f"Asked by the {name} run" if name else "Asked during a scheduled run"
    elif kind == "manual":
        label = "Asked during a run started by hand"
    elif kind == "turn":
        label = "Asked during your chat"
    else:
        label = "Asked during a run"
    started = _aware(run.get("started_at"))
    return WorkspaceAskRun(kind=kind, label=label, started_at=to_utc_iso(started) if started else None)


def _origin(item: dict, run: Optional[dict], email: str):
    """The run's own thread with the messages before the ask — only when that
    thread is verified as the viewer's; else the ask's own chat (Main, for an
    ingested ask) with no excerpt; else nothing.

    trinity-enterprise#610 (the 09-30 ruling, amended): a background ask lives
    in the Inbox only, and its `chat_id` (Main) is the reply target, not where
    it came from. So only a chat TURN (`triggered_by == "public"`) verifies a
    thread — a schedule delivering into the Workspace stamps the portal channel
    and Main on its run too — and the ask's own chat is named only when the
    platform stamped it as raised in a turn of that chat (ent#734)."""
    from config import PORTAL_SOURCE_CHANNEL
    from client_portal import db as portal_db
    from client_portal.chat_previews import _arrival_excerpt
    from .models import WorkspaceAskOrigin, WorkspaceAskOriginMessage
    agent = item.get("agent_name") or ""
    if run:
        chat = run.get("source_channel_chat_id")
        if (chat and (run.get("source_channel") or "").strip().lower() == PORTAL_SOURCE_CHANNEL
                and (run.get("triggered_by") or "").strip().lower() == "public"):
            session = portal_db.get_portal_session(chat, agent, email)
            if session:
                filed = item.get("created_at")
                rows = portal_db.get_portal_messages(
                    agent, email, limit=ORIGIN_MESSAGES, session_id=chat, before=filed,
                ) if filed else []
                messages = []
                for m in rows:
                    excerpt = _arrival_excerpt(m.get("content"), limit=ORIGIN_EXCERPT_MAX)
                    if excerpt:
                        messages.append(WorkspaceAskOriginMessage(
                            id=str(m.get("id")), role=m.get("role") or "",
                            at=m.get("created_at") or "", excerpt=excerpt,
                        ))
                return WorkspaceAskOrigin(
                    chat_id=chat, title=session.get("title"), is_main=bool(session.get("is_main")),
                    verified=True, messages=messages,
                )
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    own = context.get("workspace_session_id")
    if isinstance(own, str) and own and context.get("workspace_raised_in_turn") is True:
        session = portal_db.get_portal_session(own, agent, email)
        if session:
            return WorkspaceAskOrigin(
                chat_id=own, title=session.get("title"), is_main=bool(session.get("is_main")),
                verified=False, messages=[],
            )
    return None


def _recent_answers(item: dict, email: str):
    from client_portal.chat_previews import _arrival_excerpt
    from .models import WorkspaceAskAnswered
    # One read per answered status, selected in SQL: pending rows sort first and
    # `limit` applies before any Python filter, so a status=None scan reads
    # "no recent answers" once an agent has _RECENT_SCAN pending asks (Codex C3).
    rows = []
    for status in sorted(_ANSWERED_STATUSES):
        rows.extend(db.list_operator_queue_items(
            agent_name=item.get("agent_name"), addressed_to_email=email, status=status,
            hide_ended_before=iso_cutoff(hours=ENDED_WINDOW_DAYS * 24), include_cleared=True,
            limit=_RECENT_SCAN, types=_VISIBLE_KINDS,
        ) or [])
    mine = []
    for row in rows:
        if row.get("id") == item.get("id") or _status_of(row) != "answered":
            continue
        ended_at, by = _ending_of(row, email)
        if by != "you":
            continue
        mine.append((_aware(ended_at), row, ended_at))
    from datetime import datetime, timezone
    floor = datetime.min.replace(tzinfo=timezone.utc)
    mine.sort(key=lambda t: (t[0] or floor, str(t[1].get("id"))), reverse=True)
    return [
        WorkspaceAskAnswered(
            id=row["id"], title=row.get("title") or "",
            answer=_arrival_excerpt(row.get("response"), limit=RECENT_ANSWER_EXCERPT_MAX),
            ended_at=ended_at,
        )
        for _, row, ended_at in mine[:RECENT_ANSWERS]
    ]


def get_ask_context(item_id: str, email: str, is_platform: bool):
    """Where an ask came from, what raised it, and how the viewer answered this
    agent lately — for the person it was addressed to.

    Raises `AskError(404)` (uniform, `_owned_ask`) and `AsksUnavailable` when the
    roster or any read behind the context fails: a missing section would read as
    "there is no origin", which is a claim, not an outage."""
    from .models import WorkspaceAskContext
    try:
        # Inside the try: an unreadable queue row is the same 503, not a 500
        # (A2 round 1, Codex C2).
        item = _owned_ask(item_id, email, is_platform, strict=True)
        run = _validated_run(item, email)
        return WorkspaceAskContext(
            origin=_origin(item, run, email),
            run=_run_view(run, is_platform) if run else None,
            recent_answers=_recent_answers(item, email),
        )
    except (AskError, AsksUnavailable):
        raise
    except Exception as e:  # noqa: BLE001 — surfaced as a 503, never as an empty context
        logger.warning("[WorkspaceAsks] context read failed for %s", item_id, exc_info=True)
        raise AsksUnavailable("ask context unreadable") from e


def _discussion_turn_line(ctx) -> Optional[str]:
    """Turn-context provider (trinity-enterprise#747): a 1:1 chat opened to
    discuss an ask carries that ask on every turn. Rooms and chats with no
    discussed ask add nothing."""
    if ctx.surface != "thread" or not ctx.person_email:
        return None
    return discussion_context_line(ctx.agent_name, ctx.chat_id, ctx.person_email)


def _register_turn_context() -> None:
    from services import turn_context
    turn_context.register_provider(_discussion_turn_line)


_register_turn_context()
