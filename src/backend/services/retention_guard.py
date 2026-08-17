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

3. **FAIL CLOSED, always.** Any error refuses the prune — the count throws
   (`count_failed`), the count cannot be compared to the threshold
   (`count_uninterpretable`, #1833), the count is negative i.e. an error sentinel
   (`count_negative`, #1833), the ack lookup throws (`ack_lookup_failed`). Never
   `except: proceed`. The correct direction of failure is "keep too much"
   (`learnings.md` #1638 Lesson 1): a full disk is recoverable, deleted history is
   not. A guard that fails open is worse than no guard, because it manufactures
   confidence — and one that RAISES instead of refusing keeps the data but loses
   the alarm, which is #1833.

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
import time
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


def _describe_exception(e: BaseException) -> str:
    """`type: message`, and never raises — `str(e)` on a foreign exception can.

    Used by EVERY failure path in this module — `evaluate`'s three `except`
    branches and `announce_refusal`'s stored `_RefusalEpisode.last_error` plus its
    per-attempt WARNING. The stored form is an eager f-string, so a raising
    `__str__` escapes directly. The log form looks safer — deferred `%s`
    formatting is logging's problem — but only in production, where `handleError`
    prints a traceback to stderr and carries on; pytest's `LogCaptureHandler`
    re-raises it. Either way a function documented "never raises" must not carry a
    raise-capable expression on its failure path: that mismatch between stated
    contract and actual behaviour is #1833 itself, and this module is the last
    place to repeat it.

    Defined ABOVE `evaluate` on purpose. It was originally added for
    `announce_refusal` alone and `evaluate` — the function #1833 is named after,
    and the one whose docstring promises "does not raise" — kept passing the raw
    exception into deferred `%s` on all three of its refusal paths. Measured: with
    a re-raising handler installed, `evaluate` raised straight out of a refusal.
    Fixing one site and leaving its sibling is the incomplete-fix class this
    module has now hit twice.
    """
    try:
        return f"{type(e).__name__}: {e}"
    except Exception:                       # pragma: no cover - pathological
        return type(e).__name__


def evaluate(
    setting_key: str,
    window_days: int,
    count_fn: Callable[[int], int],
    floor: Optional[int] = None,
) -> GuardVerdict:
    """Decide whether a prune may proceed. Fails CLOSED, and does not raise.

    Every way this function can fail to reach a TRUSTWORTHY answer returns
    `allowed=False`: `count_fn` raising (`count_failed`), `count_fn` returning
    something that cannot be compared to the threshold (`count_uninterpretable`,
    #1833), a negative count — i.e. an error sentinel, since no real count is
    negative (`count_negative`, #1833) — and the acknowledgement lookup raising
    (`ack_lookup_failed`). There is no "threshold unreadable" path: the threshold
    is a module constant, so that failure mode does not exist.

    `candidates` on the returned verdict is ALWAYS an int (`-1` = unknown): it is
    interpolated into the alarm message, serialised into the alarm context, and
    returned by GET /api/settings/retention, and a non-finite float there emits a
    bare `NaN` that a strict JSON parser rejects.

    The callers in `cleanup_service._guard_allows` still wrap this call, and that
    belt is NOT redundant: if this function ever did raise, the prune would be
    skipped — but `announce_refusal` would never run, so the refusal would lose
    its ERROR and its durable operator-queue alarm. The belt protects the DATA;
    this contract protects the SIGNAL. The second caller
    (`routers/settings.py`, GET /api/settings/retention) has no belt at all: a
    raise there is a 500 on the panel an operator uses to approve the very prune
    this refused.

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
        # `_describe_exception`, never the raw `e`: deferred `%s` formatting means a
        # foreign exception whose `__str__` raises escapes THIS function, whose
        # docstring promises it does not (measured under a re-raising handler).
        logger.error(
            "[RetentionGuard] %s: candidate count failed (%s) — REFUSING prune "
            "(fail-closed, #1644)", setting_key, _describe_exception(e),
        )
        return GuardVerdict(False, -1, threshold, "count_failed")

    try:
        # #1833: the comparisons live INSIDE a try, so a `count_fn` returning
        # None / a string / a Mock surfaces as a REFUSAL with an alarm rather
        # than a TypeError the caller's outer except logs as a nondescript
        # "Error pruning ..." — or, at GET /api/settings/retention, as a 500.
        #
        # `type(...) is not bool` is load-bearing, NOT paranoia: an object whose
        # `__le__` returns a TRUTHY NON-BOOL makes a bare `bool(candidates <=
        # threshold)` True, and the guard would ALLOW a prune on a count it never
        # understood — a silent fail-OPEN introduced by the fix for a fail-closed
        # bug. Every real numeric comparison (int, float, inf, nan, bool) returns
        # exactly `bool`, so nothing legitimate is rejected. (`bool` is final in
        # CPython, so there is no subclass case; what this DOES also reject is
        # `numpy.bool_`, which is not a `bool` at all — correct and fail-closed
        # today, since all 8 accessors return a Python int, and stated here so a
        # future numpy/pandas-backed `count_fn` fails legibly, not mysteriously.)
        under = candidates <= threshold
        negative = candidates < 0
        if type(under) is not bool or type(negative) is not bool:
            # Name BOTH comparisons: reporting only `under` produced the useless
            # "comparison returned bool, expected bool" whenever it was `__lt__`
            # that misbehaved.
            raise TypeError(
                f"__le__ returned {type(under).__name__}, "
                f"__lt__ returned {type(negative).__name__}; expected bool from both"
            )
    except Exception as e:
        logger.error(
            "[RetentionGuard] %s: could not decide from a candidate count of "
            "type %s (%s) — REFUSING prune (fail-closed, #1644)",
            setting_key, type(candidates).__name__, _describe_exception(e),
        )
        return GuardVerdict(False, -1, threshold, "count_uninterpretable")

    if negative:
        # #1833: no real count is negative — all 8 accessors are int(COUNT(*)).
        # A negative return is an error sentinel, and `-1` is the one THIS module
        # uses for "unknown" (below). `-1 <= threshold` is True, so before this
        # check a `count_fn` erroring out with the module's own idiom AUTHORISED
        # an unbounded prune: the count does not bound the delete
        # (`prune_execution_logs` takes no count and drains fully), so that is
        # #1638 replayed.
        # `type(...).__name__`, not the value: `candidates` is a foreign object that
        # merely answered two comparisons with real bools, so its `__str__` can still
        # raise out of the deferred `%s` — the same class as the `except` branches
        # above. A negative count is an error sentinel; its exact value diagnoses
        # nothing the type does not.
        logger.error(
            "[RetentionGuard] %s: negative candidate count (of type %s) — REFUSING "
            "prune (fail-closed, #1644)", setting_key, type(candidates).__name__,
        )
        return GuardVerdict(False, -1, threshold, "count_negative")

    # Report an int, always. The DECISION above used the raw value (so NaN keeps
    # its pinned IEEE-754 behaviour); only what we PUBLISH is normalised. This is
    # the single fix for three surfaces: `verdict.candidates` is interpolated into
    # the alarm `message`, written to the alarm `context` (json.dumps), and
    # returned as `candidate_count` by GET /api/settings/retention. `json.dumps`
    # emits a bare `NaN` for a non-finite float — valid to Python's `json.loads`,
    # REJECTED by a browser's `JSON.parse`, which would break the two surfaces the
    # alarm exists to reach. `-1` is the "unknown" sentinel already used above.
    # `type(...) is int`, not `isinstance`: `isinstance(True, int)` is True, and a
    # bool count is a defect, not a count.
    reported = candidates if type(candidates) is int else -1

    if under:
        return GuardVerdict(True, reported, threshold, "under_threshold")

    try:
        # #1833: `bool(...)` INSIDE the try. `if acked:` takes the truth value of
        # whatever the lookup returned — in production a bool, which was equally
        # true of `candidates` and is exactly the assumption this issue exists to
        # stop relying on.
        acked = bool(is_acknowledged(setting_key, window_days))
    except Exception as e:
        logger.error(
            "[RetentionGuard] %s: acknowledgement lookup failed (%s) — REFUSING "
            "prune (fail-closed, #1644)", setting_key, _describe_exception(e),
        )
        return GuardVerdict(False, reported, threshold, "ack_lookup_failed")

    if acked:
        return GuardVerdict(True, reported, threshold, "acknowledged")

    return GuardVerdict(False, reported, threshold, "over_threshold")


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

# #1834: how long ONE refusal episode may go with an undelivered alarm before the
# per-attempt WARNING escalates to a single ERROR. An AGE, not an attempt count
# (#1897's reasoning transfers: age is monotonic and can never hand a fresh
# episode a spent budget). A module constant, NOT a setting — the
# MAX_ROWS_PER_SWEEP argument above applies unchanged.
#
# This is an ESCALATION threshold, NOT a give-up. There is deliberately no
# give-up here, which is where this DIVERGES from #1897: that sink is an external
# webhook whose outage is independent of the canary's storage, so a permanently
# misconfigured URL would otherwise retry forever. THIS sink is the platform's own
# database — the same DB the guard reads its settings from — so a failing
# `create_item` almost certainly means a DB incident (failover, disk-full, lock
# storm), and those routinely exceed 30 minutes. Giving up would mean: DB down 35
# min, then it recovers, the prune is still refused, and no queue item ever
# exists until a restart or a window change. That is #1834's exact symptom shipped
# inside #1834's fix. A retry costs one idempotent INSERT ... ON CONFLICT DO
# NOTHING per cycle per refusing key (at most 8 keys), and the episode is already
# bounded by construction — `note_allowed` or a window/reason change resets it.
#
# Expressed in SECONDS but consumed in CYCLES, and cycles are not a constant:
# `CleanupService(poll_interval=...)` is a constructor argument and an admin can
# drive extra cycles on demand — which is precisely why the bound is an age rather
# than an attempt count. At the 300s default the check (`elapsed > ...`, evaluated
# AFTER each attempt) first trips on the attempt at t=2100, i.e. the 8th — the
# same off-by-one #1897 documents.
ALARM_ESCALATION_AGE_SECONDS = 1800

# #1834: bound as a module attribute so a test can patch ONE guard symbol instead
# of `time.monotonic` itself. CI runs the whole unit suite in one process under
# pytest-randomly alongside Hypothesis; patching the stdlib `time` module for the
# process is a flake factory. `monotonic`, never `time()`: this is an AGE, and an
# age must not be at the mercy of an NTP step or a DST transition. Never persisted
# and never compared across processes, so its arbitrary epoch is irrelevant.
_clock = time.monotonic

# Refusal reasons where the guard could not determine the blast radius at all.
# `evaluate` returns at each of these BEFORE `is_acknowledged` is consulted, so an
# acknowledgement cannot clear them — the alarm must not tell an operator to
# approve something that approving cannot fix (the sweep's `count_fn` has to be
# fixed instead). Kept beside the reasons themselves so a new refusal reason has
# to decide which side it is on.
#
# `ack_lookup_failed` is deliberately NOT in this set, and the two operator
# surfaces classify it differently ON PURPOSE — do not "align" them without
# reading this. It is reached only AFTER the count was found genuinely over
# threshold, so an acknowledgement IS the right remedy and becomes effective the
# moment a transient settings-read failure clears; the alarm therefore keeps the
# acknowledge instruction. `GET /api/settings/retention` still routes it to
# `blocked_sweeps` rather than `pending_acknowledgements`, because a panel cannot
# usefully offer an approve CONTROL while the path that reads the approval is the
# thing that is broken — the click would appear to succeed and change nothing.
_UNDECIDABLE_REASONS = frozenset(
    {"count_failed", "count_uninterpretable", "count_negative"}
)


@dataclass
class _RefusalEpisode:
    """One (setting_key, window_days, reason) refusal run. NOT frozen.

    This record serves TWO masters — the log-level transition gate AND the alarm
    retry budget. Any future edit to the freshness rule silently changes retry
    semantics and vice versa; say which one you mean.

    `alarm_delivered` means "the `create_item` call did not raise", NEVER "a row
    was inserted": `create_item` is INSERT ... ON CONFLICT DO NOTHING and returns
    the id of the row that EXISTS, so on conflict it returns the pre-existing
    uuid. Treating "inserted" as the success condition would make a second uvicorn
    worker — whose write is always a conflict no-op — retry forever.
    """
    window_days: int
    reason: str = ""
    alarm_delivered: bool = False
    first_refused_at: float = 0.0     # _clock(), i.e. time.monotonic()
    attempts: int = 0
    escalated: bool = False
    last_error: Optional[str] = None


# In-process, per-key record of the current refusal episode, so the ERROR + alarm
# fire on the green->red TRANSITION rather than every 5-minute cycle. 288 identical
# ERRORs a day is how an alert gets muted, and a muted alert is the #1638 failure
# mode repeated.
#
# Deliberately in-process (not persisted, not Redis), and this is NOT the skills
# `_last_sync` mistake: nothing READS this record — it is an emitter-local memo,
# the durable artifact is the `operator_queue` row in the shared DB, and duplicate
# emission is a no-op because `_alarm_id()` is a natural key under ON CONFLICT DO
# NOTHING. `cleanup_service` runs in EVERY uvicorn worker with no leader lease, so
# both processes already call `announce_refusal` today and the natural key already
# absorbs it; two independent loops make delivery MORE likely, since worker B can
# land the row worker A failed to write. Redis would add a failure domain whose
# own outage would then have to fail open (suppressing the alarm) or closed
# (blocking) — reintroducing #1834 through the fix. Losing this on restart costs
# one extra log line, which is the safe direction.
_refusal_episodes: dict = {}


def reset_transition_memo() -> None:
    """Clear all episode state.

    Exists so tests stop monkeypatching a private module global by name (#1834):
    three suites need this reset and one of them did not have it, which is a latent
    order-dependent flake now that an episode carries a `_clock()` stamp.
    """
    _refusal_episodes.clear()


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

    #1834: a FAILED alarm write is re-attempted on every later cycle for the life
    of the refusal episode. The memo used to be written before the attempt, so one
    failed write permanently suppressed its own retry — the durable, operator-
    visible half of the signal was lost until a restart or a window change, which
    is exactly the half designed to survive a log nobody is tailing. Retrying is
    safe by construction: `create_item` is idempotent on the natural key, and the
    queue item authorizes NOTHING (the ack endpoint is the gate), so a re-attempt
    can neither re-authorize a prune nor wedge one.

    The "never raises" claim leans on `evaluate`'s guarantee that
    `verdict.candidates` is an int: the message below formats it EAGERLY, so a
    verdict carrying an object with a raising `__format__` would escape. Every
    production caller passes an `evaluate` verdict; do not relax that.

    SECURITY: carries counts and identifiers ONLY — never sample rows. Queue rows
    are durable and operator-visible, and `schedule_executions.message`/`response`/
    `error` hold user content and credential-bearing agent output. Canary G-04
    exists because `backlog_metadata` leaked secrets into exactly this kind of
    durable state; the "show the operator what would be deleted" instinct is the bug.
    """
    now = _clock()
    ep = _refusal_episodes.get(setting_key)
    # #1834: freshness keys on (window, REASON), not window alone. A sweep that
    # flips `over_threshold` -> `count_uninterpretable` at an unchanged window is
    # a different problem with a different remedy, and under a window-only key it
    # produced NO fresh ERROR and no new attempt — #1833's new signal silently
    # swallowed. `_alarm_id` is deliberately NOT changed (a second durable row per
    # reason is noise, and its natural-key shape is pinned), so the durable row
    # keeps its first content for a given (setting, window); the re-fired ERROR is
    # the signal for a reason change.
    #
    # COST of keying on the reason, accepted deliberately: a `count_fn` that FLAPS
    # between two reasons (say `count_failed` <-> `over_threshold` as a DB blip
    # comes and goes) makes every cycle "fresh", so the transition ERROR fires
    # every 5 minutes — up to 288/day, which is the alert-muting mode this memo
    # exists to prevent — and each fresh episode resets `first_refused_at`, so the
    # escalation below never trips. Judged the better trade anyway: the alternative
    # (window-only freshness) silently swallowed #1833's new signal entirely, and a
    # flapping count function is itself a defect that ought to be loud. If this is
    # ever observed in the wild, dampen it with a per-key minimum re-ERROR interval
    # rather than by reverting to a reason-blind key.
    #
    # KNOWN RESIDUAL, in BOTH directions, stated rather than discovered later:
    # because the row keeps its FIRST content for a given (setting, window), a
    # reason flip leaves the durable alarm describing the OLD reason. The two
    # directions are not equally harmless, and only one of them is inert.
    #
    #   over_threshold -> undecidable: the row still says "acknowledge to proceed".
    #     Bounded and inert — the corrected text fires as a fresh ERROR, and an ack
    #     given under the stale prompt is not consumed while the sweep stays
    #     undecidable (`evaluate` returns before `is_acknowledged`), so it neither
    #     authorizes this prune nor is silently spent.
    #
    #   undecidable -> over_threshold: the row still says "an acknowledgement will
    #     NOT clear this — the sweep's count function must be fixed", when by then
    #     an acknowledgement is EXACTLY the available remedy. This one is NOT
    #     inert: it tells the operator not to take the one action that would
    #     unblock the sweep, in the durable half specifically designed to outlive a
    #     log nobody is tailing, so the prune can stay blocked indefinitely. It is
    #     the worse direction and the reason this is a tracked follow-up rather
    #     than an accepted quirk.
    #
    # Fixing it needs either `verdict.reason` in `_alarm_id` (a second durable row
    # per reason, and its natural-key shape is pinned) or an UPDATE path the alarm
    # sink does not have — `create_item` is INSERT ... ON CONFLICT DO NOTHING. Both
    # are design decisions, not one-liners, hence the follow-up.
    fresh = ep is None or (ep.window_days, ep.reason) != (window_days, verdict.reason)
    if fresh:
        # ARM BEFORE THE ATTEMPT (#1897's ordering): the record exists before
        # `create_item` is called, so a crash / SIGKILL / BaseException inside the
        # write leaves an UNDELIVERED episode that the next cycle retries — rather
        # than a memo saying "handled" for an alarm that never landed (#1834).
        # `asyncio.CancelledError` is not an `Exception`, so arming in the except
        # branch would lose the alarm on every shutdown that lands inside the write.
        ep = _RefusalEpisode(
            window_days=window_days, reason=verdict.reason, first_refused_at=now
        )
        _refusal_episodes[setting_key] = ep

    try:
        source = "db-row" if db.get_setting_value(setting_key, None) is not None \
            else "code-default"
    except Exception:
        source = "unknown"

    if verdict.reason in _UNDECIDABLE_REASONS:
        # The generic message is false twice for an error state: "-1 candidate(s)
        # exceeds threshold 1000" is nonsense, and an acknowledgement CANNOT clear
        # it (`evaluate` returns before `is_acknowledged` is ever consulted). This
        # string is the durable alarm's `question`; prescribing an impossible
        # remedy there is #1833's own defect in the most visible place it has.
        message = (
            f"[Cleanup] REFUSED {label} prune: the guard could not determine the "
            f"blast radius at a {window_days}-day window ({source}), "
            f"reason={verdict.reason}. Nothing was deleted. An acknowledgement "
            f"will NOT clear this — the sweep's count function must be fixed "
            f"(#1644/#1833)."
        )
    elif verdict.candidates < 0:
        # An approvable refusal whose count could not be REPRESENTED: a non-finite
        # or non-integer count (NaN, inf, Decimal) compares fine, so it lands here
        # as `over_threshold`, but publication normalised it to the -1 "unknown"
        # sentinel. The generic wording then rendered the exact nonsense the
        # `_UNDECIDABLE_REASONS` split was written to stop — "-1 candidate(s)
        # exceeds threshold 1000".
        #
        # It gets its OWN branch rather than folding into the undecidable one,
        # because the undecidable text would be a fresh lie here: an acknowledgement
        # DOES clear this (measured — a NaN count with an ack returns
        # `acknowledged`/allowed, pinned by `test_1771a::test_nan_count_with_ack_is_
        # allowed`), since `is_acknowledged` is reached on this path. So: drop the
        # impossible number, keep the instruction that works. Routing non-finite
        # counts to `count_uninterpretable` instead would be the tidier-looking fix
        # and is WRONG twice: it breaks that pin, and it reclassifies a refusal an
        # ack genuinely clears as one it cannot.
        #
        # UNFIXED HALF, deliberately: this repairs the durable ALARM only. The same
        # `-1` still reaches `pending_acknowledgements` at GET /api/settings/
        # retention, where `Settings.vue` renders it as "-1 items past the N-day
        # window" beside an Approve control. Before #1833 that surface did not read
        # better — a raw NaN `json.dumps`'d to a bare `NaN`, which a browser's
        # JSON.parse rejects, taking the whole panel down — so this is a quiet wrong
        # number where there used to be a loud broken page, on an admin-only surface
        # for a count shape no accessor produces. Fixing it properly is a frontend
        # change (render "an unreportable count" for the -1 sentinel), which is the
        # same deferred follow-up as rendering `blocked_sweeps` at all.
        message = (
            f"[Cleanup] REFUSED {label} prune: the candidate count exceeds threshold "
            f"{verdict.threshold} at a {window_days}-day window ({source}) but is not "
            f"a whole number, so it cannot be reported (the sweep's count function "
            f"should return an int). reason={verdict.reason}. Nothing was deleted. "
            f"Acknowledge via POST /api/settings/retention/acknowledge to proceed "
            f"(#1644)."
        )
    else:
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

    if ep.alarm_delivered:
        # Exactly today's repeat-refusal no-op. Only a DELIVERED alarm stops the
        # attempts; an undelivered one is retried for the life of the episode.
        return

    ep.attempts += 1
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
        # Never let a failed alarm change the outcome. `alarm_delivered` stays
        # False, so the next cycle re-attempts (#1834) — the whole point.
        ep.last_error = _describe_exception(e)
        elapsed = now - ep.first_refused_at
        if not ep.escalated and elapsed > ALARM_ESCALATION_AGE_SECONDS:
            ep.escalated = True
            # Says only what THIS process knows. The in-process design above
            # relies on a sibling worker possibly having landed the row; claiming
            # "no operator-queue item exists" would send an operator hunting for a
            # missing item that is present (#1897: no counter may mean two things,
            # one level over).
            logger.error(
                "[RetentionGuard] this worker has been unable to write the refusal "
                "alarm for %s for %ds across %d attempt(s) — the prune is refused "
                "and blocked. STILL RETRYING every cycle; another worker may have "
                "written it. Last error: %s (#1834)",
                setting_key, int(elapsed), ep.attempts, ep.last_error,
            )
        else:
            # `ep.last_error`, NOT the raw `e`: deferred `%s` formatting is
            # normally logging's problem (it prints to stderr via handleError and
            # carries on), but a foreign exception whose `__str__` raises then
            # produces a silent stderr traceback in production — and pytest's
            # LogCaptureHandler re-raises it outright, so the same line would
            # break "never raises" under test. Same information, no raise.
            logger.warning("[RetentionGuard] could not raise alarm for %s "
                           "(attempt %d): %s",
                           setting_key, ep.attempts, ep.last_error)
        return
    # "Delivered" means the call did not raise — see `_RefusalEpisode`.
    ep.alarm_delivered = True


def note_allowed(setting_key: str) -> None:
    """End this key's refusal episode: a future refusal logs at ERROR again."""
    _refusal_episodes.pop(setting_key, None)
