"""The seat-level decision record — grammar, lifecycle, evidence (ent#638, R25).

A *seat* is an agent × the person it serves (the ent#637 memory scope). Every
week that seat approves, defers or kills things; the judgment used to evaporate
with the chat. This module is the policy leaf over ``db/seat_decisions.py``:

* **Grammar (tandem-07 §7a).** A record is lintable, not prose: `outcome`,
  `decided`, the `alternatives` that were live, the `criterion` that
  discriminated, who decided (role + person), `decided_at` / `review_by`, and
  what would `reverse` it. Free prose lives in `notes` only. Prose where a
  field belongs is refused with a receipt naming each failing field; a record
  with no alternatives is refused as a note (it is one).
* **Direction is not a seat decision (R C3).** Pricing, positioning, roadmap
  go to direction canon as a proposal. The platform cannot tell that from
  text, so the caller declares `scope`; a `direction` record is kept
  visibly as `routed` (it must not evaporate — the very thing R25 closes)
  and the receipt says where it belongs.
* **It expires, it is corrected by supersession, history stays.** `expired`
  is computed on read (`review_by` before today, UTC), never written; a
  correction inserts a new row that supersedes the old one; close / reverse
  are status flips. Nothing is deleted.
* **Evidence.** `reused` = records cited by a LATER record of the seat (the
  conversion metric — not volume); per `ask_class`: the distinct criteria,
  the reversals, and `stable` (≥3 records, one criterion, no reversal) — the
  autonomy dial's input (tandem-06 §2.2, ent#641), not its verdict.
* **Readers.** The seat and the agent's owner see everything; another
  stakeholder sees a seat read-only when the assignment provider says they
  hold a kind on the agent (`kinds_for`, an optional seam method — absent on
  a core build → own seat only). The filter runs before serialization.

Field names match the role pack's YAML (`record-decision`, ent#510) so the
canon-folder copy is a move, not a translation: `outcome`, `decided`,
`alternatives`, `criterion`, `decided_by: {role, person}`, `decided_at`,
`review_by`, `reversal`, `notes`, `ask_class`, `cites`.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

OUTCOMES = ("approved", "deferred", "killed")
SCOPES = ("seat", "direction")
ACTIONS = ("close", "reverse", "reconfirm", "supersede")
READER_KINDS = frozenset({"primary", "approver", "collaborator", "viewer"})

MAX_FIELD = 280          # decided / criterion / reversal
MAX_ALTERNATIVE = 160
MAX_ALTERNATIVES = 8
MAX_NOTES = 2000
MAX_CITES = 20
MAX_REVIEW_DAYS = 366
MAX_ROWS_PER_SEAT = 500
MAX_SEATS = 50
STABLE_MIN_COUNT = 3
PROMPT_BLOCK_MAX = 12    # decisions injected into a seat's turn
PROMPT_BLOCK_BYTES = 1500

_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

CANON_PROPOSAL_HINT = (
    "A decision about direction — pricing, positioning, roadmap — is not a seat "
    "decision (R C3). It is kept here as `routed` so it does not evaporate; take it "
    "to direction canon as a proposal (the canon-publish path), where it gets a "
    "status, an owner and a review date."
)


class DecisionRefused(Exception):
    """A named refusal with a receipt the caller can act on."""

    def __init__(self, code: str, message: str, status_code: int = 422,
                 receipt: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = message
        self.status_code = status_code
        self.receipt = receipt or {}

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.detail, "receipt": self.receipt}


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

def _clean(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _field_problem(name: str, value: str, cap: int, *, required: bool = True) -> Optional[str]:
    if not value:
        return f"{name} is required" if required else None
    if "\n" in value.strip():
        return f"{name} must be one line (a paragraph is prose — put it in notes)"
    if len(value) > cap:
        return f"{name} is over {cap} characters (a paragraph is prose — put it in notes)"
    return None


def _today() -> date:
    return datetime.now(timezone.utc).date()


def parse_review_by(raw: Any, *, today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    """Strict `YYYY-MM-DD`, after today, at most a year out. Returns (value, problem)."""
    value = _clean(raw)
    if not value:
        return None, "review_by is required"
    if not _DATE_RE.match(value):
        return None, "review_by must be a date (YYYY-MM-DD)"
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None, "review_by must be a real date (YYYY-MM-DD)"
    t = today or _today()
    if d <= t:
        return None, "review_by must be after today (a decision that expires today is already expired)"
    if d > t + timedelta(days=MAX_REVIEW_DAYS):
        return None, f"review_by must be within {MAX_REVIEW_DAYS} days (reconfirm it later instead)"
    return value, None


def normalize_criterion(text: str) -> str:
    """The equality the stability evidence uses: case, whitespace and trailing
    punctuation folded — a paraphrase is still a different criterion."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(".!")


