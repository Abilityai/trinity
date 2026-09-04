"""The Workspace agent page (ent#360).

An agent had no home. A roster row started a chat, so there was nowhere to see
what an agent has been doing, nowhere for it to ask you something while no chat
is open, and nowhere to show what it can do. This assembles that page.

Two constraints shape everything here, and both are subtractive:

**It reports; it does not configure.** No schedules, no skill editing, no logs,
no costs (ent#360 AC #7). Building agents stays operator-side. Model and plan
are simply not shown — the AC allows them "informational and visibility-gated",
and the cheapest way to satisfy a gate is to not open the door.

**The viewer may be an external client, not an operator.** Every field below is
chosen with that in mind, because the same page serves a portal-token client and
a platform user. Three places where that bites:

* `recent_work` is projected down to status/trigger/time/duration, plus the
  **schedule name** (#2161). The underlying accessor also returns `message`,
  `cost`, `model_used` and `source_user_email` — another user's prompt, and two
  things AC #7 excludes. The schedule name is the one string that deliberately
  does cross, because rows reading "Scheduled run" nine times tell the reader
  nothing; it is a short label, never the schedule's `message` (which is a
  prompt and is exactly what this page must not show).

  It is **not** guaranteed to be operator-authored, and calling it that would be
  the comfortable mistake: `POST /api/agents/{name}/schedules` is
  `AuthorizedAgent` and the MCP `create_agent_schedule` tool exists, so an
  agent-scoped key — hence a prompt-injected agent — can write the text that
  renders on a client's page. It is therefore treated as untrusted content and
  bounded (`MAX_SCHEDULE_NAME_CHARS`); Vue escapes it on render. The same is
  already true of `asks` (`title`/`question` are agent-authored), so this is the
  established boundary rather than a new one — but it is the reason the name is
  capped and the prompt is never loaded at all.
* `asks` carries only agent-authored `approval`/`question` items, never
  `alert`. Alerts are platform-generated (sync-failing, git-bloat, breaker
  dormancy) and are operations telemetry, not something the agent is asking a
  person. `context` is never exposed at all: it is free-form agent JSON and has
  been a credential-leak surface before (canary G-04).
* Everything is DB-sourced, so the page renders for a stopped agent (AC #6) —
  degraded, not empty.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import db
from db.canvas import AUDIENCE_ROSTER as CANVAS_AUDIENCE_ROSTER
from models import REPORT_ROWS_PAGE_MAX
from services import canvas_service

from . import db as portal_db

logger = logging.getLogger(__name__)

# ent#360 AC: the stats strip is a window. Same set the Agent Detail Overview
# offers (#1107), so the analytics accessor is reused rather than reimplemented.
WINDOWS = {"7d": 168, "14d": 336, "30d": 720}
DEFAULT_WINDOW = "7d"

# Only these reach a Workspace viewer. `alert` is platform-generated operations
# telemetry, not a question an agent is asking a person.
# #2449: `ASK_TYPES` / `MAX_ASKS` and the `_asks()` reader that used them are
# gone. This page rendered asks from a SECOND projection of the same
# `operator_queue` rows, beside the `PortalAsks` component that reads `/asks` —
# so every ask appeared twice, and the two halves disagreed: this one capped at
# 20 where `/asks` fetches 200, and carried no `status`, so it could not show an
# expired ask as expired (ent#429's AC) and counted one where the sidebar did
# not. One entity, one projection; `client_portal/asks/service.py` is it, and it
# covers strictly more (revoked share, unreadable roster, expiry, context).
MAX_RECENT_WORK = 20

MAX_CHATS = 20

# Executions the platform creates outside a cron schedule carry this sentinel
# instead of a real schedule id (reminders, manual chat turns), so it can never
# resolve to a name and is not worth a query.
NO_SCHEDULE_ID = "__manual__"

# `ScheduleCreate.name` carries no length bound, so the row label is capped
# here rather than trusted. Long enough to tell schedules apart, short enough
# that one cannot take over the list.
MAX_SCHEDULE_NAME_CHARS = 80


def _health(agent_name: str) -> dict:
    """Coarse health for the header, from the last persisted check.

    Never the live Docker state: AC #6 wants this page to render for a stopped
    agent, and a probe that needs the container is exactly what cannot answer
    then. `unknown` is an honest answer for an agent monitoring has never
    checked — monitoring is default-OFF (#1121), so on many installs that is
    every agent, and rendering "unhealthy" there would be a lie.
    """
    try:
        row = db.get_latest_health_check(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: health read failed for %s: %s", agent_name, e)
        return {"status": "unknown", "checked_at": None}
    if not row:
        return {"status": "unknown", "checked_at": None}
    return {
        "status": row.get("status") or "unknown",
        "checked_at": row.get("checked_at"),
    }


def _without_hidden_buckets(a: dict) -> dict:
    """Drop the hidden buckets from the analytics payload, and RE-DERIVE the
    counts that quoted them (#2423).

    Removing a segment while leaving the day total is worse than not filtering:
    a bar whose parts sum to 1 above a label reading 13 reports its own
    filtering as missing data, and the headline would sit above a chart that
    cannot account for it. `success_rate` is deliberately NOT recomputed — it is
    a ratio over terminal rows this function cannot see, and a number derived
    from a filtered numerator over an unfiltered denominator would be worse than
    one that is merely broad.
    """
    out = dict(a)

    # The two `by_type` fields have DIFFERENT shapes under one name: the
    # top-level total is a LIST of `{"bucket", "total"}` rows, while each
    # timeline day carries a DICT of `{bucket: count}` (`db/schedules/
    # analytics.py`). Handling only one of them is how the first draft of this
    # crashed the whole page on a real payload.
    kept_totals = [row for row in (a.get("by_type") or [])
                   if _bucket_of(row) not in _CLIENT_HIDDEN_BUCKETS]
    out["by_type"] = kept_totals
    out["buckets"] = [b for b in (a.get("buckets") or [])
                      if b not in _CLIENT_HIDDEN_BUCKETS]

    timeline = []
    for day in (a.get("timeline") or []):
        d = dict(day)
        kept = {k: v for k, v in (day.get("by_type") or {}).items()
                if k not in _CLIENT_HIDDEN_BUCKETS}
        d["by_type"] = kept
        # A day that had only hidden work becomes an empty day, not a missing
        # one — the axis stays continuous (#1107's gap-fill convention).
        d["total"] = sum(kept.values())
        timeline.append(d)
    out["timeline"] = timeline
    out["total_executions"] = sum(_total_of(row) for row in kept_totals)
    return out


def _bucket_of(row) -> str:
    """The bucket name of a top-level `by_type` entry, whichever shape it is.

    Tolerant in ONE direction only, and deliberately: the accessor emits
    `{"bucket", "total"}` rows and the module's own unavailable path defaults to
    `[]`, so a row of any other shape is unparseable — and an unparseable row is
    KEPT, never dropped. This function's job is to hide two named buckets; a
    parse failure that hid a third would be the opposite of it, silently, with
    the day totals re-derived around the hole.

    The tolerance is not there to accommodate a test double. It is there because
    `_without_hidden_buckets` re-derives `total_executions` from what survives
    this call, so a row this function cannot read still has to reach `_total_of`
    and be counted (#2423 review).
    """
    if isinstance(row, dict):
        return row.get("bucket") or ""
    return str(row)


def _total_of(row) -> int:
    if isinstance(row, dict):
        try:
            return int(row.get("total") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


# #2423: work a client can see but cannot open, control, or explain.
#
# The loops strip is `isPlatformSession`-gated (ent#458 — loops are an operator
# capability, correctly), and this page has no Loops tab, so a client had the
# COUNT of loop runs with the OUTPUT reachable only from Agent Detail. Twelve
# rows saying "Loop" and a `Loops 12` legend entry, leading nowhere.
#
# This follows the module's existing rule rather than adding one: it already
# reports rather than configures, already projects away spend and prompts, and
# already drops `alert` asks as "operations telemetry, not something the agent
# is asking a person". A loop run is the same kind of thing.
#
# Operators keep it — they can click through and read every run, so hiding it
# from them removes real signal and fixes nothing. Same `is_platform` split the
# roster and `_require_roster` already use.
_CLIENT_HIDDEN_TRIGGERS = frozenset({"loop"})

# The analytics bucket `_TRIGGER_BUCKETS` folds `loop` into (`db/schedules`).
# Named separately because the two vocabularies are genuinely different — one is
# a `triggered_by` value, the other a display label — and a single constant
# would hide that a rename on either side breaks the pair.
#
# Review pass 2: that comment described the hazard and then left nothing to
# notice it. The correspondence is now DERIVED from `_TRIGGER_BUCKETS` and
# asserted by `test_the_hidden_trigger_and_the_hidden_bucket_cannot_drift`, so a
# rename in either file fails CI instead of half-reverting this change — the
# chart would show loop counts to a client again while the rows stayed hidden,
# which is the "legend reads 12 above a list that says nothing" contradiction
# the whole change exists to remove.
_CLIENT_HIDDEN_BUCKETS = frozenset({"Loops"})


def _stats(agent_name: str, window: str, *, is_platform: bool = False) -> dict:
    """Activity chart + headline numbers, from the existing analytics accessor.

    The issue's Technical Notes name that accessor specifically, so this adds no
    query of its own: it reshapes what #1107 already computes.
    """
    hours = WINDOWS.get(window, WINDOWS[DEFAULT_WINDOW])
    try:
        a = db.get_agent_analytics(agent_name, hours)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: analytics failed for %s: %s", agent_name, e)
        return {
            "window": window, "window_hours": hours, "total_executions": 0,
            "success_rate": None, "timeline": [], "by_type": [], "buckets": [],
            "first_try": {"terminal": 0, "first_try": 0, "rate": None},
            "unavailable": True,
        }
    if not is_platform:
        a = _without_hidden_buckets(a)
    if not is_platform and not a.get("total_executions"):
        # #2423 review: `success_rate` and `first_try` are deliberately NOT
        # re-derived over the filtered set — a filtered numerator over an
        # unfiltered denominator is worse than a figure that is merely broad.
        # But that argument only holds while there is visible work to be broad
        # ABOUT. With every row hidden the strip read "0 executions · 89%
        # success · 33/37 first try", which is not broad, it is a contradiction
        # the client cannot resolve — three numbers describing work the page
        # simultaneously says did not happen.
        #
        # So the rates are withheld at exactly zero, and the UI already renders
        # a null rate as an em-dash (a fresh agent has no success rate; 0% would
        # read as "it fails every time"). Withheld, never zeroed.
        return {
            "window": window,
            "window_hours": a.get("window_hours", hours),
            "total_executions": 0,
            "success_rate": None,
            "timeline": a.get("timeline", []),
            "by_type": [],
            "buckets": [],
            "first_try": {"terminal": 0, "first_try": 0, "rate": None},
            "unavailable": False,
        }
    # Below the zero-gate on purpose: the withheld branch discards this value,
    # so computing it above cost one DB round-trip on exactly the case that
    # cannot use it (#2423 review pass 2).
    first_try = portal_db.first_try_stats(agent_name, hours)
    return {
        "window": window,
        "window_hours": a.get("window_hours", hours),
        "total_executions": a.get("total_executions", 0),
        "success_rate": a.get("success_rate"),
        # Per-day counts for the chart. `by_type` per day is kept so the chart
        # can stack by what triggered the work, as the Overview one does.
        "timeline": a.get("timeline", []),
        "by_type": a.get("by_type", []),
        # #2161: the accessor already computes the canonical stack order
        # (`_BUCKET_ORDER` filtered to what is present). Forwarding it keeps that
        # order in ONE place — deriving it client-side from `by_type` would be
        # equivalent today and free to diverge tomorrow.
        "buckets": a.get("buckets", []),
        # AC #3's "first-try rate". Distinct from success_rate above, which
        # counts a retried-then-succeeded execution as a success.
        "first_try": first_try,
        "unavailable": False,
    }


def _schedule_names(agent_name: str, rows: list[dict]) -> dict:
    """`schedule_id` → schedule NAME, for the rows that actually carry one (#2161).

    One query for the whole page, never `db.get_schedule` per row: that is an N+1,
    and it would also throw away the agent scoping this shape gets for free. The
    map is built from *this* agent's own schedules, so a foreign or stale id
    simply misses and the row falls back to its trigger label.

    Only `id → name`, and the accessor is a **projected SELECT** rather than
    `list_agent_schedules`, which returns whole `Schedule` models carrying
    `message` and `validation_prompt`. Reading those and then choosing not to
    return them would make this module's stated principle — a field that never
    leaves the service cannot be leaked by a later edit — a review invariant.
    Not loading them makes it structural, and it is one edit (`{s.id: s}`, or a
    `description` for a subtitle) away from mattering.

    The name is **truncated**: `ScheduleCreate.name` has no length bound, and
    the writer is not necessarily a human (see the note in the module docstring),
    so an unbounded string reaches a client page otherwise.

    Fails soft: a schedules read that raises costs the labels, never the rows.
    """
    wanted = {
        r.get("schedule_id") for r in rows
        if r.get("schedule_id") and r.get("schedule_id") != NO_SCHEDULE_ID
    }
    if not wanted:
        return {}
    try:
        # Already excludes soft-deleted rows (#834) — a deleted schedule's
        # historical executions read as a plain trigger label, which is honest.
        names = db.get_agent_schedule_names(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: schedule names failed for %s: %s", agent_name, e)
        return {}
    return {
        sid: name[:MAX_SCHEDULE_NAME_CHARS]
        for sid, name in names.items() if sid in wanted and name
    }


def _recent_work(agent_name: str, limit: int = MAX_RECENT_WORK, *,
                 is_platform: bool = False) -> list[dict]:
    """What the agent has been doing — shape, plus the schedule's name.

    The accessor returns `message`, `cost`, `model_used` and `source_user_email`
    among others. A Workspace viewer may be an external client, so the prompt
    text of somebody else's task, the spend, and the model are all projected
    away here rather than filtered in the UI: a field that never leaves the
    service cannot be leaked by a later template change.

    `schedule_name` (#2161) is the deliberate exception, because without it every
    scheduled row rendered the same three words. It attaches to any row whose id
    resolves — not only `triggered_by == "schedule"` — since a webhook that fires
    a schedule *is* running that schedule, and naming it is the point.
    """
    # #2423: a client sees only work it can act on or understand. The exclusion
    # is pushed into SQL rather than applied to the result, because a filter
    # that runs AFTER the LIMIT starves the list: an agent whose newest rows are
    # all loops rendered "Nothing yet." while the operator saw twenty, which is
    # worse than the bug being fixed — rows you cannot explain are confusing,
    # "this agent has done nothing" is false.
    #
    # The first fix over-fetched `MAX_RECENT_WORK * 5 = 100` and filtered in
    # Python. That is not a smaller starvation window, it is the SAME bug with a
    # constant in front of it, and the constant loses: `models.MAX_RUNS_LIMIT`
    # is 100, so ONE loop at its documented maximum emits exactly 100
    # consecutive rows and fills the whole over-fetch window. ent#458's own
    # repro ("17 rows, mostly loops") is this shape at small scale, and it is
    # the normal case for the agents this feature exists for.
    #
    # Filtering in SQL also drops the extra read entirely: the client page now
    # fetches exactly `limit` rows like the operator page, instead of five times
    # as many to throw most away.
    try:
        rows = db.get_agent_executions_summary(
            agent_name, limit=limit,
            exclude_triggers=None if is_platform else _CLIENT_HIDDEN_TRIGGERS)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: executions read failed for %s: %s", agent_name, e)
        return []
    names = _schedule_names(agent_name, rows)
    return [{
        "id": r.get("id"),
        "status": r.get("status"),
        "triggered_by": r.get("triggered_by"),
        "started_at": r.get("started_at"),
        "completed_at": r.get("completed_at"),
        "duration_ms": r.get("duration_ms"),
        "schedule_name": names.get(r.get("schedule_id")),
    } for r in rows]


def reports(agent_name: str, client_email: str, *, limit: int = 20, offset: int = 0,
            portal_session_id: str | None = None) -> list[dict]:
    """Deliverables **addressed to this person** by this agent (ent#365).

    Reuses the existing report surface (#918) exactly as the Technical Notes
    ask. Metadata only: a payload is up to 5 MiB (`REPORT_PAYLOAD_MAX_BYTES`,
    raised from 256 KB in #1537) and belongs on expansion.

    **Behaviour change, deliberate.** This used to call
    `db.get_reports_for_agent`, which is the OPERATOR question — everything the
    agent ever published — so every rostered client of an agent saw every report
    it had produced, including ones produced for a different client, rendered
    from agent-authored JSON on a client-facing surface. The Workspace asks a
    different question, and ent#365 AC #4 states it: a user sees their own, not
    the fleet. Unaddressed reports (`addressed_to_email IS NULL`) are
    operator-only and no longer appear here at all — which is AC #1, and is why
    an install whose agents have not adopted `audience_email` yet will show an
    empty Reports tab until they do.

    The scope is the caller's identity for BOTH principal kinds. A platform user
    on this surface is using the client surface and sees what was addressed to
    them; everything else stays on the operator Reports tab, which is where the
    fleet view belongs.

    `portal_session_id` narrows further to one chat, which is what the inline
    deliverable cards read.
    """
    try:
        rows = db.get_reports_for_client(
            agent_name, client_email, portal_session_id=portal_session_id,
            limit=limit, offset=offset,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: reports read failed for %s: %s", agent_name, e)
        return []
    return [{
        "id": r.get("id"),
        "report_type": r.get("report_type"),
        "title": r.get("title"),
        "display_hint": r.get("display_hint"),
        "period_start": r.get("period_start"),
        "period_end": r.get("period_end"),
        "created_at": r.get("created_at"),
    } for r in rows]


def canvases(agent_name: str) -> list[dict]:
    """The agent's canvases a Workspace client may see (ent#438).

    Narrowed **in the query** to `audience='roster'`, not filtered afterwards:
    a read that loads every canvas and drops some in Python has already put an
    operator-only surface in this process's memory one edit away from the
    response, which is the ent#365 FR-2 lesson restated. A canvas is
    operator-only unless the agent explicitly published it, so the default is
    an empty Workspace tab rather than an accidental disclosure.

    Fail-soft, like every other block on this page: a read error costs the
    canvases, never the page.
    """
    try:
        rows = db.list_agent_canvases(agent_name, audience=CANVAS_AUDIENCE_ROSTER)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: canvas read failed for %s: %s", agent_name, e)
        return []
    return canvas_service.decorate(rows, agent_name)


def canvas_detail(agent_name: str, canvas_id: str) -> Optional[dict]:
    """One roster-visible canvas with its blocks, or None (ent#438).

    The audience narrowing is a REQUIRED argument on the accessor rather than a
    check here, for the ent#365 FR-2 reason: a gate applied after the fetch has
    already loaded what it was meant to withhold, and a default would make it
    fail open.
    """
    try:
        canvas_service.validate_canvas_id(canvas_id)
    except canvas_service.CanvasError:
        return None
    try:
        row = db.get_agent_canvas(
            agent_name, canvas_id, audience=CANVAS_AUDIENCE_ROSTER
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: canvas detail failed for %s: %s", agent_name, e)
        return None
    return canvas_service.decorate([row], agent_name)[0] if row else None


def _rating_tally(agent_name: str) -> dict:
    """Up/down counts of this agent's Workspace ratings (ent#366).

    Fleet-wide for the agent, not per-reader: "how did this land with people"
    is the question a tally answers, and a per-reader count of your own two
    clicks answers nothing. It is counts only — no comments and no evaluator
    identities cross to this surface, so nothing here says who rated what.

    Fail-soft to zeros with `unavailable`, so the page renders for an agent
    whose ratings cannot be read rather than failing whole; the flag keeps the
    UI from presenting an unread tally as a real zero.
    """
    from database import db as platform_db
    try:
        tally = platform_db.workspace_rating_tally(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: rating tally failed for %s: %s", agent_name, e)
        return {"up": 0, "down": 0, "total": 0, "unavailable": True}
    return {**tally, "unavailable": False}


def build_page(email: str, agent_name: str, card: Optional[dict],
               window: str = DEFAULT_WINDOW, *, is_platform: bool = False) -> dict:
    """Assemble the page. `card` is the caller's roster entry (identity + what
    it can do), already resolved and access-checked by the caller.

    On the "rating tally" named in AC #3: it was omitted at first because nothing
    in Trinity produced ratings, and a number a user reads as "how well is this
    agent doing" has to come from something real. ent#366 shipped that source —
    a Workspace thumb writes `agent_evaluations` under `evaluator =
    workspace:<email>` — so `_rating_tally` projects it and the AC is met. This
    docstring, the flow doc and the requirement all still claimed the opposite
    two releases later; corrected in #2423 review. (The first-try rate beside it
    is separate and also real: `retry_count`, see `portal_db.first_try_stats`.)
    """
    card = card or {}
    return {
        "agent_name": agent_name,
        "header": {
            "name": agent_name,
            "description": card.get("description"),
            "avatar_url": card.get("avatar_url"),
            "owner": card.get("owner"),
            "health": _health(agent_name),
            # #2196: a projection of the card the caller already resolved, NOT a
            # second Docker read — and `or "unknown"`, never a bare `.get`.
            # `card` is documented-reachable as None (the agent vanished between
            # the roster read and this one), which `card = card or {}` above
            # turns into an explicit None here; a Literal-with-default REJECTS
            # an explicit None, so the bare form would 500 the one page ent#360
            # built to always render.
            "availability": card.get("availability") or "unknown",
            "last_active": _last_active(agent_name, is_platform=is_platform),
        },
        # "What it can do" — a projection of the briefing the roster already
        # carries (#138 / ent#380), NOT a second mechanism. ent#178 is the
        # unified exposable-skills config this becomes a view of when it lands.
        "capabilities": card.get("playbooks") or [],
        "stats": _stats(agent_name, window, is_platform=is_platform),
        # ent#366 AC #4: a RAW TALLY, never a percentage. At the volumes this
        # page sees, one thumbs-down out of one rating renders as "100%
        # negative" — a number that looks like evidence and is not. Both
        # figures cross so the denominator, which is the honest part, is on
        # screen with them.
        "ratings": _rating_tally(agent_name),
        "recent_work": _recent_work(agent_name, is_platform=is_platform),
    }


def _last_active(agent_name: str, *, is_platform: bool = False) -> Optional[str]:
    """When this agent last did anything, from its newest execution row.

    Scoped to what the viewer can SEE (#2423 review). Reading the newest row
    unconditionally reports a loop run's timestamp to a client for whom that row
    does not exist — a header saying "active 2 minutes ago" above a list whose
    newest entry is from yesterday, with nothing on the page to reconcile the
    two. Same exclusion, same reason as `_recent_work`, and pushed into SQL for
    the same reason: `limit=1` in Python cannot survive any filtering at all.
    """
    try:
        rows = db.get_agent_executions_summary(
            agent_name, limit=1,
            exclude_triggers=None if is_platform else _CLIENT_HIDDEN_TRIGGERS)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: last-active read failed for %s: %s", agent_name, e)
        return None
    return rows[0].get("started_at") if rows else None


def _window_rows(payload, offset: int, limit: int):
    """Slice a tabular payload's `rows`, or leave it alone (#2162).

    Returns `(payload, row_meta)` — `row_meta` is None whenever no window was
    applied, which is how the caller (and, downstream, the renderer's footer)
    tells a windowed table from a whole document.

    **The server decides tabularity, from the real payload.** `display_hint` is
    agent-authored and can disagree with what was actually filed, so a client
    that predicted the shape would need a 400 and a recovery re-fetch for the
    disagreement. Answering "here it is whole, and no, that wasn't a table"
    removes that branch: one request, always. Every other display_hint is a
    bounded document (a KPI tile set, a markdown body) with no row axis to
    slice, so there is nothing to answer 400 about.

    Subtractive on `rows` only: sibling keys are returned exactly as filed, the
    same as the unwindowed path. The copy is shallow so the source row is never
    truncated in place.
    """
    if not isinstance(payload, dict):
        return payload, None
    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return payload, None

    # A negative offset would silently serve rows counted from the END under an
    # honest-looking total; an unbounded limit would defeat the point of the
    # window. The route validates both (422), but the clamp is what actually
    # bounds the response and must not depend on one caller doing so.
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), REPORT_ROWS_PAGE_MAX))

    windowed = dict(payload)
    windowed["rows"] = rows[offset:offset + limit]
    # `total` is the TRUE row count, not the window — it is what "Showing 100 of
    # 12,431" reads, and a windowed total would hide the rest behind a footer
    # that never appears.
    return windowed, {"total": len(rows), "offset": offset, "limit": limit}


def report_detail(agent_name: str, report_id: str, *,
                  client_email: str,
                  rows_offset: int = 0,
                  rows_limit: Optional[int] = None) -> Optional[dict]:
    """One report's full payload, scoped to the agent whose page is open.

    The agent check is the point: report ids are global, and without it any
    rostered agent's page would read any report in the install. A foreign id
    returns None → the router's 404, identical to a nonexistent one, so this
    cannot be used to test whether a report exists (invariant #8).

    `rows_limit` (#2162) windows a tabular payload's rows, so a large table is
    never shipped whole to a client — the #1537 pattern, reached here through
    two optional params rather than a second route. The operator row reader is
    `Depends(get_current_user)`, which a portal principal (a verified email with
    no `users` row) structurally cannot satisfy; cloning it on a client-facing
    prefix would mean a second hand-written gate beside the one above, and that
    is how a 404-uniformity contract drifts. Absent `rows_limit`, this returns
    byte-for-byte what it returned before the parameter existed.

    Honest limit, same as the operator route's: the slice happens in Python
    after the whole blob is read out of the column, so this bounds the RESPONSE,
    not the read — and therefore paging MULTIPLIES reads. That is why the route
    rate-limits (a client can loop it; an operator on a JWT is a different risk).
    """
    try:
        # ent#365: the audience gate first, then the agent gate. Same shape as
        # `_asks`' ent#428 fix on the sibling surface — a report id is global,
        # and a co-shared client reading another client's deliverable is the
        # same defect as reading their ask, over a bigger blob.
        row = db.get_report_for_client(report_id, client_email)
    except Exception as e:  # noqa: BLE001
        logger.warning("agent page: report read failed for %s: %s", report_id, e)
        return None
    if not row or row.get("agent_name") != agent_name:
        return None

    payload = row.get("payload")
    row_meta = None
    if rows_limit is not None:
        payload, row_meta = _window_rows(payload, rows_offset, rows_limit)

    detail = {
        "id": row.get("id"),
        "agent_name": row.get("agent_name"),
        "report_type": row.get("report_type"),
        "title": row.get("title"),
        "display_hint": row.get("display_hint"),
        "payload": payload,
        "period_start": row.get("period_start"),
        "period_end": row.get("period_end"),
        "created_at": row.get("created_at"),
    }
    # Present ONLY when a window was actually applied: the client keys "is this
    # paged?" off its presence, so an always-present key with null fields would
    # make every bounded document render a Load-more footer it can never satisfy.
    if row_meta is not None:
        detail["row_meta"] = row_meta
    return detail
