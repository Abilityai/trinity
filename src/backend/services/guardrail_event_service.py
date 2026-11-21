"""Guardrail event surface — ingest, store and read (trinity-enterprise#165, GUARD-005).

The agent's guardrail hooks append records to a spool in the container; a
forwarder in the agent server pushes them here under the agent's OWN key
(`POST /api/agents/{name}/guardrail-events`). This module owns everything
between the HTTP glue and the SQL (Invariant #1): per-event validation, the
reporter liveness state, the per-agent read, and the counting seams other
features consume (`count_by_execution` for trinity#4281, `count_by_agent` for
the fleet view and trinity-enterprise#141), and the fleet read built on them
(`fleet_stats`, `GET /api/ops/guardrail-stats`, slice S4).

Contract rules that are load-bearing (plan §6):

* **Each event is validated on its own; one bad event never fails the batch.**
  Agent images and the backend upgrade independently. An unknown `kind` or
  `hook` is stored as `other` with the raw value in `details`; `code` is an
  open string; an event that fails a shape check is counted in `rejected`.
* **`execution_id` is attribution, not authority.** A model can start a nested
  CLI with a forged value, so every read filters by the authenticated agent and
  an id longer than turn-tier's `MAX_ID_LEN` is nulled.
* **Only allowlisted fields are stored**, each bounded, and the free-text ones
  (`rule`, `path`) pass through the credential sanitizer as defence in depth.
* **Reporting state** tells "no events" from "not reporting": `never`,
  `reporting` (a report inside `STALE_AFTER_SECONDS`), `stale`. Its
  `first_report_at` is the coverage start: history before it was never
  reported, so a consumer must not read it as clean.
* **`occurred_at` is never later than `received_at`.** An agent's clock can run
  ahead; a future `ts` is clamped to the receive time at ingest (the reported
  value kept in `details.occurred_at_reported`), so a skewed event is neither
  lost nor counted in every rolling window until it is pruned.

Events are agent-reported: an agent acting deliberately can suppress or forge
its own. This is observability, not a tamper-proof audit trail.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from database import db
from services import rate_limiter
from utils.credential_sanitizer import sanitize_text
from utils.helpers import iso_cutoff, parse_iso_timestamp, to_utc_iso, utc_now

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The contract's closed sets (plan §6.1). The hook library in the agent image
# owns the same two sets; `test_ent165_guardrail_event_contract.py` compares them.
# ---------------------------------------------------------------------------
KINDS = (
    "deny",
    "credential_redacted",
    "credential_unredacted",
    "redaction_writeback_refused",
    "scanner_error",
    "hook_error",
    "config_error",
)
HOOKS = (
    "bash_guardrail",
    "file_guardrail",
    "read_only_guard",
    "turn_tier_guard",
    "skill_gate",
    "output_scanner",
)
#: What an unknown `kind` / `hook` / `runtime` is stored and reported as.
OTHER = "other"
RUNTIMES = ("claude-code", "codex", "gemini")

# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------
MAX_EVENTS_PER_BATCH = 500
MAX_BODY_BYTES = 256 * 1024
#: turn-tier-guard.py's MAX_ID_LEN: an execution id longer than this is nulled.
MAX_EXECUTION_ID_LEN = 128
#: Per-agent request cap. The forwarder flushes at most every ~2 s plus a
#: keepalive every 5 min, so 60 a minute leaves room for 413 splits and retries.
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60
#: Three missed 5-minute keepalives.
STALE_AFTER_SECONDS = 15 * 60
READ_LIMIT_DEFAULT = 50
READ_LIMIT_MAX = 200
#: The fleet read's window: a day by default, at most the 90-day retention
#: default (ruling D10) — a longer window would count rows already pruned.
STATS_HOURS_DEFAULT = 24
STATS_HOURS_MAX = 90 * 24
#: The runtimes Trinity registers guardrail hooks on (plan §8). Every other
#: runtime has no hook, so no event can come from it: `coverage = no_hooks`.
#: The same names as `agent_service.helpers.CLAUDE_RUNTIME_NAMES` (a test
#: compares them); not imported, because that package's import is heavy.
HOOKED_RUNTIMES = frozenset({"claude-code", "claude"})
#: `coverage`, one per agent in a fleet read, worst news first in the UI.
COVERAGE_STATES = ("no_hooks", "never", "stale", "reporting")

_EVENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_TOKEN = re.compile(r"^[a-z0-9_]{1,64}$")          # kind, hook, code
_PATTERN_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MAX_PATTERNS = 32
_SHORT_FIELDS = ("verb", "tier", "turn_kind")      # <= 64 chars each
_ID_FIELDS = ("session_id", "tool_use_id")         # <= MAX_EXECUTION_ID_LEN
_BOUNDED_TEXT = {"rule": 200, "path": 512}
_MAX_TOOL_LEN = 128
# A NUL or a lone surrogate (a non-UTF-8 byte the hook decoded with
# surrogateescape, or a model-written "\\udcff" escape in a tool input) is a
# character no store can hold: psycopg2 refuses a NUL before PostgreSQL sees
# it, SQLite cannot encode a surrogate, and a surrogate that does reach a row
# (inside `details`, JSON-escaped) 500s every later GET when the response is
# encoded. One such event must never cost the batch or poison the read.
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")

# Liveness of the agent's forwarder. A HASH {first_report_at, last_report_at,
# runtime, dropped_total} with no TTL: `stale` needs the last value to outlive
# the silence. `first_report_at` is written once (HSETNX), so a later report
# never moves the coverage start. Cleared on the agent's teardown
# (`agent_runtime_state`).
REPORTER_KEY_PREFIX = "agent:guardrail_reporter:"


class GuardrailEventQueryError(ValueError):
    """A read parameter that cannot be honoured; the router maps it to 422."""


def _reporter_key(agent_name: str) -> str:
    return f"{REPORTER_KEY_PREFIX}{agent_name}"


def _get_redis():
    """The shared Redis client, or None when it is unavailable.

    Lazy import, as in heartbeat_service: keeps this module importable in unit
    tests and avoids a cycle with routers/auth.py.
    """
    try:
        from routers.auth import get_redis_client
        return get_redis_client()
    except Exception:  # noqa: BLE001 — Redis-None degrades to the DB fallback
        logger.warning("guardrail events: Redis client unavailable", exc_info=True)
        return None


def _iso(dt: datetime) -> str:
    return to_utc_iso(dt)


# ---------------------------------------------------------------------------
# Normalization (one event at a time)
# ---------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[datetime]:
    """The wire `ts` (epoch number or ISO string) as an aware datetime, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and 0 < len(value) <= 64:
        try:
            return parse_iso_timestamp(value)
        except ValueError:
            return None
    return None


