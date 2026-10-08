"""The role card in Agent details (trinity-enterprise#527, #663).

When a companion has a role (Tandem, ent#497), the Info rail shows what role it
fills, the objectives it owns or supports with each metric's value / target /
freshness, the viewer's relationship to it, and its readiness state. Framework
§8's "organisation UI over the canon" shrunk to one agent.

**Files are truth; this is a projection.** Everything about the role is read
from the agent's own container on each request, through the same agent door
the Files tab uses — never copied platform-side:

* `template.yaml` → `x-canon.clone_path` (default `canon`), and `x-role.status`
  for the template's own readiness claim (the wizard writes it, #511);
* `<canon>/roles/<id>.yaml` (framework §3.4).

**Which role is the seat on record, not the template (ent#811/#814).** The
role is the seat this agent serves FOR THIS VIEWER
(`assignment_provider.resolve_served_seat`): the seat it holds itself, else the
viewer's own seat on it, else its primary's — the same answer the agent is
given in its prompt. `x-role.role` / `x-role.seat` are no longer read; a
template that still says `x-role: sales-lead` while the Access tab says Head
of Sales showed two truths on one screen. `seat_source` says where the seat
came from (holds | person | primary | none). No seat → `role.error = no_seat`,
and supporting objectives still show. The viewer's relationship is the
provider's `kinds_for` (ent#638's optional method).

**The objectives are not this module's to compute (ent#676).** What the agent
is supposed to move, where each number is and whether to believe it is the one
objective ↔ metric join (`services/objective_join_service.read_objective_join`,
ent#666), called in process with the template and the client already in hand.
There is no second join and no second metric stale rule here; this module only
PROJECTS the join's answer down to what a Workspace client may see (TD-4): the
operator's remediation sentences, objective file paths and `owner: role:<id>`
stay on the operator door (the #78 auth-path invariant), and a finding crosses
as its code.

Every read is fail-soft and NAMED: no seat, no canon and no `x-role` → no card; an unreadable or
unparseable role file → `role.error`, never an empty role; a stopped agent →
`unavailable: agent_stopped`; objectives that could not be read →
`objectives_error`, never an empty list dressed as "this agent has none".

**Readiness is the one thing that is NOT read from the file.** `x-role.status`
is agent-writable and the 2026-09-20 ruling (#663) is that only the agent
OWNER flips `calibrating → ready`, never the agent, never automatically. So the
platform keeps the owner's stamp (`agent_role_readiness`) and the effective
state is: the stamp if one exists; else the template's `calibrating`; a
template that says `ready` with no stamp is shown as calibrating with the note
that no owner has stamped it.

HTTP-free (Invariant #1): the router maps `RoleCardRefused` to a status.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from database import db
from services.role_readiness_gate import brief_is_held, is_seat_delivery_schedule

from . import db as portal_db

logger = logging.getLogger(__name__)

#: Framework §3.5 — the staleness bound for canon FILES. On this card it
#: governs the role file's `review_by` and nothing else: whether a METRIC is
#: stale is the join's one rule (`metric_read_service.freshness`, 2× cadence),
#: never a second one here (ent#676).
STALE_AFTER_DAYS = 30
#: §7.2 step 9 — the three-strikes test asks for ten real asks.
WALKTHROUGH_ASKS = 10
#: Bounds on what crosses from author-controlled files to the card.
MAX_TEXT = 400
#: A text value or target on a metric row — the join's own bound on
#: `target_text`, applied to a recorded text value too.
MAX_VALUE_TEXT = 64
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

READINESS_STATES = ("calibrating", "ready")


class RoleCardRefused(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


# ---------------------------------------------------------------------------
# small pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def _text(v: Any, cap: int = MAX_TEXT) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:cap] if s else None


def _safe_id(v: Any) -> Optional[str]:
    s = _text(v, 64)
    return s if s and _ID_RE.match(s) else None


def parse_iso(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(as_of: Optional[str], *, now: Optional[datetime] = None,
             bound_days: int = STALE_AFTER_DAYS) -> bool:
    """Missing stamp, unparseable stamp, or older than the bound → stale.
    Never optimistic: an unknown age is stale, not current (quality bar #4).
    For a canon file's `review_by` only — never for a metric (ent#676)."""
    dt = parse_iso(as_of)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return dt < now - timedelta(days=bound_days)


def effective_readiness(template_status: Any, stamp: Optional[dict]) -> dict:
    """The one rule that keeps `ready` honest (#663).

    The owner's stamp wins when it exists. Without one, the template's word is
    `calibrating` whatever it says — and when it says `ready`, the card also
    says that no owner has stamped it, because that is the defect the ruling
    exists to make visible.
    """
    if stamp and stamp.get("status") in READINESS_STATES:
        # ent#689: the rollout's one-time seed is not an owner's act. It says
        # so, and carries no person — `changed_by` is a sentinel, not an email.
        rollout = str(stamp.get("changed_by") or "").startswith("rollout:")
        return {
            "status": stamp["status"],
            "changed_at": stamp.get("changed_at"),
            "changed_by": None if rollout else stamp.get("changed_by"),
            "source": "rollout" if rollout else "owner",
            "unstamped_ready": False,
        }
    claimed = (_text(template_status, 32) or "calibrating").lower()
    return {
        "status": "calibrating",
        "changed_at": None,
        "changed_by": None,
        "source": "template",
        "unstamped_ready": claimed == "ready",
    }


# ---------------------------------------------------------------------------
# the portal projection of the objective join (ent#676, TD-4)
#
# Every function here PICKS fields; none of them copies a row. A key the join
# grows tomorrow therefore does not reach a Workspace client until someone adds
# it here and to the model on purpose.
# ---------------------------------------------------------------------------

def _value(v: Any) -> Any:
    """A number as it is; agent-written text bounded; anything else nothing."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    return _text(v, MAX_VALUE_TEXT) if isinstance(v, str) else None


def portal_metric(row: dict) -> dict:
    """One joined metric as a Workspace client may see it.

    The finding crosses as its CODE: the sentence beside it on the operator
    door is remediation ("call refresh_metric_definitions") and names files
    the client does not own. `gap` crosses as its status — position relative
    to the target, never pace, and never the delta.
    """
    gap = row.get("gap") if isinstance(row.get("gap"), dict) else {}
    finding = row.get("finding") if isinstance(row.get("finding"), dict) else None
    target = row.get("target")
    return {
        "name": row.get("name"),
        "type": _text(row.get("type"), 32),
        "unit": _text(row.get("unit"), 32),
        "target": _value(target if target is not None else row.get("target_text")),
        "actual": _value(row.get("actual")),
        "last_point_at": row.get("last_point_at"),
        "stale": row.get("stale") is True,
        "freshness": row.get("freshness"),
        "gap": {"status": gap.get("status") or "not_computable"},
        "finding": {"code": finding["code"]} if finding and finding.get("code") else None,
    }


def portal_objective(obj: dict) -> dict:
    """One joined objective as a Workspace client may see it — no `owner`, no
    canon `path`, no `review_by`: those describe a canon the client does not
    own."""
    metrics = obj.get("metrics") if isinstance(obj.get("metrics"), list) else []
    return {
        "id": obj.get("id"),
        "statement": obj.get("statement"),
        "horizon": obj.get("horizon"),
        "status": obj.get("status"),
        "owned": obj.get("owned") is True,
        "metrics": [portal_metric(m) for m in metrics if isinstance(m, dict)],
    }


def finding_codes(join: dict) -> list[str]:
    """The join's findings as distinct codes, first seen first. Codes only: a
    file-level finding can name another role's file, and whose it is cannot be
    known when it never parsed."""
    codes: list[str] = []
    for finding in join.get("findings") or []:
        code = finding.get("code") if isinstance(finding, dict) else None
        if isinstance(code, str) and code not in codes:
            codes.append(code)
    return codes


#: The join's file-level failures: an objective file that would not read, would
#: not parse, or was refused by name. Whose file it was is unknowable (it never
#: parsed), so on the card they mean "the list may be missing some".
_FILE_FAILURE_CODES = frozenset({
    "objective_unreadable", "objective_invalid", "objective_file_skipped"})


def objectives_partial(join: dict) -> bool:
    """True when objectives joined but some objective files were not read.

    The list on the card is then possibly incomplete, and it must not look
    like a complete one (design-system principle 15). Never true for zero
    objectives — `objectives_error` names that case.
    """
    if not join.get("objectives"):
        return False
    source = join.get("source") if isinstance(join.get("source"), dict) else {}
    return (bool(set(finding_codes(join)) & _FILE_FAILURE_CODES)
            or bool(source.get("objectives_unscanned")))


def objectives_error(join: dict) -> Optional[str]:
    """Why the card shows NO objectives — or None when that is simply true.

    An agent with no objectives directory, or none that name it, has none: a
    real empty, and the card says nothing. Every other way of arriving at zero
    is a read that did not happen, and rendering it as the same empty would
    turn "could not be read" into "has none".
    """
    if join.get("objectives"):
        return None
    if join.get("unavailable"):
        return "agent_unreachable"
    source = join.get("source") if isinstance(join.get("source"), dict) else {}
    state = source.get("objectives_dir")
    codes = set(finding_codes(join))
    if state == "timeout":
        return "objectives_timeout"
    if state == "unreadable" or "objective_unreadable" in codes:
        return "objectives_unreadable"
    if (codes & {"objective_invalid", "objective_file_skipped"}
            or source.get("objectives_unscanned")):
        return "objectives_incomplete"
    return None


# ---------------------------------------------------------------------------
# agent-side reads (all fail-soft, all through the agent door)
# ---------------------------------------------------------------------------

async def _read_yaml(client, path: str) -> tuple[Optional[dict], Optional[str]]:
    """(parsed, error_code). A missing file is `not_found`; anything that is
    not a mapping is `invalid`."""
    from utils.safe_yaml import load_template_yaml
    try:
        res = await client.read_file(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: read %s failed: %s", path, e)
        return None, "unreadable"
    if not res or not res.get("success"):
        return None, "unreadable"
    if res.get("not_found") or res.get("content") is None:
        return None, "not_found"
    try:
        data = load_template_yaml(res["content"])
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: parse %s failed: %s", path, e)
        return None, "invalid"
    return (data, None) if isinstance(data, dict) else (None, "invalid")


# ---------------------------------------------------------------------------
# the card
# ---------------------------------------------------------------------------

async def build_role_card(agent_name: str, email: str, *, is_platform: bool,
                          admit_objectives: Callable[[], bool]) -> dict:
    """Everything the Role card shows, or `{"role": None}` when the agent has no role.

    The relationship line is the viewer's assignment kind on this agent
    (`kinds_for`), or `None` — rendered as "no assignment recorded", never blank.

    `admit_objectives` is the router's check against the objective-read budget
    (`services/objectives_read_budget`), called here — once, and only when the
    objectives are about to be read — so a card that stops earlier (no role, a
    stopped agent, a role file that failed) spends nothing. It is required,
    never defaulted: a caller that forgot it would reach the container fan-out
    unbounded. False leaves the objectives out and says so; the rest of the
    card still answers.
    """
    from services import docker_utils
    from services.agent_client import get_agent_client
    # Function-local so no portal suite drags the metrics stack in, and outside
    # any `try`: an import that fails here must be loud, not read as "the
    # objectives could not be read" by the fail-soft handler below.
    from services import objective_join_service

    stamp = _readiness_stamp(agent_name)
    relationship = _relationship(agent_name, email)

    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception:  # noqa: BLE001
        state = None
    if state != "running":
        # Files are truth and the files live in the container. Say so rather
        # than render a card from nothing — but the owner's stamp still shows,
        # it is the one fact that is not in the container.
        return {
            "agent_name": agent_name,
            "role": None,
            "unavailable": "agent_stopped" if state else "agent_unreachable",
            "readiness": effective_readiness(None, stamp) if stamp else None,
            "relationship": relationship,
            "can_flip_readiness": _is_owner(agent_name, email, is_platform),
        }

    client = get_agent_client(agent_name)
    template, terr = await _read_yaml(client, "template.yaml")
    xrole = template.get("x-role") if isinstance(template, dict) else None
    xrole = xrole if isinstance(xrole, dict) else {}
    has_canon = isinstance(template, dict) and isinstance(template.get("x-canon"), dict)
    served = _served_seat(agent_name, email)
    if not (served["role_id"] or has_canon or xrole):
        # No seat, no canon, no role declared → no card (AC 5). An unreadable
        # template with no seat on record is the same answer: nothing to show.
        return {"agent_name": agent_name, "role": None}

    role_id = _safe_id(served["role_id"])
    # The join's validator, not a copy of it — one rule for what may reach a
    # file read.
    root = objective_join_service.canon_root(template)
    card: dict[str, Any] = {
        "agent_name": agent_name,
        "role": {
            "id": role_id,
            "title": None, "mission": None, "status": None, "review_by": None,
            "path": f"{root}/roles/{role_id}.yaml" if (root and role_id) else None,
            "error": None,
        },
        "seat_source": served["source"],
        "objectives": [],
        "objectives_error": None,
        "objectives_partial": False,
        "finding_codes": [],
        "readiness": effective_readiness(xrole.get("status"), stamp),
        # Platform viewers only: an external client can neither act on a held
        # brief nor see the schedules it comes from (the flip is platform-only too).
        "brief_held": is_platform and _brief_held(agent_name, stamp),
        "walkthrough": _walkthrough(agent_name, email, is_platform),
        "relationship": relationship,
        "can_flip_readiness": _is_owner(agent_name, email, is_platform),
    }
    if not root:
        card["role"]["error"] = "canon_path_invalid"
        return card
    if not role_id:
        # No seat on record (or one that is not a valid id): say so on the role,
        # and still read the objectives — supporting work shows without a seat
        # (ent#812), and the join names the same `no_seat`.
        card["role"]["error"] = "role_id_invalid" if served["role_id"] else "no_seat"
    else:
        role, rerr = await _read_yaml(client, card["role"]["path"])
        if rerr:
            card["role"]["error"] = f"role_file_{rerr}"
            return card
        card["role"].update({
            "title": _text(role.get("title"), 120) or role_id,
            "mission": _text(role.get("mission")),
            "status": _text(role.get("status"), 32),
            "review_by": _text(role.get("review_by"), 32),
            "stale": is_stale(_text(role.get("review_by"), 32)) if role.get("review_by") else False,
        })

    if not admit_objectives():
        card["objectives_error"] = "objectives_rate_limited"
        return card

    # Objectives, their numbers and their freshness: the one join (ent#666).
    try:
        join = await objective_join_service.read_objective_join(
            agent_name, template=template, client=client)
    except Exception:  # noqa: BLE001 — the card is fail-soft and named
        # The store is the one thing the join raises for. Whatever it was, the
        # role and the owner's readiness control must still answer; the
        # traceback keeps a defect loud in the log.
        logger.exception("role card: objective join failed for %s", agent_name)
        card["objectives_error"] = "objectives_unreadable"
        return card
    card["objectives"] = [portal_objective(o) for o in join.get("objectives") or []
                          if isinstance(o, dict)]
    card["objectives_error"] = objectives_error(join)
    card["objectives_partial"] = objectives_partial(join)
    card["finding_codes"] = finding_codes(join)
    return card


def _served_seat(agent_name: str, email: str) -> dict:
    """The seat this agent serves for this viewer — the seam's own never-raising
    answer, with a belt for an import-time failure."""
    try:
        from services import assignment_provider
        return assignment_provider.resolve_served_seat(agent_name, email)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: seat read failed for %s: %s", agent_name, e)
        return {"role_id": None, "source": "none"}


def _relationship(agent_name: str, email: str) -> Optional[str]:
    """The viewer's assignment kind on this agent, or None. The reader gate's
    own rule (`seat_decision_service.reader_kind`): a missing method, a raise or
    an unknown kind all read as None."""
    if not email:
        return None
    try:
        from services import seat_decision_service
        return seat_decision_service.reader_kind(agent_name, email.strip().lower())
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: relationship read failed for %s: %s", agent_name, e)
        return None


def _brief_held(agent_name: str, stamp: Optional[dict]) -> bool:
    """Whether a proactive brief is being held (ent#689): the agent has a live
    seat-delivery schedule and its stamp is not `ready`. Reads the stamp the
    gate reads, never the template. Fail-soft: an unreadable schedule list says
    nothing rather than a claim about a pause."""
    status = stamp.get("status") if stamp else None
    if status == "ready":
        return False
    try:
        # Autonomy off stops every schedule before readiness is asked; saying
        # "paused until you mark it ready" then would promise a flip that
        # starts nothing. The rule itself is shared with the agents list
        # (services/role_readiness_gate.brief_is_held) so the two never disagree.
        autonomy = db.get_autonomy_enabled(agent_name)
        if not autonomy:
            return False
        seated = any(
            is_seat_delivery_schedule(s.enabled, s.deliver_to_workspace_email)
            for s in db.list_agent_schedules(agent_name)
        )
        return brief_is_held(status, autonomy, seated)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: schedule read failed for %s: %s", agent_name, e)
        return False


def _readiness_stamp(agent_name: str) -> Optional[dict]:
    try:
        return db.get_agent_role_readiness(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: readiness read failed for %s: %s", agent_name, e)
        return None


def _is_owner(agent_name: str, email: str, is_platform: bool) -> bool:
    """The platform's OWNER of the agent record — creator / infra owner, not an
    assignment kind (R8/R11). A portal-token principal is never an owner."""
    if not is_platform or not email:
        return False
    try:
        return agent_name in {r["agent_name"] for r in portal_db.get_owned_roster(email)}
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: owner check failed for %s: %s", agent_name, e)
        return False


def _walkthrough(agent_name: str, email: str, is_platform: bool) -> dict:
    """The viewer's own asks in their Main chat with this agent (capped at the
    ten the three-strikes test names) and how many replies they rated down.
    A proxy for the §7.2 walkthrough, labelled as the viewer's own count."""
    out = {"asks": 0, "target": WALKTHROUGH_ASKS, "rated_down": 0, "unavailable": False}
    try:
        from .service import workspace_evaluator
        main_id = portal_db.get_main_portal_session_id(agent_name, email)
        if not main_id:
            return out
        messages = portal_db.get_portal_messages(agent_name, email, limit=200, session_id=main_id)
        asks = [m for m in messages if m.get("role") == "user"]
        replies = [m.get("id") for m in messages if m.get("role") == "assistant" and m.get("id")]
        out["asks"] = min(len(asks), WALKTHROUGH_ASKS)
        if replies:
            mine = db.list_workspace_ratings_for_targets(
                workspace_evaluator(email, is_platform=is_platform), "message", replies)
            out["rated_down"] = sum(1 for q in mine.values() if q is not None and q < 0.5)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: walkthrough read failed for %s: %s", agent_name, e)
        out["unavailable"] = True
    return out


def flip_readiness(agent_name: str, email: str, *, is_platform: bool, status: str) -> dict:
    """The owner's act (#663). Anyone else is refused by name; the value is
    validated before the row is touched."""
    if status not in READINESS_STATES:
        raise RoleCardRefused("readiness_unknown_state",
                              f"Readiness is 'calibrating' or 'ready', not {status!r}.", 422)
    if not _is_owner(agent_name, email, is_platform):
        raise RoleCardRefused(
            "readiness_owner_only",
            "Only the agent's owner can change its readiness — the person who created "
            "it on this instance, not an assignment.", 403)
    stamp = db.set_agent_role_readiness(agent_name, status, email)
    return effective_readiness(None, stamp)
