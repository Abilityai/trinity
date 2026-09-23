"""The autonomy dial — what a companion may do unprompted (ent#641, P12).

Autonomy today is one switch per agent. Tandem needs a **dial**: a companion
starts on-request for every kind of ask and graduates, ask class by ask class,
and only when the evidence says so. Two things decide it, at two different
scopes, and this module is where they meet:

* **The instance level (L0–L3)** — the firm-level setting of
  ``tandem-06-operations.md`` §2.3. A CEILING: `L2 - Delegated classes` is the
  first level at which anything may run unprompted at all. Stored as one
  validated `system_settings` key, never a table — the value is a scalar with a
  closed vocabulary, and `PUT /api/settings/autonomy-dial` is the route that
  knows the range (the #506 / ent#297 shape).
* **The per-(seat, ask class) graduation state** — §2.2, and **[R R25]**:
  ratings are *necessary and not sufficient*. A class graduates on **rating
  history AND decision-record stability** — the same criterion applied
  repeatedly with no reversals (`services/seat_decision_service.py`, ent#638).
  "Promotion is earned by a stable criterion, never by frequency."

THE VERDICT IS A STORED EARNED-STATE × THREE LIVE CONJUNCTS

The row records only what was EARNED — the state, the evidence it rests on, and
`evidence_expires_at`. Everything that can change underneath it is ANDed at read
time instead of being written:

    graduated ⟺ stored state == "graduated"
              ∧ level ≥ L2                    (the instance ceiling)
              ∧ agent autonomy_enabled        (the hard off, AC 5)
              ∧ now <= evidence_expires_at    (the evidence still exists)

That shape is not a convenience. Written instead of read, each conjunct is a
silent failure: a class whose records all lapse at UTC midnight fires no event,
so a materialised `graduated` would outlive its own evidence with nobody
watching; and re-evaluating the fleet inside a level change is an unbounded
fan-out (there is no fleet-wide seat query) that a read-time conjunct removes
entirely — a level drop now needs zero writes. The hard-off rule generalises
the same way: flipping autonomy back restores the earned state rather than
making the seat earn it again.

THE EVIDENCE RULE, STATED

A class is promoted when ALL of:

1. **≥3 non-expired decision records** in the class (`STABLE_MIN_COUNT`);
2. **one normalized criterion** across them — the reusable judgment;
3. **no reversals among those records**. Deliberately the WINDOW, not all
   history: `seat_decision_service.stats` counts reversals over every row ever,
   which makes one reversal permanent and — with "a human may hold but never
   promote" — leaves `on_request` as the only reachable steady state. #638
   shipped that counter as this issue's INPUT and deferred the verdict here;
   this is the verdict.
4. **no negative rating by this seat on this agent in the last
   `RATING_WINDOW_DAYS`**. Three properties of that query are load-bearing and
   each was a defect in the first draft: it matches the `operator:` prefix as
   well as `workspace:` (on a single-operator install the seat person IS the
   platform principal, and their thumbs-down lands under the other prefix); it
   reads `COALESCE(updated_at, created_at)` (a re-rate updates only
   `updated_at`, so an up→down flip inside the window is invisible to a
   `created_at` predicate — and that flip is exactly what AC 3 is about); and
   the window is a FIXED span anchored now, never "since the oldest surviving
   record", which would let a blocking thumbs-down fall out as records expire —
   promotion by the clock, which R25 forbids.
5. **not guard-capped**. Canon: a class that moves another role's held metric
   can never become unprompted on rating history alone. The flag is
   operator-set here and its default is `not_assessed`, carried in the
   evidence — so "reviewed and clear" reads differently from "nobody looked".

Every "no" is a NAMED blocker, because a state a person cannot explain is a
state they cannot trust. A class with no evidence at all is `on_request` — the
absence of a verdict is never permission.

WHAT THIS MODULE DOES NOT DO

It ships the state and the verdict, not an executor: nothing in the dispatch
path starts running because a class graduated. The consumer in this cut is the
seat's own prompt block (`seat_decision_service.prompt_block`), which now tells
the companion which classes it is on-request for — so the verdict changes
behaviour where the companion can act on it. The enforced approval gate (#164)
and graduated control grants (#251) are different questions and stay theirs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- the instance level ----------------------------------------------------
#: `system_settings` key. The `instance:` prefix is the scope this level is
#: declared at — canon says "per firm; never per agent", and Trinity has no firm
#: entity, so today there is exactly one scope. A later per-fleet dial is a
#: second key, not a migration.
LEVEL_KEY = "autonomy_dial_level:instance"
LEVELS = ("L0", "L1", "L2", "L3")
#: Canon §2.3's own labels, so the operator reads the same words in both places.
LEVEL_LABELS = {
    "L0": "Continuity — companions absent or on-request only",
    "L1": "Companion — on-request; brief on for ready seats",
    "L2": "Delegated classes — unprompted on graduated ask classes; guard-capped",
    "L3": "Load-bearing judgment — unprompted broadly; human gates on consequence",
}
#: The first level at which ANYTHING may run unprompted (§2.3's table).
UNPROMPTED_FROM = "L2"
#: An install that has never set the dial is at `L1`: the level every existing
#: install is already at in behaviour (companions answer when asked). Migrating
#: must never make something unprompted.
DEFAULT_LEVEL = "L1"

# --- the graduation rule ---------------------------------------------------
STATE_ON_REQUEST = "on_request"
STATE_GRADUATED = "graduated"
STATES = (STATE_ON_REQUEST, STATE_GRADUATED)

GUARD_NOT_ASSESSED = "not_assessed"
GUARD_CAPPED = "capped"
GUARD_CLEAR = "clear"
GUARD_STATES = (GUARD_NOT_ASSESSED, GUARD_CAPPED, GUARD_CLEAR)

#: Mirrors `seat_decision_service.STABLE_MIN_COUNT` by import, not by copy.
RATING_WINDOW_DAYS = 30
#: Ratings by the seat itself. `workspace:` is the client surface; `operator:`
#: is the same person reaching the Workspace as a platform principal — on a
#: single-operator install every rating is the second kind.
RATING_PREFIXES = ("workspace:", "operator:")
#: What the rule version stamps on the evidence, so a stored verdict can be read
#: against the rule that produced it rather than against today's.
RULE_VERSION = "2026-09-23"

# Named blockers — the vocabulary a person and a companion both read.
BLOCK_LEVEL = "level_below_l2"
BLOCK_AUTONOMY_OFF = "agent_autonomy_off"
BLOCK_EVIDENCE_EXPIRED = "evidence_expired"
BLOCK_TOO_FEW = "too_few_records"
BLOCK_CRITERIA = "criterion_not_stable"
BLOCK_REVERSAL = "reversal_in_window"
BLOCK_RATING = "negative_rating_in_window"
BLOCK_GUARD = "guard_metric_capped"
BLOCK_HELD = "held_by_operator"

BLOCKER_TEXT = {
    BLOCK_LEVEL: "the instance dial is below L2, so nothing runs unprompted anywhere",
    BLOCK_AUTONOMY_OFF: "this agent's autonomy switch is off — the hard off, which the dial never overrides",
    BLOCK_EVIDENCE_EXPIRED: "the decisions this was promoted on have passed their review date",
    BLOCK_TOO_FEW: "fewer than three decisions on record for this kind of ask",
    BLOCK_CRITERIA: "the decisions did not apply one consistent criterion",
    BLOCK_REVERSAL: "a decision in this window was reversed",
    BLOCK_RATING: "this seat rated the agent down inside the rating window",
    BLOCK_GUARD: "this class moves a metric another role holds — it can never graduate on this evidence alone",
    BLOCK_HELD: "an operator is holding this class on-request",
}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _now_iso() -> str:
    from utils.helpers import utc_now_iso

    return utc_now_iso()


# ---------------------------------------------------------------------------
# The instance level
# ---------------------------------------------------------------------------

def get_level(db) -> str:
    """The instance level, or `DEFAULT_LEVEL`. Never raises, never invents a
    level: an unreadable or unknown value reads as the default, which is below
    the unprompted threshold."""
    try:
        raw = (db.get_setting_value(LEVEL_KEY, DEFAULT_LEVEL) or "").strip().upper()
    except Exception as e:  # noqa: BLE001 — a settings read must not fail a turn
        logger.warning("[autonomy-dial] level read failed (%s) — defaulting", type(e).__name__)
        return DEFAULT_LEVEL
    return raw if raw in LEVELS else DEFAULT_LEVEL


def level_allows_unprompted(level: str) -> bool:
    """`L2` is the first level at which anything may run unprompted (§2.3)."""
    return level in LEVELS and LEVELS.index(level) >= LEVELS.index(UNPROMPTED_FROM)


def set_level(db, level: str, *, changed_by: str) -> str:
    """Write the validated level. The caller owns the gate (admin + interactive
    — raising the ceiling is a GRANT); this owns the vocabulary."""
    value = (level or "").strip().upper()
    if value not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    db.set_setting(LEVEL_KEY, value)
    logger.info("[autonomy-dial] level set to %s by %s", value, changed_by)
    return value


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------

def _evidence_hash(evidence: Dict[str, Any]) -> str:
    """Identity of a verdict's evidence, so an unchanged re-evaluation writes
    nothing and two workers racing on the same row converge."""
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]


def negative_rating_since(db, agent_name: str, seat_email: str, *,
                          days: int = RATING_WINDOW_DAYS,
                          now: Optional[datetime] = None) -> Optional[str]:
    """The timestamp of this seat's most recent thumbs-down on this agent inside
    the window, or None. See the module docstring for why the prefixes and the
    COALESCE are both load-bearing. Never raises — an unreadable ratings table
    blocks promotion (the safe direction) rather than failing the read."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return db.latest_negative_seat_rating(
            agent_name, [f"{p}{(seat_email or '').strip().lower()}" for p in RATING_PREFIXES], cutoff
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[autonomy-dial] rating read failed for %s (%s)", agent_name, type(e).__name__)
        return "unreadable"


