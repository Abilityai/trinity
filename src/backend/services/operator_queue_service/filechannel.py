"""The agent-file channel's module helpers (#3785, S5).

Everything here exists only for `~/.trinity/operator-queue.json`: the ingest
clamp and the bounds it applies, the #2915 comparison of an agent's entry with
its row (the closed detail vocabulary, the fingerprint, `changed_fields`), the
#3130 `platform.ingestion` marker, and the write-back's answer match. The
ingest and write-back mixins (`ingest.py`, `write_back.py`) read it as
`filechannel.X`; #3438, which retires the file channel, deletes this module.

Package-owned names (the config block, the ask contract) are read as `_oq.X`
at call time, so a patch on `oqs.X` reaches this reader too. Nothing here
binds a copy of one. The package re-exports what tests read as data; a reader
inside the package names this module.
"""

import json
import logging
import re
from typing import Optional, Sequence

from services import operator_queue_service as _oq
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now_iso

from . import rows

logger = logging.getLogger("services.operator_queue_service")


def _valid_execution_id(value) -> Optional[str]:
    """#1632: an execution_id salvaged into a truncation marker must itself be
    bounded and id-shaped — else a 100 KB execution_id smuggled through the
    marker would defeat the context cap (C4)."""
    if (
        isinstance(value, str)
        and 0 < len(value) <= _oq.OPERATOR_QUEUE_EXECUTION_ID_MAX
        and _oq._ID_RE.match(value)
    ):
        return value
    return None


def _bounded_type(value):
    """#3385: the ingest bound on an agent-authored `type` — truncate-with-marker,
    strings only. Shared by the clamp and the #2915 fingerprint so a clamped
    row never reads as a rewrite of its own oversize entry."""
    return _oq._truncate_with_marker(value, _oq.OPERATOR_QUEUE_TYPE_MAX) if isinstance(value, str) else value


def _scalar_text(value):
    """#3314: a JSON number/bool title or question is stored as its text, so the
    stored value is the same on every DB backend and the fingerprint can match it.
    Anything else is returned unchanged (str, None, and containers — a non-empty
    container still fails the insert, as today). A FALSY scalar (0, 0.0, False)
    is also returned unchanged: `create_item` stores the `or`-default for it
    ("Agent request" / "(no details provided)"), and rows ingested before this
    fix carry that default too, so stringifying it here would fingerprint "0"
    against a row that says "Agent request" and flip every such pre-fix row to
    409 `item_diverged` on the next sync."""
    if isinstance(value, (bool, int, float)) and value:
        return str(value)
    return value


def _validated_addressee(agent_name: str, raw) -> Optional[str]:
    """The email an ask is addressed to, or None (ent#364).

    This is the one field on an agent-authored item that is an AUTHORIZATION
    decision: it determines who may answer the ask and whose Workspace sidebar it
    appears in. So it is validated here, at the same boundary that clamps every
    other agent-supplied field, against the agent's own roster — an agent may
    address only someone it was already shared with.

    `include_owned=False` is the rule, not a default (see `agent_on_roster`'s
    docstring): an external client's scope is exactly what was shared with them,
    and the owned-agents branch would hand a client agents nobody gave them.

    Fails CLOSED: a malformed value, an off-roster address, or a roster lookup that
    raises all yield None — an operator ask — because addressing an ask we could
    not validate is worse than not addressing it. Never raises, per this module's
    #1632 contract.
    """
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    if not email or len(email) > _oq.OPERATOR_QUEUE_EMAIL_MAX or "@" not in email:
        return None
    try:
        from client_portal.service import agent_on_roster

        if agent_on_roster(agent_name, email, include_owned=False):
            return email
        logger.info(
            "[OperatorQueue] %s addressed an item to an off-roster email; "
            "treating it as an operator ask (ent#364)",
            agent_name,
        )
    except Exception:  # noqa: BLE001 — a roster read must never break ingestion
        logger.warning(
            "[OperatorQueue] could not validate the addressee for %s; "
            "treating it as an operator ask (ent#364)",
            agent_name,
            exc_info=True,
        )
    return None


