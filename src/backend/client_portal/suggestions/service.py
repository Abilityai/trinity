"""Workspace suggestions — business rules (trinity-enterprise#465).

Two halves, kept apart so the rules are testable without a database:

* `build(signals, now=..., can_configure=...)` — PURE. Signals in, ranked
  `(Suggestion, fingerprint)` pairs out. Every threshold and every word of copy
  lives here, against an injected `now` (UTC).
* `get_suggestions` / `record_feedback` — gather the signals (DB reads run in a
  thread, concurrently with the one bounded briefing fetch), apply the viewer's
  dismissals, and bound the write.

The fingerprint is the IDENTITY of the state an item reports — the first
failure of a streak, the set of held schedules — never a count, so a dismissed
item stays dismissed while the same state persists and comes back when a new
one starts (requirement §5.39).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from . import db as sdb
from .models import PortalSuggestions, Suggestion, SuggestionAction

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — one place, named in requirement §5.39.
# ---------------------------------------------------------------------------
MAX_SHOWN = 5
MAX_UNUSED_PLAYBOOKS = 3
FAILING_STREAK = 3
DORMANT_DAYS = 14
DISABLED_DAYS = 7
NEVER_FIRED_GRACE = timedelta(hours=1)
#: How far back runs and attributed invocations are read. Execution rows are
#: pruned by retention anyway; this bounds the scan on a busy agent.
LOOKBACK_DAYS = 90
#: Runs read per schedule for the failure streak.
RUNS_PER_SCHEDULE = 10
#: Agent-authored schedule names are capped before they cross (§5.11).
MAX_NAME_CHARS = 80
#: The briefing (a live call into the agent) is cached per agent this long.
BRIEFING_TTL_SECONDS = 60.0
#: Agents held in that cache per process; the fleet size bounds it in practice.
BRIEFING_CACHE_MAX = 500

_FAILED = frozenset({"failed", "error"})
#: Neither count toward a streak nor break it: a skipped tick (lock held, git
#: freeze) or a cancel says nothing about whether the schedule works, and an
#: in-flight run has no verdict yet.
_NEUTRAL = frozenset({"skipped", "cancelled", "running", "queued", "pending_retry"})

#: A playbook starter is exactly `/<name> ` (`service._playbook_starter`);
#: anything else in the briefing is template use-case text, which is not offered
#: (whether someone has "used" a free-text example is unverifiable).
_STARTER = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9._:-]{0,99}) $")
_SLASH = re.compile(r"^\s*/([A-Za-z0-9][A-Za-z0-9._:-]{0,99})(?:\s|$)")

#: Display order — what is waiting on the viewer first, then what is broken,
#: then what could be better.
_ORDER = (
    "asks", "decisions_due", "schedule_failing", "autonomy_held",
    "schedule_never_fired", "schedule_disabled", "dormant", "unused_playbook",
)
#: The feedback write accepts only keys of these classes (bounded, and never a
#: path segment).
KEY_CLASSES = frozenset(_ORDER)


class SuggestionError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Playbook:
    name: str
    title: str
    description: Optional[str] = None


@dataclass
class Signals:
    """Everything `build` reads. Defaults describe an agent with nothing to say."""
    ask_ids: Sequence[str] = ()
    decisions_due_ids: Sequence[str] = ()
    autonomy_enabled: bool = True
    schedules: Sequence[dict] = ()
    runs: Dict[str, List[dict]] = field(default_factory=dict)
    overdue_reminders: int = 0
    last_user_message_at: Optional[str] = None
    #: None = the agent did not answer (capabilities unknown), [] = none exposed.
    playbooks: Optional[Sequence[Playbook]] = None
    used_playbooks: frozenset = frozenset()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(dt: datetime) -> str:
    """`Sep 3` — a date the viewer can check against the schedule list."""
    return f"{dt:%b} {dt.day}"


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _hash(values) -> str:
    joined = "\n".join(sorted(str(v) for v in values))
    return hashlib.sha256(joined.encode()).hexdigest()[:16]


def _cap(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= MAX_NAME_CHARS else text[: MAX_NAME_CHARS - 1] + "…"


def slash_name(text: Optional[str]) -> Optional[str]:
    """The playbook a message starts with (`/weekly-report now` → `weekly-report`)."""
    m = _SLASH.match(text or "")
    return m.group(1).lower() if m else None


def playbooks_from_briefing(items) -> List[Playbook]:
    """Briefing hint items → playbooks. Use-case hints are dropped (see `_STARTER`)."""
    out: List[Playbook] = []
    for p in items or ():
        starter = getattr(p, "starter_prompt", None) if not isinstance(p, dict) else p.get("starter_prompt")
        m = _STARTER.match(starter or "")
        if not m:
            continue
        title = (getattr(p, "title", None) if not isinstance(p, dict) else p.get("title")) or m.group(1)
        desc = getattr(p, "description", None) if not isinstance(p, dict) else p.get("description")
        out.append(Playbook(name=m.group(1), title=title, description=desc))
    return out


def failure_streak(runs: Sequence[dict]) -> Tuple[int, Optional[dict], Optional[dict]]:
    """`(count, newest_failure, first_failure)` over runs newest-first.

    Failures count, `_NEUTRAL` rows are stepped over, anything else (a success)
    ends the streak.
    """
    count, newest, first = 0, None, None
    for r in runs:
        status = (r.get("status") or "").lower()
        if status in _NEUTRAL:
            continue
        if status in _FAILED:
            count += 1
            newest = newest or r
            first = r
            continue
        break
    return count, newest, first


def first_expected_fire(cron: str, tz_name: Optional[str], created: datetime) -> Optional[datetime]:
    """The cron's first fire after `created`, in UTC; None when it can't be computed."""
    try:
        from croniter import croniter
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name or "UTC")
        nxt = croniter(cron, created.astimezone(tz)).get_next(datetime)
    except Exception:  # noqa: BLE001 — an unparseable cron is not a suggestion
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(timezone.utc)


def _link(agent_name: str) -> SuggestionAction:
    return SuggestionAction(type="link", value=f"/agents/{quote(agent_name, safe='')}?tab=schedules")


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def build(signals: Signals, *, agent_name: str, now: datetime,
          can_configure: bool) -> List[Tuple[Suggestion, str]]:
    """Every eligible suggestion for this viewer, ranked, with its fingerprint.

    Not truncated and not filtered by dismissals — the caller does both, so the
    feedback path can check a key against the full eligible set.
    """
    items: Dict[str, List[Tuple[Suggestion, str]]] = {k: [] for k in _ORDER}

    def add(cls: str, s: Suggestion, fp: str) -> None:
        items[cls].append((s, fp))

    # -- waiting on the viewer ------------------------------------------------
    if signals.ask_ids:
        n = len(signals.ask_ids)
        add("asks", Suggestion(
            key="asks", kind="invoke", source="asks", door="platform",
            title="Answer what this agent asked you",
            signal=f"{_plural(n, 'question')} waiting on you",
            action=SuggestionAction(type="open_section", value="asks"),
        ), _hash(signals.ask_ids))

    if signals.decisions_due_ids:
        n = len(signals.decisions_due_ids)
        add("decisions_due", Suggestion(
            key="decisions_due", kind="invoke", source="decisions", door="platform",
            title="Review decisions past their date",
            signal=f"{_plural(n, 'decision')} past the review date",
            action=SuggestionAction(type="open_section", value="decisions"),
        ), _hash(signals.decisions_due_ids))

    # -- schedule health (owner / admin only) ---------------------------------
    if can_configure:
        live = list(signals.schedules)
        enabled = [s for s in live if s.get("enabled")]
        failing_ids = set()
        for s in enabled:
            count, newest, first = failure_streak(signals.runs.get(s["id"], ()))
            if count >= FAILING_STREAK:
                failing_ids.add(s["id"])
                when = _parse(newest.get("started_at"))
                add("schedule_failing", Suggestion(
                    key=f"schedule_failing:{s['id']}", kind="configure", source="schedules",
                    door="owner_or_admin",
                    title=f"Check “{_cap(s.get('name') or 'schedule')}”",
                    signal=f"Failed {count} runs in a row" + (f" · last {_day(when)}" if when else ""),
                    action=_link(agent_name),
                ), str(first.get("id")))

        if not signals.autonomy_enabled and (enabled or signals.overdue_reminders):
            parts = []
            if enabled:
                parts.append(f"{_plural(len(enabled), 'schedule')} won't run")
            if signals.overdue_reminders:
                parts.append(f"{_plural(signals.overdue_reminders, 'reminder')} held")
            last = max((d for d in (_parse(s.get("last_run_at")) for s in live) if d), default=None)
            add("autonomy_held", Suggestion(
                key="autonomy_held", kind="configure", source="schedules", door="owner_or_admin",
                title="Turn on autonomy, or pause these schedules",
                signal=" and ".join(parts) + " — autonomy is off"
                + (f" · last run {_day(last)}" if last else ""),
                action=_link(agent_name),
            ), _hash([s["id"] for s in enabled] + [f"reminders:{bool(signals.overdue_reminders)}"]))

        if signals.autonomy_enabled:
            for s in enabled:
                if s.get("last_run_at") or s["id"] in failing_ids:
                    continue
                created = _parse(s.get("created_at"))
                if not created:
                    continue
                due = first_expected_fire(s.get("cron_expression") or "", s.get("timezone"), created)
                if due is None or now - due < NEVER_FIRED_GRACE:
                    continue
                add("schedule_never_fired", Suggestion(
                    key=f"schedule_never_fired:{s['id']}", kind="configure", source="schedules",
                    door="owner_or_admin",
                    title=f"Check “{_cap(s.get('name') or 'schedule')}”",
                    signal=f"Enabled since {_day(created)} · has never run",
                    action=_link(agent_name),
                ), s["id"])

        for s in live:
            if s.get("enabled") or not s.get("last_run_at"):
                continue
            since = _parse(s.get("updated_at"))
            if not since or now - since < timedelta(days=DISABLED_DAYS):
                continue
            add("schedule_disabled", Suggestion(
                key=f"schedule_disabled:{s['id']}", kind="configure", source="schedules",
                door="owner_or_admin",
                title=f"Re-enable or delete “{_cap(s.get('name') or 'schedule')}”",
                signal=f"Disabled since {_day(since)} · it used to run",
                action=_link(agent_name),
            ), str(s.get("updated_at")))

    # -- the viewer's own use -------------------------------------------------
    last = _parse(signals.last_user_message_at)
    if last and now - last >= timedelta(days=DORMANT_DAYS):
        add("dormant", Suggestion(
            key="dormant", kind="invoke", source="usage", door="platform",
            title="Pick up where you left off",
            signal=f"Your last conversation was {_day(last)}",
            action=SuggestionAction(type="open_chat"),
        ), str(signals.last_user_message_at))

    scheduled = {slash_name(s.get("message")) for s in signals.schedules if s.get("enabled")}
    used = set(signals.used_playbooks) | {n for n in scheduled if n}
    for p in (signals.playbooks or ())[:]:
        if len(items["unused_playbook"]) >= MAX_UNUSED_PLAYBOOKS:
            break
        if p.name.lower() in used:
            continue
        add("unused_playbook", Suggestion(
            key=f"unused_playbook:{p.name}", kind="invoke", source="capability", door="platform",
            title=p.title, signal=f"You haven't run /{p.name} yet", description=p.description,
            action=SuggestionAction(type="prefill", value=f"/{p.name} "),
        ), p.name)

    return [pair for cls in _ORDER for pair in items[cls]]


def shape(pairs: List[Tuple[Suggestion, str]], dismissed: Dict[str, str], *,
          agent_name: str, playbooks, has_history: bool) -> PortalSuggestions:
    """Apply dismissals and the display cap; state what is and isn't known."""
    visible = [s for s, fp in pairs if dismissed.get(s.key) != fp]
    if playbooks is None:
        capabilities = "unavailable"
    else:
        capabilities = "available" if playbooks else "none"
    return PortalSuggestions(
        agent_name=agent_name,
        suggestions=visible[:MAX_SHOWN],
        total=len(visible),
        capabilities=capabilities,
        basis="history" if has_history else "capabilities_only",
    )


