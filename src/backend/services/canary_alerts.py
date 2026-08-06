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
from typing import Any, List, Optional, Tuple

from canary.snapshot import ViolationReport
from services.instance_identity import get_instance_label, sanitize_instance_label


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

    # Friendly invariant names. The bare ID (S-01, E-02, …) is opaque to
    # anyone not steeped in the catalog; the name is what makes the Slack
    # alert immediately interpretable.
    #
    # SOURCE OF TRUTH is the invariant module's own docstring title line
    # (`canary/invariants/<id>_*.py`, "X-NN — <title> (CANARY-001 …)"), NOT
    # docs/testing/orchestration-invariant-catalog.md. The catalog IDs are
    # not the registry IDs: catalog E-06 is the unimplemented #129 check
    # while registry E-06 is "no overdue next_run_at" (#1472) — the catalog
    # flags this itself, and its own cross-references are stale. G-03/G-04's
    # catalog titles also over-claim scope relative to what shipped. Sourcing
    # a name from the catalog can therefore put a confidently WRONG label on
    # a real alert, which is worse than the bare-ID fallback.
    #
    # Register differs from the docstring on purpose: a docstring names the
    # property that HOLDS ("Terminal-state closure"), an alert header names
    # the defect that FIRED ("Stuck running execution"). Derive, don't copy.
    _INVARIANT_NAMES = {
        "S-01": "Slot–row bijection",
        "S-02": "Slot overbooking",
        "S-03": "Slot TTL below floor",
        "E-01": "Stuck running execution",
        "E-02": "Phantom execution reversal",
        "E-03": "Terminal row missing completed_at",
        "E-04": "Queued row metadata unusable",
        "E-05": "Dispatched execution without session",
        "E-06": "Overdue schedule projection",
        "G-03": "Terminal row finished before it started",
        "G-04": "Credential pattern in backlog metadata",
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
            "Slot metadata HASH is missing (`missing`), has no expiry "
            "(`no_expiry`), or was created with a TTL below its OWN stored "
            "`timeout_seconds + 300s` (`below_floor`). The first two are the "
            "#226 class — metadata expires while the execution is still "
            "running, leaking the slot permanently; check the `expire()` call "
            "in `SlotService.acquire_slot`. `below_floor` is narrower than it "
            "looks (ent#336): the floor comes from the slot's own stored "
            "timeout, which `acquire_slot` writes from the same local it "
            "derives the TTL from, so it flags those two lines drifting apart "
            "— NOT a caller passing a wrong timeout."
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
        "E-03": (
            "A terminal `schedule_executions` row has a NULL `completed_at` — "
            "the status CAS landed but the paired timestamp write did not. "
            "Check the terminal writers (`task_execution_service.apply_result`, "
            "`_write_terminal_and_gate`) and `cleanup_service`'s recovery "
            "sweeps for a path that sets status without the timestamp."
        ),
        "E-04": (
            "A `queued` row's drain-replay contract is broken: `queued_at` is "
            "NULL, or `backlog_metadata` is NULL or not JSON-parseable. "
            "`backlog_service.drain_next` decodes that blob to reconstruct the "
            "task, so a malformed one stalls the agent's FIFO. The violation "
            "records a reason code only — never the metadata value."
        ),
        "E-06": (
            "An enabled schedule's `next_run_at` is further in the past than "
            "the misfire grace — the scheduler never advanced the projection "
            "(#1472; the UI renders this as \"Next: Nd ago\"). Look in "
            "`src/scheduler/` for a silent `_add_job` failure at registration "
            "or a fire path that did not re-arm the next window."
        ),
        "G-03": (
            "A terminal `schedule_executions` row has `started_at` after "
            "`completed_at`. Either the two timestamps were written by "
            "different callers against a skewed clock, or a producer copied "
            "the wrong execution's timestamp. Compare the writers in "
            "`task_execution_service` and the standalone `src/scheduler/`."
        ),
        "G-04": (
            "Rotate the matched credential first — `backlog_metadata` is "
            "stored plaintext and survives until the row is deleted (the "
            "#1449 scrub deliberately skips FAILED rows, so it can persist "
            "for the full row-retention window). Then find the producer that "
            "serialized a secret into the drain-replay payload "
            "(`backlog_service` enqueue → `task_execution_service` metadata "
            "construction) and replace it with an opaque identity token. The "
            "violation records the pattern name and row ids only; triage from "
            "those — do not read or paste the blob."
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
            "Agent container has a zombie `claude` process that has PERSISTED "
            "past the dwell window (#407 class) — a transient zombie awaiting "
            "its parent's wait() is normal and is deliberately not flagged "
            "(ent#337). Compare `first_seen_count` with `zombie_count` to tell "
            "a single stuck zombie from an accumulating leak. Restart the "
            "affected agent to clear; check agent-server's subprocess wait() "
            "path for the unreaped child."
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
        # #1987: name the instance in the payload. Resolved here rather than
        # inside the composer so `_build_slack_payload` stays a pure function
        # of its arguments — the render path never touches env or the DB.
        text, blocks = cls._build_slack_payload(
            invariant_id,
            violations,
            snapshot_time,
            previous_violation_at,
            worst.severity,
            persisted_ids,
            instance_label=get_instance_label(),
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
        *,
        instance_label: Optional[str] = None,
    ) -> Tuple[str, list]:
        """Compose the Slack message text + Block Kit blocks.

        Layout: header → summary → forensic detail → runbook hint →
        context (snapshot_time, count, last red, row ids). Tests
        identify blocks by `type` rather than index so adding/removing
        sections doesn't break them.

        `instance_label` (#1987) names the instance that fired the alert and
        is rendered into BOTH the header and the `text` fallback — the header
        so it is visible without expanding the message, the fallback because
        that is what a mobile push notification actually shows, and triaging
        "eu2, the #1766 pilot" vs "dev, unrelated" from the lock screen is
        the case that motivated it. `None` (nothing identifies this install)
        renders exactly today's unlabelled payload.

        Returns `(text, blocks)` — `text` is the fallback used by
        clients that don't render blocks (notifications, screen
        readers).
        """
        emoji = cls._SEVERITY_EMOJI.get(severity, "•")
        name = cls._INVARIANT_NAMES.get(invariant_id, invariant_id)
        # Re-sanitized at the render boundary rather than trusted from the
        # resolver — same argument `_mrkdwn_safe` makes below. It also bounds
        # the length, and an over-long `header` is a 400 from Slack that drops
        # the WHOLE message while the transition is still recorded as sent
        # (the #1880 failure mode).
        label = sanitize_instance_label(instance_label)
        prefix = f"[{label}] " if label else ""
        body = cls._render_message(invariant_id, violations, snapshot_time)
        forensic = cls._render_forensic(invariant_id, violations)
        runbook = cls._INVARIANT_RUNBOOKS.get(invariant_id)
        last_red = cls._format_last_red(previous_violation_at, snapshot_time)
        row_ref = cls._format_row_refs(persisted_ids)

        text = f"{prefix}{emoji} canary {invariant_id} {name} ({severity}): {body}"
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {prefix}{invariant_id} {name} — {severity}",
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
                lines.append(f"*Tables hit:* {', '.join(_mrkdwn_safe(t) for t in sorted(tables))}")
            if refs:
                lines.append("*Sample refs:*")
                for r in refs[:5]:
                    lines.append(
                        f"  • `{_mrkdwn_safe(r.get('table'))}."
                        f"{_mrkdwn_safe(r.get('column'))}` "
                        f"(row `{_mrkdwn_safe(r.get('row_id'))}`)"
                    )
                if len(refs) > 5:
                    lines.append(f"  • _… +{len(refs) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "S-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                redis_n = obs.get("redis_slot_count", "?")
                sql_n = obs.get("sql_running_count", "?")
                in_redis_only = obs.get("in_redis_only") or []
                in_sql_only = obs.get("in_sql_only") or []
                line = f"*{agent}*: redis={redis_n} vs sql={sql_n}"
                diff_bits: List[str] = []
                if in_redis_only:
                    diff_bits.append(
                        f"redis-only: `{', '.join(_mrkdwn_safe(x) for x in in_redis_only[:3])}`"
                        + (f" +{len(in_redis_only) - 3}" if len(in_redis_only) > 3 else "")
                    )
                if in_sql_only:
                    diff_bits.append(
                        f"sql-only: `{', '.join(_mrkdwn_safe(x) for x in in_sql_only[:3])}`"
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
                eid = _mrkdwn_safe(obs.get("execution_id"))
                prev = _mrkdwn_safe(obs.get("previous_status"), fallback="unknown")
                curr = _mrkdwn_safe(obs.get("current_status"))
                lines.append(f"  • `{eid}`: *{prev}* → *{curr}*")
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "S-02":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
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
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
                ttl = obs.get("redis_ttl_seconds", "?")
                floor = obs.get("floor_seconds", "?")
                kind = _mrkdwn_safe(obs.get("kind"))
                # ent#336: name where the floor came from — on a missing/
                # no_expiry slot the HASH may be gone, so the printed floor is
                # the agent-cap placeholder rather than this slot's own bound.
                src = _mrkdwn_safe(obs.get("floor_source"))
                lines.append(
                    f"  • *{agent}* `{eid}`: TTL={ttl}s ({kind}); "
                    f"floor={floor}s (from {src})"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-01":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
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
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
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
                agent = _mrkdwn_safe(obs.get("agent_name"))
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
                agent = _mrkdwn_safe(obs.get("agent_name"))
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
                agent = _mrkdwn_safe(obs.get("agent_name"))
                count = obs.get("zombie_count", "?")
                # ent#337: the count alone reads as noise now that transients
                # are filtered — how LONG it has held, and whether it grew
                # since first-seen, are what distinguish a stuck zombie from
                # an accumulating leak.
                held = _format_duration(obs.get("held_for_seconds"))
                first = obs.get("first_seen_count", "?")
                trend = f"{first} → {count}" if first != count else f"{count}"
                lines.append(
                    f"  • *{agent}*: {trend} zombie(s), held {held}"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-03":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
                status = _mrkdwn_safe(obs.get("status"))
                started = _mrkdwn_safe(obs.get("started_at"))
                # `completed_at` is NULL by definition on this path — that IS
                # the violation. Say so; rendering the field would print the
                # literal "None" (a `.get(k, "?")` default does NOT fire on an
                # explicit None value).
                lines.append(
                    f"  • *{agent}* `{eid}`: status={status}, "
                    f"started={started}, completed_at is NULL"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-04":
            # SECURITY: reason code + row ids only. The raw `backlog_metadata`
            # is deliberately absent from `observed_state`
            # (e04_queued_rows_have_metadata.py) because it may carry live
            # credentials and violations persist to `canary_violations`.
            # Never reach past these keys.
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
                reason = _mrkdwn_safe(obs.get("reason"))
                lines.append(f"  • *{agent}* `{eid}`: {reason}")
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "E-06":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                sid = _mrkdwn_safe(obs.get("schedule_id"))
                nxt = _mrkdwn_safe(obs.get("next_run_at"))
                overdue = _format_duration(obs.get("overdue_seconds"))
                lines.append(
                    f"  • *{agent}* schedule `{sid}`: next_run_at={nxt} "
                    f"({overdue} overdue)"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "G-03":
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
                started = _mrkdwn_safe(obs.get("started_at"))
                completed = _mrkdwn_safe(obs.get("completed_at"))
                skew = obs.get("skew_seconds")
                skew_str = "?" if skew is None else f"{skew}s"
                lines.append(
                    f"  • *{agent}* `{eid}`: started={started} > "
                    f"completed={completed} (skew {skew_str})"
                )
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        if invariant_id == "G-04":
            # SECURITY: matched pattern NAME + row ids only — never the secret,
            # the surrounding bytes, or the raw `backlog_metadata`. The check
            # (g04_no_creds_in_backlog_metadata.py) keeps all three out of
            # `observed_state` on purpose and stops at the first match so even
            # the match count stays undisclosed. Do not widen this branch.
            lines: List[str] = []
            for v in violations[:5]:
                obs = v.observed_state or {}
                agent = _mrkdwn_safe(obs.get("agent_name"))
                eid = _mrkdwn_safe(obs.get("execution_id"))
                pattern = _mrkdwn_safe(obs.get("matched_pattern"))
                lines.append(f"  • *{agent}* `{eid}`: matched `{pattern}`")
            if len(violations) > 5:
                lines.append(f"  • _… +{len(violations) - 5} more_")
            return "\n".join(lines) if lines else None

        # Fallback is deliberately STATE-FREE — an un-rendered invariant emits
        # no forensic block at all rather than a generic dump. Never replace
        # this with something that iterates `observed_state`: E-04 and G-04
        # rely on scrubbing happening at the *check*, and a generic dumper
        # would silently opt every future invariant into echoing its full
        # state to Slack. Pinned by the negative test in
        # tests/unit/test_1880_canary_alert_parity.py.
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
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            return (
                f"Slot–row bijection broke on {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        if invariant_id == "S-02":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            worst = max(violations, key=lambda v: v.observed_state.get("overbooked_by", 0))
            return (
                f"{len(agents)} agent(s) overbooked "
                f"(worst: +{worst.observed_state.get('overbooked_by', '?')} "
                f"over cap): {', '.join(agents)[:160]}."
            )
        if invariant_id == "S-03":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            kinds = sorted({_mrkdwn_safe(v.observed_state.get("kind")) for v in violations})
            return (
                f"{len(violations)} slot(s) with an unusable metadata TTL "
                f"({'/'.join(kinds)}) on {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        if invariant_id == "E-01":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
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
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            return (
                f"{len(violations)} dispatched execution(s) without "
                f"`claude_session_id` across {len(agents)} agent(s)."
            )
        if invariant_id == "L-03":
            ghosts = sorted(
                {_mrkdwn_safe(v.observed_state.get("ghost_agent_name")) for v in violations}
            )
            return (
                f"{len(ghosts)} ghost agent(s) referenced by orphan rows: "
                f"{', '.join(ghosts)[:160]}."
            )
        if invariant_id == "B-01":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            return (
                f"`db.get_queued_count` drifted from direct count on "
                f"{len(agents)} agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "B-02":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            return (
                f"{len(agents)} agent(s) have queued work with free slots "
                f"and a stale drain tick: {', '.join(agents)[:160]}."
            )
        if invariant_id == "R-01":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            total = sum(v.observed_state.get("zombie_count", 0) for v in violations)
            # ent#337: worst dwell tells the reader whether this is a fresh
            # stick or a long-running leak. `or 0` guards a None the same way
            # the E-06/G-03 branches below do.
            worst = max(
                violations,
                key=lambda v: v.observed_state.get("held_for_seconds") or 0,
            )
            worst_str = _format_duration(worst.observed_state.get("held_for_seconds"))
            return (
                f"{total} PERSISTING zombie claude process(es) across "
                f"{len(agents)} agent(s), worst held {worst_str}: "
                f"{', '.join(agents)[:160]}."
            )
        # The five branches below coerce `agent_name` with `or "?"` before
        # `sorted()`. This is NOT decoration: these are the first per-ROW
        # invariants (E-03/G-03 are bounded by the 5000-row terminal cap,
        # E-06 by schedule count), they read `agent_name` off a collector row
        # mapping rather than an `AgentSnapshot`, and `sorted({None, "a"})`
        # raises TypeError. That raise is swallowed by `canary_service`'s
        # transition loop, which then records the transition anyway — so the
        # alert is lost, the green→red cursor advances, and nothing retries.
        # Truncation (`[:160]`) matters for the same reason: a payload over
        # Slack's 3000-char section limit is rejected wholesale.
        if invariant_id == "E-03":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            return (
                f"{len(violations)} terminal row(s) with a NULL `completed_at` "
                f"across {len(agents)} agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "E-04":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            reasons = sorted({_mrkdwn_safe(v.observed_state.get("reason")) for v in violations})
            return (
                f"{len(violations)} queued row(s) with unusable drain-replay "
                f"metadata ({'/'.join(reasons)[:80]}) across {len(agents)} "
                f"agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "E-06":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            worst = max(
                violations,
                key=lambda v: v.observed_state.get("overdue_seconds") or 0,
            )
            worst_str = _format_duration(worst.observed_state.get("overdue_seconds"))
            return (
                f"{len(violations)} enabled schedule(s) with an overdue "
                f"`next_run_at` (worst: {worst_str}) across {len(agents)} "
                f"agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "G-03":
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            worst = max(
                violations,
                key=lambda v: v.observed_state.get("skew_seconds") or 0,
            )
            worst_skew = worst.observed_state.get("skew_seconds")
            skew_str = "?" if worst_skew is None else f"{worst_skew}s"
            return (
                f"{len(violations)} terminal row(s) finished before they "
                f"started (worst skew: {skew_str}) across {len(agents)} "
                f"agent(s): {', '.join(agents)[:160]}."
            )
        if invariant_id == "G-04":
            # SECURITY: pattern names only — see the G-04 note in
            # `_render_forensic`. Nothing here may echo the metadata value.
            agents = sorted({_mrkdwn_safe(v.observed_state.get("agent_name")) for v in violations})
            patterns = sorted(
                {_mrkdwn_safe(v.observed_state.get("matched_pattern")) for v in violations}
            )
            return (
                f"{len(violations)} queued row(s) matched a credential pattern "
                f"({', '.join(patterns)[:80]}) across {len(agents)} agent(s): "
                f"{', '.join(agents)[:160]}."
            )
        # Fallback is deliberately COUNT-ONLY and state-free — same contract as
        # `_render_forensic`'s terminal `return None`. Never widen it to render
        # `observed_state`; see the note there.
        return f"{invariant_id} fired {len(violations)} violation(s)."


def _mrkdwn_safe(value: Any, *, fallback: str = "?") -> str:
    """Coerce an `observed_state` value for interpolation into a Slack block.

    Does two jobs, deliberately fused so there is exactly ONE way a value
    crosses the render boundary (the file previously carried two different
    `?`-defaulting idioms, `get(k, "?")` and `get(k) or "?"`, neither of
    which escaped anything):

    1. **Absent/empty → `fallback`.** `.get(k, "?")` does NOT fire on a key
       present with value `None`, so it renders the literal "None"; and a
       `None` reaching `sorted()` raises `TypeError`, which `canary_service`
       swallows — losing the whole alert with no retry.

    2. **Escape `&`, `<`, `>`** — Slack's own documented escape set. These
       three are the security-relevant ones: `<url|text>` is a live link and
       `<!channel>` is a channel-wide mention, so an unescaped value can
       phish or mass-ping everyone in the alert channel. Control characters
       (newline, tab) collapse to a space so a value cannot forge an extra
       bullet line and fake a violation that never happened.

    Deliberately NOT escaped: `*`, `_`, `~`, and backtick. Slack defines no
    escape for them, they can only produce cosmetic emphasis — they cannot
    forge a link, a mention, or a block — and mangling them would corrupt
    legitimate values. Note the header block is `plain_text`, where none of
    this is parsed at all; this exists for the `section` blocks and the
    `text` fallback.

    Why at the render boundary and not only at write time: agent names are
    sanitized on all ~8 creation/rename paths today, so nothing hostile can
    reach here — but that is a transitive guarantee spread across eight
    call sites, re-verified by hand each audit. `services/retention_guard.py`
    already writes an `agent_name` (`_retention-guard`) that
    `sanitize_agent_name` could never produce, precisely because it strips a
    leading underscore, which shows the premise can break. Escaping here
    makes the property structural and local.
    """
    if value is None:
        return fallback
    text = value if isinstance(value, str) else str(value)
    if not text:
        return fallback
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text.strip() or fallback


def _format_duration(seconds: Optional[float]) -> str:
    """Render a second count as a compact human duration ("3d", "4h", "12m").

    Same granularity ladder as `CanaryAlerts._format_last_red`. An overdue
    `next_run_at` (E-06) is routinely measured in days, and a raw second
    count is unreadable at a glance in an alert. Returns "?" for a missing
    or non-numeric value rather than raising — a render helper must never
    be the reason an alert fails to send.
    """
    if seconds is None:
        return "?"
    try:
        secs = int(seconds)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not hypothetical padding: `int(float("inf"))` raises
        # it, and neither TypeError nor ValueError catches that. Both current
        # producers already `int()` at source so it is unreachable today — but
        # the docstring above promises this helper can never be why an alert
        # fails to send, and a promise the except clause doesn't keep is the
        # kind of prose that outlives the assumption behind it.
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def severity_rank(severity: str) -> int:
    """Higher = worse. Used to pick the loudest violation for a transition."""
    return {"minor": 1, "major": 2, "critical": 3}.get(severity, 0)
