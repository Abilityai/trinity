"""The Inbox's read of what came back (trinity-enterprise#610, D5).

`GET /client-portal/chat-state?previews=true` — the ent#359 chat-state payload
plus, for each thread with unread arrivals, the newest arrival (``latest``) and
the earliest unread message id (``first_unread_message_id``).

Why a module of its own rather than more of `service.py`: that file is the
fleet's code-health hotspot (open refactor #2556), and the only thing this
feature needs from it is an optional parameter on `get_chat_state`.

Three rules this module exists to keep:

* **One statement.** Counts and previews come from `db.unread_arrivals_with_latest`,
  a single read over the same `_UNREAD_ARRIVALS` fragment the sidebar badge
  counts, and those counts are handed to `service.get_chat_state(unread=...)` so
  it does not re-count. The "3 new" on a row and the excerpt beside it are one
  snapshot.
* **Roster-scoped previews.** An excerpt is attached only for an agent still on
  the caller's roster (`roster_agent_names`, the set the sessions batch uses), and
  only for the 100 most recent unread threads. The counts are not changed — see
  the known limitation in requirements §5.40.
* **No cost, no execution id.** The projection names its fields; nothing here
  reads the message `cost` column (AC 7).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from utils.credential_sanitizer import sanitize_text

from . import db, service

logger = logging.getLogger(__name__)

# How many unread threads get a preview. The chat-state list itself is not
# truncated (its write cap is `db.MAX_CHAT_STATE_ROWS`).
MAX_PREVIEWS = 100
EXCERPT_MAX_CHARS = 160

# The platform-written completion marker → the Inbox's outcome pill. Read from
# `source`, never from the body: an agent reply beginning "**Finished**" must not
# classify as a finished run. Any other value (NULL, 'voice', …) → None.
_OUTCOME_BY_SOURCE = {
    "completion:done": "done",
    "completion:failed": "failed",
}

_FENCE = re.compile(r"```.*?(```|$)", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LINE_MARK = re.compile(r"(?m)^\s{0,3}(#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)")
# Paired markers anywhere; a single `*`/`_` only at a word edge, so snake_case
# survives ("file_name", not "filename").
_EMPHASIS = re.compile(r"(\*\*|__|~~|`|(?<!\w)[*_]|[*_](?!\w))")
_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def _arrival_excerpt(text: Optional[str], limit: int = EXCERPT_MAX_CHARS) -> Optional[str]:
    """Plain one-line text for a preview: credentials redacted, markdown
    stripped, tags and control characters removed, whitespace collapsed, at most
    `limit` chars (an ellipsis marks a cut). Pure.

    Redaction runs FIRST, on the raw text: the markdown pass removes `_`/`*`,
    which would break a token like `github_pat_…` before the pattern saw it. The
    frontend still renders the excerpt as TEXT, never HTML — the stripping is for
    legibility, not the XSS boundary."""
    if not text:
        return None
    s = _FENCE.sub(" ", sanitize_text(text))
    s = _IMAGE.sub(r"\1", s)
    s = _LINK.sub(r"\1", s)
    s = _LINE_MARK.sub("", s)
    s = _EMPHASIS.sub("", s)
    s = _HTML_TAG.sub("", s)
    s = _CONTROL.sub(" ", s)
    s = _SPACE.sub(" ", s).strip()
    if not s:
        return None
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _outcome(source: Optional[str]) -> Optional[str]:
    return _OUTCOME_BY_SOURCE.get(source or "")


def _project(latest: dict) -> dict:
    """The `PortalChatArrival` shape. Names every field it emits — no row is
    passed through, so no column (``cost``) can ride along."""
    if latest["kind"] == "deliverable":
        return {
            "kind": "deliverable",
            "id": latest["id"],
            "at": latest["at"],
            "excerpt": _arrival_excerpt(latest.get("title")),
            "outcome": None,
            "title": latest.get("title"),
            "display_hint": latest.get("display_hint"),
        }
    return {
        "kind": "message",
        "id": latest["id"],
        "at": latest["at"],
        "excerpt": _arrival_excerpt(latest.get("content")),
        "outcome": _outcome(latest.get("source")),
    }


def get_chat_state_with_previews(email: str, is_platform: bool) -> dict:
    """`service.get_chat_state` with previews attached (see module docstring)."""
    arrivals = db.unread_arrivals_with_latest(email)
    state = service.get_chat_state(
        email, unread={sid: a["n"] for sid, a in arrivals.items()})

    roster = service.roster_agent_names(email, include_owned=is_platform)
    eligible = sorted(
        (sid for sid, a in arrivals.items()
         if a["n"] > 0 and a["latest"]["agent_name"] in roster),
        key=lambda sid: (arrivals[sid]["latest"]["at"] or "", sid),
        reverse=True,
    )[:MAX_PREVIEWS]
    chosen = set(eligible)

    for chat in state["chats"]:
        cid = chat.get("id")
        if chat.get("kind") != "thread" or cid not in chosen or not chat.get("unread"):
            continue
        a = arrivals[cid]
        chat["latest"] = _project(a["latest"])
        chat["first_unread_message_id"] = a["first_unread_message_id"]
    return state
