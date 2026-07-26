"""Retention blast-radius guard (#1644).

Refuses a retention prune that would destroy more than a threshold of data, and
requires an explicit admin acknowledgment before it proceeds.

WHY THIS EXISTS
---------------
#1638: a lowered code default silently hard-deleted 95.7% of a production
instance's execution history ~20 seconds after boot, with a green /health and one
INFO line. That fix made the *defaults* fail-safe. It added no guard on the prune
itself, so every other route to a destructive window is still open: an unvalidated
`PUT /api/settings/ops/config`, a future default regression, a direct DB write, a
migration. This guard is the backstop that does not care how the bad window
arrived — it looks only at what is about to happen.

DESIGN NOTES (each one is load-bearing; read before changing)
------------------------------------------------------------
1. **Stateless detection.** The trip condition is a bounded COUNT of the candidate
   set, not a stored watermark of the last-seen window. A watermark is smaller and
   more precise, but it is *state* — and its row would be deletable through the
   same unvalidated settings endpoints that cause the bug, silently disarming the
   guard. `learnings.md` (#1638, Lesson 2) says to audit every endpoint that can
   delete the row that protects the data; the cheapest way to pass that audit is
   to hold no such row. The ack IS state, but deleting an ack fails SAFE (the
   guard simply refuses again), so it is safe in the only direction that matters.

2. **Absolute counts, no percentage.** An earlier design compared candidates
   against the table total. That needs a denominator, and there isn't one for
   `execution_log` (whose predicate *includes* `execution_log IS NOT NULL`, so the
   prune falsifies its own denominator); it costs an unindexed full-table scan; it
   divides by zero on an empty table; and it *inverts* on the agent sweep, where 3
   purged agents are ~0% of any table but 3 destroyed volume sets. An absolute
   count needs none of it: any table's steady-state trickle is small (only rows
   crossing the cutoff in the last 5 minutes) and any anomaly is large. Table size
   is irrelevant.

3. **FAIL CLOSED, always.** Any error — the count throws, the ack lookup throws,
   the settings read throws — refuses the prune. Never `except: proceed`. The
   correct direction of failure is "keep too much" (`learnings.md` #1638 Lesson 1):
   a full disk is recoverable, deleted history is not. A guard that fails open is
   worse than no guard, because it manufactures confidence.

4. **The ack is the gate; the operator-queue item is only an alarm.** Making the
   queue item load-bearing was rejected in review: `create_item` is a blind
   `INSERT ... ON CONFLICT DO NOTHING`, so once the row exists in *any* state
   (e.g. an operator hits Clear All -> `cancelled`) re-emission is a no-op and the
   gate would wedge shut permanently and silently; and `prune_terminal_items`
   would delete the approval at 90 days — the sweep deleting its own authorization.
   Keeping the alarm decorative dissolves both: a wedged alarm costs
   discoverability, not correctness, and responding to it grants nothing.
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from database import db

logger = logging.getLogger(__name__)

# Prefix for the ack rows in `system_settings`. Blocklisted from the generic
# `PUT /api/settings/{key}` catch-all (routers/settings.py) — otherwise anything
# that can reach that endpoint could write its own approval and disarm the guard.
ACK_KEY_PREFIX = "retention_ack_"

# The trip threshold. DELIBERATELY A FIXED CONSTANT, NOT A SETTING.
#
# This was an operator-configurable knob with a Settings panel. That was wrong on
# two counts. First, nobody can reason about the right value — it depends on table
# sizes and per-cycle churn the operator can't see, so the panel needed a caption
# explaining that a *bigger* number is *worse*; a control that needs to explain
# which way is safe is the wrong control. Second, and worse: a mutable constant
# read at ACTION time that gates a destructive operation is structurally identical
# to OPS_SETTINGS_DEFAULTS — the exact thing that caused #1638. Raising it would
# have silently disarmed the guard fleet-wide. Defending that knob took clamping,
# a catch-all blocklist, a range-validated endpoint, and a direction-pinning test;
# deleting the knob deletes all of it.
#
# 1000 is chosen against STEADY STATE, not table size: at any sane window, only
# rows crossing the cutoff within one 5-minute cycle are candidates — a trickle of
# tens. Four digits of trickle means something changed (the window narrowed, or
# retention was just enabled on a mature install). Table size is irrelevant, which
# is why no percentage or denominator is involved.
#
# Lowering this is always safe. Raising it weakens every install at once — so if
# a real fleet ever needs a higher value, that is a code change with a reviewer,
# not a text box.
MAX_ROWS_PER_SWEEP = 1000

# Per-sweep floors: "the largest candidate count that may pass unacknowledged".
# Not configurable — these encode how recoverable each sweep's loss is, which is a
# property of the data, not an operator preference.
FLOOR_AGENTS = 0      # #1581: every purge destroys Docker volumes. Always ack.
FLOOR_SCHEDULES = 100


@dataclass(frozen=True)
class GuardVerdict:
    """Outcome of one guard evaluation."""
    allowed: bool
    candidates: int
    threshold: int
    reason: str


def _ack_key(setting_key: str) -> str:
    return f"{ACK_KEY_PREFIX}{setting_key}"


def is_acknowledged(setting_key: str, window_days: int) -> bool:
    """True iff an admin has acknowledged a mass prune at exactly this window.

    The ack is bound to the window, not just the setting: narrowing the window
    further invalidates the old approval, so an operator who approved "prune at
    30 days" has not thereby approved "prune at 1 day".

    Raises on a failed read. The caller MUST treat that as "not acknowledged" —
    an `except: return True` here would auto-approve fleet-wide mass deletion the
    moment the DB hiccups.
    """
    raw = db.get_setting_value(_ack_key(setting_key), None)
    if raw is None:
        return False
    return str(raw).strip() == str(window_days)


def record_acknowledgement(setting_key: str, window_days: int) -> None:
    """Store an admin's approval for a mass prune at `window_days`."""
    db.set_setting(_ack_key(setting_key), str(window_days))


