"""Tagging a person in a conversation (trinity-enterprise#631).

Someone working with an agent names a colleague in a chat or a room, and that
colleague is told. A tag is a **pointer, not a seat**:

* It writes ONE item into the queue-item ledger — the store behind the Inbox's
  four doors (ent#610) — addressed to the person, kind *Unread* (a tag asks for
  attention, not an answer). No new store and no fifth notification path
  (ent#564 names the narrow ones that exist). The row shapes are in
  `db/queue_mentions.py`.
* It changes no membership and grants no access. Opening the item shows the
  tagged message, with a little around it, ONLY to a reader who could already
  see the conversation; anyone else is told they cannot, and who can let them
  in. Not even the tagger's own words cross: the AC's "nothing the tagged
  person is not already entitled to see" is read literally.
* Only a PERSON on this instance may tag (a platform account, in the Workspace
  or on the platform). An agent never can — not by its MCP key, not by its
  queue file (`mention-` is a platform-reserved id prefix), and not by text it
  writes into a room — so nothing lets an agent reach a person on its own
  initiative. An external Workspace client cannot either: the people it could
  name are the organisation's, and listing them is an internal fact (#78).
* Who may be tagged is the ent#450 pattern: accounts that exist, are not
  suspended, and can already reach one of the conversation's agents — so the
  picker is never a fleet-wide account dump, and the same predicate answers the
  send-time check, so a refusal never disagrees with what the picker offered.
* It is bounded and idempotent: at most `MAX_TAGS_PER_MESSAGE` people per
  message, deduplicated case-insensitively, and the row's id is derived from
  (conversation, message, person), so the same person named twice — or the
  same message delivered twice — is one item.
* An agent's wake is untouched. `shared_sessions.service.resolve_mentions`
  still returns the agents to wake from the text; a person is never a wake
  target, and the message's stored `mentions` stay agent-only. The person
  pointer is stored apart from it, on the ledger, keyed to the message.

Delivery is in-app only (out-of-app reach is ent#564): the item lands in the
person's Workspace Inbox, and the surfaces say so plainly.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from database import db
from models import (
    PersonMention,
    PersonMentionConversation,
    PersonMentionDetail,
    PersonMentionMessage,
)
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

MENTION_TYPE = "mention"
# Platform-reserved in `operator_queue_service._RESERVED_ID_PREFIXES` (and in
# its about-a-person subset): an agent can neither forge nor pre-suppress one.
MENTION_ID_PREFIX = "mention-"
MAX_TAGS_PER_MESSAGE = 10
PEOPLE_LIMIT = 8
# The tagged message and what surrounds it, for a reader who may see the room.
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 2
CONTEXT_MESSAGE_MAX_CHARS = 4000
_TITLE_MAX = 300

STATE_DELIVERED = "delivered"
STATE_READ = "read"


class TagError(Exception):
    """A named refusal: HTTP status, a stable `code`, a sentence a person can
    act on, and any structured fields (`name` for a refused person)."""

    def __init__(self, status_code: int, code: str, detail: str, **extra):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True)
class Tagger:
    username: str
    email: Optional[str]
    label: str


@dataclass(frozen=True)
class Person:
    email: str
    username: str
    label: str


@dataclass(frozen=True)
class Conversation:
    kind: str            # 'room' | 'chat'
    id: str
    agent_name: str      # the ledger row's agent (a room's first agent participant)
    label: str


@dataclass(frozen=True)
class Reader:
    """The person opening their Inbox. `is_platform` is the credential kind:
    only the PLATFORM door carries an admin's every-room visibility (#78)."""
    email: str
    is_platform: bool


# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------

def person_label(user: Optional[Dict]) -> str:
    """A person's display name: `name`, else the username (often the email)."""
    if not user:
        return ""
    return " ".join(str(user.get("name") or "").split()) or str(user.get("username") or "")


def _usable(user: Optional[Dict]) -> bool:
    return bool(user) and not user.get("suspended_at")


_AGENTS_CANNOT_TAG = ("agents_cannot_tag",
                      "Agents can't tag people. A person can tag a colleague from the conversation.")