# ---------------------------------------------------------------------------
# Signal gathering
# ---------------------------------------------------------------------------

_briefing_cache: Dict[str, Tuple[float, Optional[List[Playbook]]]] = {}


async def _playbooks(agent_name: str) -> Optional[List[Playbook]]:
    """The agent's exposed playbooks via the ONE briefing path (ent#380), or None
    when the agent did not answer. Availability is resolved first so a stopped
    agent never pays a connect failure or the 3 s budget (#2196)."""
    hit = _briefing_cache.get(agent_name)
    if hit and time.monotonic() - hit[0] < BRIEFING_TTL_SECONDS:
        return hit[1]
    from client_portal import service as portal

    availability = (await portal._availability_map([agent_name])).get(agent_name, "unknown")
    briefing, ok = await portal._bounded_briefing(agent_name, availability)
    result = playbooks_from_briefing(briefing.playbooks) if ok else None
    if len(_briefing_cache) >= BRIEFING_CACHE_MAX:
        # Bounded: drop entries past their TTL first, then the oldest.
        cutoff = time.monotonic() - BRIEFING_TTL_SECONDS
        for k in [k for k, (t, _) in _briefing_cache.items() if t < cutoff] or [min(_briefing_cache, key=lambda k: _briefing_cache[k][0])]:
            _briefing_cache.pop(k, None)
    _briefing_cache[agent_name] = (time.monotonic(), result)
    return result


