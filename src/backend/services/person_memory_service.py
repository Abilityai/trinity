"""A person's memory with an agent, read by the person or by the agent's owner
(trinity-enterprise#962, MEM-001).

The person's own view lives in the Workspace (`client_portal/agent_page.memory`,
ent#637) and shows the notes only. The owner's view (`routers/person_memory.py`)
lists everyone with memory on the agent and shows both halves of the blob —
the agent's notes and the summarizer's summary — with the write history of
each. Both views read the history through `history_page` and undo through
`undo_write`, which wraps the ONE undo primitive
(`db.undo_public_user_memory_write`): the person's undo is recorded with their
own address, the operator's with the operator's.

Readers (the ent#638 seat-decision rule): the person reads their own; the
agent's owner and an admin read everyone. The routes enforce it — this module
takes the agent and the person as already authorised.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db
from services.schedule_seat_memory import NO_SCHEDULE_ID

logger = logging.getLogger(__name__)

SECTION_NOTES = "notes"
SECTION_SUMMARY = "summary"
SUMMARIZER_TRIGGER = "summarizer"
HISTORY_PAGE = 20
HISTORY_PAGE_MAX = 50
PEOPLE_PAGE = 50
PEOPLE_PAGE_MAX = 200
MAX_EMAIL_CHARS = 320
MAX_SCHEDULE_NAME_CHARS = 80    # the agent page's bound on the same label


class MemoryUndoRefused(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


class InvalidCursor(ValueError):
    """A history cursor that is not ``<written_at>|<id>``."""


class PersonNotFound(LookupError):
    """No memory for this person on this agent — the owner's uniform 404."""


