"""Platform alerts: one pending row per subject (#3246).

A platform-raised alert describes a CONDITION ("this subscription is near its
limit", "this agent's circuit is dormant"). The operator queue used to store
each reading of it as a separate MESSAGE, so a new reading filed a new pending
row and nothing ended the old ones. This module is the one place that knows
what a platform alert is about: its SUBJECT, `"<kind>:<key>"`, which the
database can see and which carries at most one pending row.

This file is the **leaf**: the kind registry, the key belt, the derivation of a
subject from a pre-#3246 row's `request_id` (+ context), and the pure upgrade
sweep planner. It is **stdlib-only at module level** — both migration tracks
(`db/migrations.py`, loaded by file path in `test_schema_parity.py`, and the
Alembic revision) import it, and so does `operator_queue_service`, which
`ask_service` imports. A module-level import of the service graph here would
close that ring (#3246 plan, E1/E2). Anything that needs the database imports
it inside the function.

Lifetimes (T4): a kind declares one of three classes —
  * ``LIFETIME_DEFAULT`` — 14 days from the last reading, env
    ``OPERATOR_PLATFORM_ALERT_LIFETIME_DAYS``;
  * ``LIFETIME_NET`` — a fixed 30 days, for edge-triggered kinds that have a
    clear hook, where expiry is only a net against a clear lost to a crash;
  * ``None`` — a person must act; the row never expires on its own.

Snooze (T5, operator ruling 2026-10-05): after a person ends an alert, a new
reading of the same subject files nothing for 7 days (env
``OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS``) unless it raises the priority or
changes one of the kind's ``material_keys``. The window is read here so the
sweep and the seam agree on it.
"""
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifetime and snooze
# ---------------------------------------------------------------------------

LIFETIME_DEFAULT = "default"
LIFETIME_NET = "net"
_LIFETIME_CLASSES = (LIFETIME_DEFAULT, LIFETIME_NET, None)

DEFAULT_LIFETIME_DAYS = 14
LIFETIME_ENV = "OPERATOR_PLATFORM_ALERT_LIFETIME_DAYS"
NET_LIFETIME_DAYS = 30

DEFAULT_SNOOZE_DAYS = 7
SNOOZE_ENV = "OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        logger.warning(
            "[platform_alerts] %s=%r is not a positive whole number of days; "
            "using the default %d", name, raw, default,
        )
        return default
    return value


def default_lifetime_days() -> int:
    """The default lifetime in days (env knob; bad value → default + WARNING)."""
    return _positive_int_env(LIFETIME_ENV, DEFAULT_LIFETIME_DAYS)


def snooze_window() -> timedelta:
    """How long a person's ending of an alert silences new readings of it."""
    return timedelta(days=_positive_int_env(SNOOZE_ENV, DEFAULT_SNOOZE_DAYS))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CLEARS_HOOK = "hook"              # the emitter ends the row when the condition clears
CLEARS_LIFETIME_ONLY = "lifetime_only"  # nothing observes the clear; the lifetime ends it
CLEARS_PERSON = "person"          # a person must act on it
_CLEARS = (CLEARS_HOOK, CLEARS_LIFETIME_ONLY, CLEARS_PERSON)


@dataclass(frozen=True)
class Kind:
    """One kind of platform alert.

    ``prefix`` is the request-id prefix its rows carry; it must fall under a
    platform-reserved prefix (`operator_queue_service._RESERVED_ID_PREFIXES`)
    so an agent cannot mint a row into it. Kinds may share a prefix (two
    conditions under one family) — they then share a lifetime class. ``event``
    kinds have no subject: every observation is its own row.
    """

    name: str
    prefix: str
    lifetime: Optional[str]
    clears: str
    budgeted_type: Optional[str] = None
    material_keys: Tuple[str, ...] = ()
    event: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name or ""):
            raise ValueError(f"kind name must be lower_snake: {self.name!r}")
        if not self.prefix or not self.prefix.endswith(("-", "_")):
            raise ValueError(f"kind {self.name}: prefix must end in '-' or '_'")
        if self.lifetime not in _LIFETIME_CLASSES:
            raise ValueError(f"kind {self.name}: unknown lifetime class {self.lifetime!r}")
        if self.clears not in _CLEARS:
            raise ValueError(f"kind {self.name}: unknown clears {self.clears!r}")