def _has_lapsed(row: dict, day: date) -> bool:
    """Has this record passed its own `review_by`? A record with no readable
    review date has NOT lapsed — an unparseable date must not quietly retire
    the evidence against a promotion."""
    try:
        return date.fromisoformat(str(row.get("review_by"))) < day
    except (TypeError, ValueError):
        return False


def class_evidence(rows: List[dict], ask_class: str, *, today: Optional[date] = None) -> Dict[str, Any]:
    """The decision half, for ONE class, over the window of non-expired records.

    Reversals are counted here rather than taken from
    `seat_decision_service.stats`, which counts them over all history — see the
    module docstring. `expires_at` is the EARLIEST `review_by` in the window:
    the evidence stops existing when its first record does, not when its last.
    """
    from services import seat_decision_service as sd

    day = today or _today()
    window, reversals, criteria, ids = [], 0, [], []
    for r in rows:
        if (r.get("ask_class") or "") != ask_class:
            continue
        eff = sd.effective_status(r, today=day)
        if eff == "reversed":
            # `effective_status` returns `reversed` for a non-active row
            # UNCONDITIONALLY — a reversed record never becomes `expired`. Counted
            # that way, one reversal ever would block this class forever, and with
            # hold-never-promote `on_request` would be the only reachable steady
            # state. A reversal is evidence for as long as its own record is:
            # past its `review_by` it leaves the window like anything else.
            if not _has_lapsed(r, day):
                reversals += 1
            continue
        if eff in ("expired", "routed"):
            continue
        window.append(r)
        ids.append(r["id"])
        crit = sd.normalize_criterion(r.get("criterion") or "")
        if crit and crit not in criteria:
            criteria.append(crit)
    review_dates = sorted(str(r.get("review_by")) for r in window if r.get("review_by"))
    return {
        "ask_class": ask_class,
        "count": len(window),
        "criteria": criteria,
        "reversals": reversals,
        "decision_ids": ids[:20],
        "expires_at": review_dates[0] if review_dates else None,
        "rule_version": RULE_VERSION,
    }