def normalise_email(value: Optional[str]) -> str:
    """Strip + lower, bounded. Membership (does this person have memory here?)
    is the real check, not a regex: the writers key memory on addresses that a
    stricter pattern would refuse, so a listed person could not be opened."""
    email = (value or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_CHARS:
        raise PersonNotFound(email)
    return email


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

def write_kind(row: dict) -> str:
    """Who wrote it, in the reader's vocabulary.

    ``summarizer`` for the background summary; ``scheduled_run`` for any write
    a SEAT run made — cron, Run now or webhook — keyed on ``schedule_id``, which
    the write boundary records only for a seat run (a Run-now fire is
    ``manual``, and keying on ``triggered_by`` rendered it as a conversation);
    ``conversation`` otherwise.
    """
    if (row.get("section") or SECTION_NOTES) == SECTION_SUMMARY:
        return "summarizer"
    return "scheduled_run" if (row.get("schedule_id") or "").strip() else "conversation"


def encode_cursor(row: dict) -> str:
    return f"{row['written_at']}|{row['id']}"


def decode_cursor(cursor: Optional[str]) -> Optional[tuple]:
    if cursor is None or cursor == "":
        return None
    at, sep, wid = str(cursor).rpartition("|")
    if not sep or not at or not wid or len(cursor) > 200:
        raise InvalidCursor(cursor)
    return at, wid


def _schedule_names(agent_name: str, rows: list) -> dict:
    """``schedule_id`` → bounded name for the rows that carry one; one projected
    read, never a per-row lookup. Fails soft: the labels, never the rows."""
    wanted = {r.get("schedule_id") for r in rows
              if r.get("schedule_id") and r.get("schedule_id") != NO_SCHEDULE_ID}
    if not wanted:
        return {}
    try:
        names = db.get_agent_schedule_names(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("person memory: schedule names failed for %s: %s", agent_name, e)
        return {}
    return {sid: name[:MAX_SCHEDULE_NAME_CHARS] for sid, name in names.items() if sid in wanted and name}


def history_page(agent_name: str, email: str, *, before: Optional[str] = None,
                 limit: int = HISTORY_PAGE, section: Optional[str] = None) -> dict:
    """One page of the write history, newest first, with a keyset cursor.

    ``undoable`` is the latest open write of the row's own section, read from
    the store rather than from this page. ``undone_by_operator`` is true when
    someone other than the person undid the write; the raw ``undone_by`` is for
    the owner's view only — the person's projection drops it.
    """
    cursor = decode_cursor(before)
    limit = max(1, min(int(limit), HISTORY_PAGE_MAX))
    rows = db.list_public_user_memory_writes(agent_name, email, limit + 1, before=cursor, section=section)
    more = len(rows) > limit
    rows = rows[:limit]
    latest = db.latest_open_public_user_memory_writes(agent_name, email)
    names = _schedule_names(agent_name, rows)
    writes = []
    for r in rows:
        row_section = r.get("section") or SECTION_NOTES
        undone_by = (r.get("undone_by") or "").strip()
        writes.append({
            "id": r["id"],
            "section": row_section,
            "kind": write_kind(r),
            "execution_id": r.get("execution_id"),
            "schedule_name": names.get(r.get("schedule_id")),
            "written_at": r["written_at"],
            "undone_at": r.get("undone_at"),
            "undone_by": undone_by or None,
            "undone_by_operator": bool(r.get("undone_at")) and bool(undone_by) and undone_by.lower() != email,
            "undoable": r["id"] == latest.get(row_section),
            "text": r.get("new_notes") or "",
            "previous_text": r.get("previous_notes") or "",
        })
    return {"writes": writes, "next_before": encode_cursor(rows[-1]) if more and rows else None}


def undo_write(agent_name: str, email: str, write_id: str, *, undone_by: str,
               section: Optional[str] = None) -> dict:
    """Revert one write, latest-first within its section — the one undo the
    person and the operator share. A NAMED 409 (`not_latest` /
    `already_undone`) rather than a silent revert that discards a later change;
    a miss is the uniform 404 — the id space is per (agent, person), so an id
    from someone else's memory reads as nonexistent, never as forbidden."""
    outcome = db.undo_public_user_memory_write(agent_name, email, write_id,
                                               undone_by=undone_by, section=section)
    if outcome == "not_found":
        raise MemoryUndoRefused("not_found", "That memory change was not found.", 404)
    if outcome == "already_undone":
        raise MemoryUndoRefused("already_undone", "That change has already been undone.")
    if outcome == "not_latest":
        raise MemoryUndoRefused(
            "not_latest",
            "A later change exists — undo the most recent one first, so nothing "
            "you have not looked at is discarded.",
        )
    record = db.get_public_user_memory(agent_name, email) or {}
    return {
        "write_id": write_id,
        "notes": record.get("agent_notes") or "",
        "summary": record.get("conversation_summary") or "",
    }


# --------------------------------------------------------------------------- #
# The owner's view
# --------------------------------------------------------------------------- #

def _owner_email(agent_name: str) -> Optional[str]:
    from services import memory_service
    return memory_service.owner_email(agent_name)


def _identities(agent_name: str) -> dict:
    """``email`` → the channel identity an access request captured (ent#737).

    Display only, and what the channel REPORTED (a name and a handle the user
    chose); the verified email is the identity. One row per (agent, email).
    """
    try:
        rows = db.list_access_requests(agent_name, None)
    except Exception as e:  # noqa: BLE001 — the label, never the list
        logger.warning("person memory: access requests unreadable for %s: %s", agent_name, e)
        return {}
    out = {}
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not email or email in out or not row.get("requester_channel_id"):
            continue
        out[email] = {
            "channel": row.get("channel"),
            "channel_name": row.get("requester_name"),
            "channel_handle": row.get("requester_handle"),
        }
    return out


def _decorate(person: dict, *, owner: Optional[str], identities: dict) -> dict:
    email = person["user_email"]
    agent_scope = bool(owner) and email == owner
    identity = identities.get(email) or {}
    return {
        **person,
        # The current owner's turns feed the AGENT scope (MEM-002), so their
        # "own" facts are the agent's; the count would be someone else's number.
        "agent_scope": agent_scope,
        "fact_count": None if agent_scope else person.get("fact_count", 0),
        "channel": identity.get("channel"),
        "channel_name": identity.get("channel_name"),
        "channel_handle": identity.get("channel_handle"),
    }


def list_people(agent_name: str, *, offset: int = 0, limit: int = PEOPLE_PAGE) -> dict:
    """Everyone with memory on the agent, newest change first, one page."""
    limit = max(1, min(int(limit), PEOPLE_PAGE_MAX))
    offset = max(0, int(offset))
    page = db.list_public_user_memory_people(agent_name, limit=limit, offset=offset)
    owner = _owner_email(agent_name)
    identities = _identities(agent_name) if page["people"] else {}
    return {
        "agent_name": agent_name,
        "total": page["total"],
        "offset": offset,
        "limit": limit,
        "people": [_decorate(p, owner=owner, identities=identities) for p in page["people"]],
    }


def _facts_in_scope(agent_name: str, email: str) -> int:
    try:
        return len(db.list_memory_facts(agent_name, email, limit=1, with_embedding=False))
    except Exception:  # noqa: BLE001
        return 0


def person_detail(agent_name: str, user_email: str) -> dict:
    """One person's memory: both sections, the identity, the first history page.

    Never creates the memory row. A person with no memory here — no non-empty
    section, no history, no live fact in their scope — is ``PersonNotFound``.
    """
    email = normalise_email(user_email)
    record = db.get_public_user_memory(agent_name, email)
    history = history_page(agent_name, email)
    notes = (record or {}).get("agent_notes") or ""
    summary = (record or {}).get("conversation_summary") or ""
    if not (notes or summary or history["writes"] or _facts_in_scope(agent_name, email)):
        raise PersonNotFound(email)
    owner = _owner_email(agent_name)
    identity = _identities(agent_name).get(email) or {}
    return {
        "agent_name": agent_name,
        "user_email": email,
        "agent_scope": bool(owner) and email == owner,
        "notes": notes,
        "summary": summary,
        "updated_at": (record or {}).get("updated_at"),
        "channel": identity.get("channel"),
        "channel_name": identity.get("channel_name"),
        "channel_handle": identity.get("channel_handle"),
        "history": history,
    }
