"""
Canary alert sink — Slack Block Kit composition + webhook post (CANARY-001 / #411).

Extracted from `services/canary_service.py` to keep the cycle orchestrator
focused on lifecycle + invariant runs. The watcher imports `CanaryAlerts`
and calls `emit_transition` once per green→red transition; everything
Slack-shaped lives here.

The split is purely organisational — there's no behaviour change vs. when
these methods lived on `CanaryService` as classmethods. Tests pivoted from
`CanaryService._foo` to `CanaryAlerts._foo` accordingly.
"""

import logging
import os
from datetime import datetime
from typing import List, Optional, Tuple

from canary.snapshot import ViolationReport


logger = logging.getLogger(__name__)


class CanaryAlerts:
    """Stateless Slack alert composer + sink for canary transitions."""

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
        "S-02": "Slot overbooking",
        "S-03": "Slot TTL below floor",
        "E-01": "Stuck running execution",
        "E-02": "Phantom execution reversal",
        "E-05": "Dispatched execution without session",
        "L-03": "Delete cascades",
        "B-01": "Queue accessor drift",
        "B-02": "Stalled backlog drain",
        "R-01": "Zombie Claude process",
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
        "S-02": (
            "Agent slot count exceeds its `max_parallel_tasks` cap — "
            "`acquire_slot` was bypassed. Check recent changes to "
            "`SlotService.acquire_slot` and any direct ZADD into "
            "`agent:slots:*`."
        ),
        "S-03": (
            "Slot metadata HASH TTL is below `execution_timeout_seconds + 300s` "
            "(or missing entirely). Catches #226 class — slot metadata "
            "expires while execution is still running, leaking the slot "
            "permanently. Check the `expire()` call in `SlotService.acquire_slot`."
        ),
        "E-01": (
            "An execution stayed `running` past `execution_timeout_seconds + 300s` "
            "buffer. Cleanup watchdog should have fired — inspect "
            "`cleanup_service` logs and the agent container for a wedged Claude."
        ),
        "E-02": (
            "An execution went terminal then non-terminal. Look for retry "
            "logic that resurrects completed rows or a status-write race."
        ),
        "E-05": (
            "A `running` execution over 60s old has no `claude_session_id`. "
            "Either agent-server failed to write back (check container logs) "
            "or `mark_no_session_executions_failed` watchdog stopped firing. "
            "Same bug class as #106."
        ),
        "L-03": (
            "An agent was deleted but a referencing row wasn't cascaded. "
            "Check the delete handler for the table(s) listed above."
        ),
        "B-01": (
            "`db.get_queued_count` disagrees with the snapshot's direct queued "
            "id-list count. Inspect recent changes to `db/schedules.py:get_queued_count` "
            "for a cache layer or status-filter regression."
        ),
        "B-02": (
            "Agent has queued work, free slots, and the drain heartbeat is stale. "
            "`CapacityManager.run_maintenance()` either stopped firing or stopped "
            "writing its `canary:drain_tick_at` heartbeat. Check backend logs "
            "for `[Capacity] maintenance tick failed`."
        ),
        "R-01": (
            "Agent container has unreaped zombie `claude` processes (#407 class). "
            "Restart the affected agent to clear; check agent-server's subprocess "
            "wait() path for the reaped child."
        ),
    }

    @classmethod
    async def emit_transition(
        cls,
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
            worst = max(violations, key=lambda v: severity_rank(v.severity))
            logger.debug(
                "canary transition (slack disabled — set CANARY_SLACK_WEBHOOK_URL): "
                "%s severity=%s violations_in_cycle=%d snapshot_time=%s",
                invariant_id,
                worst.severity,
                len(violations),
                snapshot_time,
            )
            return

        worst = max(violations, key=lambda v: severity_rank(v.severity))
        text, blocks = cls._build_slack_payload(
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
    ) -> Tuple[str, list]:
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

        if invariant_id == "S-02":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                cap = obs.get("max_parallel_tasks", "?")
                count = obs.get("slot_count", "?")
                over = obs.get("overbooked_by", "?")
                lines.append(
                    f"  • *{agent}*: slots={count}/{cap} (+{over} over)"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "S-03":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                eid = obs.get("execution_id", "?")
                ttl = obs.get("redis_ttl_seconds", "?")
                floor = obs.get("floor_seconds", "?")
                kind = obs.get("kind", "?")
                lines.append(
                    f"  • *{agent}* `{eid}`: TTL={ttl}s ({kind}); floor={floor}s"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                eid = obs.get("execution_id", "?")
                age = obs.get("age_seconds", "?")
                timeout = obs.get("execution_timeout_seconds", "?")
                buffer = obs.get("slot_ttl_buffer_seconds", "?")
                lines.append(
                    f"  • *{agent}* `{eid}`: age={age}s "
                    f"(timeout={timeout}s + buffer={buffer}s)"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-05":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                eid = obs.get("execution_id", "?")
                age = obs.get("age_seconds", "?")
                lines.append(
                    f"  • *{agent}* `{eid}`: age={age}s, no claude_session_id"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "B-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                svc = obs.get("service_count", "?")
                snap = obs.get("snapshot_count", "?")
                lines.append(
                    f"  • *{agent}*: db.get_queued_count={svc} "
                    f"vs snapshot count={snap}"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "B-02":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                q = obs.get("queued_count", "?")
                free = obs.get("free_slots", "?")
                age = obs.get("drain_tick_age_seconds")
                age_str = "never" if age is None else f"{age}s ago"
                lines.append(
                    f"  • *{agent}*: queued={q}, free_slots={free}, "
                    f"last drain tick {age_str}"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "R-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = obs.get("agent_name", "?")
                count = obs.get("zombie_count", "?")
                lines.append(f"  • *{agent}*: {count} zombie(s)")
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
        if invariant_id == "S-02":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            worst = max(violations, key=lambda v: v.observed_state.get("overbooked_by", 0))
            return (
                f"{len(agents)} agent(s) overbooked "
                f"(worst: +{worst.observed_state.get('overbooked_by', '?')} "
                f"over cap): {', '.join(agents)[:160]}."
            )
        if invariant_id == "S-03":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            kinds = sorted({v.observed_state.get("kind", "?") for v in violations})
            return (
                f"{len(violations)} slot(s) with TTL below floor "
                f"({'/'.join(kinds)}) on {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        if invariant_id == "E-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"{len(violations)} execution(s) stuck in `running` past "
                f"timeout+buffer across {len(agents)} agent(s)."
            )
        if invariant_id == "E-02":
            return (
                f"{len(violations)} execution(s) reverted from terminal "
                f"to non-terminal status."
            )
        if invariant_id == "E-05":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"{len(violations)} dispatched execution(s) without "
                f"`claude_session_id` across {len(agents)} agent(s)."
            )
        if invariant_id == "L-03":
            ghosts = sorted(
                {v.observed_state.get("ghost_agent_name") for v in violations}
            )
            return (
                f"{len(ghosts)} ghost agent(s) referenced by orphan rows: "
                f"{', '.join(ghosts)[:160]}."
            )
        if invariant_id == "B-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"`db.get_queued_count` drifted from direct count on "
                f"{len(agents)} agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "B-02":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            return (
                f"{len(agents)} agent(s) have queued work with free slots "
                f"and a stale drain tick: {', '.join(agents)[:160]}."
            )
        if invariant_id == "R-01":
            agents = sorted({v.observed_state.get("agent_name") for v in violations})
            total = sum(v.observed_state.get("zombie_count", 0) for v in violations)
            return (
                f"{total} zombie claude process(es) across {len(agents)} "
                f"agent(s): {', '.join(agents)[:160]}."
            )
        return f"{invariant_id} fired {len(violations)} violation(s)."


def severity_rank(severity: str) -> int:
    """Higher = worse. Used to pick the loudest violation for a transition."""
    return {"minor": 1, "major": 2, "critical": 3}.get(severity, 0)