def _text(value: str) -> str:
    """Display text with every unstorable character replaced by U+FFFD."""
    return _UNSTORABLE.sub("\ufffd", value)


def _bounded_str(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return _text(value)[:limit]


def normalize_event(raw: Any, received_at: str) -> Optional[Dict]:
    """One wire event -> a row dict, or None when it fails a shape check.

    Never raises on input: the caller counts None as `rejected` and carries on.
    """
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
        return None
    occurred = _parse_ts(raw.get("ts"))
    if occurred is None:
        return None
    kind, hook = raw.get("kind"), raw.get("hook")
    if not (isinstance(kind, str) and _TOKEN.match(kind)):
        return None
    if not (isinstance(hook, str) and _TOKEN.match(hook)):
        return None
    code = raw.get("code")
    if code is not None and not (isinstance(code, str) and _TOKEN.match(code)):
        return None

    details: Dict[str, Any] = {}
    # A future `ts` (the agent's clock runs ahead) is dated at the receive time:
    # kept, never rejected, but out of every window it does not belong to.
    if occurred > parse_iso_timestamp(received_at):
        details["occurred_at_reported"] = _iso(occurred)
        occurred_at = received_at
    else:
        occurred_at = _iso(occurred)
    if kind not in KINDS:
        details["kind_raw"] = kind
        kind = OTHER
    if hook not in HOOKS:
        details["hook_raw"] = hook
        hook = OTHER

    patterns = raw.get("patterns")
    if isinstance(patterns, list):
        names = [p for p in patterns if isinstance(p, str) and _PATTERN_NAME.match(p)]
        if names:
            details["patterns"] = names[:_MAX_PATTERNS]
    for field in _SHORT_FIELDS:
        value = _bounded_str(raw.get(field), 64)
        if value is not None:
            details[field] = sanitize_text(value)
    for field in _ID_FIELDS:
        value = raw.get(field)
        if (isinstance(value, str) and 0 < len(value) <= MAX_EXECUTION_ID_LEN
                and not _UNSTORABLE.search(value)):
            details[field] = value
    for field, limit in _BOUNDED_TEXT.items():
        value = raw.get(field)
        if isinstance(value, str) and value:
            # Sanitize the whole value first, so a secret straddling the cut is
            # still recognised, then bound it.
            details[field] = sanitize_text(_text(value))[:limit]

    execution_id = raw.get("execution_id")
    # An id is matched, never displayed: one that holds an unstorable character
    # is nulled like an over-long one, not rewritten into a different id.
    if not (isinstance(execution_id, str) and 0 < len(execution_id) <= MAX_EXECUTION_ID_LEN
            and not _UNSTORABLE.search(execution_id)):
        execution_id = None

    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "received_at": received_at,
        "kind": kind,
        "hook": hook,
        "tool": _bounded_str(raw.get("tool"), _MAX_TOOL_LEN),
        "code": code,
        "execution_id": execution_id,
        "details": json.dumps(details, sort_keys=True) if details else None,
    }


