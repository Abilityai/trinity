"""
Session JSONL cleanup service (SESSION_TAB_2026-04 Phase 4.2).

Bounded disk growth for the Session tab. Each agent's
``~/.claude/projects/-home-developer/<uuid>.jsonl`` accumulates one file
per Claude Code session that ever ran inside it. Deleting a session row
or hitting "Reset memory" leaves the JSONL behind unless we reap it.

Two paths feed the cleanup:

1. **Synchronous best-effort reap** triggered from the session router on
   user-initiated reset/delete. The router calls ``reap_jsonl(agent, uuid)``
   directly so the deletion happens within seconds of the user click.
   Best-effort — agent unreachable or rm failure is logged, never raised
   to the user.

2. **Periodic sweep** (every 6h, default) that diffs the on-disk JSONL set
   against the keep set (every ``cached_claude_session_id`` currently
   stored on an active ``agent_sessions`` row for that agent). JSONLs older
   than the age guard whose UUID is not in the keep set are deleted.
   Catches dropped synchronous reaps, externally-modified state, and any
   churn from fallback turns that orphaned a JSONL behind a fresh UUID.

The age guard (default 1h) prevents a race where a brand-new cold turn
writes a JSONL **before** the backend has updated
``cached_claude_session_id`` in the DB — the periodic sweep would
otherwise delete it and break the next resume.

This service uses ``execute_command_in_container`` (the same primitive
used by ``git_service.py``, ``ssh_service.py``, scheduler pre-check, and
the agent terminal) — no new agent-server endpoint needed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from database import db
from services.docker_service import execute_command_in_container, list_all_agents_fast

logger = logging.getLogger(__name__)


# Match a UUID at the start of a line (output of `find -printf '%f %T@\n'`).
_UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)

# Default cwd inside the agent — matches the cwd Claude Code runs under.
# The directory name is the cwd path with `/` replaced by `-`.
_AGENT_CWD = "/home/developer"
_PROJECTS_DIR = "/home/developer/.claude/projects/-home-developer"


@dataclass
class SessionCleanupReport:
    """One cycle's outcome — emitted to logs and surfaced for /api/debug."""

    started_at: str = ""
    duration_ms: int = 0
    agents_scanned: int = 0
    jsonls_found: int = 0
    jsonls_kept_active: int = 0
    jsonls_kept_too_young: int = 0
    jsonls_deleted: int = 0
    errors: int = 0
    per_agent: dict = field(default_factory=dict)