# Prefixes whose emitter lives outside this repository (the enterprise
# submodule). The sweep never touches them; the emitter registers its kind
# through `register(...)` from its own package.
EXTERNAL_PREFIXES = frozenset({"role-drift-"})

_D, _N = LIFETIME_DEFAULT, LIFETIME_NET

_BUILTIN_KINDS = (
    # -- the four families #3246 names ------------------------------------
    Kind("subscription_headroom", "sub-headroom-", _D, CLEARS_HOOK, material_keys=("tier",)),
    Kind("skills_legacy_adoption", "skills-legacy-adoption-", _D, CLEARS_HOOK),
    Kind("base_image_stale", "base-image-stale-", _N, CLEARS_HOOK),
    Kind("system_agent_start_failed", "base-image-stale-start-", _N, CLEARS_HOOK),
    Kind("circuit_dormant", "cb-dormant-", _N, CLEARS_HOOK),
    # -- every other in-repo platform emitter -----------------------------
    Kind("skills_reconcile_refused", "skills-reconcile-", _N, CLEARS_HOOK),
    Kind("skills_fleet_reinject", "skills-fleet-reinject-", _D, CLEARS_HOOK),
    Kind("sync_failing", "sync-failing-", _N, CLEARS_HOOK),
    Kind("sync_diverged", "sync-diverged-", _N, CLEARS_HOOK),
    Kind("git_bloat_size", "git-bloat-", _N, CLEARS_HOOK),
    Kind("git_bloat_maintenance", "git-bloat-", _N, CLEARS_HOOK),
    Kind("db_backup_failure", "db-backup-", _D, CLEARS_HOOK),
    Kind("db_backup_stale", "db-backup-", _D, CLEARS_HOOK),
    Kind("log_archive_unwritable", "log-archive-", _D, CLEARS_HOOK),
    Kind("retention_refused", "retention-guard-", _D, CLEARS_HOOK, material_keys=("reason",)),
    Kind("system_seed", "system-seed-", _N, CLEARS_HOOK),
    Kind("git_token_scrub_refused", "ent615-git-token-scrub-", _D, CLEARS_HOOK),
    Kind("git_token_scrub_unreadable", "ent615-git-token-scrub-unreadable-", _D, CLEARS_HOOK),
    Kind("queue_flood", "queue-flood-", _D, CLEARS_HOOK, budgeted_type="queue_flood"),
    Kind("alert_budget", "alert-budget-", _D, CLEARS_HOOK),
    Kind("skill_not_found", "skill-not-found-", _D, CLEARS_HOOK, budgeted_type="skill_not_found"),
    Kind("effect_unguarded", "effect-unguarded-", _D, CLEARS_LIFETIME_ONLY,
         budgeted_type="effect_unguarded"),
    Kind("validation_failed", "val_", _D, CLEARS_LIFETIME_ONLY),
    # -- a person must act: no lifetime ----------------------------------
    Kind("poison", "poison-", None, CLEARS_PERSON),
    Kind("portal_inbox_collision", "portal-inbox-collision-", None, CLEARS_PERSON),
    Kind("gitignore_untracked", "gitignore-untracked-", None, CLEARS_PERSON,
         budgeted_type="gitignore_untracked", event=True),
    Kind("workspace_problem_report", "workspace-problem-", None, CLEARS_PERSON,
         budgeted_type="workspace_problem_report", event=True),
)

KINDS: Dict[str, Kind] = {k.name: k for k in _BUILTIN_KINDS}


def register(kind: Kind) -> None:
    """Register a kind defined outside this module (the enterprise emitters).

    A duplicate name is refused: two emitters silently sharing a kind would
    share subjects, and one's clear would end the other's alert.
    """
    if kind.name in KINDS:
        raise ValueError(f"platform alert kind {kind.name!r} is already registered")
    KINDS[kind.name] = kind


