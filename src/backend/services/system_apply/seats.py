"""A manifest member's `seat:` and `person_seats:` (trinity-enterprise#1030 CP4).

`seat: <role_id>` is the canon role the AGENT itself holds (ent#811).
`person_seats: [{role, kind}]` names seats a PERSON must fill — the role and
the kind, never the person: the manifest is committed to git, and a person's
consent to fill a seat is that person's act, so apply only ever REPORTS them.

The assignment record is a registered module's, not core's. This group reads
and writes it only through the optional provider hooks on
`services/assignment_provider` (`seat_for` + `set_seat`, `person_seat_status`),
each read through `getattr`. Without a hook the class is UNOBSERVED for that
member — reported `seat_unobserved`, never "in sync" and never a blocker — and
nothing in this repo writes a seat.

* `parse_seat` / `parse_person_seats` — the parser's shape checks, refused by
  name; an email-shaped value is refused without being echoed.
* `read_member` — the snapshot's read. A provider error or malformed answer is
  a READ failure (it raises, so the snapshot blocks the member retryably);
  "no hook" is not, and neither is a provider that answers nothing (#4132:
  case `unobserved`, planned as `seat_unobserved`).
* `plan` — `seat_missing` / `seat_differs` writes when the writer exists and
  the caller passes the grant gate, `seat_requires_interactive_admin` when the
  writer exists and the caller does not, otherwise `seat_unobserved`;
  `seat_filled` / `seat_vacant` /
  `seat_unobserved` reports for person seats, never a write — and
  `seat_unobserved` for a member apply will create, which has nothing to read.
* `summarize` — `summary.roles.held_back` and its one warning line.
* `run` — op `seat_set` through `assignment_provider.write_seat`; a refusal is
  recorded in `failed[]` with the module's code.
* `holder: member` (trinity-enterprise#1174) — the ONE person seat apply
  writes: the instance's recorded hub member, as `primary`, never named in the
  manifest (an interactive admin records who on the instance). Read and
  written through the optional `member_seat_status` / `seat_member` hooks.
* `export_member` — `seat:` when the provider says the agent holds one;
  without a provider that can say — or one that answers nothing (#4132) —
  the export names the gap instead.
  Person seats are not exported: the seam answers per seat, it does not list.
* `deploy` — create mode's phase, after grants, behind the grant phase's own
  principal check: `deploy_system` is agent-callable, so anything but an
  admin in a signed-in session writes no seat and is told.

The core judges the principal before any write reaches the provider; the
actor it passes is the username, for attribution only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from services import assignment_provider
from services.assignment_provider import ROLE_ID_RE

SEAT_STEP = "seat"
PERSON_STEP = "person_seats"
GROUP_FIELDS = (SEAT_STEP, PERSON_STEP)
OP_KIND = "seat_set"
MEMBER_OP_KIND = "member_seat_set"  # trinity-enterprise#1174
#: The kinds a person may hold a seat as — the seam's `kinds_for` shape.
PERSON_SEAT_KINDS = ("primary", "approver", "collaborator", "viewer")
PERSON_SEAT_KEYS = ("role", "kind", "holder")
#: `holder:`'s only value — the instance's recorded hub member (ent#1174 T1).
HOLDER_MEMBER = "member"
#: The only kind `holder: member` may take (ent#1174 T6).
MEMBER_KIND = "primary"
UNOBSERVED_CODE = "seat_unobserved"
HELD_BACK_CODE = "seat_requires_interactive_admin"
DEPLOY_HELD_BACK_CODE = "seats_require_interactive_admin"
_HELD_BACK_NOTE = ("seating an agent requires an admin in a signed-in session, the same "
                   "gate as a grant; nothing was written. Run apply from such a session")
_UNOBSERVED_NOTE = ("no assignment module on this install answers this; the seat is "
                    "neither read nor written, which is not the same as in sync")
_NO_ANSWER_NOTE = ("the assignment module gave no answer for this agent; the seat is "
                   "neither read nor written, which is not the same as in sync")
_DIFFERS_NOTE = ("another declaration may hold this seat; apply moves it only on a "
                 "real run")
_PERSON_NOTE = "apply never fills a person seat: a person assigns themselves or is assigned"
_NOT_YET_NOTE = ("the member does not exist yet, so nothing was read; apply never fills a "
                 "person seat")
_MEMBER_HELD_BACK_NOTE = (
    "seating the hub member takes an admin: through a key, only on an agent this apply "
    "creates or one the key's owner owns; otherwise an admin in a signed-in session. "
    "Nothing was written")
_MEMBER_FILLED_NOTE = "the hub member holds the primary seat and sees this agent"
_MEMBER_DIFFERS_NOTE = ("another person holds the primary seat; apply never moves it. Move "
                        "the primary in the agent's Assignments panel from a signed-in session")
_MEMBER_UNENTITLED_NOTE = ("the module that seats the hub member is not entitled on this "
                           "instance; enable the {} entitlement, then re-run apply")
_MEMBER_NOT_RECORDED_NOTE = ("no hub member is recorded on this instance; an admin in a "
                             "signed-in session records one (PUT /api/enterprise/hub/member), "
                             "then re-run apply")


def _looks_like_email(value: Any) -> bool:
    return isinstance(value, str) and "@" in value


def _role_id(value: Any) -> Optional[str]:
    return value.lower() if isinstance(value, str) and ROLE_ID_RE.fullmatch(value) else None


def parse_seat(agent_name, value) -> Optional[str]:
    """`seat:` — one canon role id, lower-cased. Refused by name."""
    if value is None:
        return None
    if _looks_like_email(value):
        raise ValueError(f"Agent '{agent_name}' seat names a canon role, never a person: "
                         f"the value looks like an email address")
    role = _role_id(value)
    if role is None:
        raise ValueError(f"Agent '{agent_name}' seat must be a canon role id "
                         f"(letters, digits, '.', '_', '-'; at most 64)")
    return role


def parse_person_seats(agent_name, value) -> Optional[List[Dict[str, str]]]:
    """`person_seats: [{role, kind}]` — the seat and the kind, never a person.
    An email-shaped value anywhere is refused by its key and never echoed."""
    if value is None:
        return None
    shape = f"Agent '{agent_name}' person_seats must be a list of {{role, kind}}"
    if not isinstance(value, list):
        raise ValueError(shape)
    out: List[Dict[str, str]] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(shape)
        for key, v in entry.items():
            if _looks_like_email(v) or _looks_like_email(key):
                where = "a key" if _looks_like_email(key) else f"'{key}'"
                raise ValueError(
                    f"Agent '{agent_name}' person_seats[{i}]: {where} looks like an email "
                    f"address; a person seat names the role and the kind, never a person. "
                    f"To seat this instance's member, write `holder: {HOLDER_MEMBER}` with "
                    f"`kind: {MEMBER_KIND}`")
        extra = sorted(str(k) for k in entry if k not in PERSON_SEAT_KEYS)
        if extra:
            raise ValueError(
                f"Agent '{agent_name}' person_seats[{i}]: unknown key(s): {', '.join(extra)}. "
                f"A person seat names the role and the kind, never a person; recognised "
                f"keys are: {', '.join(PERSON_SEAT_KEYS)}.")
        role = _role_id(entry.get("role"))
        if role is None:
            raise ValueError(f"Agent '{agent_name}' person_seats[{i}]: role must be a canon "
                             f"role id")
        kind = entry.get("kind")
        if kind not in PERSON_SEAT_KINDS:
            raise ValueError(f"Agent '{agent_name}' person_seats[{i}]: kind must be one of "
                             f"{', '.join(PERSON_SEAT_KINDS)}")
        seat = {"role": role, "kind": kind}
        if "holder" in entry:  # trinity-enterprise#1174
            if entry["holder"] != HOLDER_MEMBER:
                raise ValueError(f"Agent '{agent_name}' person_seats[{i}]: holder's only legal "
                                 f"value is '{HOLDER_MEMBER}' (the instance's recorded member)")
            if kind != MEMBER_KIND:
                raise ValueError(f"Agent '{agent_name}' person_seats[{i}]: holder: "
                                 f"{HOLDER_MEMBER} is allowed only with kind: {MEMBER_KIND}")
            seat["holder"] = HOLDER_MEMBER
        if seat not in out:
            out.append(seat)
    if sum(1 for seat in out if seat.get("holder")) > 1:
        raise ValueError(f"Agent '{agent_name}' person_seats: at most one entry may name "
                         f"holder: {HOLDER_MEMBER} (an agent has one primary)")
    return out


def declares(cfg) -> bool:
    return cfg.seat is not None or cfg.person_seats is not None


def member_entry(cfg) -> Optional[Dict[str, str]]:
    """The `holder: member` person seat, when this install can read and write
    it (trinity-enterprise#1174). Without both hooks it stays an ordinary,
    report-only person seat."""
    if not assignment_provider.member_seat_writer_installed():
        return None
    return next((s for s in cfg.person_seats or [] if s.get("holder")), None)


def read_member_seat(name: str, cfg) -> Optional[str]:
    """The snapshot's read of the hub member's seat on `name` — which may not
    exist yet. None when the manifest declares none (or the hooks are absent).
    Raises `SeatReadFailed` on a provider error or a malformed answer."""
    entry = member_entry(cfg)
    if entry is None:
        return None
    status = assignment_provider.resolve_member_seat(name, entry["role"], entry["kind"])
    if status == "error":
        raise SeatReadFailed("the hub member's seat on this agent")
    return status


class SeatReadFailed(RuntimeError):
    """The provider failed or answered malformed: retryable, never unobserved."""


class SeatUnobserved(RuntimeError):
    """No provider on this install can say which seat an agent holds."""


def read_member(name: str, cfg) -> Tuple[Optional[Dict[str, Any]], Dict[Tuple[str, str], str]]:
    """`(seat, person_seats)` for a live member. `seat` is None when the
    writer is not installed (unobserved), else `{"case", "role_id"}`;
    `person_seats` maps `(role, kind)` to `filled` / `vacant` / `unobserved`.
    Raises `SeatReadFailed` on a provider error or a malformed answer."""
    seat = None
    if cfg.seat is not None and assignment_provider.seat_writer_installed():
        answer = assignment_provider.resolve_seat(name)
        if answer["reason"] in ("error", "malformed"):
            raise SeatReadFailed("the seat this agent holds")
        if answer["reason"] == "no_answer":
            # #4132: the provider answered nothing — it cannot say, which is
            # not "holds none" (that would plan a `seat_missing` write).
            seat = {"case": "unobserved", "role_id": None}
        else:
            role = answer["role_id"]
            if role is not None and _role_id(role) is None:
                raise SeatReadFailed("the seat this agent holds")
            seat = {"case": answer["case"], "role_id": role.lower() if role else None}
    people: Dict[Tuple[str, str], str] = {}
    held = member_entry(cfg)
    for entry in cfg.person_seats or []:
        if entry is held:
            continue  # read by `read_member_seat` (trinity-enterprise#1174)
        status = assignment_provider.resolve_person_seat(name, entry["role"], entry["kind"])
        if status == "error":
            raise SeatReadFailed("a person seat on this agent")
        people[(entry["role"], entry["kind"])] = status
    return seat, people


def export_member(name: str) -> Optional[str]:
    """The seat the agent HOLDS, as `seat:` declares it; None when it holds
    none. Raises `SeatUnobserved` when no provider can say (no `seat:` there
    means "cannot say", not "holds none"), and `SeatReadFailed` on a provider
    error or an answer that is not a role id (never exported)."""
    answer = assignment_provider.resolve_seat(name)
    if answer["reason"] in ("no_provider", "unsupported", "no_answer"):
        raise SeatUnobserved("the seat this agent holds")
    if answer["reason"] in ("error", "malformed"):
        raise SeatReadFailed("the seat this agent holds")
    if answer["case"] != "holds":
        return None
    role = _role_id(answer["role_id"])
    if role is None:
        raise SeatReadFailed("the seat this agent holds")
    return role


def plan(mp, *, seat: Optional[Dict[str, Any]], people: Optional[Dict[Tuple[str, str], str]],
         creating: bool, seat_writer: bool, apply_change, make_op,
         member: Optional[str] = None, member_writer: bool = False) -> None:
    """Changes and ops for one member. Never a blocker: an unobserved class
    is a report, a held-back seat is a report, and a read failure blocked the
    member in the snapshot. `seat_writer` is whether the CALLER passes the
    grant gate (`dependencies.is_interactive_admin`); `member` is the
    snapshot's `read_member_seat` answer and `member_writer` whether the
    caller may write the `holder: member` seat on THIS member
    (trinity-enterprise#1174)."""
    cfg = mp.config
    name, short = mp.name, mp.short_name
    if cfg.seat is not None:
        if not assignment_provider.seat_writer_installed():
            mp.changes.append(apply_change(SEAT_STEP, "report", UNOBSERVED_CODE,
                                           desired=cfg.seat, note=_UNOBSERVED_NOTE))
        elif not creating and seat and seat["case"] == "unobserved":
            mp.changes.append(apply_change(SEAT_STEP, "report", UNOBSERVED_CODE,
                                           desired=cfg.seat, note=_NO_ANSWER_NOTE))
        elif creating or not (seat and seat["case"] == "holds" and seat["role_id"] == cfg.seat):
            current = None if creating or not seat or seat["case"] == "none" else dict(seat)
            if not seat_writer:
                mp.changes.append(apply_change(SEAT_STEP, "report", HELD_BACK_CODE,
                                               current=current, desired=cfg.seat,
                                               note=_HELD_BACK_NOTE))
            else:
                mp.changes.append(apply_change(SEAT_STEP, "write",
                                               "seat_differs" if current else "seat_missing",
                                               current=current, desired=cfg.seat,
                                               note=_DIFFERS_NOTE if current else None))
                mp.ops.append(make_op(OP_KIND, name, short, {"role_id": cfg.seat}))
    held = member_entry(cfg)
    others = [e for e in cfg.person_seats or [] if e is not held]
    if others:
        installed = assignment_provider.person_seat_reader_installed()
        unobserved_note = _NOT_YET_NOTE if installed and creating else _UNOBSERVED_NOTE
        by_code: Dict[str, List[Dict[str, str]]] = {}
        for entry in others:
            if not installed or creating:
                status = "unobserved"
            else:
                status = (people or {}).get((entry["role"], entry["kind"]), "unobserved")
            by_code.setdefault(f"seat_{status}", []).append(dict(entry))
        for code in ("seat_vacant", "seat_filled", UNOBSERVED_CODE):
            if code in by_code:
                note = unobserved_note if code == UNOBSERVED_CODE else _PERSON_NOTE
                mp.changes.append(apply_change(PERSON_STEP, "report", code,
                                               desired=by_code[code], note=note))
    if held is not None:
        _plan_member_seat(mp, held, member, member_writer, apply_change, make_op)


def _plan_member_seat(mp, entry, status, writer, apply_change, make_op) -> None:
    """The `holder: member` seat (trinity-enterprise#1174). `desired` is the
    role id, so the executor's landed key matches the write's. `partial` (one
    of seat and share) is written again: both writes are idempotent."""
    role = entry["role"]
    reports = {"filled": ("seat_filled", _MEMBER_FILLED_NOTE),
               "differs": ("member_seat_differs", _MEMBER_DIFFERS_NOTE),
               "no_member": ("member_not_recorded", _MEMBER_NOT_RECORDED_NOTE)}
    missing, _, entitlement = (status or "").partition(":")
    if status in reports:
        code, note = reports[status]
    elif missing == assignment_provider.ENTITLEMENT_MISSING:
        code = assignment_provider.ENTITLEMENT_MISSING
        note = _MEMBER_UNENTITLED_NOTE.format(f"`{entitlement}`" if entitlement else "module's")
    elif status not in ("vacant", "partial"):
        code, note = UNOBSERVED_CODE, _NO_ANSWER_NOTE
    elif not writer:
        code, note = HELD_BACK_CODE, _MEMBER_HELD_BACK_NOTE
    else:
        mp.changes.append(apply_change(PERSON_STEP, "write", "seat_missing", desired=role))
        mp.ops.append(make_op(MEMBER_OP_KIND, mp.name, mp.short_name,
                              {"role_id": role, "kind": entry["kind"]}))
        return
    mp.changes.append(apply_change(PERSON_STEP, "report", code, desired=role, note=note))


def summarize(members: Dict) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """`summary.roles.held_back` over every member's plan, and the one warning
    line when anything was held back (the grants group's shape). Blocked
    members are not counted: nothing about them runs."""
    held_back = [{"name": mp.name,
                  "seat": c.desired if c.field == SEAT_STEP
                  else f"{c.desired} ({HOLDER_MEMBER}, {MEMBER_KIND})"}
                 for mp in members.values() if mp.verdict != "blocked"
                 for c in mp.changes if c.code == HELD_BACK_CODE]
    if not held_back:
        return held_back, None
    listing = "; ".join(f"{h['name']}: {h['seat']}" for h in held_back)
    count = len(held_back)
    return held_back, (
        f"{count} seat{'s' if count != 1 else ''} not written ({HELD_BACK_CODE}): seating "
        f"an agent requires an admin in a signed-in session, the same gate as a grant. "
        f"Held back: {listing}. Everything else in the manifest still applies.")


class _Refused(Exception):
    """A module refusal, as `failed[].reason` names it."""


def _actor(user) -> Optional[str]:
    """Attribution only — the username, as `set_grant` and `assign_skill`
    record it. The principal was judged before this is called."""
    return getattr(user, "username", None)


async def run(op, actor, out, attempt) -> None:
    """One `seat_set` op, through the provider's own write."""
    name, short, role = op.member, op.short_name, op.args["role_id"]

    def write():
        result = assignment_provider.write_seat(name, role, _actor(actor))
        if "refused" in result:
            raise _Refused(f"{result['refused']}: the assignment module refused the seat")
        return True

    await attempt(name, short, SEAT_STEP, write, key=role)


async def run_member(op, actor, out, attempt, *, request=None) -> None:
    """One `member_seat_set` op (trinity-enterprise#1174): the module's own
    write, then the audit row the share route writes — with the caller and no
    address (the module resolved who; nothing names them here)."""
    name, short, role = op.member, op.short_name, op.args["role_id"]
    result: Dict[str, Any] = {}

    def write():
        result.update(assignment_provider.write_member_seat(
            name, role, op.args["kind"], _actor(actor)))
        if "refused" in result:
            raise _Refused(f"{result['refused']}: the assignment module refused the "
                           f"member seat")
        return True

    if await attempt(name, short, PERSON_STEP, write, key=role) and (
            result.get("changed") or result.get("shared")):
        await _audit_member_seat(name, role, result, actor, request)
        if result.get("shared"):
            await _broadcast_shared(name)


async def _broadcast_shared(name) -> None:
    """The share route's `agent_shared` event, through its manager, so open
    sharing panels and every worker's cached roster refresh (ent#467).
    Identifiers only (#918): the module resolved who, and no address crosses
    back to the core."""
    import json

    from routers import sharing
    if sharing.manager is None:
        return
    try:
        await sharing.manager.broadcast(json.dumps({"event": "agent_shared",
                                                    "data": {"name": name}}))
    except Exception:  # noqa: BLE001 — the write landed; a lost event is logged
        logging.getLogger(__name__).warning(
            "member seat on %s landed but agent_shared was not broadcast", name, exc_info=True)


async def _audit_member_seat(name, role, result, actor, request) -> None:
    from services.platform_audit_service import AuditEventType, platform_audit_service
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.AUTHORIZATION, event_action="share", source="api",
            actor_user=actor,
            actor_ip=request.client.host if request is not None and request.client else None,
            target_type="agent", target_id=name,
            endpoint=request.scope["path"] if request is not None else None,
            request_id=getattr(request.state, "request_id", None) if request is not None else None,
            details={"via": "apply_member_seat", "role_id": role, "kind": MEMBER_KIND,
                     "shared": bool(result.get("shared")),
                     "seated": bool(result.get("changed"))})
    except Exception:  # noqa: BLE001 — the write landed; a lost audit row is logged
        logging.getLogger(__name__).warning(
            "member seat on %s landed but its audit row was not written", name, exc_info=True)


def _wanted(agent_names: Dict[str, str], agents_config: Dict) -> Dict[str, str]:
    return {agent_names[short]: cfg.seat for short, cfg in agents_config.items()
            if short in agent_names and cfg.seat is not None}


def deploy_hold_back_warning(agent_names: Dict[str, str], agents_config: Dict,
                             current_user) -> Optional[str]:
    """Create mode: without the writer, or for any caller but an admin in a
    signed-in session (the grant phase's gate — an agent key's name is its
    admin owner's, so a provider handed only a name could not tell them
    apart), no seat is written and the caller is told which were held back."""
    from dependencies import is_interactive_admin
    wanted = _wanted(agent_names, agents_config)
    if not wanted:
        return None
    listing = "; ".join(f"{n}: {r}" for n, r in wanted.items())
    if not assignment_provider.seat_writer_installed():
        return (f"Seats not written ({UNOBSERVED_CODE}): no assignment module on this install "
                f"writes a seat. Held back: {listing}.")
    if is_interactive_admin(current_user):
        return None
    return (f"Seats not written ({DEPLOY_HELD_BACK_CODE}): seating an agent requires an admin "
            f"in a signed-in session, the same gate as a grant. Held back: {listing}.")


def deploy(agent_names: Dict[str, str], agents_config: Dict, current_user) -> List[str]:
    """Create mode's `seat:` phase, after grants: best-effort, one warning per
    refusal, never past the gate above. Person seats are never filled."""
    warning = deploy_hold_back_warning(agent_names, agents_config, current_user)
    if warning is not None:
        return [warning]
    warnings: List[str] = []
    for name, role in _wanted(agent_names, agents_config).items():
        try:
            result = assignment_provider.write_seat(name, role, _actor(current_user))
        except Exception as e:  # noqa: BLE001 — best-effort, like every deploy phase
            warnings.append(f"Seat '{role}' not written for '{name}': {type(e).__name__}")
            continue
        if "refused" in result:
            warnings.append(f"Seat '{role}' not written for '{name}': {result['refused']}")
    return warnings