def load_details(raw: Optional[str]) -> Dict[str, Any]:
    """The stored `details` JSON as a dict (`{}` when there is none)."""
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("guardrail event details is not a JSON object")
    return value


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def check_rate(agent_name: str) -> rate_limiter.RateLimitResult:
    """Record one POST against the agent's request cap."""
    return rate_limiter.check(
        f"guardrail_events:{agent_name}", RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS)


def normalize_runtime(runtime: Optional[str]) -> Optional[str]:
    if runtime is None:
        return None
    return runtime if runtime in RUNTIMES else OTHER


def ingest(agent_name: str, events: List[Any], *, runtime: Optional[str], dropped: int) -> Dict[str, int]:
    """Validate and store one batch for `agent_name` (the AUTH-resolved name).

    Returns `{"accepted", "duplicates", "rejected"}`. A storage failure raises:
    the forwarder treats a 5xx as retryable and keeps the records.
    """
    received_at = utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows: List[Dict] = []
    rejected = 0
    for raw in events:
        row = normalize_event(raw, received_at)
        if row is None:
            rejected += 1
        else:
            rows.append(row)
    accepted, duplicates = db.insert_guardrail_events(agent_name, rows)
    record_report(agent_name, normalize_runtime(runtime), dropped, received_at)
    if rejected:
        logger.info("guardrail events: %s rejected %d malformed event(s)", agent_name, rejected)
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


# ---------------------------------------------------------------------------
# Reporter liveness
# ---------------------------------------------------------------------------

def record_report(agent_name: str, runtime: Optional[str], dropped: int, at: str) -> bool:
    """Mark the agent as reporting. Best-effort: False when Redis is down (the
    read then falls back to the newest stored row)."""
    redis = _get_redis()
    if redis is None:
        return False
    try:
        key = _reporter_key(agent_name)
        mapping = {"last_report_at": at}
        if runtime is not None:
            mapping["runtime"] = runtime
        pipe = redis.pipeline()
        pipe.hset(key, mapping=mapping)
        # First write wins: the coverage start never moves forward on a report.
        pipe.hsetnx(key, "first_report_at", at)
        pipe.hincrby(key, "dropped_total", max(int(dropped), 0))
        pipe.execute()
        return True
    except Exception:  # noqa: BLE001 — liveness is additive; never fail the ingest
        logger.warning("guardrail events: could not record report for %s", agent_name, exc_info=True)
        return False