def _kind(name: str) -> Kind:
    try:
        return KINDS[name]
    except KeyError:
        raise KeyError(f"unregistered platform alert kind {name!r}") from None


def lifetime_for(kind_name: str) -> Optional[timedelta]:
    """The kind's lifetime, measured from its last reading; None = never expires."""
    lifetime = _kind(kind_name).lifetime
    if lifetime is None:
        return None
    if lifetime == LIFETIME_NET:
        return timedelta(days=NET_LIFETIME_DAYS)
    return timedelta(days=default_lifetime_days())


# ---------------------------------------------------------------------------
# Key belt / subject
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _belt_key(key: Any) -> str:
    if isinstance(key, bool) or not isinstance(key, (str, int)):
        raise ValueError(f"platform alert key must be a string, got {type(key).__name__}")
    text = str(key).strip()
    if not text:
        raise ValueError("platform alert key is empty")
    if _KEY_RE.match(text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def subject_for(kind_name: str, key: Any) -> Optional[str]:
    """``"<kind>:<key>"`` with the key belted: id-shaped and ≤128 chars stays,
    anything else becomes ``sha256(key)[:16]``. An event kind has no subject
    (None); a condition kind without a usable key raises ValueError."""
    kind = _kind(kind_name)
    if kind.event:
        return None
    return f"{kind.name}:{_belt_key(key)}"


# ---------------------------------------------------------------------------
# Legacy subject derivation (the upgrade sweep's one source of id parsing)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LegacyMatch:
    """A pre-#3246 row read back: its kind, and its subject when the id (+ one
    named context key) identifies the condition. ``subject is None`` means
    "this kind, but the row does not say which condition" — never guessed."""

    kind: str
    subject: Optional[str]


_TS = r"\d{4}-\d{2}-\d{2}T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?"
_DAY = r"\d{4}-\d{2}-\d{2}"


def _rx(pattern: str) -> Callable[[str], Optional[str]]:
    compiled = re.compile(pattern)

    def parse(rest: str, _ctx: Mapping) -> Optional[str]:
        m = compiled.fullmatch(rest)
        return m.group(1) if m else None

    return parse


def _ctx_str(ctx: Mapping, name: str) -> Optional[str]:
    value = ctx.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _headroom(rest: str, _ctx: Mapping) -> Optional[str]:
    # sub-headroom-{safe_sid}-{episode}-{tier} | sub-headroom-fleet-{episode};
    # episode is a day or `unknown-<day>`. Non-greedy sid so `unknown-` is
    # read as the episode, not swallowed into a sid.
    m = re.fullmatch(rf"(.+?)-(?:unknown-)?{_DAY}-(?:warn|crit)", rest)
    if m:
        return m.group(1)
    if re.fullmatch(rf"fleet-(?:unknown-)?{_DAY}", rest):
        return "fleet"
    return None


def _legacy_adoption(rest: str, ctx: Mapping) -> Optional[str]:
    # Steady state carries sha256(raw url)[:12]; the failure branch is
    # timestamped and carries only the SCRUBBED url in context. For a URL with
    # no embedded credentials the two hashes agree; for one with credentials
    # the failure rows land on a second subject — two survivors, never a wrong
    # merge.
    m = re.fullmatch(r"refused-([0-9a-f]{12})", rest)
    if m:
        return m.group(1)
    url = _ctx_str(ctx, "url")
    if url and re.fullmatch(_TS, rest):
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:12]
    return None


def _effect_unguarded(_rest: str, ctx: Mapping) -> Optional[str]:
    effect, reason = _ctx_str(ctx, "effect_type"), _ctx_str(ctx, "reason")
    return f"{effect}:{reason}" if effect and reason else None


def _alert_budget(rest: str, ctx: Mapping) -> Optional[str]:
    return _ctx_str(ctx, "alert_type") or _rx(r".+-([a-z0-9_]+)-b\d+")(rest, ctx)


def _context_key(name: str) -> Callable[[str, Mapping], Optional[str]]:
    return lambda _rest, ctx: _ctx_str(ctx, name)


