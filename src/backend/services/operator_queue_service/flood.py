"""The file seam's flood alert (#3785, S6): `FloodAlertMixin`.

`_maybe_emit_flood_alert` raises ONE alert per over-cap episode (#1632, #3130)
through `platform_alerts.observe` (#3254), and `_end_flood_episode` ends the
episode and its alert when `_sync_agent` sees the condition clear. The episode
set and the episode cooldown are instance state initialized in
`PollerCoreMixin.__init__`; the 6-hour re-read times live in a dict the flood
method creates on the instance for itself (`_flood_refreshed_at`), so it moved
here with the method and nothing else touches it.

`OperatorQueueSyncService` is composed from this mixin in the package
`__init__` (the `db/schedules` shape, Invariant #2); `_sync_agent` reaches both
methods through `self.`. The cooldown knob is package-owned config (O1), read
as `_oq.X` at call time. #3438, which retires the file channel, deletes this
module.
"""

import logging
import time

from services import operator_queue_service as _oq

logger = logging.getLogger("services.operator_queue_service")


# `OperatorQueueSyncService` is composed from this mixin in `__init__`; the
# instance state these methods read through `self.` is initialized there.
class FloodAlertMixin:
    async def _maybe_emit_flood_alert(
        self, agent_name: str, held: int = 0, reason: str = "ingestion_cap"
    ):
        """#1632 / #3130: ONE flood alert per over-cap EPISODE.

        Fired when this agent's ingestion was depth-held / rate-skipped, or its
        queue file was oversized.

        Edge-triggered (#3130): an episode alerts once, however many cycles or
        cooldown windows it lasts, and the next alert needs the condition to
        have cleared first (`_sync_agent` ends the episode through
        `_end_flood_episode`, which ends its alert too). It used to be one
        alert per cooldown window for as long as the condition held — and since
        the alert counted toward the very depth it reported, the condition never
        cleared: 241 alerts in one day on one agent. The cooldown stays as the
        minimum spacing between episodes, so a cap that flaps cannot re-alert
        every cycle.

        Reported through `platform_alerts.observe` (#3254) as kind
        `queue_flood`, keyed by the reason: one pending row per (agent, reason),
        rewritten in place. Budgeted (#1677, #3130): the kind's budgeted type is
        `queue_flood`, so `observe` applies the per-(agent, type) budget — an
        agent's own filing volume drives this emitter, which is the test that
        classification applies. The budget is the backstop for what the
        in-memory episode state cannot see (a leader failover re-reads once,
        onto the same row). Excluded from the agent's own depth read by its
        reserved prefix. Softened wording to avoid cry-wolf (C9).

        Re-read while it lasts (#3254 R9): an episode that persists is observed
        again at most every 6 hours, so the kind's 14-day lifetime ends only a
        row nobody re-read (a lost clear), never one whose condition still
        holds. The refresh time per (agent, reason) lives in a dict this method
        creates on the instance for itself (`_flood_refreshed_at`).

        The cooldown and the episode are stamped BEFORE every reading (a re-read
        included). Outcomes:
          * created / updated / snoozed → the episode is consumed (one alert per
            episode; a snoozed reading is a person's ending, which stands);
          * failed on the episode's first reading → the episode is RELEASED so
            the alarm is retried, and the cooldown stamp spaces those retries to
            at most one per window (review I1: holding the episode on a failure
            silenced the alarm until the condition cleared — for a runaway agent,
            never);
          * failed on a re-read → the episode is KEPT: an earlier reading filed
            its alert, and only a live episode lets `_end_flood_episode` end it
            when the condition clears (AC 2). The re-read is retried after the
            cooldown, not after the full 6 hours;
          * refused at budget → the episode is consumed. The agent already has
            a full budget of pending flood alerts and the budget emits its own
            `alert-budget-` episode alert, so re-knocking every window would
            add nothing but budget-episode churn. An unregistered kind is the
            same (a code defect retrying cannot fix).
        Never raises (`observe` never does): an alert must not kill the sync.
        """
        from services import platform_alerts

        key = (agent_name, reason)
        now = time.monotonic()
        refreshed = vars(self).setdefault("_flood_refreshed_at", {})
        rereading = key in self._flood_episodes
        if rereading:
            # R9: the 6-hour re-read of a persisting episode, the #3255
            # base-image alarm's spacing (BASE_IMAGE_ALERT_COOLDOWN_SECONDS).
            last_read = refreshed.get(key)
            if last_read is not None and now - last_read < 6 * 60 * 60:
                return  # this episode already alerted and was read recently
        else:
            # `None` = never alerted for this agent. A `0.0` default would be
            # wrong: `time.monotonic()` is seconds from an arbitrary reference
            # and can be < COOLDOWN on a freshly-booted process, so
            # `now - 0.0 < COOLDOWN` would suppress the FIRST-ever alert for
            # the first COOLDOWN seconds of uptime.
            last = self._flood_alert_cooldown.get(agent_name)
            if last is not None and now - last < _oq.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS:
                return  # too soon after the last episode — the next cycle retries
            self._flood_episodes.add(key)
        # Stamped before every reading, a re-read included, so a failed one —
        # released below — is retried no sooner than the cooldown (I1).
        self._flood_alert_cooldown[agent_name] = now
        refreshed[key] = now

        if reason == "oversize_file":
            title = f"Agent '{agent_name}' produced an oversized operator-queue file"
            question = (
                f"Agent '{agent_name}' wrote an operator-queue.json larger than the "
                f"allowed size; its requests are not being ingested. This can "
                f"indicate a runaway or compromised agent — review its recent "
                f"activity before acting on its requests."
            )
        else:
            title = f"Agent '{agent_name}' exceeded its operator-queue ingestion limit"
            question = (
                f"Agent '{agent_name}' produced more operator-queue requests than the "
                f"per-agent ingestion limit allows; {held} request(s) were held and "
                f"are not shown. Held requests stay in the agent's file and are read "
                f"again once it is under the limit; the agent is told they are held. "
                f"This can indicate a runaway or compromised agent — review its "
                f"recent activity before acting on its requests."
            )

        outcome = platform_alerts.observe(
            agent_name, "queue_flood", reason,
            title=title, question=question, priority="high",
            context={"held": held, "reason": reason},
        )
        if outcome == platform_alerts.OBSERVED_FAILED:
            if rereading:
                # keep the episode so its end still clears the alert; retry the
                # re-read once the cooldown has passed
                refreshed[key] = now - 6 * 60 * 60 + _oq.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS
            else:
                self._flood_episodes.discard(key)  # retry next window (I1)
                refreshed.pop(key, None)
            return
        if outcome in (platform_alerts.OBSERVED_CREATED, platform_alerts.OBSERVED_UPDATED):
            logger.warning(
                f"Operator-queue flood alert {outcome} for {agent_name} "
                f"(reason={reason}, held={held})"
            )

    def _end_flood_episode(self, agent_name: str, reason: str) -> None:
        """#3130 / #3254: the over-cap condition behind `reason` cleared.

        Called from `_sync_agent` where it knows (a sane-size read ends an
        oversize episode; a cycle that held nothing ends an ingestion-cap one).
        Ends the episode, so the next over-cap reading alerts again, and, only
        when one was running, ends its alert as the platform
        (`platform_alerts.clear`, a compare-and-set on pending: a person's
        ending is never overwritten). Every cycle without an episode costs no
        read. Never raises (`clear` never does).
        """
        key = (agent_name, reason)
        if key not in self._flood_episodes:
            return
        self._flood_episodes.discard(key)
        from services import platform_alerts
        platform_alerts.clear(agent_name, "queue_flood", reason)