def _clamp_ingested_item(req: dict, agent_name: str = "") -> dict:
    """#1632: total field-hygiene clamp for an agent-authored queue item.

    Called INSIDE the create try/except so any failure is quarantined by #1525
    rather than hot-looping — but it is written to NEVER raise (every branch is
    isinstance-guarded, json.dumps is wrapped). Returns a NEW dict; never mutates
    the caller's request.

    NOT PURE, and the name undersells it. Two of the steps below reach the
    database, and one of them WRITES:

    * `addressed_to_email` (ent#364) is resolved against the agent's roster —
      an authorization decision, and the reason it is not simply copied through.
    * `context.workspace_session_id` (ent#429) is stripped and then re-written
      with a thread this call may CREATE (`_workspace_thread_for`).

    So this is not safe to call speculatively "just to see what a clamped item
    would look like": today's one production caller creates the row immediately
    after, and a second caller that did not would leave an empty client thread
    behind. Both reads/writes are fail-soft and neither can raise, so the #1525
    contract above still holds.

    - title / question: truncate-with-marker.
    - type: truncate-with-marker (#3385).
    - context: non-dict → {} (fixes the create_item .get crash class); serialized
      > cap OR non-serializable → a marker object that preserves only a *valid*
      (≤EXECUTION_ID_MAX, id-shaped) execution_id.
    - options: serialized > cap OR non-serializable → a small marker list.
    - created_at: normalized to ingest time (defeats future-date sort-pinning);
      expires_at is left untouched (honored).
    - priority: validate-only (unknown → medium; a legit `critical` is untouched —
      the depth cap already bounds critical *volume*).
    - addressed_to_email: resolved, never trusted (ent#364) — see above.
    - context.workspace_session_id: agent value stripped, platform value written
      for an addressed ask (ent#429) — see above.
    """
    out = dict(req)

    title = _scalar_text(out.get("title"))
    if isinstance(title, str):
        out["title"] = _oq._truncate_with_marker(title, _oq.OPERATOR_QUEUE_TITLE_MAX)

    question = _scalar_text(out.get("question"))
    if isinstance(question, str):
        out["question"] = _oq._truncate_with_marker(question, _oq.OPERATOR_QUEUE_QUESTION_MAX)

    if "type" in out:
        out["type"] = _bounded_type(out["type"])

    # ent#364: the addressee is an authorization decision, so it is resolved here
    # rather than trusted. Absent/invalid/off-roster → None, i.e. an operator ask.
    #
    # Resolved BEFORE the context block below, not after, because ent#429 writes
    # the addressee's thread id INTO context — doing it afterwards would add
    # bytes the size cap had already signed off on.
    out["addressed_to_email"] = _validated_addressee(agent_name, out.get("addressed_to_email"))

    context = out.get("context")
    if not isinstance(context, dict):
        # Non-dict context (str/list/None) → {} — also fixes the pre-existing
        # create_item execution_id `.get` crash. An ask with no context at all is
        # the COMMON case for a scheduled run, and it is exactly the one that must
        # still get a thread (ent#429), so the attach happens on this branch too.
        out["context"] = {}
        if out["addressed_to_email"]:
            thread_id = _oq._workspace_thread_for(agent_name, out["addressed_to_email"])
            if thread_id:
                out["context"][_oq._WORKSPACE_THREAD_KEY] = thread_id
    else:
        # ent#429: `workspace_session_id` is PLATFORM-written and this is the one
        # place that writes it. Stripped UNCONDITIONALLY first — an agent that
        # could author it would be choosing which conversation its ask claims to
        # belong to, and `_project` hands that straight to the client as
        # `chat_id`. The projection's docstring already promised "platform-written
        # context only"; nothing enforced it until now.
        #
        # Rebuilt rather than popped: `out = dict(req)` is a SHALLOW copy, so the
        # context dict is still the caller's, and this function's contract is that
        # it never mutates the request it was handed.
        context = {k: v for k, v in context.items() if k not in _oq._PLATFORM_CONTEXT_KEYS}
        out["context"] = context
        if out["addressed_to_email"]:
            thread_id = _oq._workspace_thread_for(agent_name, out["addressed_to_email"])
            if thread_id:
                context[_oq._WORKSPACE_THREAD_KEY] = thread_id
        ctx_bytes = _oq._json_bytes(context)  # None = non-serializable
        if ctx_bytes is None or ctx_bytes > _oq.OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
            marker = {
                "_truncated": True,
                "_original_bytes": ctx_bytes,
                "execution_id": _valid_execution_id(context.get("execution_id")),
            }
            # The thread id survives truncation. It is platform-written and ~32
            # bytes, and dropping it would make an oversize agent context the one
            # way to produce a homeless ask (ent#429).
            if context.get(_oq._WORKSPACE_THREAD_KEY):
                marker[_oq._WORKSPACE_THREAD_KEY] = context[_oq._WORKSPACE_THREAD_KEY]
            out["context"] = marker

    options = out.get("options")
    if options is not None:
        opt_bytes = _oq._json_bytes(options)  # None = non-serializable
        if opt_bytes is None or opt_bytes > _oq.OPERATOR_QUEUE_OPTIONS_MAX_BYTES:
            out["options"] = [_oq._OPTIONS_DROPPED_MARKER]

    # #3313: isinstance first — a list/object priority is unhashable, and the
    # set membership test raised out of this never-raises clamp.
    if not isinstance(out.get("priority"), str) or out["priority"] not in _oq._VALID_PRIORITIES:
        out["priority"] = "medium"

    # Ignore the agent-supplied created_at (a future date pins the item atop the
    # `created_at DESC` sort, C4). expires_at is honored as authored.
    out["created_at"] = utc_now_iso()

    return out