def _constant(value: str) -> Callable[[str, Mapping], Optional[str]]:
    return lambda _rest, _ctx: value


def _whole(rest: str, _ctx: Mapping) -> Optional[str]:
    return rest or None


def _no_subject(_rest: str, _ctx: Mapping) -> Optional[str]:
    return None


def _db_backup(rest: str, ctx: Mapping) -> Tuple[str, Optional[str]]:
    alert_type = ctx.get("alert_type")
    if alert_type in ("db_backup_failure", "db_backup_stale"):
        return alert_type, _ctx_str(ctx, "backup_dir")
    return "db_backup_failure", None


# (id prefix, kind, key parser) — matched longest prefix first. A parser
# returns the natural key or None. `db-backup-` picks its kind from context.
_LEGACY_PARSERS: Tuple[Tuple[str, str, Callable], ...] = (
    ("sub-headroom-", "subscription_headroom", _headroom),
    ("skills-legacy-adoption-", "skills_legacy_adoption", _legacy_adoption),
    ("base-image-stale-start-", "system_agent_start_failed", _rx(r"(.+)-\d+")),
    ("base-image-stale-", "base_image_stale", _rx(rf"(.+)-{_TS}")),
    ("cb-dormant-", "circuit_dormant", _rx(rf"(.+)-{_TS}")),
    ("skills-reconcile-", "skills_reconcile_refused", _rx(r"(.+)-\d+")),
    ("skills-fleet-reinject-", "skills_fleet_reinject", _constant("fleet")),
    ("sync-failing-", "sync_failing", _rx(rf"(.+)-{_TS}")),
    ("sync-diverged-", "sync_diverged", _rx(rf"(.+)-{_TS}")),
    # Two conditions (size, maintenance) share this prefix and are told apart
    # only in prose: kind known for the lifetime, subject never guessed.
    ("git-bloat-", "git_bloat_size", _no_subject),
    ("db-backup-", "db_backup_failure", _db_backup),
    ("log-archive-", "log_archive_unwritable", _context_key("path")),
    ("retention-guard-", "retention_refused", _rx(r"(.+)-\d+")),
    ("system-seed-", "system_seed", _whole),
    ("ent615-git-token-scrub-unreadable-", "git_token_scrub_unreadable", _rx(rf"(.+)-{_DAY}")),
    ("ent615-git-token-scrub-", "git_token_scrub_refused", _rx(rf"(.+)-{_DAY}")),
    ("queue-flood-", "queue_flood", _context_key("reason")),
    ("alert-budget-", "alert_budget", _alert_budget),
    ("skill-not-found-", "skill_not_found", _context_key("command")),
    ("effect-unguarded-", "effect_unguarded", _effect_unguarded),
    ("val_", "validation_failed", _rx(r"(.+)_\d{14}")),
    ("poison-", "poison", _whole),
    ("portal-inbox-collision-", "portal_inbox_collision", _whole),
    ("gitignore-untracked-", "gitignore_untracked", _no_subject),
    ("workspace-problem-", "workspace_problem_report", _no_subject),
)
_LEGACY_PARSERS = tuple(sorted(_LEGACY_PARSERS, key=lambda p: -len(p[0])))


def _as_context(context: Any) -> Dict:
    if isinstance(context, dict):
        return context
    if isinstance(context, (str, bytes)) and context:
        try:
            parsed = json.loads(context)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def derive_legacy_subject(request_id: Any, context: Any) -> Optional[LegacyMatch]:
    """Read a pre-#3246 platform row back into (kind, subject).

    None when the prefix is not an in-repo platform kind (an agent's own ask,
    a gate row, an enterprise prefix) — the sweep must leave those alone.
    """
    if not isinstance(request_id, str) or not request_id:
        return None
    ctx = _as_context(context)
    for prefix, kind_name, parse in _LEGACY_PARSERS:
        if not request_id.startswith(prefix) or kind_name not in KINDS:
            continue
        rest = request_id[len(prefix):]
        result = parse(rest, ctx)
        if isinstance(result, tuple):
            kind_name, key = result
        else:
            key = result
        subject = None
        if key is not None:
            try:
                subject = subject_for(kind_name, key)
            except ValueError:
                subject = None
        return LegacyMatch(kind_name, subject)
    return None