def _gather(agent_name: str, email: str, now: datetime, can_configure: bool) -> dict:
    """The DB half of the signals. Each read degrades on its own — a failing
    source costs its items, never the list."""
    from database import db
    from services.seat_decision_service import effective_status

    from client_portal.asks.service import list_asks

    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    out: dict = {}

    def attempt(name, fn, default):
        try:
            out[name] = fn()
        except Exception:  # noqa: BLE001
            logger.warning("[ent#465] suggestions: %s read failed for %s", name, agent_name, exc_info=True)
            out[name] = default

    attempt("ask_ids", lambda: [a.id for a in list_asks(email, True, agent_name) if a.status == "pending"], [])
    attempt("decisions_due_ids", lambda: [
        r["id"] for r in db.list_seat_decisions(agent_name, email, limit=500)
        if effective_status(r, today=now.date()) == "expired"
    ], [])
    attempt("schedules", lambda: sdb.list_schedules(agent_name), [])
    attempt("last_user_message_at", lambda: sdb.last_user_message_at(agent_name, email), None)
    attempt("used_playbooks", lambda: frozenset(
        n for n in (slash_name(t) for t in sdb.slash_texts_by_viewer(agent_name, email, since)) if n
    ), frozenset())
    attempt("dismissed", lambda: sdb.dismissed_fingerprints(agent_name, email), {})
    attempt("has_runs", lambda: sdb.viewer_has_runs(agent_name, email, since), False)
    if can_configure:
        attempt("autonomy_enabled", lambda: bool(db.get_autonomy_enabled(agent_name)), True)
        attempt("runs", lambda: sdb.recent_runs_by_schedule(
            agent_name, since, RUNS_PER_SCHEDULE,
            [x["id"] for x in out.get("schedules", []) if x.get("enabled")],
        ), {})
        attempt("overdue_reminders", lambda: sdb.count_overdue_reminders(agent_name, now_iso), 0)
    return out