def consume_acknowledgement(setting_key: str) -> None:
    """Clear the ack after a guarded prune has actually run (single-use).

    Deliberately single-use rather than sticky-per-window. A sticky ack would let
    one approval authorize an unboundedly larger delete later at the same window;
    consuming it re-arms the guard. This is cheap in practice: the prune accessors
    drain fully (`chunk_size` bounds each transaction, not the call), so one ack
    buys one complete drain, after which the candidate set is back to a
    sub-threshold trickle and the guard stays silent.

    Called only AFTER a successful prune — if the prune raised, the ack survives
    so the operator doesn't have to approve the same intent twice.
    """
    db.delete_setting(_ack_key(setting_key))


def evaluate(
    setting_key: str,
    window_days: int,
    count_fn: Callable[[int], int],
    floor: Optional[int] = None,
) -> GuardVerdict:
    """Decide whether a prune may proceed. NEVER raises. Fails CLOSED.

    Args:
        setting_key: the retention window's `system_settings` key (identity of the
            thing being gated, and what an ack is bound to).
        window_days: the resolved window, already read by the caller.
        count_fn: `limit -> count`, bounded. Must share the prune's predicate —
            see `db/schedules.py:_execution_row_prune_predicate` for why.
        floor: largest count that passes unacknowledged. Defaults to
            MAX_ROWS_PER_SWEEP; pass FLOOR_AGENTS / FLOOR_SCHEDULES for the
            non-row sweeps.

    Returns:
        GuardVerdict. `allowed=False` means DO NOT PRUNE.
    """
    # A constant, so there is no read to fail — one whole failure mode (and its
    # fail-closed branch) disappeared with the setting.
    threshold = MAX_ROWS_PER_SWEEP if floor is None else floor

    try:
        # limit = threshold + 1 so a return of exactly threshold+1 means
        # "strictly more than threshold" — all the comparison needs.
        candidates = count_fn(threshold + 1)
    except Exception as e:
        logger.error(
            "[RetentionGuard] %s: candidate count failed (%s) — REFUSING prune "
            "(fail-closed, #1644)", setting_key, e,
        )
        return GuardVerdict(False, -1, threshold, "count_failed")

    if candidates <= threshold:
        return GuardVerdict(True, candidates, threshold, "under_threshold")

    try:
        acked = is_acknowledged(setting_key, window_days)
    except Exception as e:
        logger.error(
            "[RetentionGuard] %s: acknowledgement lookup failed (%s) — REFUSING "
            "prune (fail-closed, #1644)", setting_key, e,
        )
        return GuardVerdict(False, candidates, threshold, "ack_lookup_failed")

    if acked:
        return GuardVerdict(True, candidates, threshold, "acknowledged")

    return GuardVerdict(False, candidates, threshold, "over_threshold")