# ---------------------------------------------------------------------------
# Upgrade sweep planner (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stamp:
    """One row write the sweep plans. A field left None is not written."""

    id: Any
    subject: Optional[str] = None
    last_seen_at: Optional[str] = None
    expires_at: Optional[str] = None
    context: Optional[Dict] = None


@dataclass(frozen=True)
class SweepPlan:
    # survivors: subject + last_seen_at + context.seen_count, expires_at when it was NULL
    survivor_stamps: Tuple[Stamp, ...] = ()
    # pending duplicates to end as one batch: cancelled / platform / superseded
    ended_ids: Tuple[Any, ...] = ()
    # known kind, no derivable subject: expires_at only (so it leaves on its own)
    lifetime_stamps: Tuple[Stamp, ...] = ()
    # person-ended rows inside the snooze window: subject only (read by the seam)
    snooze_stamps: Tuple[Stamp, ...] = field(default=())

    def is_empty(self) -> bool:
        return not (self.survivor_stamps or self.ended_ids
                    or self.lifetime_stamps or self.snooze_stamps)


def _iso_z(dt: datetime) -> str:
    # Same shape as utils.helpers.utc_now_iso (Invariant #16).
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_key(row: Mapping) -> Tuple:
    # Newest wins: created_at, then id (the #3130 rule); NULL created_at is oldest.
    created = row.get("created_at")
    return (created is not None, created or "", str(row.get("id")))


def plan_sweep(rows: Iterable[Mapping], *, now: datetime) -> SweepPlan:
    """Plan the one-shot upgrade sweep over fetched queue rows.

    ``rows``: mappings with ``id, agent_name, request_id, status, created_at,
    expires_at, context, subject, disposed_by, disposed_at, raised_by``. Rules:

    * Only platform-raised rows (``raised_by`` NULL) whose request id derives
      to an in-repo kind are considered; agent-raised and gate rows, unknown
      and external prefixes are never touched.
    * Pending rows are grouped by ``(agent_name, subject)``. A row that already
      carries a subject holds the slot (it is the seam's live row); otherwise
      the newest by ``created_at`` then ``id`` survives. Every other pending
      row of the group is ended.
    * Survivors are stamped with ``subject``, ``last_seen_at = created_at``
      (``now`` when NULL), ``context.seen_count = 1`` and — only when
      ``expires_at`` is NULL and the kind has a lifetime — ``now + lifetime``.
    * Pending rows of a known kind with no derivable subject get the lifetime
      stamp only.
    * Person-ended rows whose ending falls inside the snooze window get
      ``subject`` only, so the seam's snooze can see them.
    * Planning over the plan's own result yields an empty plan.
    """
    if now.tzinfo is None:
        raise ValueError("plan_sweep needs an aware `now`")
    snooze_since = now - snooze_window()

    groups: Dict[Tuple[Any, str], list] = {}
    lifetime_stamps, snooze_stamps = [], []
    for row in rows:
        if row.get("raised_by") is not None:
            # An agent's own ask or a gate row: never the platform's, whatever
            # its id looks like (the prefix was unreserved when it was minted).
            continue
        match = derive_legacy_subject(row.get("request_id"), row.get("context"))
        if match is None:
            continue
        stored_subject = row.get("subject")
        status = row.get("status")
        if status == "pending":
            subject = stored_subject or match.subject
            if subject is not None:
                groups.setdefault((row.get("agent_name"), subject), []).append(row)
                continue
            lifetime = lifetime_for(match.kind)
            if lifetime is not None and row.get("expires_at") is None:
                lifetime_stamps.append(Stamp(id=row.get("id"), expires_at=_iso_z(now + lifetime)))
            continue
        if (stored_subject is None and match.subject is not None
                and row.get("disposed_by") == "person"):
            disposed = _parse_iso(row.get("disposed_at"))
            if disposed is not None and disposed >= snooze_since:
                snooze_stamps.append(Stamp(id=row.get("id"), subject=match.subject))

    survivor_stamps, ended_ids = [], []
    for (_agent, subject), members in groups.items():
        holders = [r for r in members if r.get("subject")]
        survivor = max(holders or members, key=_age_key)
        ended_ids.extend(r.get("id") for r in members if r is not survivor)
        if survivor.get("subject"):
            continue
        match = derive_legacy_subject(survivor.get("request_id"), survivor.get("context"))
        lifetime = lifetime_for(match.kind)
        expires_at = None
        if lifetime is not None and survivor.get("expires_at") is None:
            expires_at = _iso_z(now + lifetime)
        context = dict(_as_context(survivor.get("context")))
        context["seen_count"] = 1
        survivor_stamps.append(Stamp(
            id=survivor.get("id"),
            subject=subject,
            last_seen_at=survivor.get("created_at") or _iso_z(now),
            expires_at=expires_at,
            context=context,
        ))

    return SweepPlan(
        survivor_stamps=tuple(survivor_stamps),
        ended_ids=tuple(ended_ids),
        lifetime_stamps=tuple(lifetime_stamps),
        snooze_stamps=tuple(snooze_stamps),
    )