class SessionCleanupService:
    """Periodic JSONL reaper for the Session tab.

    Inject ``poll_interval_seconds`` and ``min_age_seconds`` for tests so
    the gating windows can be tightened without restarting the process.
    """

    def __init__(
        self,
        *,
        poll_interval_seconds: int = 6 * 3600,  # 6h per design doc Phase 4.2
        min_age_seconds: int = 3600,            # 1h race guard
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self.min_age_seconds = min_age_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self.last_run_at: Optional[str] = None
        self.last_report: Optional[SessionCleanupReport] = None

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[SessionCleanup] started — interval=%ds, age_guard=%ds",
            self.poll_interval_seconds,
            self.min_age_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("[SessionCleanup] stopped")

    async def _loop(self) -> None:
        # Skip the first immediate run on startup — give the platform a
        # minute to settle (other services are also spinning up).
        await asyncio.sleep(60)
        while self._running:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[SessionCleanup] cycle failed: %s", e)
            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise

    # ---- main entry points ---------------------------------------------

    async def run_cycle(self) -> SessionCleanupReport:
        """One full sweep across all running agents."""
        if self._lock.locked():
            logger.debug("[SessionCleanup] cycle already in progress, skipping")
            return self.last_report or SessionCleanupReport()

        async with self._lock:
            return await self._run_cycle_inner()

    async def _run_cycle_inner(self) -> SessionCleanupReport:
        report = SessionCleanupReport(started_at=datetime.now(timezone.utc).isoformat())
        start = datetime.now(timezone.utc)

        try:
            all_agents = list_all_agents_fast()
        except Exception as e:
            logger.warning("[SessionCleanup] cannot list agents: %s", e)
            report.errors += 1
            self.last_report = report
            return report

        for agent in all_agents:
            agent_name = getattr(agent, "name", None)
            agent_status = getattr(agent, "status", None)
            # Only sweep running containers — `docker exec` against a stopped
            # container is wasted work (and the JSONLs persist on the volume
            # for the next start anyway).
            if not agent_name or agent_status != "running":
                continue
            try:
                per = await self._sweep_agent(agent_name)
                report.per_agent[agent_name] = per
                report.agents_scanned += 1
                report.jsonls_found += per["found"]
                report.jsonls_kept_active += per["kept_active"]
                report.jsonls_kept_too_young += per["kept_too_young"]
                report.jsonls_deleted += per["deleted"]
                report.errors += per["errors"]
            except Exception as e:
                logger.exception(
                    "[SessionCleanup] agent=%s sweep failed: %s", agent_name, e
                )
                report.errors += 1

        report.duration_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        self.last_run_at = report.started_at
        self.last_report = report
        if report.jsonls_deleted > 0 or report.errors > 0:
            logger.info(
                "[SessionCleanup] cycle done — agents=%d found=%d deleted=%d "
                "kept_active=%d kept_young=%d errors=%d duration=%dms",
                report.agents_scanned,
                report.jsonls_found,
                report.jsonls_deleted,
                report.jsonls_kept_active,
                report.jsonls_kept_too_young,
                report.errors,
                report.duration_ms,
            )
        return report

    async def _sweep_agent(self, agent_name: str) -> dict:
        """List on-disk JSONL UUIDs, diff against the keep set, reap."""
        per = {"found": 0, "kept_active": 0, "kept_too_young": 0, "deleted": 0, "errors": 0}

        try:
            keep_set = set(db.list_active_claude_session_ids(agent_name))
        except Exception as e:
            logger.warning(
                "[SessionCleanup] agent=%s could not load keep set: %s", agent_name, e
            )
            per["errors"] += 1
            return per

        # `find -printf '%f %T@\n'` lists "<filename> <mtime_epoch>" pairs.
        # mtime is what we want for the age guard — modification time, not
        # creation, so a recently-resumed session counts as fresh.
        container = f"agent-{agent_name}"
        listing = await execute_command_in_container(
            container,
            f"sh -c 'find {shlex.quote(_PROJECTS_DIR)} -maxdepth 1 -type f -name \"*.jsonl\" "
            f"-printf \"%f %T@\\n\" 2>/dev/null || true'",
            timeout=30,
        )
        if listing.get("exit_code", 1) != 0:
            # Directory doesn't exist yet (no sessions ever), or container
            # isn't reachable. Either way, nothing to do.
            return per

        cutoff_epoch = (
            datetime.now(timezone.utc) - timedelta(seconds=self.min_age_seconds)
        ).timestamp()
        reap_paths: List[str] = []

        for line in (listing.get("output") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            filename, mtime_str = parts
            if not filename.endswith(".jsonl"):
                continue
            uuid_part = filename[:-6]  # strip ".jsonl"
            if not _UUID_RE.match(uuid_part):
                continue
            per["found"] += 1

            if uuid_part in keep_set:
                per["kept_active"] += 1
                continue
            try:
                mtime = float(mtime_str)
            except ValueError:
                continue
            if mtime > cutoff_epoch:
                # Brand-new file (race window with cold turn that hasn't
                # cached yet). Skip this cycle; we'll catch it next time.
                per["kept_too_young"] += 1
                continue
            reap_paths.append(f"{_PROJECTS_DIR}/{filename}")

        if reap_paths:
            for path in reap_paths:
                rm = await execute_command_in_container(
                    container, f"rm -f {shlex.quote(path)}", timeout=10
                )
                if rm.get("exit_code") == 0:
                    per["deleted"] += 1
                else:
                    per["errors"] += 1
                    logger.warning(
                        "[SessionCleanup] agent=%s rm failed for %s: %s",
                        agent_name,
                        path,
                        (rm.get("output") or "")[:200],
                    )

        return per

    # ---- synchronous best-effort path (called from router) -------------

    async def reap_jsonl(self, agent_name: str, claude_session_id: str) -> bool:
        """Delete a single JSONL right now. Best-effort; never raises.

        Returns True if the rm succeeded, False on any failure (container
        unreachable, file already gone, permissions, etc.). The router
        logs the outcome but does not surface it to the user — the
        periodic sweep will retry.
        """
        if not claude_session_id or not _UUID_RE.match(claude_session_id):
            return False
        path = f"{_PROJECTS_DIR}/{claude_session_id}.jsonl"
        container = f"agent-{agent_name}"
        try:
            result = await execute_command_in_container(
                container, f"rm -f {shlex.quote(path)}", timeout=10
            )
            ok = result.get("exit_code") == 0
            if not ok:
                logger.warning(
                    "[SessionCleanup] reap_jsonl failed agent=%s uuid=%s: %s",
                    agent_name,
                    claude_session_id,
                    (result.get("output") or "")[:200],
                )
            return ok
        except Exception as e:
            logger.warning(
                "[SessionCleanup] reap_jsonl exception agent=%s uuid=%s: %s",
                agent_name,
                claude_session_id,
                e,
            )
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_session_cleanup_service: Optional[SessionCleanupService] = None


def get_session_cleanup_service() -> SessionCleanupService:
    global _session_cleanup_service
    if _session_cleanup_service is None:
        _session_cleanup_service = SessionCleanupService()
    return _session_cleanup_service