# --------------------------------------------------------------------------
# Alarm surface (informational only — the ack endpoint is the gate)
# --------------------------------------------------------------------------

# Host for the alarm's `agent_name` (the column is NOT NULL and has no FK).
# Leading underscore is deliberate: `utils.helpers.sanitize_agent_name` strips
# leading non-alphanumerics, so this name is uncreatable through agent creation
# and can never collide with a real agent. That matters — a real agent named this
# would inherit its owner into the item's ACL, and the 5s operator-queue sync loop
# would start writing the alarm into that agent's queue file.
ALARM_AGENT_NAME = "_retention-guard"

# In-process, per-key memo of the last verdict, so the ERROR + alarm fire on the
# green->red TRANSITION rather than every 5-minute cycle. 288 identical ERRORs a
# day is how an alert gets muted, and a muted alert is the #1638 failure mode
# repeated. Deliberately in-process (not persisted): losing it on restart costs
# one extra log line, which is the safe direction.
_last_refused: dict = {}


def _alarm_id(setting_key: str, window_days: int) -> str:
    """Natural key: one alarm per (setting, window), idempotent by construction.

    `create_item` is INSERT ... ON CONFLICT DO NOTHING on `id`, so re-emitting is a
    no-op. This is only safe because the alarm is decorative — if it were the gate,
    a `cancelled` row here would wedge it shut forever (that is exactly why the
    gate lives in the ack endpoint instead).
    """
    return f"retention-guard-{setting_key}-{window_days}"


def announce_refusal(
    setting_key: str,
    label: str,
    window_days: int,
    verdict: GuardVerdict,
) -> None:
    """Log + raise the operator alarm for a refused prune. Never raises.

    SECURITY: carries counts and identifiers ONLY — never sample rows. Queue rows
    are durable and operator-visible, and `schedule_executions.message`/`response`/
    `error` hold user content and credential-bearing agent output. Canary G-04
    exists because `backlog_metadata` leaked secrets into exactly this kind of
    durable state; the "show the operator what would be deleted" instinct is the bug.
    """
    fresh = _last_refused.get(setting_key) != window_days
    _last_refused[setting_key] = window_days

    try:
        source = "db-row" if db.get_setting_value(setting_key, None) is not None \
            else "code-default"
    except Exception:
        source = "unknown"

    message = (
        f"[Cleanup] REFUSED {label} prune: {verdict.candidates} candidate(s) "
        f"exceeds threshold {verdict.threshold} at a {window_days}-day window "
        f"({source}), reason={verdict.reason}. Nothing was deleted. Acknowledge via "
        f"POST /api/settings/retention/acknowledge to proceed (#1644)."
    )
    if fresh:
        logger.error(message)
    else:
        logger.info(message)

    if not fresh:
        return
    try:
        db.create_operator_queue_item(
            ALARM_AGENT_NAME,
            {
                "id": _alarm_id(setting_key, window_days),
                "type": "alert",
                "priority": "critical",
                "title": f"Retention prune refused: {label}",
                "question": message,
                # Identifiers and counts only — see the SECURITY note above.
                "context": {
                    "alert_type": "retention_blast_radius",
                    "setting_key": setting_key,
                    "window_days": window_days,
                    "window_source": source,
                    "candidate_count": verdict.candidates,
                    "threshold": verdict.threshold,
                    "reason": verdict.reason,
                },
                # Must stay NULL: `mark_operator_queue_expired` flips any pending
                # row past `expires_at` to `expired` fleet-wide every 5s.
                "expires_at": None,
            },
        )
    except Exception as e:
        # The alarm is decorative; the refusal already happened and is logged.
        # Never let a failed alarm change the outcome.
        logger.warning("[RetentionGuard] could not raise alarm for %s: %s",
                       setting_key, e)


def note_allowed(setting_key: str) -> None:
    """Clear the transition memo so a future refusal logs at ERROR again."""
    _last_refused.pop(setting_key, None)
