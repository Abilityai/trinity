"""The operator queue's listing engine (trinity-enterprise#815; #3785).

One page of `GET /api/operator-queue` for a principal: the agent-key
narrowing (`narrow_to_agent_key`), the `agent_names` narrowing, the keyset
(cursor) walk with its Redis priority snapshot, and `list_for_principal`,
which composes them over one WHERE builder so the page, `total` and the flags
describe the same rows.

Package-owned names (the person-prefix tuples) are read as `_oq.X`, the
collaborators as `deps.X`; nothing here binds a copy. The package re-exports
what callers outside read (the router, and tests that call a name); a reader
inside the package names this module (`listing.X`).
"""

import base64
import hashlib
import json
import logging
import re
import uuid
from typing import Optional

from services import operator_queue_service as _oq
from utils.helpers import iso_cutoff, parse_iso_timestamp

from . import deps

logger = logging.getLogger("services.operator_queue_service")


# ============================================================================
# trinity-enterprise#815 — a broad listing is complete before the limit
# ============================================================================

class QueueListError(Exception):
    """A refusal `list_for_principal` names; the router maps it to HTTP."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# `agent_names` (trinity-enterprise#815): the MCP passes its own permit set,
# one query parameter per name. Bounded so a request line stays well inside
# every HTTP server's limit; the MCP refuses a larger set before sending.
AGENT_NAMES_MAX = 500


def narrow_to_agent_key(current_user, accessible):
    """The agents an AGENT-scoped key's queue read covers: its reach
    (`dependencies.principal_reach`: itself, its edges, the agents it spawned,
    trinity-enterprise#872) intersected with its owner's accessible set (or the
    bare set when the owner is an admin, `accessible is None`). Every other principal — a person's JWT
    or user key, a system key, the event loopback — gets `accessible` back
    unchanged (trinity-enterprise#815).

    Narrowing for COMPLETENESS, not an authorization point: an agent key
    resolves to its owner, so its broad read used to rank the owner's whole
    fleet, cut it at `limit`, and leave the MCP to drop the rows the agent may
    not see — its own rows could sit below the cut. The MCP's `checkAgentAccess`
    stays the gate; enforcement on the raw routes is ent#629.

    Keyed on `mcp_scope` / `agent_name` directly, never `acting_agent_name()`,
    which maps a system key to `trinity-system` and would narrow the system
    agent. Pure DB (`agent_permissions`, the ent#727 precedent): no Docker read,
    so a Docker fault cannot shrink the set; a failed read keeps only itself.
    """
    if getattr(current_user, "mcp_scope", None) != "agent":
        return accessible
    allowed = deps.principal_reach(current_user)
    if allowed is None:
        raise QueueListError(403, "agent key carries no agent identity")
    return allowed if accessible is None else allowed & set(accessible)


def _effective_agents(narrowed, agent_names):
    """`agent_names` only ever narrows: intersected with what the caller may
    see, or taken as given when the caller sees every agent."""
    if agent_names is None:
        return narrowed
    if len(agent_names) > AGENT_NAMES_MAX:
        raise QueueListError(422, f"agent_names: at most {AGENT_NAMES_MAX} names")
    if any(not name or not name.strip() for name in agent_names):
        raise QueueListError(422, "agent_names: a blank name is not allowed")
    wanted = set(agent_names)
    return wanted if narrowed is None else narrowed & wanted


# --- the keyset (cursor) walk ------------------------------------------------
#
# A walk starts with `cursor=start` and continues with each `next_cursor`.
# Within one walk no id is returned twice, and every row that matches both when
# the walk starts and when the walk reaches it is returned exactly once, under
# concurrent inserts, endings and platform-alert priority changes. The bound:
# an ending must commit within `db.operator_queue.WALK_WATERMARK_MARGIN_S` of
# the timestamp it stamped (a breach is logged at error there).
#
# The token is `base64url(JSON {"v":1,"w":W,"k":[sec,prk,st,id],"f":fp,"s":walk})`.
# It is NOT signed and carries no authority: every page re-applies the caller's
# visibility, so a forged token can only choose a position over rows the caller
# may already see. `s` names the walk's priority snapshot in Redis (TTL 1 h).

WALK_SNAPSHOT_TTL_S = 3600
_WALK_KEY = "opqueue:walk:{}"
_WALK_SENTINEL = "_"
CURSOR_MAX_BYTES = 4096
CURSOR_KEY_TEXT_MAX = 1024
_WALK_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FILTER_KEYS = ("status", "type", "priority", "agent_name", "since")


def filter_fingerprint(filters: dict) -> str:
    """The caller-chosen filters a cursor belongs to. `agent_names`, `limit` and
    the principal's narrowed set are deliberately NOT in it: the MCP re-reads its
    permits every page, and a permission change must not break a walk — it
    changes which agents' rows match, never the order of the others."""
    canon = json.dumps({k: filters.get(k) for k in _FILTER_KEYS},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _key_text_ok(value) -> bool:
    return (isinstance(value, str) and len(value) <= CURSOR_KEY_TEXT_MAX
            and not any(ord(c) < 0x20 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def encode_cursor(watermark: str, key, fingerprint: str, walk_id: str) -> Optional[str]:
    """The `next_cursor` for a page whose last row has `key`, or None when that
    key cannot be carried (a legacy agent-authored id or `created_at` longer
    than the decoder allows, holding a control character, or whose escaped
    JSON would exceed `CURSOR_MAX_BYTES`) — the caller then says so instead of
    truncating a key or quietly ending the walk.

    The byte check is on the encoded JSON, not only per-field characters:
    `json.dumps` escapes a non-ASCII character to six bytes and `"` / a backslash to
    two, so a 700-character legacy id could pass the per-field check and still
    produce a token `decode_cursor` refuses (ent#815 fix 2). Within the cap the
    token is also within the decoder's base64 length bound."""
    sec, prk, st, item_id = key
    if not (_key_text_ok(st) and _key_text_ok(item_id)):
        return None
    raw = json.dumps({"v": 1, "w": watermark, "k": [sec, prk, st, item_id],
                      "f": fingerprint, "s": walk_id}, separators=(",", ":"))
    if len(raw.encode("utf-8")) > CURSOR_MAX_BYTES:
        return None
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def _bad_cursor(why: str) -> QueueListError:
    return QueueListError(422, f"cursor: {why}; pass cursor=start to begin a walk, "
                               "then the next_cursor of each page")


def decode_cursor(token: str) -> dict:
    """Strict: every malformed token is a 422 naming `cursor`, never a silent
    restart (which would re-return rows the caller already has)."""
    if not _B64URL_RE.match(token) or len(token) > (CURSOR_MAX_BYTES * 4 + 2) // 3 + 4:
        raise _bad_cursor("not a token this server issued")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, TypeError):
        raise _bad_cursor("not a token this server issued")
    if len(raw) > CURSOR_MAX_BYTES:
        raise _bad_cursor(f"longer than {CURSOR_MAX_BYTES} bytes")
    try:
        tok = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _bad_cursor("not a token this server issued")
    if not isinstance(tok, dict) or set(tok) != {"v", "w", "k", "f", "s"}:
        raise _bad_cursor("not a token this server issued")
    if tok["v"] != 1:
        raise _bad_cursor("an unsupported version")
    w = tok["w"]
    if not isinstance(w, str) or not _ISO_Z_RE.match(w):
        raise _bad_cursor("a malformed watermark")
    try:
        parse_iso_timestamp(w)
    except ValueError:
        raise _bad_cursor("a malformed watermark")
    k = tok["k"]
    if not (isinstance(k, list) and len(k) == 4):
        raise _bad_cursor("a malformed position")
    sec, prk, st, item_id = k
    if type(sec) is not int or sec not in (0, 1) or type(prk) is not int or not 0 <= prk <= 4:
        raise _bad_cursor("a malformed position")
    if not (_key_text_ok(st) and _key_text_ok(item_id)):
        raise _bad_cursor("a malformed position")
    if not isinstance(tok["f"], str) or not isinstance(tok["s"], str) \
            or not _WALK_ID_RE.match(tok["s"]):
        raise _bad_cursor("not a token this server issued")
    return {"w": w, "after": (sec, prk, st, item_id), "f": tok["f"], "walk": tok["s"]}


def _walk_redis():
    r = deps.get_breaker_redis()
    if r is None:
        raise QueueListError(
            503, "cursor walks need Redis, which is unreachable; retry, or page with offset")
    return r


def _start_walk_snapshot(snapshot: dict) -> str:
    """Record the walk's platform-alert priorities under a new walk id (option B,
    ent#815). The sentinel keeps an empty snapshot distinguishable from an
    expired one."""
    walk_id = uuid.uuid4().hex
    key = _WALK_KEY.format(walk_id)
    try:
        r = _walk_redis()
        pipe = r.pipeline()
        pipe.hset(key, mapping={_WALK_SENTINEL: "1", **{k: str(v) for k, v in snapshot.items()}})
        pipe.expire(key, WALK_SNAPSHOT_TTL_S)
        pipe.execute()
    except QueueListError:
        raise
    except Exception as e:  # a Redis fault mid-call: same answer as unreachable
        raise QueueListError(503, f"cursor walks need Redis, which failed ({type(e).__name__}); "
                                  "retry, or page with offset")
    return walk_id


def _load_walk_snapshot(walk_id: str) -> dict:
    try:
        stored = _walk_redis().hgetall(_WALK_KEY.format(walk_id))
    except QueueListError:
        raise
    except Exception as e:
        raise QueueListError(503, f"cursor walks need Redis, which failed ({type(e).__name__}); "
                                  "retry, or page with offset")
    if not stored or _WALK_SENTINEL not in stored:
        raise QueueListError(410, "this walk has expired; restart with cursor=start")
    return {k: int(v) for k, v in stored.items() if k != _WALK_SENTINEL}


_UNCARRIABLE_KEY_WARNING = (
    "a row's sort key is too long to page past (id {prefix}…); "
    "use offset paging for this filter"
)


_BELT_DROP_WARNING = (
    "{n} item(s) on this page were withheld after the read (a stored request_id "
    "the database filter could not match); total not verified"
)


def list_for_principal(
    current_user,
    *,
    accessible,
    exclude_about_a_person: bool,
    project,
    filters: dict,
    limit: int,
    offset: int = 0,
    agent_names=None,
    cursor: Optional[str] = None,
    owned_agents=None,
) -> dict:
    """One page of `GET /api/operator-queue` for this caller, with the fields
    that make it honest (trinity-enterprise#815).

    `owned_agents` (trinity-enterprise#866): the agents this caller OWNS, or None
    when the owner-only rule does not apply (an admin). With a set, an
    owner-only row (`OWNER_ONLY_ID_PREFIXES`) is listed only for those agents —
    a SQL condition like every other visibility filter, so `total` stays exact.

    Every visibility filter is a SQL condition on the one WHERE builder, so the
    page, `total` and the flags are computed over the same rows:
    - `has_more` is read off the page itself (`limit + 1` rows asked for), never
      a separate count, so a row landing between the two cannot make it lie;
    - `next_offset` is `offset + limit` while `has_more`, else None;
    - `total` is a `COUNT` over the same filters — a snapshot.

    The agent set is `narrow_to_agent_key` (an agent key: `{self} ∪ permitted`)
    further narrowed by `agent_names`; it applies to the page, `total` and the
    flags alike.

    With `cursor` the page is a step of a keyset walk (see `encode_cursor`):
    `next_cursor` continues it and `next_offset` is None. Without it the route
    is in offset mode, ordered exactly as before, and `next_cursor` is None.

    `project` is the router's per-principal projection (`_for_principal`). It
    stays a BELT: on SQLite and a UTF-8, non-Turkic PostgreSQL the SQL
    exclusion equals `is_about_a_person` exactly, so it should never fire;
    under a Turkic collation a legacy upper-case row can reach it. If it drops
    a row the SQL page returned, `total` becomes None and `warnings` says so. The paging fields are
    unaffected — they describe the SQL page.
    """
    agents = _effective_agents(narrow_to_agent_key(current_user, accessible), agent_names)
    exclude = _oq.ABOUT_A_PERSON_ID_PREFIXES if exclude_about_a_person else None
    where = dict(filters, accessible_agent_names=agents,
                 exclude_request_id_prefixes=exclude)
    if owned_agents is not None:
        where.update(owner_only_request_id_prefixes=_oq.OWNER_ONLY_ID_PREFIXES,
                     owner_agent_names=set(owned_agents))
    warnings = []
    next_cursor = None
    if cursor is None:
        rows = deps.db.list_operator_queue_items(**where, limit=limit + 1, offset=offset)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_offset = offset + limit if has_more else None
    else:
        if offset:
            raise QueueListError(
                422, "cursor and offset cannot be combined: page a walk with cursor alone")
        fingerprint = filter_fingerprint(filters)
        if cursor == "start":
            watermark = iso_cutoff(seconds=deps._queue_db.WALK_WATERMARK_MARGIN_S)
            snapshot = deps.db.operator_queue_walk_alert_priorities(watermark=watermark, **where)
            walk_id = _start_walk_snapshot(snapshot)
            after = None
        else:
            token = decode_cursor(cursor)
            if token["f"] != fingerprint:
                raise QueueListError(422, "cursor does not match these filters; "
                                          "restart with cursor=start")
            watermark, after, walk_id = token["w"], token["after"], token["walk"]
            snapshot = _load_walk_snapshot(walk_id)
        keyed = deps.db.list_operator_queue_items_walk(
            watermark=watermark, snapshot=snapshot, after=after, limit=limit + 1, **where)
        has_more = len(keyed) > limit
        keyed = keyed[:limit]
        page = [item for item, _ in keyed]
        next_offset = None
        if has_more:
            last_key = keyed[-1][1]
            next_cursor = encode_cursor(watermark, last_key, fingerprint, walk_id)
            if next_cursor is None:
                warnings.append(_UNCARRIABLE_KEY_WARNING.format(prefix=str(last_key[3])[:32]))
    total = deps.db.count_operator_queue_items(**where)
    flags = deps.db.count_operator_queue_flags(accessible_agent_names=agents)

    items = project(page)
    dropped = len(page) - len(items)
    if dropped:
        total = None
        warnings.append(_BELT_DROP_WARNING.format(n=dropped))
        logger.warning(
            "[operator-queue] list: the projection withheld %d row(s) the SQL "
            "exclusion kept (ent#815); total reported as null", dropped,
        )
    body = {
        "items": items,
        "count": len(items),
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset,
        "next_cursor": next_cursor,
        "undelivered_count": flags["undelivered"],
        "closed_by_filer_count": flags["closed_by_filer"],
    }
    if warnings:
        body["warnings"] = warnings
    return body