# #3024: the terminal statuses the write-back itself writes into the agent's file
# (#1017). `db.get_terminal_items_for_agent` hands the write-back exactly the rows
# `awaits_terminal_flip` is True for — one rule in two spellings, pinned together
# by a real-DB test (test_2915_operator_queue_sync_honesty.py).
_FLIPPED_BY_WRITE_BACK = frozenset({"cancelled", "expired"})


def awaits_terminal_flip(row) -> bool:
    """Will this cycle's write-back write the row's ending into the agent's entry?

    A cancelled / expired row whose flip has not landed: never attempted, or
    retried after `undelivered` — except `entry_missing`, whose entry the agent
    had already dropped. Until the flip lands, a still-`pending` entry with the
    row's id is the ORIGINAL entry awaiting it, not a re-used id (#3024).
    """
    return (
        row.get("status") in _FLIPPED_BY_WRITE_BACK
        and row.get("delivery_state") in (None, rows.DELIVERY_UNDELIVERED)
        and (row.get("delivery_detail") or "") != "entry_missing"
    )

# `sync_detail` / `delivery_detail` are durable, operator-visible, audited
# columns — a CLOSED vocabulary. Field names, folded status tokens and failure
# kinds only; never `str(e)`, never `response.text` (agent-controlled), never
# agent-authored text.
_DETAIL_RE = re.compile(r"^[a-z0-9_,]{1,96}$")
_AGENT_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")
READ_FAILURE_THRESHOLD = 3            # consecutive failed cycles before `unconfirmed` (sync_health precedent)
LAST_CONFIRMED_REFRESH_SECONDS = 60   # `last_confirmed_at` cadence — one batched UPDATE per agent per minute
_PLATFORM_BLOCK_KEY = "platform"      # the receipt lives under `platform` in the agent's entry
_AGING_SINCE_KEY = "aging_since"
_CONTENT_FIELDS = ("title", "question", "options", "expires_at", "type", "priority", "context", "addressee")
_CONTEXT_TRUNCATED_SENTINEL = "<truncated>"