def evaluate_class(evidence: Dict[str, Any], *, guard_state: str,
                   negative_rating: Optional[str], held: bool) -> Dict[str, Any]:
    """The earned verdict for one class — the four evidence conjuncts, no live
    ones. Returns `{state, blocked_by[], evidence}`; `blocked_by` is every
    reason, not the first, because a person fixing one wants to see the rest."""
    from services.seat_decision_service import STABLE_MIN_COUNT

    blocked: List[str] = []
    if held:
        blocked.append(BLOCK_HELD)
    if guard_state == GUARD_CAPPED:
        blocked.append(BLOCK_GUARD)
    if evidence["count"] < STABLE_MIN_COUNT:
        blocked.append(BLOCK_TOO_FEW)
    elif len(evidence["criteria"]) != 1:
        blocked.append(BLOCK_CRITERIA)
    if evidence["reversals"]:
        blocked.append(BLOCK_REVERSAL)
    if negative_rating:
        blocked.append(BLOCK_RATING)
    full = {**evidence, "guard_metric": guard_state,
            "negative_rating_at": negative_rating, "decided_at": _now_iso()}
    return {
        "state": STATE_ON_REQUEST if blocked else STATE_GRADUATED,
        "blocked_by": blocked,
        "evidence": full,
        "evidence_hash": _evidence_hash({k: full[k] for k in
                                         ("count", "criteria", "reversals", "guard_metric",
                                          "negative_rating_at", "expires_at", "rule_version")}),
        "expires_at": evidence["expires_at"],
    }


