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
  "no hook" is not.
* `plan` — `seat_missing` / `seat_differs` writes when the writer exists,
  otherwise `seat_unobserved`; `seat_filled` / `seat_vacant` /
  `seat_unobserved` reports for person seats, never a write — and
  `seat_unobserved` for a member apply will create, which has nothing to read.
* `run` — op `seat_set` through `assignment_provider.write_seat`; a refusal is
  recorded in `failed[]` with the module's code.
* `export_member` — `seat:` when the provider says the agent holds one;
  without a provider that can say, the export names the gap instead.
  Person seats are not exported: the seam answers per seat, it does not list.
* `deploy` — create mode's phase, after grants, behind the grant phase's own
  principal check: `deploy_system` is agent-callable, so anything but an
  admin in a signed-in session writes no seat and is told.

The core judges the principal before any write reaches the provider; the
actor it passes is the username, for attribution only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from services import assignment_provider
from services.assignment_provider import ROLE_ID_RE

SEAT_STEP = "seat"
PERSON_STEP = "person_seats"
GROUP_FIELDS = (SEAT_STEP, PERSON_STEP)
OP_KIND = "seat_set"
#: The kinds a person may hold a seat as — the seam's `kinds_for` shape.
PERSON_SEAT_KINDS = ("primary", "approver", "collaborator", "viewer")
PERSON_SEAT_KEYS = ("role", "kind")
UNOBSERVED_CODE = "seat_unobserved"
DEPLOY_HELD_BACK_CODE = "seats_require_interactive_admin"
_UNOBSERVED_NOTE = ("no assignment module on this install answers this; the seat is "
                    "neither read nor written, which is not the same as in sync")
_DIFFERS_NOTE = ("another declaration may hold this seat; apply moves it only on a "
                 "real run")
_PERSON_NOTE = "apply never fills a person seat: a person assigns themselves or is assigned"
_NOT_YET_NOTE = ("the member does not exist yet, so nothing was read; apply never fills a "
                 "person seat")


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
                    f"address; a person seat names the role and the kind, never a person")
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
        if seat not in out:
            out.append(seat)
    return out


def declares(cfg) -> bool:
    return cfg.seat is not None or cfg.person_seats is not None


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
        role = answer["role_id"]
        if role is not None and _role_id(role) is None:
            raise SeatReadFailed("the seat this agent holds")
        seat = {"case": answer["case"], "role_id": role.lower() if role else None}
    people: Dict[Tuple[str, str], str] = {}
    for entry in cfg.person_seats or []:
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
    if answer["reason"] in ("no_provider", "unsupported"):
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
         creating: bool, apply_change, make_op) -> None:
    """Changes and ops for one member. Never a blocker: an unobserved class
    is a report, and a read failure blocked the member in the snapshot."""
    cfg = mp.config
    name, short = mp.name, mp.short_name
    if cfg.seat is not None:
        if not assignment_provider.seat_writer_installed():
            mp.changes.append(apply_change(SEAT_STEP, "report", UNOBSERVED_CODE,
                                           desired=cfg.seat, note=_UNOBSERVED_NOTE))
        elif creating:
            mp.changes.append(apply_change(SEAT_STEP, "write", "seat_missing", desired=cfg.seat))
            mp.ops.append(make_op(OP_KIND, name, short, {"role_id": cfg.seat}))
        elif not (seat and seat["case"] == "holds" and seat["role_id"] == cfg.seat):
            current = None if not seat or seat["case"] == "none" else dict(seat)
            mp.changes.append(apply_change(SEAT_STEP, "write",
                                           "seat_differs" if current else "seat_missing",
                                           current=current, desired=cfg.seat,
                                           note=_DIFFERS_NOTE if current else None))
            mp.ops.append(make_op(OP_KIND, name, short, {"role_id": cfg.seat}))
    if cfg.person_seats:
        installed = assignment_provider.person_seat_reader_installed()
        unobserved_note = _NOT_YET_NOTE if installed and creating else _UNOBSERVED_NOTE
        by_code: Dict[str, List[Dict[str, str]]] = {}
        for entry in cfg.person_seats:
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

    await attempt(name, short, SEAT_STEP, write)


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