def validate_record(payload: dict, *, today: Optional[date] = None) -> dict:
    """The restricted grammar. Returns the clean fields or raises
    :class:`DecisionRefused` with a receipt naming every failing field."""
    problems: Dict[str, str] = {}
    outcome = _clean(payload.get("outcome")).lower()
    if outcome not in OUTCOMES:
        problems["outcome"] = f"outcome must be one of {', '.join(OUTCOMES)}"
    scope = _clean(payload.get("scope") or "seat").lower()
    if scope not in SCOPES:
        problems["scope"] = f"scope must be one of {', '.join(SCOPES)}"

    decided = _clean(payload.get("decided"))
    criterion = _clean(payload.get("criterion"))
    reversal = _clean(payload.get("reversal"))
    for name, value in (("decided", decided), ("criterion", criterion), ("reversal", reversal)):
        p = _field_problem(name, value, MAX_FIELD)
        if p:
            problems[name] = p

    raw_alts = payload.get("alternatives")
    alternatives: List[str] = []
    if isinstance(raw_alts, str):
        raw_alts = [raw_alts]
    if isinstance(raw_alts, (list, tuple)):
        for a in raw_alts:
            a = _clean(a)
            if a and a not in alternatives:
                alternatives.append(a)
    if not alternatives:
        notes_only = _clean(payload.get("notes")) and not (decided and criterion)
        raise DecisionRefused(
            "decision_is_a_note",
            "A decision with no alternatives is a note, not a decision — keep it in the "
            "seat's memory. Record the options that were live and the criterion that "
            "chose between them.",
            receipt={"fields": {"alternatives": "at least one alternative that was live"},
                     "prose_only": bool(notes_only)},
        )
    if len(alternatives) > MAX_ALTERNATIVES:
        problems["alternatives"] = f"at most {MAX_ALTERNATIVES} alternatives"
    for a in alternatives:
        p = _field_problem("alternatives", a, MAX_ALTERNATIVE)
        if p:
            problems["alternatives"] = p
            break
    if decided and decided in alternatives:
        problems.setdefault("alternatives", "the decided option is what won; list the others")

    review_by, p = parse_review_by(payload.get("review_by"), today=today)
    if p:
        problems["review_by"] = p

    notes = _clean(payload.get("notes")) or None
    if notes and len(notes) > MAX_NOTES:
        problems["notes"] = f"notes over {MAX_NOTES} characters"

    ask_class = _clean(payload.get("ask_class")).lower() or None
    if ask_class and not _SLUG_RE.match(ask_class):
        problems["ask_class"] = "ask_class must be a slug (a-z, 0-9, -, _; 64 max)"
    role = _clean(payload.get("decided_by_role")) or None
    if role and not _SLUG_RE.match(role.lower()):
        problems["decided_by_role"] = "decided_by_role must be a role id slug"

    raw_cites = payload.get("cites") or []
    cites = [c for c in raw_cites if isinstance(c, str) and c.strip()] if isinstance(raw_cites, (list, tuple)) else []
    if len(cites) > MAX_CITES:
        problems["cites"] = f"at most {MAX_CITES} citations"
    request_id = _clean(payload.get("request_id")) or None

    if problems:
        raise DecisionRefused(
            "decision_prose_only",
            "The record is prose where a field belongs — each field is one line; the "
            "reasoning goes in notes. Nothing was stored.",
            receipt={"fields": problems},
        )
    return {
        "outcome": outcome, "scope": scope, "decided": decided, "alternatives": alternatives,
        "criterion": criterion, "reversal": reversal, "review_by": review_by, "notes": notes,
        "ask_class": ask_class, "decided_by_role": role.lower() if role else None,
        "cites": list(dict.fromkeys(cites)), "request_id": request_id,
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def effective_status(row: dict, *, today: Optional[date] = None) -> str:
    """`expired` is a state computed from `review_by`, never stored (§7a: it
    expires rather than accreting). `review_by == today` is NOT expired."""
    status = row.get("status") or "active"
    if status != "active":
        return status
    try:
        if date.fromisoformat(str(row.get("review_by"))) < (today or _today()):
            return "expired"
    except (TypeError, ValueError):
        pass
    return "active"


def _validate_cites(db, agent_name: str, seat_email: str, cites: List[str]) -> None:
    """Every citation must be a record of the SAME seat — any status (history is
    the point). A cross-seat or unknown id is the uniform `unknown_citation`."""
    if not cites:
        return
    known = {r["id"] for r in db.list_seat_decisions(agent_name, seat_email, limit=MAX_ROWS_PER_SEAT)}
    unknown = [c for c in cites if c not in known]
    if unknown:
        raise DecisionRefused(
            "unknown_citation",
            "A cited decision must be one of this seat's own records.",
            receipt={"fields": {"cites": f"unknown: {', '.join(unknown[:5])}"}},
        )


def _request_belongs_to_agent(db, agent_name: str, request_id: str) -> bool:
    """`request_id` links a record to the operator-queue decision REQUEST it
    answers (#611's gate). It must name an item of THIS agent; unknown or
    foreign is the uniform `unknown_request`."""
    try:
        item = db.get_operator_queue_item(request_id)
    except Exception:  # noqa: BLE001
        return False
    if item is None:
        return False
    owner = item.get("agent_name") if isinstance(item, dict) else getattr(item, "agent_name", None)
    return owner == agent_name


def record(db, *, agent_name: str, seat_email: str, decided_by_person: str,
           payload: dict, source_execution_id: Optional[str] = None,
           decided_at: Optional[str] = None) -> dict:
    """Validate and insert. A `direction` record is stored as `routed` — visible,
    expiring, never a seat decision — and the caller receives the canon hint."""
    from utils.helpers import utc_now_iso

    fields = validate_record(payload)
    if fields["request_id"] and not _request_belongs_to_agent(db, agent_name, fields["request_id"]):
        raise DecisionRefused("unknown_request", "request_id does not name a decision request of this agent.",
                              receipt={"fields": {"request_id": "unknown"}})
    _validate_cites(db, agent_name, seat_email, fields["cites"])
    status = "routed" if fields["scope"] == "direction" else "active"
    row = db.insert_seat_decision({
        **fields,
        "agent_name": agent_name,
        "seat_email": seat_email,
        "decided_by_person": decided_by_person,
        "decided_at": decided_at or utc_now_iso(),
        "status": status,
        "source_execution_id": source_execution_id,
    })
    return row


def act(db, *, agent_name: str, seat_email: str, decision_id: str, action: str,
        by: str, reason: Optional[str] = None, review_by: Any = None,
        fields: Optional[dict] = None) -> dict:
    """close / reverse / reconfirm / supersede an ACTIVE decision of this seat.
    Anything else is a named 404/409 — never a silent no-op."""
    if action not in ACTIONS:
        raise DecisionRefused("unknown_action", f"action must be one of {', '.join(ACTIONS)}")
    row = db.get_seat_decision(agent_name, decision_id)
    if not row or row.get("seat_email") != (seat_email or "").strip().lower():
        raise DecisionRefused("decision_not_found", "No such decision for this seat.", status_code=404)
    if row.get("status") != "active":
        raise DecisionRefused(
            "decision_not_active",
            f"This decision is {row.get('status')}; only an active decision can be "
            f"{action}d." if action != "supersede" else
            f"This decision is {row.get('status')}; correct the active one.",
            status_code=409, receipt={"status": row.get("status")},
        )
    if action in ("close", "reverse"):
        reason = _clean(reason) or None
        if action == "reverse" and not reason:
            raise DecisionRefused("reason_required", "A reversal names what changed — that is the "
                                  "evidence the autonomy dial reads.",
                                  receipt={"fields": {"reason": "required for reverse"}})
        if reason and len(reason) > MAX_FIELD:
            raise DecisionRefused("decision_prose_only", "reason is one line.",
                                  receipt={"fields": {"reason": f"over {MAX_FIELD} characters"}})
        status = "closed" if action == "close" else "reversed"
        if not db.set_seat_decision_status(agent_name, decision_id, status, reason=reason, by=by):
            raise DecisionRefused("decision_not_active", "This decision changed under you; reload.",
                                  status_code=409)
        return db.get_seat_decision(agent_name, decision_id)
    if action == "reconfirm":
        value, p = parse_review_by(review_by)
        if p:
            raise DecisionRefused("decision_prose_only", p, receipt={"fields": {"review_by": p}})
        if not db.reconfirm_seat_decision(agent_name, decision_id, value):
            raise DecisionRefused("decision_not_active", "This decision changed under you; reload.",
                                  status_code=409)
        return db.get_seat_decision(agent_name, decision_id)
    # supersede: the correction is a NEW record; the old one stays as history.
    from utils.helpers import utc_now_iso

    merged = {**{k: row.get(k) for k in ("outcome", "decided", "alternatives", "criterion", "reversal",
                                          "review_by", "notes", "ask_class", "decided_by_role", "cites")},
              **(fields or {})}
    merged.setdefault("scope", "seat")
    clean = validate_record(merged)
    _validate_cites(db, agent_name, seat_email, clean["cites"])
    new_row = db.supersede_seat_decision(agent_name, decision_id, {
        **clean,
        "agent_name": agent_name,
        "seat_email": seat_email,
        "decided_by_person": by,
        "decided_at": utc_now_iso(),
        "request_id": row.get("request_id"),
        "source_execution_id": None,
    })
    if new_row is None:
        raise DecisionRefused("decision_not_active", "This decision changed under you; reload.",
                              status_code=409)
    return new_row


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def stats(rows: List[dict], *, today: Optional[date] = None) -> dict:
    """`reused` counts records cited by a LATER record of the same seat — a
    superseding record does not implicitly cite its predecessor. Per
    `ask_class`: the distinct normalized criteria, the reversals, and `stable`
    (≥ STABLE_MIN_COUNT records, one criterion, no reversal); expired records
    are excluded from stability, `routed` records from everything."""
    seat_rows = [r for r in rows if r.get("status") != "routed"]
    by_id = {r["id"]: r for r in seat_rows}
    cited: set = set()
    for r in seat_rows:
        for c in r.get("cites") or []:
            target = by_id.get(c)
            if target and str(target.get("decided_at") or "") < str(r.get("decided_at") or ""):
                cited.add(c)
    recorded = len(seat_rows)
    classes: Dict[str, dict] = {}
    for r in seat_rows:
        cls = r.get("ask_class")
        if not cls:
            continue
        eff = effective_status(r, today=today)
        c = classes.setdefault(cls, {"ask_class": cls, "count": 0, "criteria": [], "reversals": 0,
                                     "expired": 0, "stable": False})
        c["count"] += 1
        if eff == "reversed":
            c["reversals"] += 1
        if eff == "expired":
            c["expired"] += 1
            continue
        crit = normalize_criterion(r.get("criterion") or "")
        if crit and crit not in c["criteria"]:
            c["criteria"].append(crit)
    for c in classes.values():
        c["stable"] = (c["count"] - c["expired"]) >= STABLE_MIN_COUNT and len(c["criteria"]) == 1 and c["reversals"] == 0
    return {
        "recorded": recorded,
        "reused": len(cited),
        "reuse_rate": round(len(cited) / recorded, 3) if recorded else 0.0,
        "reversed": sum(1 for r in seat_rows if r.get("status") == "reversed"),
        "ask_classes": sorted(classes.values(), key=lambda c: (-c["count"], c["ask_class"])),
    }


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def reader_kind(agent_name: str, reader_email: str) -> Optional[str]:
    """What the assignment provider says this reader holds on the agent, or
    None. The seam's own failure rule: a missing method, a raise or a shape
    outside the allowlist all read as None — never a wider read."""
    try:
        from services import assignment_provider
        provider = assignment_provider.get_provider()
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(provider, "kinds_for", None) if provider is not None else None
    if fn is None:
        return None
    try:
        answer = fn(agent_name, reader_email)
    except Exception:  # noqa: BLE001
        logger.warning("[ent#638] provider.kinds_for failed for %s", agent_name, exc_info=True)
        return None
    kind = answer.get("kind") if isinstance(answer, dict) else None
    return kind if isinstance(kind, str) and kind in READER_KINDS else None


def readable_seats(db, agent_name: str, reader_email: str, *, is_owner: bool) -> Dict[str, bool]:
    """`{seat_email: writable}` for this reader. Own seat always (writable);
    the owner sees every seat (writable); a provider-declared stakeholder sees
    every seat read-only (per-agent in v1 — DEBT_INBOX 2026-09-22)."""
    me = (reader_email or "").strip().lower()
    seats: Dict[str, bool] = {me: True}
    if is_owner or reader_kind(agent_name, me) is not None:
        for s in db.list_seat_decision_seats(agent_name, limit=MAX_SEATS):
            seats.setdefault(s, is_owner)
    return seats


# ---------------------------------------------------------------------------
# Serialization — two audiences, two shapes
# ---------------------------------------------------------------------------

def to_human(row: dict, *, today: Optional[date] = None, writable: bool = False) -> dict:
    """The Workspace shape: every field, including the people."""
    return {
        "id": row["id"],
        "seat": row.get("seat_email"),
        "outcome": row.get("outcome"),
        "decided": row.get("decided"),
        "alternatives": list(row.get("alternatives") or []),
        "criterion": row.get("criterion"),
        "reversal": row.get("reversal"),
        "decided_by": {"role": row.get("decided_by_role"), "person": row.get("decided_by_person")},
        "decided_at": row.get("decided_at"),
        "review_by": row.get("review_by"),
        "notes": row.get("notes"),
        "ask_class": row.get("ask_class"),
        "scope": row.get("scope") or "seat",
        "status": effective_status(row, today=today),
        "supersedes_id": row.get("supersedes_id"),
        "cites": list(row.get("cites") or []),
        "request_id": row.get("request_id"),
        "close_reason": row.get("close_reason"),
        "closed_at": row.get("closed_at"),
        "closed_by": row.get("closed_by"),
        "reconfirmed_at": row.get("reconfirmed_at"),
        "writable": writable,
    }


def to_agent(row: dict, *, seat_email: str, today: Optional[date] = None) -> dict:
    """The companion's shape: NO person emails (the provider's "never an email"
    rule, one layer down — an execution can be an anonymous public turn).
    `decided_by.person` is a label: `seat` for the seat itself, `owner` for
    anyone else."""
    person = (row.get("decided_by_person") or "").lower()
    return {
        "id": row["id"],
        "outcome": row.get("outcome"),
        "decided": row.get("decided"),
        "alternatives": list(row.get("alternatives") or []),
        "criterion": row.get("criterion"),
        "reversal": row.get("reversal"),
        "decided_by": {"role": row.get("decided_by_role"),
                       "person": "seat" if person == (seat_email or "").lower() else "owner"},
        "decided_at": row.get("decided_at"),
        "review_by": row.get("review_by"),
        "notes": row.get("notes"),
        "ask_class": row.get("ask_class"),
        "scope": row.get("scope") or "seat",
        "status": effective_status(row, today=today),
        "supersedes_id": row.get("supersedes_id"),
        "cites": list(row.get("cites") or []),
        "close_reason": row.get("close_reason"),
    }


def prompt_block(db, agent_name: str, seat_email: str, *, today: Optional[date] = None) -> Optional[str]:
    """The seat's ACTIVE decisions, criterion first, for the turn's system
    prompt — the read-into-context path that makes reuse possible. Bounded
    (PROMPT_BLOCK_MAX records / ~PROMPT_BLOCK_BYTES). None when there are none
    or the read fails — a prompt must never break on this."""
    try:
        rows = db.list_seat_decisions(agent_name, seat_email, limit=MAX_ROWS_PER_SEAT)
    except Exception as e:  # noqa: BLE001
        logger.warning("[ent#638] decisions read failed for %s: %s", agent_name, e)
        return None
    active = [r for r in rows if effective_status(r, today=today) == "active"][:PROMPT_BLOCK_MAX]
    if not active:
        return None
    lines = ["## This seat's standing decisions", "",
             "Reuse the criterion when the same kind of ask comes up; cite the id when you record "
             "a decision that leans on one (record_decision cites=[...]).", ""]
    size = sum(len(l) for l in lines)
    for r in active:
        line = (f"- [{r['id']}] {r.get('outcome')}: {r.get('decided')} — because {r.get('criterion')}"
                + (f" (ask class: {r['ask_class']})" if r.get("ask_class") else "")
                + f"; review by {r.get('review_by')}; reverses if {r.get('reversal')}")
        if size + len(line) > PROMPT_BLOCK_BYTES:
            lines.append("- … more on the Workspace")
            break
        lines.append(line)
        size += len(line)
    lines.append("")
    return "\n".join(lines)