def _detail(token) -> str:
    """Fold any candidate into the closed detail vocabulary; unknown ⇒ `other`."""
    s = str(token or "").strip().lower()
    return s if _DETAIL_RE.match(s) else "other"


def _fold_agent_status(raw) -> str:
    """An agent-written status becomes a short lowercase token or `other`."""
    s = str(raw or "").strip().lower()
    return s if _AGENT_STATUS_RE.match(s) else "other"


def _read_failure_detail(result: dict) -> str:
    """Why a read failed, as a token — a status code or a class, never text."""
    code = result.get("status_code") if isinstance(result, dict) else None
    if isinstance(code, int):
        return f"http_{code}"
    err = str(result.get("error") or "") if isinstance(result, dict) else ""
    return "timeout" if "timeout" in err.lower() else "unreachable"


def _normalise_expires(value) -> str:
    if not value:
        return ""
    try:
        return to_utc_iso(parse_iso_timestamp(str(value)))
    except Exception:  # noqa: BLE001 — an unparseable deadline compares as its text
        return str(value)


def _canonical_options(options) -> str:
    # #3314: mirrors `db/operator_queue.py` storing `json.dumps(options) if
    # item.get("options") else None` — a falsy value ([], "", {}, 0, False) is
    # stored NULL, so it must canonicalise like None or the untouched entry
    # reads as a rewrite.
    if not options:
        return ""
    try:
        return json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps([_oq._OPTIONS_DROPPED_MARKER])


def _entry_content(req: dict) -> dict:
    """The comparable content of an agent's file entry — the PURE half of
    `_clamp_ingested_item` (caps, markers) plus `create_item`'s defaults, so it
    matches what the row stored at ingest byte for byte. `_clamp_ingested_item`
    itself is deliberately not called: it reads the roster and may create a
    workspace thread, and must never run speculatively."""
    title = _scalar_text(req.get("title"))
    title = _oq._truncate_with_marker(title, _oq.OPERATOR_QUEUE_TITLE_MAX) if isinstance(title, str) else None
    question = _scalar_text(req.get("question"))
    question = _oq._truncate_with_marker(question, _oq.OPERATOR_QUEUE_QUESTION_MAX) if isinstance(question, str) else None
    options = req.get("options")
    if options is not None:
        try:
            if len(json.dumps(options).encode("utf-8")) > _oq.OPERATOR_QUEUE_OPTIONS_MAX_BYTES:
                options = [_oq._OPTIONS_DROPPED_MARKER]
        except (TypeError, ValueError):
            options = [_oq._OPTIONS_DROPPED_MARKER]
    return {
        "title": title or "Agent request",
        "question": question or title or "(no details provided)",
        "options": _canonical_options(options),
        "expires_at": _normalise_expires(req.get("expires_at")),
        "type": _comparable_type(req.get("type")),
        "priority": _comparable_priority(req.get("priority")),
        "context": _comparable_context(req.get("context")),
        "addressee": _comparable_addressee(req.get("addressed_to_email")),
    }


def _row_content(row: dict) -> dict:
    return {
        "title": row.get("title") or "Agent request",
        "question": row.get("question") or row.get("title") or "(no details provided)",
        "options": _canonical_options(row.get("options")),
        "expires_at": _normalise_expires(row.get("expires_at")),
        "type": _comparable_type(row.get("type")),
        "priority": _comparable_priority(row.get("priority")),
        "context": _comparable_context(row.get("context")),
        "addressee": _comparable_addressee(row.get("addressed_to_email")),
    }


def _comparable_type(value) -> str:
    # #3385: bound BEFORE the empty-value default, so a row holding the clamped
    # value and an entry still holding the raw one compare equal.
    value = _bounded_type(value)
    return "question" if value in (None, "") else str(value)