_UNAVAILABLE = ("tagging_unavailable",
                "Tagging a person is available to people with an account on this Trinity instance.")


def _from_user(user: Optional[Dict]) -> Tagger:
    if not _usable(user):
        raise TagError(403, *_UNAVAILABLE)
    return Tagger(username=user["username"], email=(user.get("email") or "").strip().lower() or None,
                  label=person_label(user))


def tagger_for(principal) -> Tagger:
    """The person tagging, from the AUTH CONTEXT — never from a request body.

    Refuses an agent (any key carrying an agent identity), a non-person
    platform principal (a system key), and an external Workspace client."""
    if getattr(principal, "agent_name", None) or getattr(principal, "connector_agent", None):
        raise TagError(403, *_AGENTS_CANNOT_TAG)
    if getattr(principal, "is_portal", False):            # a room's WorkspacePrincipal
        if not getattr(principal, "is_platform", False):
            raise TagError(403, *_UNAVAILABLE)
        return _from_user(db.get_user_by_email(getattr(principal, "email", "") or ""))
    from dependencies import is_person_principal
    if not is_person_principal(principal):
        raise TagError(403, *_AGENTS_CANNOT_TAG)
    return _from_user(db.get_user_by_username(getattr(principal, "username", "") or ""))


def tagger_for_portal(principal, *, is_person: bool) -> Tagger:
    """The Workspace 1:1 chat's tagger (a `PortalPrincipal`): a platform person only."""
    if not getattr(principal, "is_platform", False):
        raise TagError(403, *_UNAVAILABLE)
    if not is_person:
        raise TagError(403, *_AGENTS_CANNOT_TAG)
    return _from_user(db.get_user_by_email(getattr(principal, "email", "") or ""))


def taggable_people(agent_names: List[str], query: str, tagger: Tagger,
                    limit: int = PEOPLE_LIMIT) -> List[Dict]:
    """The picker's rows: `{email, label}` for accounts matching `query` that may
    be tagged in a conversation with `agent_names`. An empty query lists no one."""
    rows = db.list_taggable_people(agent_names, query, exclude_email=tagger.email, limit=limit)
    return [{"email": (r.get("email") or "").strip().lower(), "label": person_label(r)} for r in rows]


def _normalise(raw: Iterable) -> List[str]:
    out: List[str] = []
    for t in raw or ():
        e = str(t or "").strip().lower()
        if e and e not in out:
            out.append(e)
    return out


def resolve_tags(raw: Iterable, agent_names: List[str], tagger: Tagger) -> List[Person]:
    """Validate a message's tags BEFORE anything is written; refuse by name.

    Deduplicated case-insensitively, so naming one person twice is one tag.
    A name that cannot be tagged — unknown, suspended, or without access to any
    of the conversation's agents — gets ONE refusal, so the check is not an
    oracle for which addresses have accounts."""
    emails = _normalise(raw)
    if len(emails) > MAX_TAGS_PER_MESSAGE:
        raise TagError(422, "too_many_tags",
                       f"A message can tag at most {MAX_TAGS_PER_MESSAGE} people.",
                       limit=MAX_TAGS_PER_MESSAGE)
    agents = ", ".join(sorted(set(agent_names or ()))) or "this conversation's agents"
    people: List[Person] = []
    for email in emails:
        if tagger.email and email == tagger.email:
            raise TagError(422, "cannot_tag_yourself", "You can't tag yourself.", name=email)
        row = db.get_taggable_person(agent_names, email)
        if not row:
            raise TagError(422, "unknown_person",
                           f"{email} can't be tagged here — you can tag people on this "
                           f"instance who work with {agents}.", name=email)
        people.append(Person(email=email, username=row["username"], label=person_label(row)))
    return people


# ---------------------------------------------------------------------------
# Delivery — one ledger row per (message, person)
# ---------------------------------------------------------------------------

