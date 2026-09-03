"""Resumable-turn core — the `--resume` engine, shared by both continuous
conversation surfaces.

Every primitive here was previously private to ``routers/sessions.py``. It moved
out for abilityai/trinity-enterprise#358: the Workspace absorbs the Session
surface, and it can only do that without a continuity downgrade if it runs the
*same* engine — cached Claude UUID → per-``(agent, uuid)`` resume lock →
``execute_task(persist_session=True, resume_session_id=…)`` → one cold retry when
the JSONL is gone. Two copies of that dance would drift, and the failure mode of
drift is silent: a turn that quietly forgets.

So the engine lives here (Invariant #1 — the router held business logic), and the
two callers own only their own persistence:

* ``routers/sessions.py`` → ``agent_sessions`` (per platform user)
* ``client_portal/service.py`` → ``enterprise_portal_sessions`` (per client email)

``routers/sessions.py`` re-exports the moved names under their original private
aliases, so the legacy surface — and the tests pinning it — see no change.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Callable, NamedTuple, Optional

from fastapi import HTTPException

from database import db
from services.docker_service import get_agent_container, get_agent_status_from_container

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime capability
# ---------------------------------------------------------------------------

# #1187 Phase H: runtimes whose turns must NOT use the cached-UUID `--resume`
# machinery. They support plain chat continuity but not the Claude-JSONL
# resume/fallback/reaping model, so we run a stateless turn for them instead.
# ONE backend constant — keep in sync with the agent-side
# `RuntimeCapabilities.session_tab_resume`.
RUNTIMES_WITHOUT_SESSION_TAB_RESUME = {"codex"}


def supports_session_resume(agent_name: str) -> bool:
    """False for runtimes (e.g. Codex) that lack the cached-UUID `--resume`
    machinery. Defaults to True on any lookup failure — assume Claude-like
    so a transient Docker hiccup never silently downgrades a real session."""
    try:
        container = get_agent_container(agent_name)
        if container is None:
            return True
        status = get_agent_status_from_container(container)
        runtime = (status.runtime or "claude-code").lower()
        return runtime not in RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    except Exception:
        logger.warning(
            "[SessionTurn] runtime lookup failed for %s; assuming resume-capable",
            agent_name, exc_info=True,
        )
        return True


# ---------------------------------------------------------------------------
# Redis primitives — resume lock (#20992) + in-flight sentinel (#759)
# ---------------------------------------------------------------------------

# Two distinct Redis primitives gate a resumable turn:
#
# 1. `session_lock:{agent}:{uuid}` — per-(agent, claude_uuid) lock that
#    serialises concurrent `claude --resume <same-uuid>` calls (Anthropic
#    #20992: concurrent resume calls corrupt the JSONL). Cold turns take
#    `session_lock:cold:{session_key}` instead (#779) — there is no JSONL to
#    corrupt yet, but two concurrent first turns would otherwise race on the
#    cache write and orphan one.
#
# 2. `session_inflight:{session_key}` — per-session sentinel SET for the
#    duration of any turn (cold or warm). Drives `turn_in_progress` so a UI can
#    reattach on activation (#759). Distinct from the resume lock because the
#    lock's key shape changes between cold and warm turns.
#
# TTL for both is dynamic: at acquire time we look up the per-agent
# `execution_timeout_seconds` (default 3600) and add a 30s buffer, capped at
# 7230s. Stale-lock cleanup after a backend crash is bounded by this TTL
# (worst case ≈ 2h); admins can manually `DEL` if needed.

LOCK_TTL_FALLBACK = 7230          # cap + default on lookup failure (≈ 2h)
LOCK_WAIT_TOTAL_SECONDS = 30.0    # hard ceiling for chat UX
LOCK_POLL_INTERVAL_SECONDS = 0.25

LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def session_lock_key(agent_name: str, claude_session_id: str) -> str:
    """Canonical key for the per-(agent, uuid) resume lock.

    Extracted as a helper so the producer (``ResumeLock``) and any consumer
    that probes lock state share one source of truth. A typo here would be a
    silent split-brain — locks claimed against one key, probed against another.
    """
    return f"session_lock:{agent_name}:{claude_session_id}"


def session_inflight_key(session_id: str) -> str:
    """Sentinel key set for the duration of any turn.

    Distinct from the resume lock — that one serialises JSONL writes; this one
    signals "a turn is in flight on this session id" to the UI's onActivated
    re-sync (#759). Covers cold turns, which the warm lock shape skips.
    """
    return f"session_inflight:{session_id}"


def resolve_lock_ttl(agent_name: str) -> int:
    """Resolve a turn's TTL from the agent's per-agent execution timeout.

    Uses ``db.get_execution_timeout(agent_name)`` (default 3600s) plus a 30s
    buffer, capped at ``LOCK_TTL_FALLBACK``. Falls back to the cap on any
    lookup failure — safer to over-TTL than under-TTL since both keys are
    auto-expiring strings, not state we care to keep precise.
    """
    try:
        timeout = db.get_execution_timeout(agent_name)
        return min(timeout + 30, LOCK_TTL_FALLBACK)
    except Exception as e:
        logger.warning(
            "[SessionTurn] get_execution_timeout failed for %s (%s) — using fallback %ds",
            agent_name,
            e,
            LOCK_TTL_FALLBACK,
        )
        return LOCK_TTL_FALLBACK


# #2214: the bound on ONE turn — distinct from the lock TTL above. Both read the
# same per-agent knob (TIMEOUT-001), but their failure directions differ, which
# is why there are two resolvers instead of one:
#   * the LOCK falls back to its CAP — over-TTL is harmless on an auto-expiring
#     key, and under-TTL lets a concurrent `--resume` corrupt the JSONL;
#   * the TURN falls back to the platform DEFAULT — the timeout allows billable
#     work, so "assume the platform default" beats "assume the maximum".
TURN_TIMEOUT_FALLBACK_SECONDS = 3600   # = the TIMEOUT-001 default (#665) — what an
                                       # agent with no stored override runs at
TURN_TIMEOUT_MIN_SECONDS = 60          # = TIMEOUT-001's PUT-validated range,
TURN_TIMEOUT_MAX_SECONDS = 7200        #   re-applied as a read-side clamp (#506
                                       #   pattern) so a stray stored row cannot
                                       #   collapse or explode the bound


def resolve_turn_timeout(agent_name: str) -> int:
    """The bound on ONE turn = the agent's own execution timeout (TIMEOUT-001).

    The per-agent knob operators already set via ``PUT /api/agents/{name}/timeout``
    (default 3600, range 60–7200). Read-side clamped into that same range and
    fail-open to the platform default — a DB hiccup must never produce a 0s or
    crashed turn. Deliberately distinct from ``resolve_lock_ttl`` (see the
    constants block above for why the fallbacks differ).
    """
    try:
        t = int(db.get_execution_timeout(agent_name))
        return max(TURN_TIMEOUT_MIN_SECONDS, min(t, TURN_TIMEOUT_MAX_SECONDS))
    except Exception as e:
        logger.warning(
            "[SessionTurn] get_execution_timeout failed for %s (%s) — using default %ds",
            agent_name,
            e,
            TURN_TIMEOUT_FALLBACK_SECONDS,
        )
        return TURN_TIMEOUT_FALLBACK_SECONDS


def get_async_redis():
    """Lazy async-Redis client. Returns None if unavailable."""
    try:
        import redis.asyncio as aioredis  # noqa: WPS433
        from config import REDIS_URL  # noqa: WPS433
    except Exception as e:
        logger.warning("[SessionTurn] Cannot import async Redis client: %s", e)
        return None
    try:
        return aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        logger.warning("[SessionTurn] Cannot construct async Redis client: %s", e)
        return None


async def set_session_inflight(session_id: str, ttl: int) -> None:
    """SET the in-flight sentinel for a session. Degrades silently."""
    redis = get_async_redis()
    if redis is None:
        return
    try:
        await redis.set(session_inflight_key(session_id), "1", ex=ttl)
    except Exception as e:
        logger.warning(
            "[SessionTurn] inflight SET failed for %s (%s) — degraded mode",
            session_id,
            e,
        )


async def clear_session_inflight(session_id: str) -> None:
    """DEL the in-flight sentinel. Best-effort; TTL is the backstop."""
    redis = get_async_redis()
    if redis is None:
        return
    try:
        await redis.delete(session_inflight_key(session_id))
    except Exception as e:
        logger.warning(
            "[SessionTurn] inflight DEL failed for %s (%s) — TTL will expire",
            session_id,
            e,
        )


async def is_turn_in_flight(session_id: str) -> bool:
    """Whether a turn is currently in flight on this session.

    Covers cold + warm turns. Returns False if Redis is unavailable (degraded
    mode — the frontend falls back to a message_count delta to detect
    completion).
    """
    redis = get_async_redis()
    if redis is None:
        return False
    try:
        return bool(await redis.exists(session_inflight_key(session_id)))
    except Exception as e:
        logger.warning(
            "[SessionTurn] inflight EXISTS failed for %s (%s) — degraded",
            session_id,
            e,
        )
        return False


class ResumeLockBusy(HTTPException):
    """Another turn holds this session's resume lock.

    Subclasses ``HTTPException`` (429) deliberately: the Session router has
    surfaced exactly that status since the lock shipped, and a caller that
    cannot map it still gets the right HTTP answer by default. Callers with
    their own error vocabulary — the Workspace, whose chat raises
    ``ClientPortalError`` — catch this precise type and translate it.
    """

    def __init__(self, key: str):
        super().__init__(
            status_code=429,
            detail={
                "error": "Another turn on this session is in progress",
                "retry_after": 5,
                "session_lock_key": key,
            },
        )


class ResumeLock:
    """Async context manager for the per-session turn lock.

    Two key shapes:
      * **warm turn** — ``session_lock:{agent}:{claude_session_id}`` keyed by
        the cached Claude UUID (via ``session_lock_key``).
      * **cold turn** — ``session_lock:cold:{session_id}`` keyed by the
        persisted session row id (#779). Cold turns previously short-circuited
        to ``key=None`` (no lock), allowing two concurrent first-turn POSTs to
        race on the cache write and orphan a JSONL.

    Acts as a no-op when Redis is unavailable (degraded mode — log and proceed;
    lock contention is an optimisation, not a correctness gate at the platform
    layer).
    """

    def __init__(
        self,
        agent_name: str,
        claude_session_id: Optional[str],
        session_id: str,
        ttl_seconds: int = LOCK_TTL_FALLBACK,
    ):
        self._key = (
            session_lock_key(agent_name, claude_session_id)
            if claude_session_id
            else f"session_lock:cold:{session_id}"
        )
        self._ttl = ttl_seconds
        self._token = secrets.token_urlsafe(16)
        self._redis = None
        self._held = False

    async def __aenter__(self) -> "ResumeLock":
        self._redis = get_async_redis()
        if self._redis is None:
            logger.warning(
                "[SessionTurn] Redis unavailable for resume lock %s — proceeding unlocked",
                self._key,
            )
            return self

        deadline = asyncio.get_event_loop().time() + LOCK_WAIT_TOTAL_SECONDS
        while True:
            try:
                acquired = await self._redis.set(
                    self._key,
                    self._token,
                    nx=True,
                    ex=self._ttl,
                )
            except Exception as e:
                logger.warning(
                    "[SessionTurn] Redis SET failed for %s (%s) — proceeding unlocked",
                    self._key,
                    e,
                )
                self._redis = None
                return self

            if acquired:
                self._held = True
                return self
            if asyncio.get_event_loop().time() >= deadline:
                raise ResumeLockBusy(self._key)
            await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        if not self._held or self._redis is None:
            return
        try:
            await self._redis.eval(LOCK_RELEASE_LUA, 1, self._key, self._token)
        except Exception as e:
            logger.warning(
                "[SessionTurn] Lock release failed for %s (%s) — TTL will expire it",
                self._key,
                e,
            )


class InflightSentinel:
    """Async context manager that brackets a turn with the sentinel.

    SET on enter, DEL on exit (success or exception). The DEL is
    guaranteed-best-effort: if Redis is down or the call fails, the TTL is the
    backstop.
    """

    def __init__(self, session_id: str, ttl_seconds: int):
        self._session_id = session_id
        self._ttl = ttl_seconds

    async def __aenter__(self) -> "InflightSentinel":
        await set_session_inflight(self._session_id, self._ttl)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        await clear_session_inflight(self._session_id)


# ---------------------------------------------------------------------------
# Resume-fallback detection (Phase 2.2)
# ---------------------------------------------------------------------------

# When `claude --resume <uuid>` cannot find the JSONL the CLI prints
# "No conversation found with session ID: ..." to stderr. The agent server
# bubbles that up as the 5xx body that lands in TaskExecutionResult.error.
# We match on the substring (case-insensitive) so a wording bump in a future
# Claude release still triggers the fallback path. E2/E3 in the design doc.
RESUME_NOT_FOUND_MARKERS = (
    "no conversation found",
    "session not found",
)


def is_resume_not_found(error_text: Optional[str]) -> bool:
    if not error_text:
        return False
    lowered = error_text.lower()
    return any(marker in lowered for marker in RESUME_NOT_FOUND_MARKERS)


# ---------------------------------------------------------------------------
# The turn itself
# ---------------------------------------------------------------------------


class ResumableTurn(NamedTuple):
    """Outcome of one resumable turn.

    ``real_uuid`` is the Claude session id the turn actually ran under — the
    value the caller must cache for the next turn. It is None when the runtime
    reported none (a stateless turn, or a failure before the session started).

    A NamedTuple rather than a dataclass, matching the chat-signal types
    (`ChatAdmission` et al.): `@dataclass` resolves its defining module out of
    `sys.modules` at class-creation time, which breaks any caller that execs
    this module from a file path without registering it — exactly what the
    unit tests do to dodge package-import pollution.
    """

    result: Any
    real_uuid: Optional[str]
    fallback_fired: bool = False
    fallback_reason: Optional[str] = None
    resumed: bool = False


async def run_resumable_turn(
    *,
    agent_name: str,
    session_key: str,
    message: str,
    cached_uuid: Optional[str],
    triggered_by: str,
    lock_ttl: Optional[int] = None,
    cold_message: Optional[str] = None,
    on_resume_failure: Optional[Callable[[], None]] = None,
    **execute_kwargs,
) -> ResumableTurn:
    """Run one turn that reattaches to ``cached_uuid`` when there is one.

    The sequence, and why each step is where it is:

    1. **Runtime gate.** A cached UUID is dropped for a runtime that has no
       ``--resume`` (Codex), because the fallback below reads Claude's failure
       wording and would mis-handle another CLI's.
    2. **Resume lock**, held across both attempts. Concurrent ``--resume`` on
       one UUID corrupts the JSONL (Anthropic #20992).
    3. **The turn**, always with ``persist_session=True`` — even a cold turn
       must write the JSONL, or turn 2 has nothing to resume.
    4. **Cold retry, once**, only when we *had* a UUID and the failure is the
       missing-JSONL marker. ``cold_message`` lets a caller send different text
       on that retry: the Workspace replays conversation history there, because
       a cold turn has no session memory to carry it.

    ``on_resume_failure`` fires before the retry so the caller can clear its
    cached UUID inside the same lock — a crash between the two leaves a stale
    id that simply re-fires this path on the next turn (self-healing).

    Raises ``ResumeLockBusy`` (429) when another turn holds the lock. Never
    raises on an agent-side failure: that arrives as ``result.status``.
    """
    resumed_with = cached_uuid
    if cached_uuid and not supports_session_resume(agent_name):
        logger.info(
            "[SessionTurn] agent=%s runtime lacks resume — running a stateless turn",
            agent_name,
        )
        resumed_with = None

    ttl = lock_ttl if lock_ttl is not None else resolve_lock_ttl(agent_name)

    from services.task_execution_service import get_task_execution_service
    service = get_task_execution_service()

    fallback_fired = False
    fallback_reason: Optional[str] = None

    async with ResumeLock(agent_name, resumed_with, session_key, ttl_seconds=ttl):
        result = await service.execute_task(
            agent_name=agent_name,
            message=message,
            triggered_by=triggered_by,
            resume_session_id=resumed_with,
            persist_session=True,
            **execute_kwargs,
        )

        if (
            resumed_with
            and getattr(result, "status", None) != "success"
            and is_resume_not_found(getattr(result, "error", None))
        ):
            fallback_fired = True
            fallback_reason = "resume_jsonl_not_found"
            if on_resume_failure is not None:
                try:
                    on_resume_failure()
                except Exception as e:  # noqa: BLE001 — bookkeeping must not eat the retry
                    logger.warning(
                        "[SessionTurn] resume-failure bookkeeping failed for %s: %s",
                        session_key, e,
                    )
            logger.warning(
                "[SessionTurn] event=session_resume_fallback agent=%s session=%s "
                "stale_uuid=%s reason=%s",
                agent_name, session_key, resumed_with, fallback_reason,
            )
            # Retry once cold. The stale UUID is gone, so the new turn writes a
            # fresh JSONL under a new id — no contention with anyone, by
            # definition.
            #
            # The retry gets its OWN execution row. Attempt 1's row is already
            # terminal (the failure above CAS-wrote it FAILED) and its
            # agent-side stream has closed, so reusing that id would write a
            # second terminal to a finished row, emit a spurious
            # agent.task.failed, and run a 30-120s turn under an id the client
            # has already stopped watching. `execute_task` creates the row when
            # none is supplied.
            retry_kwargs = {k: v for k, v in execute_kwargs.items() if k != "execution_id"}
            result = await service.execute_task(
                agent_name=agent_name,
                message=cold_message if cold_message is not None else message,
                triggered_by=triggered_by,
                resume_session_id=None,
                persist_session=True,
                **retry_kwargs,
            )
            resumed_with = None

    return ResumableTurn(
        result=result,
        real_uuid=getattr(result, "session_id", None),
        fallback_fired=fallback_fired,
        fallback_reason=fallback_reason,
        resumed=bool(resumed_with),
    )
