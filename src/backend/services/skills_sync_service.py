"""
Skills Library Lifecycle Automation (trinity-enterprise#236).

Two behaviors, both default-OFF, both riding primitives that already exist:

1. **Scheduled auto-sync** — periodically runs the SAME ``sync_library()`` the
   Settings button calls. Cross-worker leader-locked so ``--workers 2`` doesn't
   double-clone.
2. **Fleet-wide re-inject** — after a sync that actually moved the library
   commit, re-inject assigned skills across running agents with ``force=False``,
   so the ent#183 tree-SHA skip makes unchanged skills free.

**Why the backend and not the standalone scheduler.** The scheduler container
sits on ``trinity-platform`` only and deliberately never talks to agents
(Network Topology) — the fleet sweep must. Hosting the timer here and the sweep
there would split one operation across two processes for no gain, so both live
here behind a Redis leader lease (the #1464 ``monitoring:leader`` shape).

Reporting is honest by construction: every agent's outcome is recorded, the
aggregate is persisted for the Settings panel, and a run with ≥1 failure raises
an operator alarm. A silent fleet sweep is worse than no fleet sweep — the
operator would believe the library reached agents it never reached.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from database import db
from redis_breaker_util import get_breaker_redis
from services.settings_service import (
    get_skills_auto_sync_interval,
    is_skills_auto_reinject_enabled,
    is_skills_auto_sync_enabled,
)
from services.skill_service import (
    RECONCILE_ALARM_AGENT_NAME,
    SkillInjectionBusy,
    skill_service,
    skills_alarm_last_read_before,
)

logger = logging.getLogger(__name__)

# One leader across all uvicorn workers — whoever holds it runs the cycle.
_LEADER_KEY = "skills:sync:leader"

# Persisted aggregate of the last fleet sweep, rendered in Settings.
FLEET_LAST_RUN_KEY = "skills_fleet_reinject_last_run"

# The fleet sweep's alarm is ONE condition for the whole fleet (#3254): the
# subject `skills_fleet_reinject:fleet`. The key equals the legacy parser's
# constant (`platform_alerts._LEGACY_PARSERS`), so the upgrade sweep stamps a
# pre-move row with the subject this module now files (R1).
FLEET_ALERT_KEY = "fleet"
# The alarm's context names every failed agent (the clear's evidence, #3254
# R10), apart from the report's 25-name display list; capped well inside the
# operator queue's 64 KiB context belt. A longer list is stored capped, and its
# row is then left to its lifetime (the clear cannot see every failed agent).
FLEET_ALERT_MAX_AGENTS = 500

# Idle poll while auto-sync is disabled. Short enough that flipping the toggle
# takes effect promptly, long enough to cost nothing.
_DISABLED_POLL_SECONDS = 60


def _fleet_concurrency() -> int:
    """Max agents re-injected in parallel (default 5).

    Each slot holds a per-agent Redis lock and pushes tar payloads into a
    container; an unbounded ``gather`` over a large fleet would contend with
    live traffic on exactly the shared resources (Docker, the agent HTTP
    surface) that agents need to serve their own work.
    """
    raw = os.getenv("SKILLS_FLEET_INJECT_CONCURRENCY", "")
    try:
        value = int(raw)
        return value if value > 0 else 5
    except (TypeError, ValueError):
        return 5


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SkillsLibrarySyncService:
    """Background auto-sync + fleet re-inject loop (ent#236)."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Unique per worker process so the leader lease is only ever refreshed
        # or released by its own holder.
        self._worker_id = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._is_leader = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Skills library sync service started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        self._release_leadership()
        logger.info("Skills library sync service stopped")

    # -------------------------------------------------------------------------
    # Leadership (mirrors monitoring_service #1464)
    # -------------------------------------------------------------------------

    def _try_acquire_leadership(self, ttl: int) -> bool:
        """True iff this worker owns the lease for this cycle.

        Fail-open on a Redis error: a duplicated git pull into the same clone is
        wasteful but harmless, whereas failing closed would silently stop
        auto-sync entirely — the mode the operator cannot see.
        """
        r = get_breaker_redis()
        if r is None:
            return True
        try:
            if r.set(_LEADER_KEY, self._worker_id, nx=True, ex=ttl):
                return True
            if r.get(_LEADER_KEY) == self._worker_id:
                r.expire(_LEADER_KEY, ttl)
                return True
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("skills sync leader lock check failed-open (%s)", e)
            return True

    def _release_leadership(self) -> None:
        try:
            r = get_breaker_redis()
            if r is not None and r.get(_LEADER_KEY) == self._worker_id:
                r.delete(_LEADER_KEY)
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------------------
    # Loop
    # -------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            sleep_for = _DISABLED_POLL_SECONDS
            try:
                # Config is re-read EVERY cycle (never cached at start): that is
                # what lets an admin change the interval or flip the flag and
                # have it apply without a backend restart.
                enabled = await asyncio.to_thread(is_skills_auto_sync_enabled)
                if enabled:
                    interval = await asyncio.to_thread(get_skills_auto_sync_interval)
                    sleep_for = interval
                    # 3× interval so one or two missed refreshes don't drop
                    # leadership, but a dead holder's lease expires within ~3
                    # cycles and a sibling takes over.
                    leader = self._try_acquire_leadership(max(1, interval) * 3)
                    if leader and not self._is_leader:
                        logger.info(
                            "Skills auto-sync acquired leadership (worker %s)",
                            self._worker_id,
                        )
                    elif not leader and self._is_leader:
                        logger.info(
                            "Skills auto-sync yielded leadership (worker %s)",
                            self._worker_id,
                        )
                    self._is_leader = leader
                    if leader:
                        await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the loop
                logger.error(f"Skills auto-sync cycle failed: {e}")

            await asyncio.sleep(max(1, sleep_for))

    async def run_cycle(self) -> Dict[str, Any]:
        """One scheduled sync (+ optional fleet sweep). Never raises."""
        # sync_library is synchronous git subprocess work — off the event loop.
        result = await asyncio.to_thread(skill_service.sync_library)

        if result.get("busy"):
            # An admin's manual sync holds the clone lock. Not a failure, not
            # worth a WARNING — the next tick picks it up.
            logger.info("Skills library auto-sync skipped: another sync is running")
            return {"synced": False, "busy": True}

        if not result.get("success"):
            # No alarm here by design: the failure is already durable in
            # `skills_library_last_*` and rendered in the Settings panel, and a
            # repeating operator-queue item for an unreachable GitHub would be
            # the muted-alert failure mode (#1644 lesson).
            logger.warning(
                "Skills library auto-sync failed: %s", result.get("error")
            )
            return {"synced": False, "error": result.get("error")}

        await self._audit_sync(result)

        if not result.get("commit_changed"):
            return {"synced": True, "commit_changed": False, "fleet": None}

        if not await asyncio.to_thread(is_skills_auto_reinject_enabled):
            return {"synced": True, "commit_changed": True, "fleet": None}

        report = await self.run_fleet_reinject(commit_sha=result.get("commit_sha"))
        return {"synced": True, "commit_changed": True, "fleet": report}

    # -------------------------------------------------------------------------
    # Fleet re-inject
    # -------------------------------------------------------------------------

    async def run_fleet_reinject(
        self, commit_sha: Optional[str] = None, trigger: str = "auto_sync"
    ) -> Dict[str, Any]:
        """Re-inject assigned skills across running agents. Never raises.

        ``force=False`` deliberately: the ent#183 version check turns an agent
        whose skills are already current into a metadata read, so a sweep after
        a one-skill change costs one transfer, not a fleet's worth.
        """
        started_at = _utc_now_iso()
        agents = await asyncio.to_thread(self._eligible_agents)
        semaphore = asyncio.Semaphore(_fleet_concurrency())
        results: Dict[str, Dict[str, Any]] = {}

        # ent#530: resolve the library's sets ONCE per sweep, not per agent.
        try:
            from services import skill_set_service
            sets_lib = await asyncio.to_thread(skill_set_service.library_sets)
        except Exception:  # noqa: BLE001
            sets_lib = None

        async def _one(agent_name: str) -> None:
            async with semaphore:
                results[agent_name] = await self._reinject_agent(agent_name, sets_lib)

        if agents:
            await asyncio.gather(
                *(_one(name) for name in agents), return_exceptions=True
            )

        failures = {
            name: r.get("error", "unknown")
            for name, r in results.items()
            if r.get("status") == "failed"
        }
        report = {
            "started_at": started_at,
            "finished_at": _utc_now_iso(),
            "trigger": trigger,
            "commit_sha": commit_sha,
            "agents_total": len(agents),
            "agents_injected": sum(
                1 for r in results.values() if r.get("status") == "injected"
            ),
            "agents_skipped": sum(
                1 for r in results.values() if r.get("status") == "skipped"
            ),
            "agents_failed": len(failures),
            # Bounded: this blob is rendered in a panel and stored in a settings
            # row, not a log sink.
            "failures": dict(sorted(failures.items())[:25]),
        }

        self._persist_report(report)
        await self._audit_fleet(report)
        if failures:
            self._announce_fleet_failures(report, sorted(failures))
        elif self._sweep_reached_every_agent(agents, results):
            self._clear_fleet_failures(started_at, set(agents))
        logger.info(
            "Skills fleet re-inject: %s agents, %s injected, %s skipped, %s failed",
            report["agents_total"], report["agents_injected"],
            report["agents_skipped"], report["agents_failed"],
        )
        return report

    @staticmethod
    def _eligible_agents() -> List[str]:
        """Running agents that should receive a fleet re-inject.

        Stopped agents are deliberately out of scope — the existing start path
        already injects with ``force=False``, so they pick the update up for
        free the moment they run, and pushing to a stopped container would only
        manufacture failures.

        Ephemeral ghosts are excluded (the #69 fleet-hygiene precedent):
        they are disposable, budgeted, and frequently mid-discard, so sweeping
        them adds noise and races the discard lock for no benefit.
        """
        try:
            from services.docker_service import list_all_agents_fast

            names: List[str] = []
            for agent in list_all_agents_fast():
                if getattr(agent, "status", None) != "running":
                    continue
                name = getattr(agent, "name", None)
                if not name:
                    continue
                try:
                    info = db.get_agent_ephemeral_info(name)
                    if isinstance(info, dict) and info.get("is_ephemeral"):
                        continue
                except Exception:  # noqa: BLE001 — unknown ⇒ treat as durable
                    pass
                names.append(name)
            return sorted(names)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"could not enumerate agents for fleet re-inject: {e}")
            return []

    @staticmethod
    async def _reinject_agent(agent_name: str, sets_lib: Optional[dict] = None) -> Dict[str, Any]:
        """One agent's re-inject, with every failure mode named.

        ent#530 order: set-derived rows reconciled (DB, fail-closed) → names read →
        inject (lock) → prune outside the lock when the set reconcile dropped
        members, so a member removed upstream leaves a RUNNING agent now rather
        than at its next restart.
        """
        try:
            dropped: list = []
            if sets_lib is not None:
                from services import skill_set_service
                _, dropped = await asyncio.to_thread(skill_set_service.reconcile_agent, agent_name, sets_lib)
            # trinity-enterprise#753: a library change can newly recommend
            # approval for a skill this agent holds — gated before the re-inject;
            # defaults of members the sync dropped go after the prune removed
            # their packages (never raises).
            from services import skill_gate_map_service
            gate_ctx = skill_gate_map_service.GateContext(trigger="library_sync")
            await skill_gate_map_service.reconcile_library_gates(agent_name, ctx=gate_ctx)
            skill_names = await asyncio.to_thread(db.get_agent_skill_names, agent_name)
            if not skill_names:
                if dropped:
                    prune = await skill_service.reconcile_agent_skills(agent_name, [])
                    await skill_gate_map_service.reconcile_library_gates(
                        agent_name, ctx=gate_ctx, add=False,
                        drop_defaults=skill_gate_map_service.drop_after_prune(prune))
                return {"status": "skipped", "reason": "no_skills"}
            result = await skill_service.inject_skills(agent_name, skill_names, force=False)
            if dropped:
                prune = await skill_service.reconcile_agent_skills(agent_name, skill_names)
                await skill_gate_map_service.reconcile_library_gates(
                    agent_name, ctx=gate_ctx, add=False,
                    drop_defaults=skill_gate_map_service.drop_after_prune(prune))
            # #2703: the sweep changes the listing — open surfaces refetch.
            from services.skill_service import broadcast_skills_changed
            await broadcast_skills_changed(agent_name)
            if not result.get("success"):
                # Per-skill detail already rides the result map and the agent's
                # own injection log; the fleet report keeps a bounded summary.
                return {
                    "status": "failed",
                    "error": f"{result.get('skills_failed', 0)} skill(s) failed",
                }
            return {
                "status": "injected",
                "injected": result.get("skills_injected", 0),
                "unchanged": result.get("skills_unchanged", 0),
            }
        except SkillInjectionBusy:
            # Skip-and-report, never wait: the agent is already mid-injection
            # (a start, or a manual sync), and that injection is reading the same
            # library clone we just updated.
            return {"status": "skipped", "reason": "busy"}
        except Exception as e:  # noqa: BLE001
            return {"status": "failed", "error": str(e)[:200]}

    # -------------------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------------------

    @staticmethod
    def _persist_report(report: Dict[str, Any]) -> None:
        """Store the aggregate for the Settings panel. Never raises."""
        try:
            db.set_setting(FLEET_LAST_RUN_KEY, json.dumps(report)[:8000])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"could not persist fleet re-inject report: {e}")

    @staticmethod
    def _announce_fleet_failures(report: Dict[str, Any],
                                 failed_agents: Optional[List[str]] = None) -> None:
        """Operator alarm for a partially-failed sweep. Never raises.

        Carries counts and agent NAMES only — never per-skill errors, which can
        embed library paths and agent output.

        #3254: one platform condition for the fleet,
        `skills_fleet_reinject:fleet` on the `_skills-sync` host. Each bad run
        updates the one pending row in place (the latest counts, agents and
        commit; seen count, last-seen time), where the old id carried the run's
        finish timestamp and filed every bad run as its own row that nothing
        ever ended. A sweep that reached every agent ends the row
        (`_clear_fleet_failures`). The kind's 14-day lifetime runs from the
        latest bad run; sweeps run only when a sync moves the library commit,
        so a failure no later sweep re-reads leaves the queue after 14 days
        even if the agent was never fixed (accepted in the #3254 plan; the
        Settings panel keeps the last report).
        """
        try:
            from services import platform_alerts

            message = (
                f"[Skills] Fleet re-inject completed with {report['agents_failed']} "
                f"failed agent(s) of {report['agents_total']} at commit "
                f"{report.get('commit_sha') or 'unknown'}. Affected: "
                f"{', '.join(sorted(report['failures'])) or 'unknown'}."
            )
            platform_alerts.observe(
                RECONCILE_ALARM_AGENT_NAME, "skills_fleet_reinject", FLEET_ALERT_KEY,
                priority="medium",
                title="Skills fleet re-inject partially failed",
                question=message,
                context={
                    "alert_type": "skills_fleet_reinject_failed",
                    "agents_total": report["agents_total"],
                    "agents_failed": report["agents_failed"],
                    "commit_sha": report.get("commit_sha"),
                    # every failed agent (capped), not the report's 25: the
                    # clear must see each one current again (R10)
                    "agents": (sorted(failed_agents) if failed_agents is not None
                               else sorted(report["failures"]))[:FLEET_ALERT_MAX_AGENTS],
                },
            )
        except Exception as e:  # noqa: BLE001 — the alarm is decorative
            logger.warning(f"could not raise fleet re-inject alarm: {e}")

    @staticmethod
    def _sweep_reached_every_agent(
        agents: List[str], results: Dict[str, Dict[str, Any]]
    ) -> bool:
        """Positive evidence that the fleet condition cleared (#3254, R10).

        The absence of a failure is not a recovery. Three sweeps report zero
        failures without having seen the fleet current, and none of them may
        end the alarm:

        * an EMPTY enumeration — `_eligible_agents` returns ``[]`` on a Docker
          error exactly as on an idle fleet (the #2196 class);
        * an agent skipped as ``busy`` — it was never re-injected, so whether
          its earlier failure is fixed is unknown;
        * an agent with no result at all (``gather`` swallowed an exception).

        So: at least one agent, and every enumerated agent reported injected,
        or skipped because it holds no skills. An outcome this list does not
        name never counts as evidence.
        """
        def _current(outcome: Any) -> bool:
            if not isinstance(outcome, dict):
                return False
            if outcome.get("status") == "injected":
                return True
            return outcome.get("status") == "skipped" and outcome.get("reason") == "no_skills"

        return bool(agents) and all(_current(results.get(name)) for name in agents)

    @staticmethod
    def _clear_fleet_failures(sweep_started_at: str, reached: Set[str]) -> None:
        """A sweep reached every running agent and saw each one current: the
        platform ends the fleet alarm's row (#3254). Sweeps overlap, so a
        failure read after this sweep started — a newer sweep's — is newer
        than this sweep's evidence and stays for the next sweep
        (`skills_alarm_last_read_before`). Every agent the row names must be
        among those this sweep `reached`: an agent that failed and then
        stopped is not enumerated, and a sweep that never looked at it is no
        evidence that it recovered (R10). The row's list must name every failed
        agent: the report keeps at most 25 names, so a row whose failure count
        exceeds its list, or whose list is missing or malformed, is unknown
        evidence and is left to its lifetime. On an install that never raised it
        this is one read and no write. Never raises."""
        from services import platform_alerts

        def _names_reached(row: Dict[str, Any]) -> bool:
            context = row.get("context") or {}
            named = context.get("agents")
            if not (isinstance(named, list) and named
                    and all(isinstance(n, str) and n for n in named)):
                return False
            failed = context.get("agents_failed")
            if not isinstance(failed, int) or isinstance(failed, bool) or failed > len(named):
                return False
            return set(named) <= reached

        if skills_alarm_last_read_before("skills_fleet_reinject", FLEET_ALERT_KEY,
                                         sweep_started_at, row_reached=_names_reached):
            platform_alerts.clear(RECONCILE_ALARM_AGENT_NAME, "skills_fleet_reinject",
                                  FLEET_ALERT_KEY)

    @staticmethod
    async def _audit_sync(result: Dict[str, Any]) -> None:
        try:
            from services.platform_audit_service import (
                platform_audit_service, AuditEventType,
            )
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="skills_library_auto_sync",
                source="system",
                target_type="skills_library",
                target_id=result.get("commit_sha") or "unknown",
                details={
                    # ent#237: a sweep spans N sources, so there is no single
                    # `action` to record. The per-source breakdown is the honest
                    # form — a lone top-level action would name one arbitrary
                    # source's outcome and read as the whole library's.
                    "sources": [
                        {
                            "source_id": s.get("source_id"),
                            "name": s.get("name"),
                            "action": s.get("action"),
                            "success": bool(s.get("success")),
                            "commit_sha": s.get("commit_sha"),
                            "commit_changed": bool(s.get("commit_changed")),
                        }
                        for s in (result.get("sources") or [])
                    ],
                    "synced": result.get("synced"),
                    "failed": result.get("failed"),
                    "commit_sha": result.get("commit_sha"),
                    "commit_changed": bool(result.get("commit_changed")),
                    "skill_count": result.get("skill_count"),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _audit_fleet(report: Dict[str, Any]) -> None:
        try:
            from services.platform_audit_service import (
                platform_audit_service, AuditEventType,
            )
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="skills_fleet_reinject",
                source="system",
                target_type="skills_library",
                target_id=report.get("commit_sha") or "unknown",
                details={
                    "trigger": report.get("trigger"),
                    "agents_total": report.get("agents_total"),
                    "agents_injected": report.get("agents_injected"),
                    "agents_skipped": report.get("agents_skipped"),
                    "agents_failed": report.get("agents_failed"),
                },
            )
        except Exception:  # noqa: BLE001
            pass


# Global service instance
skills_sync_service = SkillsLibrarySyncService()