# ---------------------------------------------------------------------------
# The seam: observe / clear / reconcile (#3246 C4)
#
# Every import below is function-local: this module stays a stdlib-only leaf
# at import time (the migration tracks import `plan_sweep` from it, and
# `operator_queue_service` calls back into it — a module-level import of the
# service graph would close the cycle E1 names).
# ---------------------------------------------------------------------------

OBSERVED_CREATED = "created"
OBSERVED_UPDATED = "updated"
OBSERVED_SNOOZED = "snoozed"
OBSERVED_REFUSED_UNREGISTERED = "refused_unregistered"
OBSERVED_REFUSED_AT_BUDGET = "refused_at_budget"
OBSERVED_FAILED = "failed"

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _snoozed_by(kind: Kind, ended: Optional[Mapping], priority: str, context: Mapping) -> bool:
    """A person ended this subject's row inside the snooze window, and the new
    reading is neither a priority increase nor a material change."""
    if not ended:
        return False
    if _PRIORITY_RANK.get(priority, 0) > _PRIORITY_RANK.get(ended.get("priority"), 0):
        return False
    old = _as_context(ended.get("context"))
    return all(context.get(k) == old.get(k) for k in kind.material_keys)


def _spawn(factory: Callable[[], Any]) -> None:
    try:
        from services import operator_resume_service
        operator_resume_service.spawn_on_loop(factory)
    except Exception:  # noqa: BLE001 — the row is committed; a lost trigger is not fatal
        logger.warning("[platform-alerts] could not schedule a follow-up", exc_info=True)


async def _broadcast_sync(agent_name: str) -> None:
    """Thin trigger (#918: identifiers only); listeners refetch the access-controlled list.

    Keyed by the row's host agent, like the create path's `operator_queue_new`,
    so the scope filter delivers it only to clients who may see that agent's
    queue (ent#467). The poller's fleet-level `operator_queue_sync` is one
    trigger per cycle spanning agents; this one is always about one row.
    """
    from services import operator_queue_service as oqs
    manager = oqs._websocket_manager
    if manager:
        await manager.broadcast(json.dumps({
            "type": "operator_queue_sync",
            "data": {"agent_name": agent_name},
        }))