def live_verdict(stored: Optional[dict], *, level: str, autonomy_enabled: bool,
                 today: Optional[date] = None) -> Dict[str, Any]:
    """The stored earned-state ANDed with the three live conjuncts.

    This is the ONLY function a consumer should ask "is this class unprompted?".
    A missing row is `on_request` with no blockers of its own — nothing has been
    earned yet, and the absence of a verdict is not permission.
    """
    blocked = list((stored or {}).get("blocked_by") or [])
    state = (stored or {}).get("state") or STATE_ON_REQUEST
    if not level_allows_unprompted(level):
        blocked.append(BLOCK_LEVEL)
    if not autonomy_enabled:
        blocked.append(BLOCK_AUTONOMY_OFF)
    expires = (stored or {}).get("evidence_expires_at")
    if state == STATE_GRADUATED and expires:
        try:
            if date.fromisoformat(str(expires)) < (today or _today()):
                blocked.append(BLOCK_EVIDENCE_EXPIRED)
        except (TypeError, ValueError):
            blocked.append(BLOCK_EVIDENCE_EXPIRED)
    unprompted = state == STATE_GRADUATED and not blocked
    return {
        "state": STATE_GRADUATED if unprompted else STATE_ON_REQUEST,
        "earned_state": state,
        "unprompted": unprompted,
        "blocked_by": list(dict.fromkeys(blocked)),
        "evidence": (stored or {}).get("evidence") or {},
        "evidence_expires_at": expires,
        "guard_metric": ((stored or {}).get("evidence") or {}).get("guard_metric", GUARD_NOT_ASSESSED),
        "held": bool((stored or {}).get("held")),
    }