def can_configure(email: str, agent_name: str, is_admin: bool) -> bool:
    from client_portal import service as portal

    return bool(is_admin) or portal.portal_owns_agent(email, agent_name, True)


async def _compute(agent_name: str, email: str, is_admin: bool, now: Optional[datetime]):
    now = now or datetime.now(timezone.utc)
    configure = can_configure(email, agent_name, is_admin)
    gathered, playbooks = await asyncio.gather(
        asyncio.to_thread(_gather, agent_name, email, now, configure),
        _playbooks(agent_name),
    )
    dismissed = gathered.pop("dismissed", {})
    has_runs = gathered.pop("has_runs", False)
    signals = Signals(playbooks=playbooks, **gathered)
    pairs = build(signals, agent_name=agent_name, now=now, can_configure=configure)
    # History = the viewer's own Workspace messages OR runs attributed to them
    # (operator chat, MCP) — an owner who only uses the console has history.
    has_history = bool(signals.last_user_message_at) or bool(has_runs)
    return pairs, dismissed, signals, has_history


async def get_suggestions(agent_name: str, email: str, *, is_admin: bool,
                          now: Optional[datetime] = None) -> PortalSuggestions:
    pairs, dismissed, signals, has_history = await _compute(agent_name, email, is_admin, now)
    return shape(pairs, dismissed, agent_name=agent_name, playbooks=signals.playbooks,
                 has_history=has_history)


async def record_feedback(agent_name: str, email: str, *, is_admin: bool, key: str,
                          action: str, now: Optional[datetime] = None) -> None:
    """Record an accept or dismiss for a suggestion CURRENTLY emitted for this
    viewer. Anything else is a 404 with nothing written — the write is bounded
    to real items and the fingerprint is the server's, never the client's."""
    if key.split(":", 1)[0] not in KEY_CLASSES:
        raise SuggestionError(422, "unknown suggestion key")
    pairs, _, _, _ = await _compute(agent_name, email, is_admin, now)
    match = next(((s, fp) for s, fp in pairs if s.key == key), None)
    if match is None:
        raise SuggestionError(404, "Suggestion not found")
    s, fp = match
    from utils.helpers import utc_now_iso

    await asyncio.to_thread(
        sdb.record_feedback, email=email, agent_name=agent_name, key=key,
        source=s.source, action=action, fingerprint=fp, now=utc_now_iso(),
    )
