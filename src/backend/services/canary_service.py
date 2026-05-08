"""
Canary watcher service (CANARY-001 / Issue #411).

Runs in the backend process. Every 5 minutes:

1. `collect_snapshot()` — read Redis, SQLite, agent registries.
2. `run_invariants(snapshot)` — apply S-01 / E-02 / L-03 (Phase 1 set).
3. Persist any violations to `canary_violations`.
4. Detect green→red transitions per invariant against the previously-stored
   latest violation; fire one Slack alert per transition via incoming
   webhook (`CANARY_SLACK_WEBHOOK_URL` env var; unset = silent sink).

Modeled on `services/cleanup_service.py` — single asyncio task, idempotent
start/stop, lock-guarded re-entrancy. Disabled by default; enable per
deployment with `CANARY_ENABLED=1`. Production deployment is staging/dev.

Why a service and not a Trinity agent (Issue #411 design discussion):
the watcher does no LLM reasoning — it's a deterministic library invocation
on a 5-minute timer. Running it as a Trinity agent would add an LLM call
per cycle and a separate container for no benefit. Deterministic checks
belong in the backend; the agents are the *fleet* (load generators), which
are deployed via the canary-fleet manifest.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from canary import collect_snapshot, run_invariants
from canary.snapshot import ViolationReport
from database import db


@dataclass
class CycleResult:
    """Outcome of one canary cycle.

    `violations` is the per-invariant list of `ViolationReport`s the
    deterministic library produced (now persisted to `canary_violations`).

    `transition_invariant_ids` is the subset the service classified as
    a fresh green→red flip this cycle, not a continuation of an
    already-known violation. The router exposes this directly to
    operators so the on-demand `/api/canary/run-cycle` response matches
    what the alert sink (when wired) will see.

    `persisted_violation_ids` is index-aligned with `violations`: for
    each `ViolationReport` in `violations[inv_id][i]`, the row id
    returned by `insert_canary_violation` is at
    `persisted_violation_ids[inv_id][i]` — or `None` if the insert
    failed. Lets the router surface row ids without re-querying.
    """

    violations: Dict[str, List[ViolationReport]] = field(default_factory=dict)
    persisted_violation_ids: Dict[str, List[Optional[int]]] = field(default_factory=dict)
    transition_invariant_ids: List[str] = field(default_factory=list)
    # snapshot_time of the most recent prior violation per transitioning
    # invariant — used by the alert sink to render "last red 2h ago" and
    # by the run-cycle endpoint to surface `previous_violation_at`. Only
    # populated for invariants in `transition_invariant_ids`. Value is
    # `None` if this is the first-ever violation for that invariant.
    previous_violation_at: Dict[str, Optional[str]] = field(default_factory=dict)
    snapshot_time: str = ""
    sources_unavailable: List[str] = field(default_factory=list)

logger = logging.getLogger(__name__)


# Five-minute cadence per the design doc. Deliberately the same as
# cleanup_service to share the operator's mental model (both are "every
# 5 min the backend reconciles state").
CANARY_INTERVAL_SECONDS = 300

# Redis key holding the snapshot_time of the most recent cycle that ran.
# Used by transition detection so a continuously-red invariant fires a
# notification once (on the first cycle that catches it) rather than every
# cycle thereafter — see `_run_cycle_inner` for the rule.
REDIS_KEY_LAST_CYCLE = "canary:last_cycle_at"


class CanaryService:
    """Background watcher loop for the canary invariant harness."""

    def __init__(self, interval_seconds: int = CANARY_INTERVAL_SECONDS):
        self.interval = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        # Counters surface in /api/health-style monitoring; useful for
        # confirming the service is actually firing on deployed instances.
        self.cumulative_cycles: int = 0
        self.cumulative_violations: int = 0
        self.cumulative_transitions: int = 0
        self.last_run_at: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the background loop. No-op if already running or disabled."""
        if not self._is_enabled():
            logger.info("canary: disabled (set CANARY_ENABLED=1 to enable)")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"canary watcher started (interval={self.interval}s)")

    def stop(self):
        """Stop the background loop cleanly."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("canary watcher stopped")

    @staticmethod
    def _is_enabled() -> bool:
        return os.getenv("CANARY_ENABLED", "0") == "1"

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _loop(self):
        """Loop forever: cycle → sleep → cycle."""
        # Short delay so the backend is fully ready before the first cycle —
        # avoids a noisy "sources_unavailable" log line during cold start.
        await asyncio.sleep(30)
        while self._running:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let a cycle exception kill the loop. Log and retry
                # next interval. Mirrors cleanup_service.
                logger.exception("canary cycle raised; will retry next interval")
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # One cycle
    # ------------------------------------------------------------------

    async def run_cycle(
        self,
        invariant_ids: Optional[Iterable[str]] = None,
    ) -> CycleResult:
        """Run one canary cycle. Public so it can be invoked from tests
        or by the operator via `POST /api/canary/run-cycle`.

        Returns a `CycleResult` carrying the per-invariant violation
        lists this cycle produced *and* the subset classified as
        green→red transitions. Both pieces of truth come from the same
        code path the background loop uses, so the on-demand endpoint
        cannot disagree with the alert sink (when wired).
        """
        if self._lock.locked():
            logger.debug("canary: cycle already in progress, skipping")
            return CycleResult()
        async with self._lock:
            return await self._run_cycle_inner(invariant_ids)

    async def _run_cycle_inner(
        self,
        invariant_ids: Optional[Iterable[str]],
    ) -> CycleResult:
        # Capture pre-cycle state for green→red transition detection.
        # Two reads BEFORE the cycle runs:
        #   1. previous_latest — the most recent persisted violation per
        #      invariant id. Tells us "when was this invariant last red".
        #   2. prev_cycle_at — snapshot_time of the prior cycle (any cycle,
        #      regardless of outcome). Tells us "when did we last look".
        #
        # An invariant transition (green→red) fires only when (a) the
        # invariant has violations this cycle AND (b) the previous cycle
        # was green for it — i.e. the latest stored violation predates
        # the previous cycle's snapshot. This is the only rule that
        # silences continuously-red invariants without losing real
        # green→red flips, including red→green→red.
        previous_latest = db.get_latest_canary_violation_per_invariant()
        prev_cycle_at = self._read_prev_cycle_at()

        # Heavy work — synchronous SQLite + Redis reads. Offload to a thread
        # so we don't block the asyncio loop while sqlite3 is blocking.
        snapshot = await asyncio.to_thread(collect_snapshot)
        results = await asyncio.to_thread(run_invariants, snapshot, invariant_ids)

        persisted_count = 0
        # Index-aligned with `results[inv_id]` — `None` slot means insert
        # failed. The router uses these ids directly instead of re-querying
        # by (invariant_id, snapshot_time).
        persisted_ids: Dict[str, List[Optional[int]]] = {}
        for inv_id, vlist in results.items():
            inv_ids: List[Optional[int]] = []
            for v in vlist:
                try:
                    row_id = db.insert_canary_violation(
                        invariant_id=v.invariant_id,
                        tier=v.tier,
                        severity=v.severity,
                        snapshot_time=snapshot.snapshot_time,
                        observed_state=v.observed_state,
                        signal_query=v.signal_query,
                    )
                    inv_ids.append(row_id)
                    persisted_count += 1
                except Exception:
                    inv_ids.append(None)
                    logger.exception(
                        "canary: failed to persist violation %s; continuing",
                        v.invariant_id,
                    )
            persisted_ids[inv_id] = inv_ids

        # Detect green→red transitions and emit one notification per.
        transition_ids: List[str] = []
        previous_violation_at: Dict[str, Optional[str]] = {}
        for inv_id, vlist in results.items():
            if not vlist:
                continue
            if not self._is_green_to_red(inv_id, previous_latest, prev_cycle_at):
                continue
            # Capture the prior snapshot_time BEFORE emit so the alert
            # sink can render "last red Xm ago". `previous_latest` was
            # loaded pre-cycle (line ~214) and is None when this is the
            # first-ever violation for the invariant — pass that through
            # honestly rather than papering over with the current cycle.
            prev = previous_latest.get(inv_id) or {}
            previous_violation_at[inv_id] = prev.get("snapshot_time")
            try:
                await self._emit_transition(
                    inv_id,
                    vlist,
                    snapshot.snapshot_time,
                    previous_violation_at[inv_id],
                    persisted_ids.get(inv_id, []),
                )
                transition_ids.append(inv_id)
            except Exception:
                logger.exception(
                    "canary: failed to emit transition notification for %s",
                    inv_id,
                )

        # Update counters + last-run.
        self.cumulative_cycles += 1
        self.cumulative_violations += persisted_count
        self.cumulative_transitions += len(transition_ids)
        self.last_run_at = snapshot.snapshot_time
        # Persist this cycle's snapshot_time for the NEXT cycle's transition
        # check. Done AFTER notifications so a crash mid-emit doesn't
        # advance the cursor and silence a real transition on retry.
        self._write_prev_cycle_at(snapshot.snapshot_time)

        if persisted_count or snapshot.sources_unavailable:
            logger.info(
                "canary cycle: violations=%d transitions=%d unavailable=%s",
                persisted_count,
                len(transition_ids),
                snapshot.sources_unavailable,
            )

        return CycleResult(
            violations=results,
            persisted_violation_ids=persisted_ids,
            transition_invariant_ids=transition_ids,
            previous_violation_at=previous_violation_at,
            snapshot_time=snapshot.snapshot_time,
            sources_unavailable=list(snapshot.sources_unavailable),
        )

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    # Severity → Slack emoji. Common monitoring convention; rendered in
    # the header block so the alert is scannable at a glance even with
    # the channel collapsed in the sidebar.
    _SEVERITY_EMOJI = {
        "critical": "🚨",
        "major": "⚠️",
        "minor": "🟡",
    }

    # Friendly invariant names — paired with the catalog at
    # docs/testing/orchestration-invariant-catalog.md. The bare ID
    # (S-01, E-02, …) is opaque to anyone not steeped in the catalog;
    # the name is what makes the Slack alert immediately interpretable.
    _INVARIANT_NAMES = {
        "S-01": "Slot–row bijection",
        "E-02": "Phantom execution reversal",
        "L-03": "Delete cascades",
    }

    # One-line runbook hint per invariant. Kept short on purpose —
    # the alert is the entry point, the catalog has the full prose.
    # Tells the on-call where to start looking, not what to do.
    _INVARIANT_RUNBOOKS = {
        "S-01": (
            "Redis slot ZSET diverged from running schedule_executions rows. "
            "Inspect for crashed `slot.release()` calls; `cleanup_service` "
            "should reconcile within one cycle."
        ),
        "E-02": (
            "An execution went terminal then non-terminal. Look for retry "
            "logic that resurrects completed rows or a status-write race."
        ),
        "L-03": (
            "An agent was deleted but a referencing row wasn't cascaded. "
            "Check the delete handler for the table(s) listed above."
        ),
    }

    async def _emit_transition(
        self,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
        previous_violation_at: Optional[str],
        persisted_ids: List[Optional[int]],
    ) -> None:
        """Fire a Slack alert for a green→red transition.

        Reads the webhook URL from the `CANARY_SLACK_WEBHOOK_URL` env var.
        If unset, logs at debug and returns — green→red detection still
        runs and rows are still persisted to `canary_violations`, the
        sink is just silent. Mirrors the `CANARY_ENABLED` env-gating
        pattern for the watcher itself.

        The webhook URL is the credential. We don't echo it in any log
        line. Failures are logged and swallowed so a hung webhook can't
        break the cycle — `slack_service.post_webhook` already enforces
        a 5s timeout.
        """
        webhook_url = os.getenv("CANARY_SLACK_WEBHOOK_URL", "").strip()
        if not webhook_url:
            # Emit a structured debug line so operators can confirm the
            # transition was *detected* even when alerts are silent.
            worst = max(violations, key=lambda v: _severity_rank(v.severity))
            logger.debug(
                "canary transition (slack disabled — set CANARY_SLACK_WEBHOOK_URL): "
                "%s severity=%s violations_in_cycle=%d snapshot_time=%s",
                invariant_id,
                worst.severity,
                len(violations),
                snapshot_time,
            )
            return

        worst = max(violations, key=lambda v: _severity_rank(v.severity))
        text, blocks = self._build_slack_payload(
            invariant_id,
            violations,
            snapshot_time,
            previous_violation_at,
            worst.severity,
            persisted_ids,
        )

        # Lazy import — avoids dragging the SlackService init (and its
        # httpx client) into test paths that exercise the canary library
        # without the wider services tree.
        from services.slack_service import slack_service

        success, error = await slack_service.post_webhook(webhook_url, text, blocks=blocks)
        if not success:
            logger.warning(
                "canary slack webhook failed for %s: %s (cycle continues, row persisted)",
                invariant_id,
                error,
            )
        else:
            logger.info(
                "canary slack alert sent: %s severity=%s violations_in_cycle=%d",
                invariant_id,
                worst.severity,
                len(violations),
            )

    @classmethod
    def _build_slack_payload(
        cls,
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
        previous_violation_at: Optional[str],
        severity: str,
        persisted_ids: List[Optional[int]],
    ) -> tuple:
        """Compose the Slack message text + Block Kit blocks.

        Layout: header → summary → forensic detail → runbook hint →
        context (snapshot_time, count, last red, row ids). Tests
        identify blocks by `type` rather than index so adding/removing
        sections doesn't break them.

        Returns `(text, blocks)` — `text` is the fallback used by
        clients that don't render blocks (notifications, screen
        readers).
        """
        emoji = cls._SEVERITY_EMOJI.get(severity, "•")
        name = cls._INVARIANT_NAMES.get(invariant_id, invariant_id)
        body = cls._render_message(invariant_id, violations, snapshot_time)
        forensic = cls._render_forensic(invariant_id, violations)
        runbook = cls._INVARIANT_RUNBOOKS.get(invariant_id)
        last_red = cls._format_last_red(previous_violation_at, snapshot_time)
        row_ref = cls._format_row_refs(persisted_ids)

        text = f"{emoji} canary {invariant_id} {name} ({severity}): {body}"
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {invariant_id} {name} — {severity}",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            },
        ]
        if forensic:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": forensic},
            })
        if runbook:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{runbook}_"},
            })
        # Context line: row refs first if present (most actionable),
        # then snapshot_time + count + last-red badge.
        ctx_parts: List[str] = []
        if row_ref:
            ctx_parts.append(row_ref)
        ctx_parts.extend([
            f"`{snapshot_time}`",
            f"{len(violations)} violation(s) this cycle",
            last_red,
        ])
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": " · ".join(ctx_parts)}
            ],
        })
        return text, blocks

    @classmethod
    def _render_forensic(
        cls,
        invariant_id: str,
        violations: List[ViolationReport],
    ) -> Optional[str]:
        """Per-invariant rendering of the forensic detail.

        The shape of `observed_state` differs per invariant — there's
        no useful generic rendering. Each branch picks the fields that
        actually help triage, in a Slack-mrkdwn format. Truncated to
        keep the message scannable; full state is in the violation
        row referenced by id in the context line.

        Returns `None` when the rendering would be empty — caller
        omits the block entirely rather than emit an empty one.
        """
        if invariant_id == "L-03":
            tables: set = set()
            refs: list = []
            for v in violations:
                obs = v.observed_state or {}
                tables.update(obs.get("tables_hit") or [])
                for r in obs.get("sample_refs") or []:
                    refs.append(r)
            lines: List[str] = []
            if tables:
                lines.append(f"*Tables hit:* {', '.join(sorted(tables))}")
            if refs:
                lines.append("*Sample refs:*")
                for r in refs[:5]:
                    lines.append(
                        f"  • `{r.get('table')}.{r.get('column')}` "
                        f"(row `{r.get('row_id')}`)"
                    )
                if len(refs) > 5:
                    lines.append(f"  • _… +{len(refs) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "S-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                redis_n = obs.get("redis_slot_count", "?")
                sql_n = obs.get("sql_running_count", "?")
                in_redis_only = obs.get("in_redis_only") or []
                in_sql_only = obs.get("in_sql_only") or []
                line = f"*{agent}*: redis={redis_n} vs sql={sql_n}"
                diff_bits: List[str] = []
                if in_redis_only:
                    diff_bits.append(
                        f"redis-only: `{', '.join(in_redis_only[:3])}`"
                        + (f" +{len(in_redis_only) - 3}" if len(in_redis_only) > 3 else "")
                    )
                if in_sql_only:
                    diff_bits.append(
                        f"sql-only: `{', '.join(in_sql_only[:3])}`"
                        + (f" +{len(in_sql_only) - 3}" if len(in_sql_only) > 3 else "")
                    )
                if diff_bits:
                    line += "\n  " + " · ".join(diff_bits)
                lines.append(line)
            if len(violations) > 5:
                lines.append(f"_… +{len(violations) - 5} more agent(s)_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-02":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                eid = obs.get("execution_id", "?")
                prev = obs.get("previous_status") or "unknown"
                curr = obs.get("current_status", "?")
                lines.append(f"  • `{eid}`: *{prev}* → *{curr}*")
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        return None

    @staticmethod
    def _format_row_refs(persisted_ids: List[Optional[int]]) -> Optional[str]:
        """Render "violation #21" / "violations #21,#22,#23" / range form.

        Drops `None` slots (insert failures). Returns `None` when no
        rows persisted — caller skips the row-ref segment entirely
        rather than emit "violation None".
        """
        ids = [i for i in (persisted_ids or []) if i is not None]
        if not ids:
            return None
        if len(ids) == 1:
            return f"violation #{ids[0]}"
        if len(ids) <= 3:
            return f"violations {', '.join(f'#{i}' for i in ids)}"
        # 4+: collapse to range with count to keep the line tidy.
        return f"violations #{min(ids)}–#{max(ids)} ({len(ids)} total)"

    @staticmethod
    def _format_last_red(
        previous_violation_at: Optional[str],
        snapshot_time: str,
    ) -> str:
        """Render "last red Xm ago" / "first red" for the context block.

        Best-effort: if either timestamp fails to parse we fall back to
        "first red" rather than crash the alert. Slack will render the
        block fine without the badge.
        """
        if not previous_violation_at:
            return "first red for this invariant"
        try:
            from datetime import datetime
            prev = datetime.fromisoformat(previous_violation_at.replace("Z", "+00:00"))
            now = datetime.fromisoformat(snapshot_time.replace("Z", "+00:00"))
            delta = now - prev
            secs = int(delta.total_seconds())
            if secs < 60:
                return f"last red {secs}s ago"
            if secs < 3600:
                return f"last red {secs // 60}m ago"
            if secs < 86400:
                return f"last red {secs // 3600}h ago"
            return f"last red {secs // 86400}d ago"
        except Exception:
            return "first red for this invariant"

    @staticmethod
    def _render_message(
        invariant_id: str,
        violations: List[ViolationReport],
        snapshot_time: str,
    ) -> str:
        """Human-readable one-liner for the Slack message body.

        Time is intentionally omitted — the Slack Block Kit payload
        carries a relative "just now / 4m ago" context badge, and the
        precise ISO `snapshot_time` is preserved in the `canary_violations`
        row for forensic correlation. Embedding it in the message text
        would be redundant.
        """
        if invariant_id == "S-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"Slot–row bijection broke on {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        if invariant_id == "E-02":
            return (
                f"{len(violations)} execution(s) reverted from terminal "
                f"to non-terminal status."
            )
        if invariant_id == "L-03":
            ghosts = sorted(
                {v.observed_state.get("ghost_agent_name") for v in violations}
            )
            return (
                f"{len(ghosts)} ghost agent(s) referenced by orphan rows: "
                f"{', '.join(ghosts)[:160]}."
            )
        return f"{invariant_id} fired {len(violations)} violation(s)."

    # ------------------------------------------------------------------
    # Cycle-state side-table (Redis)
    # ------------------------------------------------------------------

    @staticmethod
    def _redis():
        """Redis client shared with the slot service. Lazy import so this
        module stays loadable in tests without a live Redis."""
        from services.slot_service import get_slot_service

        return get_slot_service().redis

    def _read_prev_cycle_at(self) -> Optional[str]:
        """Snapshot_time of the prior cycle, or None on first ever run.

        Falls back to None on any Redis error — that turns the next
        cycle's transition detection into "all violations are transitions"
        for that single cycle, which is verbose but never misses a real
        flip. We chose verbose-on-failure over silent-on-failure because
        the canary's whole reason to exist is catching transitions.
        """
        try:
            return self._redis().get(REDIS_KEY_LAST_CYCLE)
        except Exception:
            logger.exception("canary: failed to read previous-cycle marker")
            return None

    def _write_prev_cycle_at(self, snapshot_time: str) -> None:
        """Advance the previous-cycle cursor to this cycle's snapshot_time."""
        try:
            self._redis().set(REDIS_KEY_LAST_CYCLE, snapshot_time)
        except Exception:
            logger.exception("canary: failed to persist previous-cycle marker")

    @staticmethod
    def _is_green_to_red(
        invariant_id: str,
        previous_latest: dict,
        prev_cycle_at: Optional[str],
    ) -> bool:
        """Decide whether this cycle's violation is a fresh transition.

        Green→red iff the latest persisted violation for this invariant
        predates the previous cycle's snapshot_time. Cases:

        - First-ever cycle (prev_cycle_at is None): every violation is a
          transition. Operators expect to be told once when the harness
          first starts seeing problems.
        - First-ever violation (previous_latest entry absent): transition.
        - Continuing-red (latest violation timestamp == prev_cycle_at):
          continuation, no notification — the previous cycle saw it too.
        - Red→green→red (latest violation predates prev_cycle_at):
          transition — there was at least one clean cycle in between.
        """
        prev = previous_latest.get(invariant_id)
        if prev is None:
            return True
        if prev_cycle_at is None:
            return True
        # `>=` so a same-snapshot replay from an immediate manual rerun
        # is treated as a continuation rather than re-firing.
        return prev["snapshot_time"] < prev_cycle_at

def _severity_rank(severity: str) -> int:
    """Higher = worse. Used to pick the loudest violation for a transition."""
    return {"minor": 1, "major": 2, "critical": 3}.get(severity, 0)


# Module-level singleton, mirrors cleanup_service.
canary_service = CanaryService()
