"""What a queue row says about itself, read on both sides of the package (#3785, S5).

The predicates every reader shares — `is_platform_minted` and the person
predicates (#715, ent#631, ent#866) — the #2915 receipt vocabulary
(`SYNC_*`, `DELIVERY_*`) and the operator's aging bound (`aging_hours`,
`is_aged`, `annotate_aging`). The router, the ask sink and the Workspace read
them through the package re-exports; the file channel reads them as `rows.X`.

None is ever rebound. A name read on both sides is never patched either:
no single target reaches every reader, so the #3785 guard (G7) refuses it.
A name read only outside the package (`is_about_a_person`) is patched on the
package, which is where all its readers read it. The id-prefix tuples
they compare against are package-owned (O1) and read as `_oq.X`, and the
settings read goes through `deps.db`.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from services import operator_queue_service as _oq
from utils.helpers import parse_iso_timestamp, to_utc_iso

from . import deps

logger = logging.getLogger("services.operator_queue_service")


def is_platform_minted(item) -> bool:
    """Was this queue item raised by the PLATFORM rather than by the agent?

    ent#499. The two agent-facing return paths — the responded write-back into
    ``~/.trinity/operator-queue.json`` and the ent#329 respond→resume dispatch —
    both exist to close a loop the AGENT opened: it parked a question, a human
    answered, the answer goes back. A platform alarm opened no such loop. The
    agent never asked, is not waiting, and in ent#499's case is the SUBJECT of
    the complaint rather than its author.

    Feeding those back is not merely useless, it is a disclosure: ent#499's body
    carries a client's email and their verbatim words, which ent#366 deliberately
    withholds from the rated agent (``comment_withheld``). Without this predicate
    an operator clicking "Got it" hands both to that agent within one 5s sync
    cycle, and — with ``operator_resume_enabled`` — spends one of its turns doing
    it.

    Keyed on the reserved id prefixes, which are already the platform's marker
    for "an agent may not mint this id" (#1632). One predicate, both sinks, so
    they cannot drift.
    """
    if isinstance(item, str):
        candidate = item
    elif isinstance(item, dict):
        candidate = item.get("request_id") or item.get("id") or ""
    else:
        candidate = getattr(item, "request_id", "") or getattr(item, "id", "") or ""
    return str(candidate).strip().lower().startswith(_oq._RESERVED_ID_PREFIXES)


def is_person_tag(item: Optional[dict]) -> bool:
    """trinity-enterprise#631: is this row a person's tag of another person —
    the reserved `mention-` id AND the type, never the agent-authored type
    alone? The operator's door answers such a row as not there."""
    from db.queue_mentions import is_person_tag as _is_tag
    return _is_tag(item)


def is_about_a_person(item: dict) -> bool:
    """Is this row one of the platform's heads-ups ABOUT a person (#715)? They
    are the operator's: the queue's reads never return one to a machine key."""
    candidate = item.get("request_id") or ""
    return str(candidate).strip().lower().startswith(_oq._ABOUT_A_PERSON_ID_PREFIXES)


def is_owner_only(item: dict) -> bool:
    """Is this row readable only by its agent's owner (and admins)? Compared
    like `is_about_a_person`, so the SQL twin excludes exactly the same rows."""
    candidate = item.get("request_id") or ""
    return str(candidate).strip().lower().startswith(_oq.OWNER_ONLY_ID_PREFIXES)


# =========================================================================
# #2915: sync honesty — the vocabulary the loop writes, the fingerprint it
# compares, and the aging predicate every reader shares.
#
# The file contract is unchanged (ingestion is create-only; an agent's rewrite
# is NEVER applied in place — an approval is frozen to the exact action the
# human read). What this adds is that every way the two sides can disagree is
# detected, recorded on the row, and audited, instead of presenting as fine.
# =========================================================================

SYNC_CONFIRMED = "confirmed"          # entry present, same content, same status
SYNC_CHANGED = "changed"              # entry present, content rewritten (detail = fields)
SYNC_CLOSED_BY_FILER = "closed_by_filer"  # entry carries a status the agent set
SYNC_MISSING = "missing"              # entry gone (entry_missing / file_missing)
SYNC_STALE_ID = "stale_id"            # a pending entry re-uses a terminal row's id
SYNC_UNCONFIRMED = "unconfirmed"      # the poller could not reconcile (detail = why)
SYNC_STATES = frozenset({
    SYNC_CONFIRMED, SYNC_CHANGED, SYNC_CLOSED_BY_FILER, SYNC_MISSING,
    SYNC_STALE_ID, SYNC_UNCONFIRMED,
})
# States that audit as `diverged` when entered, `reconciled` when left for confirmed.
DIVERGED_STATES = frozenset({SYNC_CHANGED, SYNC_CLOSED_BY_FILER, SYNC_MISSING, SYNC_STALE_ID})
# States on which a response is refused (409 item_diverged) without an explicit
# acknowledgement: the card the human read is not what the agent now holds.
REFUSE_RESPONSE_STATES = frozenset({SYNC_CHANGED, SYNC_CLOSED_BY_FILER})

DELIVERY_DELIVERED = "delivered"
DELIVERY_UNDELIVERED = "undelivered"
DELIVERY_NOT_APPLICABLE = "not_applicable"
DELIVERY_STATES = frozenset({DELIVERY_DELIVERED, DELIVERY_UNDELIVERED, DELIVERY_NOT_APPLICABLE})

# The operator's aging bound (#2915): a setting, read per call.
OPERATOR_QUEUE_AGING_HOURS_KEY = "operator_queue_aging_hours"
OPERATOR_QUEUE_AGING_HOURS_DEFAULT = 24


def aging_hours() -> int:
    """The operator's bound, `0` = disabled; unreadable ⇒ the default + WARN."""
    try:
        raw = deps.db.get_setting_value(
            OPERATOR_QUEUE_AGING_HOURS_KEY, str(OPERATOR_QUEUE_AGING_HOURS_DEFAULT)
        )
        return max(0, int(str(raw).strip()))
    except Exception as e:  # noqa: BLE001 — a bad setting must not break a list
        logger.warning("[OperatorQueue] aging bound unreadable (%s); using %d h",
                       e, OPERATOR_QUEUE_AGING_HOURS_DEFAULT)
        return OPERATOR_QUEUE_AGING_HOURS_DEFAULT


def _aged_at(item: dict, hours: int) -> Optional[datetime]:
    if hours <= 0 or item.get("status") != "pending":
        return None
    created = item.get("created_at")
    if not created:
        return None
    try:
        return parse_iso_timestamp(str(created)) + timedelta(hours=hours)
    except Exception:  # noqa: BLE001
        return None


def is_aged(item: dict, hours: Optional[int] = None, now: Optional[datetime] = None) -> bool:
    """Has this pending item waited past the operator's bound? ONE predicate for
    the list API, the portal projection and the receipt the poller writes."""
    hours = aging_hours() if hours is None else hours
    at = _aged_at(item, hours)
    if at is None:
        return False
    return at <= (now or datetime.now(timezone.utc))


def annotate_aging(items: list, hours: Optional[int] = None) -> list:
    """Adds `aging` / `aged_since` to every item in place; the frontend renders,
    it never recomputes."""
    hours = aging_hours() if hours is None else hours
    now = datetime.now(timezone.utc)
    for item in items:
        at = _aged_at(item, hours)
        aged = at is not None and at <= now
        item["aging"] = aged
        item["aged_since"] = to_utc_iso(at) if aged else None
    return items