def _comparable_priority(value) -> str:
    # #3313: isinstance first, as in the clamp — an unhashable value raised here.
    return value if isinstance(value, str) and value in _oq._VALID_PRIORITIES else "medium"


def _comparable_addressee(value) -> Optional[str]:
    return (str(value).strip().lower() or None) if isinstance(value, str) else None


def _comparable_context(ctx) -> str:
    """The clamp's PURE half for `context` (#2989 review): non-dict → {}, the
    platform's workspace keys (`_PLATFORM_CONTEXT_KEYS` — the thread, ent#429,
    and the raised-in-turn flag, ent#734) stripped (never agent content), and an oversize / unserialisable value → one sentinel — which
    is also what a row holds after ingest (`_truncated`). Compared as canonical
    JSON. Sized the way the clamp sizes it, so the cap is crossed on both sides
    at (very nearly) the same input."""
    if not isinstance(ctx, dict):
        return "{}"
    if ctx.get("_truncated") is True:
        return _CONTEXT_TRUNCATED_SENTINEL
    body = {k: v for k, v in ctx.items() if k not in _oq._PLATFORM_CONTEXT_KEYS}
    try:
        if len(json.dumps(body).encode("utf-8")) > _oq.OPERATOR_QUEUE_CONTEXT_MAX_BYTES:
            return _CONTEXT_TRUNCATED_SENTINEL
        return json.dumps(body, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return _CONTEXT_TRUNCATED_SENTINEL


def changed_fields(row: dict, req: dict) -> list:
    """Which content fields the agent rewrote since ingest. `addressee` counts
    only when the row resolved one: the ingest value is an authorization
    decision against the roster (ent#364), so an addressee the roster refused
    is not a rewrite — a re-address is ent#619's supersede vocabulary."""
    a, b = _row_content(row), _entry_content(req)
    out = []
    for f in _CONTENT_FIELDS:
        if f == "addressee" and not a["addressee"]:
            continue
        if a[f] != b[f]:
            out.append(f)
    return out


def _well_formed_queue(data) -> bool:
    """An object whose `requests` (if present) is a list. Anything else is the
    WRONG SHAPE and is read as `unconfirmed`, never as an empty queue (#2989
    review: `{"requests": {...}}` used to mark every row `missing` and then
    overwrite the agent's file; a top-level array raised inside `gather`)."""
    return isinstance(data, dict) and isinstance(data.get("requests", []), list)


# #3130: why a queue file's new entries were held — the file-level
# `platform.ingestion.reason`, a closed vocabulary (never agent text). The
# strongest reason in a cycle wins: a full queue needs the agent to wait for an
# ask to end, a rate hold clears by itself, a malformed id needs a fix.
HOLD_QUEUE_FULL = "queue_full"
HOLD_RATE_LIMITED = "rate_limited"
HOLD_INVALID_ID = "invalid_id"
# #3243: an entry over the authoring caps (options / an agent's title). Ranked
# lowest — the offending ids ride in their own list on the marker, so a
# self-clearing `queue_full`/`rate_limited` reason can never hide them.
HOLD_INVALID_OPTIONS = "invalid_options"
HOLD_INVALID_TITLE = "invalid_title"
_HOLD_RANK = {HOLD_INVALID_OPTIONS: 0, HOLD_INVALID_TITLE: 0,
              HOLD_INVALID_ID: 1, HOLD_RATE_LIMITED: 2, HOLD_QUEUE_FULL: 3}
# How many offending ids the marker names per list (ids already `_ID_RE`-valid).
_MARKER_MAX_IDS = 10
_INGESTION_KEY = "ingestion"
# "Leave the marker as it is" — distinct from None, which means "remove it".
_MARKER_UNCHANGED = object()


def _stronger_hold(current: Optional[str], new: str) -> str:
    return new if current is None or _HOLD_RANK[new] > _HOLD_RANK[current] else current


def _ingestion_marker(reason: Optional[str], invalid_options: Sequence[str] = (),
                      invalid_title: Sequence[str] = ()) -> Optional[dict]:
    """The marker a held file should carry, or None when nothing is held. `since`
    is stamped when it is written (`_apply_ingestion_marker`). #3243: the ids of
    entries held over the authoring caps, with the limits in force, ride beside
    `reason` — only the agent can fix them, so they are named whatever reason
    wins."""
    if reason is None:
        return None
    marker = {"reason": reason, "max_pending": _oq.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT}
    if invalid_options:
        marker[HOLD_INVALID_OPTIONS] = list(invalid_options)[:_MARKER_MAX_IDS]
        marker["max_options"] = _oq.OPERATOR_QUEUE_MAX_OPTIONS
        marker["max_option_chars"] = _oq.OPERATOR_QUEUE_OPTION_MAX_CHARS
    if invalid_title:
        marker[HOLD_INVALID_TITLE] = list(invalid_title)[:_MARKER_MAX_IDS]
        marker["max_title_chars"] = _oq.OPERATOR_QUEUE_ASK_TITLE_MAX_CHARS
    return marker


def _current_marker(queue_data) -> Optional[dict]:
    block = queue_data.get(_PLATFORM_BLOCK_KEY) if isinstance(queue_data, dict) else None
    marker = block.get(_INGESTION_KEY) if isinstance(block, dict) else None
    return marker if isinstance(marker, dict) else None


def _marker_differs(queue_data, wanted: Optional[dict]) -> bool:
    """Whether the file's marker is not the one wanted. Compared on the fields
    the platform decides (`reason`, `max_pending`), never on `since`: a hold
    that continues keeps its start time and is not rewritten. A marker the agent
    wrote itself is read as data only — a different value is overwritten."""
    current = _current_marker(queue_data)
    if wanted is None:
        return current is not None
    # #3243: the key set too — a fixed entry drops its id list while the
    # reason (say `queue_full`) continues, and the stale list must go.
    return (current is None or set(current) - {"since"} != set(wanted)
            or any(current.get(k) != v for k, v in wanted.items()))


def _apply_ingestion_marker(queue_data: dict, wanted: Optional[dict], now: str) -> bool:
    """Set or remove `platform.ingestion` on the freshly re-read file. True when
    the file changed."""
    if not _marker_differs(queue_data, wanted):
        return False
    block = queue_data.get(_PLATFORM_BLOCK_KEY)
    if wanted is None:
        if isinstance(block, dict):
            block.pop(_INGESTION_KEY, None)
            if not block:
                queue_data.pop(_PLATFORM_BLOCK_KEY, None)
        return True
    if not isinstance(block, dict):
        block = {}
        queue_data[_PLATFORM_BLOCK_KEY] = block
    current = _current_marker(queue_data)
    since = current.get("since") if current and current.get("reason") == wanted["reason"] else None
    block[_INGESTION_KEY] = {**wanted, "since": since if isinstance(since, str) else now}
    return True


def _deliver_into(req: dict, resp: dict) -> None:
    # #715: never who answered — the agent's file carries the answer, not a
    # person's email.
    req["status"] = "responded"
    req["response"] = resp["response"]
    req["response_text"] = resp.get("response_text")
    req["responded_at"] = resp.get("responded_at")


def _is_our_answer(req: dict, resp: dict) -> bool:
    """Did a previous write-back land THIS answer in the entry? Matched on the
    answer's own timestamp, so an entry the agent closed itself (`acknowledged`
    with no `responded_at`) is never mistaken for a delivered one."""
    return (
        req.get("status") in ("responded", "acknowledged")
        and bool(resp.get("responded_at"))
        and req.get("responded_at") == resp.get("responded_at")
    )