def observe(
    agent_name: str,
    kind: str,
    key: Any = None,
    *,
    title: str,
    question: str,
    priority: str = "high",
    context: Optional[Mapping] = None,
) -> str:
    """Report a new reading of a platform condition (#3246). Never raises.

    The subject's pending row is updated in place (one row per subject, the
    latest reading); with no pending row, a reading a person ended inside the
    snooze window (`OPERATOR_PLATFORM_ALERT_SNOOZE_DAYS`, default 7) files
    nothing unless its priority rose or a `material_keys` value changed; else
    a fresh row is filed with the kind's lifetime as `expires_at`, measured
    from this reading. Returns one of the ``OBSERVED_*`` outcomes.
    """
    try:
        k = KINDS.get(kind)
        if k is None:
            logger.error("[platform-alerts] refusing an alert of unregistered kind %r for %s",
                         kind, agent_name)
            return OBSERVED_REFUSED_UNREGISTERED
        from database import db
        from utils.helpers import utc_now_iso

        subject = subject_for(kind, key)
        ctx = dict(context) if isinstance(context, Mapping) else {}
        now = datetime.now(timezone.utc)
        if subject is not None and db.find_pending_operator_queue_by_subject(agent_name, subject) is None:
            since = _iso_z(now - snooze_window())
            ended = db.find_person_ended_operator_queue_by_subject(agent_name, subject, since)
            if _snoozed_by(k, ended, priority, ctx):
                return OBSERVED_SNOOZED
        lifetime = lifetime_for(kind)
        stem = _belt_key(key) + "-" if subject is not None else ""
        item = {
            "id": f"{k.prefix}{stem}{utc_now_iso()}",
            "type": k.budgeted_type or "alert",
            "status": "pending",
            "priority": priority,
            "title": title,
            "question": question,
            "context": ctx,
            "created_at": utc_now_iso(),
            "expires_at": _iso_z(now + lifetime) if lifetime is not None else None,
        }
        cap = None
        if k.budgeted_type:
            from services import operator_queue_service as oqs
            cap = oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE
        out = db.create_platform_operator_queue_item(
            agent_name, item, subject=subject, max_pending_for_type=cap)
        if out["outcome"] == OBSERVED_REFUSED_AT_BUDGET:
            from services import operator_queue_service as oqs
            _spawn(lambda: oqs._maybe_emit_alert_budget_episode(
                agent_name, k.budgeted_type, ctx.get("triggered_by")))
            return OBSERVED_REFUSED_AT_BUDGET
        if out.get("changed"):
            _spawn(lambda: _broadcast_sync(agent_name))
        return out["outcome"]
    except Exception:  # noqa: BLE001 — an alert must never break its emitter
        logger.error("[platform-alerts] observe failed for %s/%s", agent_name, kind, exc_info=True)
        return OBSERVED_FAILED


def _end(rows: Iterable[Mapping], reason: Optional[str]) -> int:
    ids = [r["id"] for r in rows if r]
    if not ids:
        return 0
    from services import ask_service
    return len(ask_service.clear_platform(ids, reason=reason or ask_service.CONDITION_CLEARED).rows)


def clear(agent_name: str, kind: str, key: Any = None, *, reason: Optional[str] = None) -> int:
    """The condition behind `(agent, kind, key)` cleared: the platform ends its
    pending row (ent#611 ledger, `disposed_by = 'platform'`). A row a person
    ended first is untouched. Returns the number ended; never raises."""
    try:
        subject = subject_for(kind, key)
        if subject is None:
            return 0
        from database import db
        return _end([db.find_pending_operator_queue_by_subject(agent_name, subject)], reason)
    except Exception:  # noqa: BLE001
        logger.error("[platform-alerts] clear failed for %s/%s", agent_name, kind, exc_info=True)
        return 0


def reconcile(agent_name: str, kind: str, live_keys: Iterable[Any]) -> int:
    """End every pending row of `kind` for `agent_name` whose key is not in
    `live_keys` — for an emitter that knows the full current set rather than
    each clear. Returns the number ended; never raises."""
    try:
        _kind(kind)
        live = {subject_for(kind, key) for key in live_keys}
        from database import db
        rows = db.list_operator_queue_items(agent_name=agent_name, status="pending", limit=1000)
        stale = [r for r in rows
                 if (r.get("subject") or "").startswith(f"{kind}:") and r["subject"] not in live]
        return _end(stale, None)
    except Exception:  # noqa: BLE001
        logger.error("[platform-alerts] reconcile failed for %s/%s", agent_name, kind, exc_info=True)
        return 0
