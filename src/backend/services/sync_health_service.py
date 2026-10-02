"""
Sync Health Service (#389 S1).

Polls each git-enabled agent on an interval, reads its `/api/git/status`
response (which now carries dual ahead/behind + persisted sync-state from
the auto-sync heartbeat), upserts `agent_sync_state`, and emits a
`sync_failing` operator-queue entry when the consecutive-failures counter
crosses the alert threshold.

Lifecycle mirrors `OperatorQueueSyncService`:
  - `start()` kicks off `_poll_loop`
  - `stop()` cancels the task
  - Internally swallows exceptions so the heartbeat never dies silently

The poll interval defaults to 60 s — sync failures are slow-moving, and a
tighter loop would multiply SQLite writes for no benefit (PERF-269). #2742
makes it overridable via `SYNC_HEALTH_POLL_INTERVAL_SECONDS` (read at call
time so monkeypatching works), with the default deliberately unchanged: the
cadence is already 15x oversampled against its own producer — the agent's
auto-sync heartbeat writes `sync-state.json` every 900 s and every consumer
threshold in `utils/syncHealth.js` is 24 h / 7 d — so the knob is there for an
operator who wants the fetch load down, not because 60 s was wrong.

Leader-leased across uvicorn workers (#2742, the #1464/#1632 shape).
Production runs `--workers 2` and `main.py` starts this service in EVERY
worker, so the fleet was polled twice a minute per agent and each poll runs a
30 s credentialed `git fetch` inside the agent container.

The lease fails **OPEN** — Redis unreachable ⇒ every worker polls, i.e. exactly
the pre-#2742 behaviour. Failing closed would darken the `sync_failing` signal
precisely when infrastructure is degraded, and this is an observability feed.

**Named side effect, because it is not idempotent.** `db.upsert_sync_state`
*increments* `consecutive_failures` on every `failed` upsert, and
`ALERT_THRESHOLD` is an edge trigger off that counter. Two unleased workers
therefore drove a failing agent to `sync_failing` in ~90 s; one leader takes
~180 s. That is arguably the counter finally meaning what its name says (3
consecutive failed *polls* = 3 minutes), but it is a change to an alerting
path, so it is documented here, covered by a test, and called out in the PR.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from database import db
from redis_breaker_util import get_breaker_redis
from services import git_service
from services import sync_freeze_policy
from services.sync_health_view import sync_view
from services.agent_client import AgentClient
from utils.helpers import parse_iso_timestamp, to_utc_iso, utc_now_iso

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60  # seconds
ALERT_THRESHOLD = 3  # emit sync_failing entry after N consecutive failures

# #2742: single Redis key holding the current sync-health leader's worker id.
# One poller across all uvicorn workers. Collides with none of the five existing
# leases (monitoring / opqueue / skills:sync / canary / subscription:recovery).
_LEADER_KEY = "synchealth:leader"

# #2742: compare-and-delete release. A plain GET-then-DEL is not atomic — a
# worker whose lease expired between the two can delete a sibling's FRESH grant,
# which is a second poller for one whole cycle. EVAL is outside the `-@dangerous`
# categories denied to the `backend`/`scheduler` ACL users.
_RELEASE_IF_MINE = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _poll_interval_seconds() -> int:
    """Poll cadence, read at CALL time (#2742).

    Shape borrowed from `agent_server.routers.git._maintenance_timeout_seconds`:
    an import-time copy makes env monkeypatching silently inert. Parse-guarded
    and positive-clamped — a garbage or non-positive value degrades to the
    default rather than becoming a hot loop.
    """
    raw = os.getenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL
    return value if value > 0 else DEFAULT_POLL_INTERVAL


# #1595: edge-triggered git-bloat alerting — the killed-auto-gc failure class
# was "completely silent until the disk fills"; a column behind an API nobody
# queries reproduces that. Both thresholds compare prior DB row → new value so
# each crossing fires exactly once per episode (the sync_failing pattern).
GIT_DIR_ALERT_BYTES = int(os.getenv("GIT_DIR_ALERT_BYTES", str(10 * 1024**3)))
MAINTENANCE_FAILURES_ALERT_THRESHOLD = 3

# trinity-enterprise#706: id prefix of the one-per-episode divergence-freeze
# item. Platform-reserved in operator_queue_service._RESERVED_ID_PREFIXES, so an
# agent cannot pre-create the id and silence the alarm about itself.
SYNC_DIVERGED_ALERT_PREFIX = "sync-diverged-"


# PostgreSQL column ceilings. `Integer` is int4 there (SQLite's INTEGER is 64-bit
# either way, which is how #2800 shipped): a value the boundary admits but the
# column cannot hold makes the whole upsert raise `NumericValueOutOfRange`, and
# the agent's sync health goes dark. So the boundary's ceiling MUST match the
# column's (#2827): int4 for the counters, int8 only for the one byte count that
# was widened.
INT4_MAX = 2**31 - 1
INT8_MAX = 2**63 - 1


def _coerce_nonneg_int(value, *, ceiling: int = INT4_MAX) -> Optional[int]:
    """Boundary guard for agent-supplied numbers (#1595).

    `sync-state.json` is agent-writable: a compromised/prompt-injected agent
    can persist arbitrary JSON into it, and SQLite's dynamic typing would
    accept strings/objects silently. Same posture as `/health clone_status`
    (enum only, never agent-supplied strings). bool is an int subclass —
    rejected explicitly.

    `ceiling` is the destination's own bound (#2827). It shipped as a flat
    `< 2**63` for every caller while seven of the eight `agent_sync_state`
    columns it feeds are int4 — so an agent writing `2**40` into `pack_count`
    made every upsert for it raise on PostgreSQL, exactly the #2800 class one
    column over. The default is the int4 column bound because that is what
    almost every consumer is; a caller with a wider destination says so.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= ceiling:
        return value
    return None


def _coerce_counter(payload: dict, *keys: str) -> int:
    """An agent-supplied ahead/behind counter, coerced, defaulting to 0 (#2827).

    The first key present wins (`ahead_main`, else the legacy `ahead`). These
    four used to be passed UNCOERCED into int4 columns — a stranger to the
    `_coerce_nonneg_int` guard two lines above them.
    """
    for key in keys:
        if key in payload and payload[key] is not None:
            coerced = _coerce_nonneg_int(payload[key])
            return coerced if coerced is not None else 0
    return 0


# #2742: an explicit UTC offset, which `startup.sh` always writes (`...Z`).
_ISO_OFFSET_RE = re.compile(r"[+-]\d{2}:?\d{2}$")

# #2742: how old a `lock_recovery.at` may be and still be worth announcing.
# Floor at one hour rather than the poll interval: `startup.sh` stamps it at
# container boot and the agent server folds it in on its FIRST status read, and
# a cold boot (image pull, clone, credential injection) can put several minutes
# between those two. The clamp's job is to reject nonsense — a far-future `at`
# would otherwise be "newer" on every tick forever — and the DEDUP below, not
# the clamp, is what stops a real record repeating.
_LOCK_RECOVERY_MAX_AGE_SECONDS = 3600
_LOCK_RECOVERY_FUTURE_GRACE_SECONDS = 60


def _coerce_lock_recovery(value, poll_interval: int = DEFAULT_POLL_INTERVAL):
    """Rebuild the boot-reap record from coerced values, or drop it (#2742).

    `sync-state.json` is agent-authored and the agent server merges it wholesale
    (`merged.update(data)`), so `last_lock_recovery` is fully agent-controlled
    even on an agent where nothing ever reaped anything — and
    `git_service.get_git_status` proxies `response.json()` UNMODIFIED to the UI
    and the MCP tool. The backend therefore never passes the agent's dict
    through; it builds a new one from values it has checked.

    Three guards, each for a specific failure:

    - `isinstance(value, str)` before parsing: `parse_iso_timestamp` raises
      `AttributeError` (not `ValueError`) on a non-str.
    - `except (ValueError, TypeError)` around the parse AND the comparison: a
      valid-but-naive ISO string parses cleanly and then raises `TypeError` on
      `aware > naive`. That raise would land in `_sync_agent` AFTER the upsert
      and be swallowed by `_poll_cycle`'s `gather(return_exceptions=True)`, so
      the symptom is a LOST ALERT with no traceback. The same guard, with the
      same comment, already exists in the file this record comes from
      (`agent_server/routers/git.py::_maybe_run_git_maintenance`).
    - An explicit UTC offset is REQUIRED. Our own writer always stamps `Z`, so a
      naive value did not come from us; this is a boundary over agent-authored
      JSON and the house posture is to reject rather than guess (the
      `_coerce_nonneg_int` docstring states the same threat model).

    Returns a fresh dict or None. Never raises.
    """
    if not isinstance(value, dict):
        return None
    raw_at = value.get("at")
    if not isinstance(raw_at, str) or not (0 < len(raw_at) <= 64):
        return None
    if not (raw_at.endswith("Z") or _ISO_OFFSET_RE.search(raw_at)):
        return None
    try:
        parsed = parse_iso_timestamp(raw_at)
        now = datetime.now(timezone.utc)
        if parsed > now + timedelta(seconds=_LOCK_RECOVERY_FUTURE_GRACE_SECONDS):
            return None
        if parsed < now - timedelta(seconds=_LOCK_RECOVERY_MAX_AGE_SECONDS):
            return None
    except (ValueError, TypeError, OverflowError):
        return None

    locks = value.get("locks")
    return {
        "at": parsed.isoformat(),
        # Agent-reachable free text: bounded, and it never reaches a format
        # string as anything but a `%s` argument.
        "locks": locks[:200] if isinstance(locks, str) else "",
    }


def _coerce_lock_stuck(value):
    """Rebuild the stuck-lock report from coerced INTS only (#2742).

    The agent-supplied `path` is deliberately dropped: it is composed from a
    `.git` the agent can point anywhere, it adds nothing to a fleet-level
    WARNING, and a free-text field that reaches a log is how the next log
    injection gets written. Returns None unless at least one usable int survives.
    """
    if not isinstance(value, dict):
        return None
    # Log-only ints, never a column: the wide ceiling is honest here.
    coerced = {
        key: _coerce_nonneg_int(value.get(key), ceiling=INT8_MAX)
        for key in ("age_seconds", "stable_for_seconds", "sightings", "size_bytes")
    }
    if all(v is None for v in coerced.values()):
        return None
    return {k: v for k, v in coerced.items() if v is not None}

def _coerce_agent_timestamp(value) -> Optional[datetime]:
    """An agent-authored ISO timestamp as aware UTC, or None (trinity-enterprise#706).

    The `_coerce_lock_recovery` posture for a value that becomes a column: a
    bounded string with an EXPLICIT offset (our writer always stamps one, so a
    naive value did not come from us), parseable, and not in the future beyond
    the same grace. A far-future value would otherwise win every `max()` below
    forever. Never raises.
    """
    if not isinstance(value, str) or not (0 < len(value) <= 64):
        return None
    if not (value.endswith("Z") or _ISO_OFFSET_RE.search(value)):
        return None
    try:
        parsed = parse_iso_timestamp(value)
        now = datetime.now(timezone.utc)
        if parsed > now + timedelta(seconds=_LOCK_RECOVERY_FUTURE_GRACE_SECONDS):
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed


def _config_value(config, key: str):
    """Read a git-config field from the model the poller gets, or a dict."""
    if isinstance(config, dict):
        return config.get(key)
    return getattr(config, key, None)


def _last_successful_push_at(sync_state: dict, config, prior: Optional[dict]) -> Optional[str]:
    """The last push that landed, monotonic (trinity-enterprise#706).

    The later of: the heartbeat's own record (`last_successful_push_at`, #3011);
    an operator Push (`agent_git_config.last_sync_at`, stamped only on success);
    and the value already stored. An agent image older than #3011 has no such
    key at all, so its last `success` cycle stands in. A #3011 image that has
    never pushed carries the key as None and does NOT fall back — a
    success-status cycle there is not evidence that anything was pushed.
    """
    candidates = []
    if "last_successful_push_at" in sync_state:
        candidates.append(_coerce_agent_timestamp(sync_state.get("last_successful_push_at")))
    elif sync_state.get("last_sync_status") == "success":
        candidates.append(_coerce_agent_timestamp(sync_state.get("last_sync_at")))
    operator_push = _config_value(config, "last_sync_at")
    if isinstance(operator_push, datetime):
        candidates.append(
            operator_push if operator_push.tzinfo else operator_push.replace(tzinfo=timezone.utc)
        )
    stored = (prior or {}).get("last_successful_push_at")
    if isinstance(stored, str):
        try:
            candidates.append(parse_iso_timestamp(stored))
        except ValueError:
            pass
    valid = [c for c in candidates if c is not None]
    return to_utc_iso(max(valid)) if valid else None


def _episode_clock(prior_value, *, active: bool, known: bool, now: str):
    """Set-once / clear / keep for `diverged_since` and `dirty_since` (#706).

    Returns the value to store: the prior start while the episode continues, a
    fresh `now` when one begins, None when it ends, and the prior value
    untouched when the observation could not be computed.
    """
    if active:
        return prior_value or now
    if not known:
        return prior_value
    return None


# WebSocket manager injected from main.py (optional, mirrors operator-queue pattern).
_websocket_manager = None


def set_websocket_manager(manager):
    global _websocket_manager
    _websocket_manager = manager


# trinity-enterprise#703: the pull cycle's status vocabulary (agent-written).
_PULL_STATUSES = frozenset({"success", "failed", "skipped", "never"})


def _pull_status(value) -> Optional[str]:
    return value if isinstance(value, str) and value in _PULL_STATUSES else None


def _pull_text(value, limit: int) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


class SyncHealthService:
    """Background service that keeps agent_sync_state fresh and raises alerts."""

    def __init__(self, poll_interval: Optional[int] = None):
        # `is not None`, never truthiness: the tests construct this with
        # poll_interval=0 to mean "one cycle then exit".
        self._poll_interval_override = poll_interval
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # #2742: unique per worker PROCESS, and stable across cycles — the
        # lease has to recognise its own grant in order to refresh it, which is
        # exactly why `SingleFlightLock` (a fresh token per acquire) cannot be
        # adopted here. Mirrors monitoring #1464 / opqueue #1632.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False  # last observed leadership, for transition logs
        # #2742: dedup comparands for the two new log lines. Against the last
        # OBSERVED value, never against `last_check_at` — that column is
        # re-stamped to `now` on EVERY upsert, so "at is newer than
        # last_check_at" would be true forever, per agent, and a single
        # far-future `at` would flood the log for the life of the process.
        self._last_lock_recovery: Dict[str, Dict] = {}
        self._lock_stuck_agents: set = set()
        # trinity-enterprise#706: the last sync_diverged item id this process
        # raised per agent — only to skip a redundant INSERT every poll while a
        # freeze lasts. The DB's (agent_name, request_id) conflict target is
        # what makes it one item per episode across restarts and workers.
        self._diverged_alerted: Dict[str, str] = {}

    @property
    def poll_interval(self) -> int:
        """Effective cadence: an explicit constructor value, else the env read.

        A property rather than an attribute so `SYNC_HEALTH_POLL_INTERVAL_SECONDS`
        is honoured at call time — the module-level singleton is built at import,
        and an import-time copy is how an env knob becomes silently inert.
        """
        if self._poll_interval_override is not None:
            return self._poll_interval_override
        return _poll_interval_seconds()

    # -------------------------- lifecycle --------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Sync health service started (interval=%ss)", self.poll_interval
        )

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        # #2742: hand leadership off on a graceful shutdown instead of making
        # the survivor wait out the TTL with nobody polling.
        self._release_leadership()
        logger.info("Sync health service stopped")

    # -------------------------- leader lease (#2742) --------------------------

    def _leader_ttl(self) -> int:
        """Lease TTL. Refreshed once at the TOP of each `_poll_cycle`, so it has
        to outlast one worst-case cycle plus the inter-cycle sleep or leadership
        flaps and both workers poll anyway. A cycle is `asyncio.gather` over every
        git-enabled agent with a 10 s per-agent client timeout, then a
        `poll_interval` sleep — so `3x` at the 60 s default is 180 s, with the 30 s
        floor covering the poll_interval=0 test construction."""
        return max(self.poll_interval * 3, 30)

    def _try_acquire_leadership(self) -> bool:
        """True iff this worker holds the lease for this cycle (#2742).

        Fail-OPEN: Redis unreachable or erroring ⇒ act as leader, which is the
        pre-#2742 behaviour (every worker polls). Failing closed would stop the
        only feed that ever raises `sync_failing`, at exactly the moment the
        infrastructure is already degraded.
        """
        r = get_breaker_redis()
        if r is None:
            return True  # fail-open: no Redis → behave as the sole worker
        ttl = self._leader_ttl()
        try:
            if r.set(_LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            # Already held — refresh the TTL only if the lease is OURS.
            if r.get(_LEADER_KEY) == self._worker_id:
                r.expire(_LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:
            logger.warning("sync-health leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        """Delete the lease iff we still hold it (best-effort, never raises).

        Compare-and-delete in ONE round trip: a GET-then-DEL lets a worker whose
        lease expired between the two calls delete a sibling's fresh grant, which
        is a second poller for a whole cycle.
        """
        try:
            r = get_breaker_redis()
            if r is not None:
                r.eval(_RELEASE_IF_MINE, 1, _LEADER_KEY, self._worker_id)
        except Exception:
            pass
        self._is_leader = False

    # -------------------------- loop --------------------------

    async def _poll_loop(self):
        while self._running:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("sync health poll cycle raised")

            if self.poll_interval > 0:
                try:
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
            else:
                # Test-only: single cycle, then exit.
                break

    async def _poll_cycle(self):
        """One pass over every git-enabled agent.

        #2742: only the lease-holding worker polls. `main.py` starts this service
        in every uvicorn worker and prod runs `--workers 2`, so the fleet was
        being asked for `/api/git/status` twice a minute per agent — each ask
        running a 30 s credentialed `git fetch` inside the container.
        """
        leader = self._try_acquire_leadership()
        if leader and not self._is_leader:
            logger.info(
                "Sync health loop acquired leadership (worker %s)", self._worker_id
            )
        elif not leader and self._is_leader:
            logger.info(
                "Sync health loop yielded leadership (worker %s)", self._worker_id
            )
        self._is_leader = leader
        if not leader:
            return

        try:
            configs = db.list_git_enabled_agents()
        except Exception:
            logger.debug("could not list git-enabled agents", exc_info=True)
            return

        if not configs:
            return

        tasks = [self._sync_agent(cfg) for cfg in configs]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_agent(self, config) -> None:
        """Pull one agent's git status, upsert DB, maybe alert."""
        agent_name = getattr(config, "agent_name", None) or config["agent_name"]

        payload = await self._fetch_git_status(agent_name)
        if payload is None:
            # Agent unreachable this tick — don't write anything; we'd rather
            # wait for a signal than flap the counter.
            return

        if not payload.get("git_enabled", True):
            # Git no longer enabled — nothing to track.
            return

        sync_state = payload.get("sync_state") or {}
        last_sync_status = sync_state.get("last_sync_status") or "never"
        last_sync_at = sync_state.get("last_sync_at")
        last_error_summary = sync_state.get("last_error_summary")

        last_commit = (payload.get("last_commit") or {})
        local_head_sha = last_commit.get("sha")

        prior = db.get_sync_state(agent_name)
        prior_failures = prior["consecutive_failures"] if prior else 0
        prior_git_dir_bytes = (prior.get("git_dir_bytes") if prior else None) or 0
        prior_maintenance_failures = (
            prior.get("maintenance_failures") if prior else None
        ) or 0

        # #1595: agent-supplied ints coerced at the boundary (see helper).
        # #2827: each at ITS column's ceiling — git_dir_bytes is the one BIGINT.
        git_dir_bytes = _coerce_nonneg_int(sync_state.get("git_dir_bytes"), ceiling=INT8_MAX)
        pack_count = _coerce_nonneg_int(sync_state.get("pack_count"))
        loose_objects = _coerce_nonneg_int(sync_state.get("loose_objects"))
        maintenance_failures = _coerce_nonneg_int(
            sync_state.get("maintenance_failures")
        )

        # trinity-enterprise#706: the divergence and dirt episode clocks. The
        # decision reads the RAW values, so an uncomputable count (None) is
        # never mistaken for 0. `behind is None` with a known `ahead == 0` is
        # "no upstream" (#2105) — nothing on origin to be behind — and clears.
        # The prior value is passed explicitly for "keep" (no sentinel), so a
        # set-once start survives any number of polls.
        now_iso = utc_now_iso()
        ahead_raw = _coerce_nonneg_int(payload.get("ahead_working"))
        behind_raw = _coerce_nonneg_int(payload.get("behind_working"))
        diverged_since = _episode_clock(
            (prior or {}).get("diverged_since"),
            active=bool(ahead_raw) or bool(behind_raw),
            known=ahead_raw is not None,
            now=now_iso,
        )
        # A clock KEPT because the tuple was uncomputable keeps the counts that
        # started it: `_coerce_counter` reads the unknown as 0, which would sit
        # next to a live `diverged_since` and freeze with "0 behind / 0 ahead".
        # None is `upsert`'s "keep the prior value" (#706 /review).
        keep_working_counts = (
            ahead_raw is None and not behind_raw and diverged_since is not None
        )
        dirty_files = _coerce_nonneg_int(payload.get("changes_count"))
        dirty_since = _episode_clock(
            (prior or {}).get("dirty_since"),
            active=bool(dirty_files),
            known=dirty_files is not None,
            now=now_iso,
        )

        updated = db.upsert_sync_state(
            agent_name,
            last_sync_at=last_sync_at,
            last_sync_status=last_sync_status,
            last_error_summary=last_error_summary,
            last_remote_sha_main=payload.get("last_remote_sha_main"),
            last_remote_sha_working=local_head_sha,
            # #2827: coerced like their siblings — these four went in raw.
            ahead_main=_coerce_counter(payload, "ahead_main", "ahead"),
            behind_main=_coerce_counter(payload, "behind_main", "behind"),
            ahead_working=(
                None if keep_working_counts
                else _coerce_counter(payload, "ahead_working")),
            behind_working=(
                None if keep_working_counts
                else _coerce_counter(payload, "behind_working")),
            git_dir_bytes=git_dir_bytes,  # #1596 bloat observability
            pack_count=pack_count,  # #1595
            loose_objects=loose_objects,  # #1595
            maintenance_failures=maintenance_failures,  # #1595
            # trinity-enterprise#703: the container's pull cycle. Agent-written
            # JSON, so the status is bounded to the vocabulary and the counter
            # coerced like its neighbours.
            last_pull_at=_pull_text(sync_state.get("last_pull_at"), 64),
            last_pull_status=_pull_status(sync_state.get("last_pull_status")),
            behind_after_pull=_coerce_nonneg_int(sync_state.get("behind_after_pull")),
            last_pull_error=_pull_text(sync_state.get("last_pull_error"), 500),
            last_successful_pull_at=_pull_text(sync_state.get("last_successful_pull_at"), 64),
            consecutive_pull_failures=_coerce_nonneg_int(sync_state.get("consecutive_pull_failures")),
            consecutive_pull_skips=_coerce_nonneg_int(sync_state.get("consecutive_pull_skips")),
            diverged_since=diverged_since,  # trinity-enterprise#706
            dirty_files=dirty_files,  # #706: None (garbage/absent) keeps the prior
            dirty_since=dirty_since,  # #706
            last_successful_push_at=_last_successful_push_at(  # #706
                sync_state, config, prior),
            last_check_at=now_iso,
        )

        # Edge-triggered alert: only emit when we cross the threshold.
        new_failures = updated["consecutive_failures"]
        if prior_failures < ALERT_THRESHOLD <= new_failures:
            self._emit_sync_failing_alert(agent_name, updated)

        # trinity-enterprise#706: a divergence freeze is in force → one
        # sync_diverged item for the episode. Rule 2 (sync_failing) already
        # has its own item above, so only the divergence cause raises this.
        verdict = sync_view(updated, config)
        if verdict["freeze_cause"] == "divergence":
            self._emit_sync_diverged_alert(agent_name, updated, verdict)

        # #2742: a self-healed wedge, announced once. No operator-queue item
        # and no DB column — the boot reap already fixed it, so this is a log
        # line an operator can find, not a decision anyone has to make.
        recovery = _coerce_lock_recovery(
            sync_state.get("last_lock_recovery"), self.poll_interval
        )
        if recovery is not None and self._last_lock_recovery.get(agent_name) != recovery:
            self._last_lock_recovery[agent_name] = recovery
            logger.warning(
                "%s recovered a stale git lock at container start (at=%s, locks=%s)",
                agent_name, recovery["at"], recovery["locks"],
            )

        # #2742: a lock that is stuck RIGHT NOW, edge-triggered. Reported, never
        # removed — see `_index_lock_stuck` in the agent server for why. This is
        # the "tell the truth about state" half: a stale lock does not fail
        # `git status`, so without this line a wedged workspace is invisible
        # until a human notices the commits stopped.
        stuck = _coerce_lock_stuck(payload.get("index_lock_stuck"))
        if stuck is not None:
            if agent_name not in self._lock_stuck_agents:
                self._lock_stuck_agents.add(agent_name)
                logger.warning(
                    "%s has a git index.lock that has been unchanged across %s "
                    "status reads (%ss) — a running git may still hold it; the "
                    "race-free repair is a container restart, whose boot reap "
                    "clears it with the PID namespace empty",
                    agent_name,
                    stuck.get("sightings"),
                    stuck.get("stable_for_seconds"),
                )
        else:
            self._lock_stuck_agents.discard(agent_name)

        # #1595: edge-triggered git-bloat / maintenance-health alerts.
        new_git_dir_bytes = updated.get("git_dir_bytes") or 0
        if prior_git_dir_bytes < GIT_DIR_ALERT_BYTES <= new_git_dir_bytes:
            self._emit_git_bloat_alert(
                agent_name, updated,
                reason=(
                    f".git has grown to {new_git_dir_bytes / 1024**3:.1f} GiB "
                    f"(alert threshold {GIT_DIR_ALERT_BYTES / 1024**3:.1f} GiB)"
                ),
            )
        new_maintenance_failures = updated.get("maintenance_failures") or 0
        if (
            prior_maintenance_failures
            < MAINTENANCE_FAILURES_ALERT_THRESHOLD
            <= new_maintenance_failures
        ):
            self._emit_git_bloat_alert(
                agent_name, updated,
                reason=(
                    f"git maintenance has failed {new_maintenance_failures} "
                    "consecutive times — the repo may be too large to repack "
                    "within its budget"
                ),
            )

    async def _fetch_git_status(self, agent_name: str) -> Optional[Dict]:
        """Fetch /api/git/status from the agent. None on unreachable."""
        try:
            client = AgentClient(agent_name)
            response = await client.get("/api/git/status", timeout=10.0)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            logger.debug("agent %s unreachable", agent_name, exc_info=True)
            return None

    # -------------------------- alerting --------------------------

    def _emit_sync_failing_alert(self, agent_name: str, state: Dict) -> None:
        """Insert a sync_failing operator-queue entry.

        The ID embeds the emission timestamp so each distinct failure series
        produces a unique row. This method is only called on the edge where
        consecutive_failures crosses from (threshold-1) → threshold, so we
        fire at most once per series naturally.
        """
        now = utc_now_iso()
        last_sync_at = state.get("last_sync_at") or now
        item_id = f"sync-failing-{agent_name}-{now}"
        error = state.get("last_error_summary") or ""
        failures = state["consecutive_failures"]
        context = {
            "last_error_summary": error,
            "last_sync_at": last_sync_at,
            "consecutive_failures": failures,
        }
        # #2107: a refused push is not a flaky one — it fails identically every
        # cycle until someone changes the token, so name the cause and the fix
        # instead of a count that reads the same at 3 as at 64.
        if git_service.is_push_denied(error):
            title = "Git token can't push"
            question = (
                f"{agent_name}'s GitHub token can read its repository but is not "
                f"allowed to push, so none of its work is being saved "
                f"({failures} syncs refused). This will not recover on its own."
            )
            context["cause"] = "push_denied"
            context["remediation"] = (
                "Give the agent's GitHub token write access to the repository "
                "(fine-grained token: Contents: Read and write; classic token: "
                "the `repo` scope), or set a per-agent token that has it. The "
                "next sync pushes everything that is waiting."
            )
        else:
            title = "Git sync failing"
            question = f"{agent_name}'s git sync has failed {failures} times in a row."
        item = {
            "id": item_id,
            "agent_name": agent_name,
            "type": "sync_failing",
            "status": "pending",
            "priority": "high",
            "title": title,
            "question": question,
            "context": context,
            "created_at": now,
        }
        try:
            db.create_operator_queue_item(agent_name, item)
            logger.warning(
                "sync_failing emitted for %s (failures=%s)",
                agent_name, state["consecutive_failures"],
            )
        except Exception:
            logger.exception("failed to emit sync_failing alert")

    def _emit_sync_diverged_alert(self, agent_name: str, state: Dict, verdict: Dict) -> None:
        """trinity-enterprise#706: one operator-queue item per divergence episode.

        The id is deterministic per episode (`diverged_since` is set once), so
        every repeat poll, a restart and a fail-open double leader converge on
        ONE row via create_item's `(agent_name, request_id)` conflict target —
        the `alert-budget-` precedent, with no new column. Not auto-resolved
        when the episode ends; a new episode has a new id. The context carries
        counts and the recommendation, never agent-authored error text.
        """
        diverged_since = state.get("diverged_since")
        item_id = f"{SYNC_DIVERGED_ALERT_PREFIX}{agent_name}-{diverged_since}"
        if self._diverged_alerted.get(agent_name) == item_id:
            return
        age = sync_freeze_policy.format_age(verdict["divergence_age_s"] or 0)
        item = {
            "id": item_id,
            "agent_name": agent_name,
            "type": "sync_diverged",
            "status": "pending",
            "priority": "high",
            "title": f"Agent diverged from GitHub for {age} — schedules paused",
            "question": (
                f"{agent_name}: {verdict['freeze_reason']}. Its scheduled runs are "
                "paused until it is back in step with its repository."
            ),
            "context": {
                "ahead": state.get("ahead_working") or 0,
                "behind": state.get("behind_working") or 0,
                "dirty_files": state.get("dirty_files"),
                "diverged_since": diverged_since,
                "divergence_age_s": verdict["divergence_age_s"],
                "recommendation": verdict["recommendation"],
            },
            "created_at": utc_now_iso(),
        }
        try:
            db.create_operator_queue_item(agent_name, item)
            self._diverged_alerted[agent_name] = item_id
            logger.warning(
                "sync_diverged raised for %s (%s)", agent_name, verdict["freeze_reason"]
            )
        except Exception:
            logger.exception("failed to emit sync_diverged alert")

    def _emit_git_bloat_alert(self, agent_name: str, state: Dict, *, reason: str) -> None:
        """#1595: operator-queue entry for repo bloat / failing maintenance.

        Edge-triggered by the caller (prior DB row vs new value crosses the
        threshold), so each episode fires once — mirrors
        :meth:`_emit_sync_failing_alert`.
        """
        now = utc_now_iso()
        item = {
            "id": f"git-bloat-{agent_name}-{now}",
            "agent_name": agent_name,
            "type": "git_bloat",
            "status": "pending",
            "priority": "high",
            "title": "Agent git repo needs attention",
            "question": f"{agent_name}: {reason}.",
            "context": {
                "git_dir_bytes": state.get("git_dir_bytes"),
                "pack_count": state.get("pack_count"),
                "loose_objects": state.get("loose_objects"),
                "maintenance_failures": state.get("maintenance_failures"),
            },
            "created_at": now,
        }
        try:
            db.create_operator_queue_item(agent_name, item)
            logger.warning("git_bloat emitted for %s: %s", agent_name, reason)
        except Exception:
            logger.exception("failed to emit git_bloat alert")


# Module-level singleton mirrors operator_queue_service.
sync_health_service = SyncHealthService()