def evaluate_seat(db, agent_name: str, seat_email: str, *,
                  persist: bool = False, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Every ask class this seat has decisions for, with its live verdict.

    ``persist`` is False on every READ. A read that writes makes an owner
    opening the panel a writer for every other seat, and two workers then race
    on rows neither of them changed; the event paths (a decision written or
    acted on, a rating, an operator hold) pass True.
    """
    from services.seat_decision_service import MAX_ROWS_PER_SEAT

    seat = (seat_email or "").strip().lower()
    rows = db.list_seat_decisions(agent_name, seat, limit=MAX_ROWS_PER_SEAT)
    classes = sorted({(r.get("ask_class") or "") for r in rows} - {""})
    if not classes:
        return []
    stored_rows = {s["ask_class"]: s for s in db.list_seat_ask_class_states(agent_name, seat)}
    negative = negative_rating_since(db, agent_name, seat)
    level = get_level(db)
    autonomy_enabled = bool(db.get_autonomy_enabled(agent_name))

    out: List[Dict[str, Any]] = []
    for ask_class in classes:
        stored = stored_rows.get(ask_class) or {}
        guard = stored.get("guard_metric") or GUARD_NOT_ASSESSED
        verdict = evaluate_class(
            class_evidence(rows, ask_class, today=today),
            guard_state=guard, negative_rating=negative, held=bool(stored.get("held")),
        )
        if persist and verdict["evidence_hash"] != stored.get("evidence_hash"):
            try:
                db.upsert_seat_ask_class_state(
                    agent_name=agent_name, seat_email=seat, ask_class=ask_class,
                    state=verdict["state"], blocked_by=verdict["blocked_by"],
                    evidence=verdict["evidence"], evidence_hash=verdict["evidence_hash"],
                    evidence_expires_at=verdict["expires_at"],
                    previous_hash=stored.get("evidence_hash"),
                )
                stored = db.get_seat_ask_class_state(agent_name, seat, ask_class) or stored
            except Exception as e:  # noqa: BLE001 — a verdict write must not fail the event
                logger.warning("[autonomy-dial] state write failed for %s/%s: %s",
                               agent_name, ask_class, type(e).__name__)
        merged = {**stored, **{k: verdict[k] for k in ("state", "blocked_by", "evidence")},
                  "evidence_expires_at": verdict["expires_at"]}
        live = live_verdict(merged, level=level, autonomy_enabled=autonomy_enabled, today=today)
        out.append({"ask_class": ask_class, **live})
    return out


def seat_summary(db, agent_name: str, seat_email: str, **kw) -> Dict[str, Any]:
    """What the Workspace panel and the MCP read both answer with."""
    level = get_level(db)
    autonomy_enabled = bool(db.get_autonomy_enabled(agent_name))
    return {
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "ceiling_allows_unprompted": level_allows_unprompted(level),
        "agent_autonomy_enabled": autonomy_enabled,
        "rating_window_days": RATING_WINDOW_DAYS,
        "rule_version": RULE_VERSION,
        "classes": evaluate_seat(db, agent_name, seat_email, **kw),
    }


def prompt_lines(classes: List[Dict[str, Any]], *, cap: int = 8) -> Optional[str]:
    """What the companion is told, in its own turn: which kinds of ask it may
    act on unprompted and which it may not. Without this the verdict is a
    surface nobody reads — "nothing is unprompted until promoted" has to be
    something the model is actually told."""
    if not classes:
        return None
    lines = ["## What you may do unprompted", ""]
    for c in classes[:cap]:
        if c["unprompted"]:
            lines.append(f"- `{c['ask_class']}`: graduated — you may act without being asked.")
        else:
            why = BLOCKER_TEXT.get((c["blocked_by"] or [None])[0], "not yet graduated")
            lines.append(f"- `{c['ask_class']}`: on-request — ask first ({why}).")
    lines.append("")
    lines.append("Anything not listed here is on-request. Recording decisions with a consistent "
                 "criterion is what graduates a class; a reversal or a thumbs-down returns it.")
    lines.append("")
    return "\n".join(lines)