def _h(value: str, n: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def conversation_prefix(kind: str, conversation_id: str) -> str:
    """Every tag in one conversation shares this id prefix."""
    return f"{MENTION_ID_PREFIX}{_h(f'{kind}:{conversation_id}', 16)}-"


def request_id(conv: Conversation, message_id: str, email: str) -> str:
    """Deterministic: the same person on the same message is the same row."""
    return f"{conversation_prefix(conv.kind, conv.id)}{_h(message_id, 12)}-{_h(email, 12)}"


def _tag_state(row: Dict) -> Dict:
    ctx = ((row.get("context") or {}).get("mention") or {})
    return {
        "label": (ctx.get("person") or {}).get("label") or "",
        "state": STATE_READ if row.get("status") == STATE_READ else STATE_DELIVERED,
        "read_at": row.get("acknowledged_at") if row.get("status") == STATE_READ else None,
    }


def deliver(conv: Conversation, message_id: str, seq: Optional[int],
            people: List[Person], tagger: Tagger) -> List[Dict]:
    """Write one Unread item per person (idempotent) and return the tagger's
    view of each — `{label, state, read_at}`."""
    now = utc_now_iso()
    out: List[Dict] = []
    for person in people:
        context = {"mention": {
            "conversation": {"kind": conv.kind, "id": conv.id, "label": conv.label},
            "message_id": message_id,
            "seq": seq,
            "tagged_by": {"username": tagger.username, "label": tagger.label},
            "person": {"label": person.label},
        }}
        row, inserted = db.create_person_mention(
            agent_name=conv.agent_name,
            request_id=request_id(conv, message_id, person.email),
            addressed_to_email=person.email,
            title=f"{tagger.label} mentioned you"[:_TITLE_MAX],
            question=f"In {conv.label}"[:_TITLE_MAX],
            context=context,
            now=now,
        )
        if inserted:
            logger.info("person tag: %s tagged a person in %s %s", tagger.username, conv.kind, conv.id)
        out.append(_tag_state(row))
    return out


def tags_by_message(kind: str, conversation_id: str, message_ids: Optional[Iterable[str]] = None,
                    *, tagged_by: Optional[str] = None,
                    agent_names: Optional[List[str]] = None) -> Dict[str, List[Dict]]:
    """The tagger's marks: message id → `[{label, state, read_at}]`, one read
    for the whole conversation. `message_ids` narrows to those messages;
    `tagged_by` (a username) to one tagger's tags — a read receipt is the
    sender's, never the room's. `agent_names` (every agent the conversation
    has had) lets the read use the ledger's `(agent_name, request_id)` index."""
    wanted = None if message_ids is None else {m for m in message_ids if m}
    if wanted is not None and not wanted:
        return {}
    out: Dict[str, List[Dict]] = {}
    for row in db.list_person_mentions_by_prefix(conversation_prefix(kind, conversation_id),
                                                 agent_names=agent_names):
        ctx = ((row.get("context") or {}).get("mention") or {})
        mid = ctx.get("message_id")
        if wanted is not None and mid not in wanted:
            continue
        if tagged_by is not None and (ctx.get("tagged_by") or {}).get("username") != tagged_by:
            continue
        out.setdefault(mid, []).append(_tag_state(row))
    return out


# ---------------------------------------------------------------------------
# The tagged person's door (the Workspace Inbox)
# ---------------------------------------------------------------------------

def _project(row: Dict) -> PersonMention:
    ctx = ((row.get("context") or {}).get("mention") or {})
    conv = ctx.get("conversation") or {}
    read = row.get("status") == STATE_READ
    return PersonMention(
        id=row["id"],
        agent_name=row["agent_name"],
        state="read" if read else "unread",
        created_at=row.get("created_at") or "",
        read_at=row.get("acknowledged_at") if read else None,
        tagged_by=(ctx.get("tagged_by") or {}).get("label") or "",
        conversation=PersonMentionConversation(kind=conv.get("kind") or "room",
                                               label=conv.get("label") or ""),
    )


def list_for_reader(reader: Reader) -> List[PersonMention]:
    return [_project(r) for r in db.list_person_mentions_for(reader.email)]


def _mine(reader: Reader, item_id: str) -> Dict:
    row = db.get_person_mention(item_id) if item_id else None
    if not row or (row.get("addressed_to_email") or "").strip().lower() != (reader.email or "").strip().lower():
        # One answer for "not yours" and "not there" (Invariant #8).
        raise TagError(404, "not_found", "That item isn't in your Inbox.")
    return row


def mark_read(reader: Reader, item_id: str) -> PersonMention:
    _mine(reader, item_id)
    row = db.mark_person_mention_read(item_id, reader.email, utc_now_iso())
    if not row:
        raise TagError(404, "not_found", "That item isn't in your Inbox.")
    return _project(row)


def open_for_reader(reader: Reader, item_id: str) -> PersonMentionDetail:
    row = _mine(reader, item_id)
    base = _project(row)
    ctx = ((row.get("context") or {}).get("mention") or {})
    conv = ctx.get("conversation") or {}
    tagger_label = (ctx.get("tagged_by") or {}).get("label") or ""
    if conv.get("kind") == "room" and conv.get("id"):
        return _open_room(reader, base, conv["id"], ctx)
    # A 1:1 chat is its owner's alone — the tagger is the one who can share it.
    return PersonMentionDetail(**base.model_dump(), can_see=False, message=None, context=[],
                               can_let_you_in=[tagger_label] if tagger_label else [])


def _room_access(room_id: str, reader: Reader) -> bool:
    """Could this reader ALREADY see the room? A live participant (as a platform
    user or as a Workspace identity), or an admin on the platform door."""
    from shared_sessions import db as rdb

    def _active(p):
        return bool(p) and not p.get("left_at")

    email = (reader.email or "").strip().lower()
    if not email:
        return False
    if _active(rdb.get_participant(room_id, "workspace_user", email)):
        return True
    user = db.get_user_by_email(email)
    if not _usable(user):
        return False
    if _active(rdb.get_participant(room_id, "user", user["username"])):
        return True
    return bool(reader.is_platform and user.get("role") == "admin")


def _labeller():
    cache: Dict[str, str] = {}

    def label(kind: str, identity: Optional[str]) -> str:
        if kind == "system" or not identity:
            return "System"
        if kind == "agent":
            return identity
        if kind == "user":
            if identity not in cache:
                cache[identity] = person_label(db.get_user_by_username(identity)) or identity
            return cache[identity]
        return identity
    return label


def _clip(text: str) -> str:
    text = text or ""
    return text if len(text) <= CONTEXT_MESSAGE_MAX_CHARS else text[:CONTEXT_MESSAGE_MAX_CHARS - 1].rstrip() + "…"


def _open_room(reader: Reader, base: PersonMention, room_id: str, ctx: Dict) -> PersonMentionDetail:
    from shared_sessions import db as rdb
    room = rdb.get_room(room_id)
    if room and _room_access(room_id, reader):
        seq = ctx.get("seq")
        label = _labeller()
        rows = rdb.get_messages(room_id, since_seq=max(0, int(seq) - CONTEXT_BEFORE - 1),
                                limit=CONTEXT_BEFORE + CONTEXT_AFTER + 1) if isinstance(seq, int) else []
        window = [PersonMentionMessage(
            id=m["id"], seq=m.get("seq"),
            sender_kind="agent" if m["sender_kind"] == "agent" else (
                "system" if m["sender_kind"] == "system" else "person"),
            sender_label=label(m["sender_kind"], m.get("sender_identity")),
            content=_clip(m.get("content") or ""),
            created_at=m.get("created_at"),
            tagged=m["id"] == ctx.get("message_id"),
        ) for m in rows]
        tagged = next((m for m in window if m.tagged), None)
        conv = base.conversation.model_copy(update={"id": room_id, "label": room.get("name") or base.conversation.label})
        return PersonMentionDetail(**{**base.model_dump(), "conversation": conv},
                                   can_see=True, message=tagged, context=window, can_let_you_in=[])
    # Not a member: say so, and name who runs the room. Nothing of its content.
    who: List[str] = []
    if room:
        label = _labeller()
        for p in rdb.list_participants(room_id):
            if p.get("left_at") or p.get("role") != "moderator" or p.get("kind") == "agent":
                continue
            name = label(p["kind"], p["identity"])
            if name and name not in who:
                who.append(name)
    return PersonMentionDetail(**base.model_dump(), can_see=False, message=None, context=[],
                               can_let_you_in=who)