def clear_reporter(agent_name: str) -> None:
    """Forget the agent's reporter state (delete / rename / purge). Best-effort."""
    redis = _get_redis()
    if redis is None:
        return
    try:
        redis.delete(_reporter_key(agent_name))
    except Exception:  # noqa: BLE001 — best-effort, never raise into the lifecycle op
        logger.warning("guardrail events: could not clear reporter for %s", agent_name, exc_info=True)


def _state_for(last_report_at: str, now: datetime) -> str:
    age = now - parse_iso_timestamp(last_report_at)
    return "reporting" if age <= timedelta(seconds=STALE_AFTER_SECONDS) else "stale"


def reporting_state(agent_name: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """`{state, last_report_at, first_report_at, runtime, dropped_total}` for
    one agent.

    Redis holds the forwarder's last report. When it has no record (Redis down,
    flushed or restored) the newest stored row stands in for it, because an
    agent with rows HAS reported and reading `never` would be a lie.

    `first_report_at` is the coverage start: Redis's first-write-wins value,
    else the EARLIEST stored row's receive time, else the last report. Every
    fallback is at or after the true first report, so a lost Redis can only
    move coverage later (more history reads "before coverage"), never earlier.
    Never later than `last_report_at`; null exactly when `state` is `never`.
    """
    now = now or utc_now()
    record: Dict[str, str] = {}
    redis = _get_redis()
    if redis is not None:
        try:
            record = redis.hgetall(_reporter_key(agent_name)) or {}
        except Exception:  # noqa: BLE001 — degrade to the DB fallback below
            logger.warning("guardrail events: reporter read failed for %s", agent_name, exc_info=True)
            record = {}
    last = record.get("last_report_at")
    if not last:
        last = db.latest_guardrail_event_received_at(agent_name)
    earliest = None
    if last and not record.get("first_report_at"):
        earliest = db.earliest_guardrail_event_received_at(agent_name)
    return _reporting_from(record, last, earliest, now)


def _reporting_from(record: Dict[str, str], last: Optional[str], earliest: Optional[str],
                    now: datetime) -> Dict[str, Any]:
    """The reporting dict from a reporter record, the report time to use (the
    record's own, else the newest stored row's) and the oldest stored row's
    receive time (the coverage-start fallback). One builder for the single and
    the batch read, so the two cannot disagree."""
    if not last:
        return {"state": "never", "last_report_at": None, "first_report_at": None,
                "runtime": None, "dropped_total": 0}
    first = record.get("first_report_at") or earliest or last
    # Two reports racing can write the start after a later-dated last report;
    # the agent really did report at `last`, so the earlier one is still safe.
    first = min(first, last)
    return {
        "state": _state_for(last, now),
        "last_report_at": last,
        "first_report_at": first,
        "runtime": record.get("runtime"),
        "dropped_total": int(record.get("dropped_total") or 0),
    }


def reporting_states(agent_names: Iterable[str], now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """`reporting_state` for many agents: one Redis pipeline and one grouped DB
    read for the agents Redis has no record of, instead of two round trips per
    agent. Same answer as calling `reporting_state` on each name."""
    names = list(dict.fromkeys(agent_names))
    if not names:
        return {}
    now = now or utc_now()
    records: Dict[str, Dict[str, str]] = {}
    redis = _get_redis()
    if redis is not None:
        try:
            pipe = redis.pipeline()
            for name in names:
                pipe.hgetall(_reporter_key(name))
            records = {name: rec or {} for name, rec in zip(names, pipe.execute())}
        except Exception:  # noqa: BLE001 — degrade to the DB fallback below
            logger.warning("guardrail events: batch reporter read failed", exc_info=True)
            records = {}
    # The agents whose record lacks the last report or the coverage start: one
    # grouped read gives both fallbacks (newest row, oldest row).
    missing = [n for n in names
               if not (records.get(n, {}).get("last_report_at")
                       and records.get(n, {}).get("first_report_at"))]
    bounds = db.guardrail_event_received_bounds_by_agent(missing) if missing else {}
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        record = records.get(name, {})
        earliest, newest = bounds.get(name, (None, None))
        out[name] = _reporting_from(record, record.get("last_report_at") or newest, earliest, now)
    return out


# ---------------------------------------------------------------------------
# Per-agent read
# ---------------------------------------------------------------------------

def _encode_cursor(occurred_at: str, row_id: int) -> str:
    raw = json.dumps([occurred_at, row_id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        occurred_at, row_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise GuardrailEventQueryError("invalid_cursor") from exc
    if not isinstance(occurred_at, str) or not isinstance(row_id, int) or isinstance(row_id, bool):
        raise GuardrailEventQueryError("invalid_cursor")
    return occurred_at, row_id


def _query_ts(value: Optional[str], name: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return _iso(parse_iso_timestamp(value))
    except ValueError as exc:
        raise GuardrailEventQueryError(f"invalid_{name}") from exc


def _row_out(row: Dict) -> Dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "received_at": row["received_at"],
        "kind": row["kind"],
        "hook": row["hook"],
        "tool": row["tool"],
        "code": row["code"],
        "execution_id": row["execution_id"],
        "details": load_details(row["details"]),
    }


def list_events(
    agent_name: str,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    kind: Optional[str] = None,
    execution_id: Optional[str] = None,
    limit: int = READ_LIMIT_DEFAULT,
    before: Optional[str] = None,
) -> Dict[str, Any]:
    """`{events, next_cursor, reporting}` — newest first, one page."""
    if not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT_MAX:
        raise GuardrailEventQueryError("invalid_limit")
    if kind is not None and kind not in KINDS and kind != OTHER:
        raise GuardrailEventQueryError("invalid_kind")
    if execution_id is not None and not 0 < len(execution_id) <= MAX_EXECUTION_ID_LEN:
        raise GuardrailEventQueryError("invalid_execution_id")
    cursor = _decode_cursor(before) if before is not None else None
    rows = db.list_guardrail_events(
        agent_name,
        since=_query_ts(since, "since"),
        until=_query_ts(until, "until"),
        kind=kind,
        execution_id=execution_id,
        limit=limit + 1,
        before=cursor,
    )
    page, more = rows[:limit], len(rows) > limit
    next_cursor = _encode_cursor(page[-1]["occurred_at"], page[-1]["id"]) if more else None
    return {
        "events": [_row_out(r) for r in page],
        "next_cursor": next_cursor,
        "reporting": reporting_state(agent_name),
    }


# ---------------------------------------------------------------------------
# Counting seams
# ---------------------------------------------------------------------------

def count_by_execution(agent_name: str, execution_ids: Iterable[str]) -> Dict[str, Any]:
    """Per-execution counts for one agent, with its reporting state.

    `{"reporting": {...}, "executions": {<id>: {"by_kind": {kind: n},
    "by_hook_code": {"<hook>:<code>": n}}}}`. Executions with no events are
    absent; events without a `code` count in `by_kind` only. Filtered by the
    authenticated agent, so a forged id on another agent's event never counts.
    Evaluate at read time: events can land seconds after the hook fired.
    """
    # Coverage BEFORE counts: a batch committing between the two reads is then
    # counted rather than establishing coverage over counts that missed it.
    reporting = reporting_state(agent_name)
    executions: Dict[str, Dict[str, Dict[str, int]]] = {}
    for g in db.count_guardrail_events_by_execution(agent_name, list(execution_ids)):
        entry = executions.setdefault(g["execution_id"], {"by_kind": {}, "by_hook_code": {}})
        entry["by_kind"][g["kind"]] = entry["by_kind"].get(g["kind"], 0) + g["n"]
        if g["code"]:
            hc = f"{g['hook']}:{g['code']}"
            entry["by_hook_code"][hc] = entry["by_hook_code"].get(hc, 0) + g["n"]
    return {"reporting": reporting, "executions": executions}


def count_by_agent(hours: int, agent_names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """`{"agents": {<agent>: {"by_kind": {kind: n}}}}` over the last `hours`.

    `agent_names` scopes the answer (the caller passes the agents its principal
    may see; an empty list answers nothing). Agents with no events are absent.
    """
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        raise GuardrailEventQueryError("invalid_hours")
    agents: Dict[str, Dict[str, Dict[str, int]]] = {}
    groups = db.count_guardrail_events_by_agent(
        iso_cutoff(hours), None if agent_names is None else list(agent_names))
    for g in groups:
        by_kind = agents.setdefault(g["agent_name"], {"by_kind": {}})["by_kind"]
        by_kind[g["kind"]] = by_kind.get(g["kind"], 0) + g["n"]
    return {"agents": agents}


# ---------------------------------------------------------------------------
# Fleet read (slice S4)
# ---------------------------------------------------------------------------

def _container_runtimes() -> Optional[Dict[str, str]]:
    """`{agent: runtime}` from the containers' runtime label in one Docker round
    trip, or None when Docker cannot be asked. Lazy import: keeps this module
    importable without the Docker SDK."""
    from services.docker_service import agent_container_runtimes
    return agent_container_runtimes()


def coverage_of(runtime: Optional[str], state: str) -> str:
    """One coverage word per agent. A runtime with no hooks says so before
    anything else: its forwarder may send keepalives (`reporting`), but no hook
    can ever produce an event, and "no events" would read as healthy. An unknown
    runtime (no label, no report, or one this backend does not know) is the
    platform default, Claude Code."""
    if runtime and runtime != OTHER and runtime.lower() not in HOOKED_RUNTIMES:
        return "no_hooks"
    return state


def fleet_agent_names(owner_username: Optional[str]) -> List[str]:
    """The agents a fleet read covers: every live agent when `owner_username`
    is None (the caller decided the principal is a fleet admin), else the live
    agents that user OWNS — owned, not shared, the per-agent read's own tier."""
    if owner_username is None:
        return list(db.get_accessible_agent_names("", is_admin=True) or [])
    return list(db.get_agents_by_owner(owner_username) or [])


def fleet_stats(hours: int, agent_names: Iterable[str]) -> Dict[str, Any]:
    """Counts per kind and per agent over the last `hours`, with every agent's
    reporting state and coverage — agents with no events included, so silence
    is visible rather than absent.

    `{"hours", "since", "events", "by_kind", "coverage": {state: n},
      "agents": [{agent_name, runtime, coverage, events, by_kind, reporting}]}`,
    loudest agent first, then by name.
    """
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= STATS_HOURS_MAX:
        raise GuardrailEventQueryError("invalid_hours")
    names = sorted(set(agent_names))
    since = iso_cutoff(hours)
    counts = count_by_agent(hours, names)["agents"] if names else {}
    states = reporting_states(names)
    labels = (_container_runtimes() or {}) if names else {}

    by_kind_total: Dict[str, int] = {}
    tally = {state: 0 for state in COVERAGE_STATES}
    agents: List[Dict[str, Any]] = []
    for name in names:
        by_kind = dict(counts.get(name, {}).get("by_kind", {}))
        reporting = states[name]
        runtime = labels.get(name) or reporting.get("runtime")
        coverage = coverage_of(runtime, reporting["state"])
        tally[coverage] += 1
        for kind, n in by_kind.items():
            by_kind_total[kind] = by_kind_total.get(kind, 0) + n
        agents.append({
            "agent_name": name,
            "runtime": runtime,
            "coverage": coverage,
            "events": sum(by_kind.values()),
            "by_kind": by_kind,
            "reporting": reporting,
        })
    agents.sort(key=lambda a: (-a["events"], a["agent_name"]))
    return {
        "hours": hours,
        "since": since,
        "events": sum(by_kind_total.values()),
        "by_kind": by_kind_total,
        "coverage": tally,
        "agents": agents,
    }
