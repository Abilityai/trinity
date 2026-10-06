"""Gated skills — the approval is raised before the executor sees the request
(trinity-enterprise#751).

A person can mark a skill on an agent "requires approval by role X" (the map is
ent#753's storage; `list_skill_gates` reads it). When a request dispatched to
that agent invokes a gated skill, `enforce` decides before anything else runs:

* **ungated** — nothing in the request names a gated skill; dispatch as today.
* **self-approved** — a PERSON proven by the entry point (never an agent key, a
  system key, a channel or public identity, an event loopback or a schedule)
  who is the approver asked; dispatch, and the entry audits it.
* **SkillApprovalRequired** — the request is frozen in `skill_gate_requests`
  and an approval ask is raised to the role; nothing runs now. On approval the
  ending observer dispatches the frozen request exactly once.
* **SkillGateRefused** — a named refusal; nothing runs and nothing is raised.

Every "could not tell" ends in a refusal, never a dispatch: an unreadable gate
map, an unreadable skill, an approver nobody fills.

Matching reads the requester's OWN text (each entry passes it), never the
composed message: a history prefix would re-gate every later turn after one
mention, and approving it would replay that history.

Gate asks do not spend the executor's own ask budget (`ask_service.raise_ask`);
the gate caps itself here — per requester per executor, per executor, and a
per-requester rate — before the in-container exec.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from config import PORTAL_SOURCE_CHANNEL
from database import db
from services import rate_limiter, role_addressing
from services.skill_gate_errors import (  # noqa: F401 — re-exported for entries and tests
    APPROVAL_PENDING_CODE,
    SkillApprovalRequired,
    SkillGateError,
    SkillGateRefused,
)
from services.skill_packaging import SKILL_NAME_RE
from utils.credential_sanitizer import sanitize_text
from utils.helpers import to_utc_iso, utc_now
from utils.skill_invocation import find_gated_invocations

logger = logging.getLogger(__name__)

# Caps (user ruling at the #751 plan gate). Only `pending` records count.
# Constants, not env knobs: an env read the compose files do not wire is a lever
# that does nothing on deploy (the #1056 packaging class).
MAX_PENDING_PER_REQUESTER = 10
MAX_PENDING_PER_EXECUTOR = 50
RATE_PER_REQUESTER = 10
RATE_WINDOW_SECONDS = 60

DEFAULT_DEADLINE_HOURS = 24
# The approver sees the WHOLE request and the run sends exactly that, so a
# request is refused, not truncated, when the card cannot show it all. Measured
# like the ask sink measures `proposal` (JSON bytes — non-ASCII escapes to
# \uXXXX), leaving room for the proposal's other fields under its 8 KiB cap.
CARD_INPUT_MAX_BYTES = 6000
QUESTION_PREVIEW_CHARS = 500
FINGERPRINT_TIMEOUT_SECONDS = 10

APPROVE = "Approve"
REJECT = "Reject"
OPTIONS = [APPROVE, REJECT]

# The approved dispatch carries this trigger only when it re-enters an entry
# point for a record already claimed; nothing else skips the check.
SKILL_GATE_TRIGGER = "skill_gate"

# Requester kinds — what the per-requester cap keys on and who is told.
KIND_AGENT = "agent"
KIND_PERSON = "person"
KIND_SCHEDULE = "schedule"
KIND_CHANNEL = "channel"
KIND_PUBLIC = "public"
KIND_PAID = "paid"
KIND_CONNECTOR = "connector"
KIND_OTHER = "other"


@dataclass(frozen=True)
class SkillGate:
    """One gated skill on one agent: who approves, and how long they have."""

    approver: str = "primary"
    deadline_hours: Optional[int] = None


def list_skill_gates(agent_name: str) -> Dict[str, SkillGate]:
    """The gated skills on `agent_name`, keyed by skill name.

    ent#753 owns the storage and replaces this body with its read. Until it
    lands nothing is gated, so #751 ships inert.
    """
    return {}


@dataclass(frozen=True)
class Requester:
    """Who asked. Built by the entry point from what it authenticated.

    `is_person` is True only when the entry proved a person principal — the one
    fact self-approval may rest on. `key` is what the per-requester cap counts.
    """

    kind: str
    key: str
    agent_name: Optional[str] = None
    email: Optional[str] = None
    execution_id: Optional[str] = None
    mcp_key_id: Optional[str] = None
    is_person: bool = False
    label: Optional[str] = None

    def display(self) -> str:
        if self.label:
            return self.label
        if self.kind == KIND_AGENT and self.agent_name:
            return f"Agent {self.agent_name}"
        if self.kind == KIND_PERSON and self.email:
            return self.email
        return {
            KIND_SCHEDULE: "A schedule",
            KIND_CHANNEL: "A messaging-channel user",
            KIND_PUBLIC: "A public-link visitor",
            KIND_PAID: "A paid caller",
            KIND_CONNECTOR: "A connector client",
        }.get(self.kind, "A caller")


@dataclass(frozen=True)
class GateDecision:
    """What an entry does next when `enforce` returns (it raises otherwise)."""

    skills: Tuple[str, ...] = ()
    self_approved_by: Optional[str] = None

    @property
    def ungated(self) -> bool:
        return not self.skills


async def enforce(
    agent_name: str,
    *,
    request_text: Optional[str],
    requester: Requester,
    triggered_by: str,
    occurrence_key: Optional[str] = None,
    origin_execution_id: Optional[str] = None,
    dispatch: Optional[Mapping[str, Any]] = None,
    refuse_only: bool = False,
    gates: Optional[Mapping[str, SkillGate]] = None,
) -> GateDecision:
    """Decide whether a request to `agent_name` may be dispatched now.

    Returns a `GateDecision` (ungated or self-approved); raises
    `SkillApprovalRequired` when an approval was raised, `SkillGateRefused`
    otherwise. `occurrence_key` names THIS occurrence (a pre-created execution
    id, a fan-out task id, the caller's Idempotency-Key): a retry of the same
    occurrence replays its first approval instead of raising a second one.
    `refuse_only` entries (no row, no identity) refuse a gated request instead
    of raising an approval. `gates` is the map a caller already read
    (`read_gates`), so the check stays one read per dispatch.
    """
    if triggered_by == SKILL_GATE_TRIGGER:
        return GateDecision()
    gates = dict(gates) if gates is not None else read_gates(agent_name)
    if not gates:
        return GateDecision()

    names = find_gated_invocations(request_text, gates)
    if not names:
        return GateDecision()

    roles = {gates[n].approver for n in names}
    if len(roles) > 1:
        raise SkillGateRefused(
            422, "mixed_approvers",
            "These skills are approved by different people; request them separately.",
            skills=names)
    role = roles.pop()
    approvers = _approvers(agent_name, role)

    if requester.is_person and requester.email and requester.email.strip().casefold() in approvers:
        return GateDecision(tuple(names), self_approved_by=requester.email)

    if refuse_only:
        raise SkillGateRefused(
            403, "approval_not_available_here",
            f"{_skill_list(names)} on {agent_name} needs approval, which "
            "can't be requested from here. Ask in chat instead.", skills=names)

    clean_text = sanitize_text(request_text or "")
    if _json_bytes(clean_text) > CARD_INPUT_MAX_BYTES:
        raise SkillGateRefused(
            422, "request_too_long",
            f"This request is too long for an approver to read in full "
            f"({_json_bytes(clean_text)} > {CARD_INPUT_MAX_BYTES} bytes). Shorten it and ask again.",
            limit=CARD_INPUT_MAX_BYTES)

    request_id = _request_id(agent_name, requester.key, occurrence_key)
    existing = db.get_gate_request(request_id)
    if existing:
        raise _answer_for_record(existing, role)

    _check_caps(agent_name, requester)
    fingerprints = await _fingerprints_or_refuse(agent_name, names)

    hours = min((gates[n].deadline_hours for n in names if gates[n].deadline_hours),
                default=DEFAULT_DEADLINE_HOURS)
    expires_at = to_utc_iso(utc_now() + timedelta(hours=hours))
    record, created = db.create_gate_request(
        request_id=request_id,
        agent_name=agent_name,
        skills=names,
        request_text=clean_text,
        fingerprints=fingerprints,
        requester_kind=requester.kind,
        requester_key=requester.key,
        source_agent=requester.agent_name,
        requester_email=requester.email,
        requester_execution_id=requester.execution_id,
        requester_mcp_key_id=requester.mcp_key_id,
        origin_execution_id=origin_execution_id,
        triggered_by=triggered_by,
        dispatch=dict(dispatch or {}),
    )
    if not created:   # a concurrent call for the same occurrence won
        raise _answer_for_record(record, role)

    ask = _ask_body(agent_name, request_id, names, clean_text, requester, fingerprints,
                    role, expires_at)
    from services.ask_service import AskRejected
    try:
        receipt = _raise_ask(agent_name, ask)
    except AskRejected as e:
        db.transition_gate_request(request_id, "refused", detail=e.code)
        raise SkillGateRefused(e.status, e.code, e.message, **e.extra) from e
    except Exception as e:
        # Never leave a pending record with no ask behind it: it would count
        # against the caps forever and no ending would ever consume it.
        logger.exception("[SkillGate] could not raise the approval for %s", request_id)
        db.transition_gate_request(request_id, "refused", detail="approval_unavailable")
        raise SkillGateRefused(
            503, "approval_unavailable",
            "The approval could not be raised; nothing was run. Try again shortly.") from e
    db.attach_gate_ask(request_id, receipt["id"])
    raise SkillApprovalRequired(request_id=request_id, agent_name=agent_name, skills=names,
                                approver_role=role, expires_at=receipt.get("expires_at"))


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _approvers(agent_name: str, role: str) -> List[str]:
    """The casefolded emails `role` reaches on `agent_name`; refuses when it
    reaches nobody (an `approver` with no assignments provider, an `operator`
    role, an owner with no email)."""
    try:
        res = role_addressing.resolve(agent_name, role)
    except role_addressing.RoleRefused:
        res = None
    people = [p.casefold() for p in (res.people if res else []) if p]
    if not people:
        raise SkillGateRefused(
            422, "role_unassigned",
            f"Nobody fills the {role} role for {agent_name}, so this gated request "
            "can't be approved. Ask the agent's owner to assign one.", role=role)
    return people


def read_gates(agent_name: str) -> Dict[str, SkillGate]:
    """The agent's gate map, or a named refusal — never "nothing is gated"
    because the read failed."""
    try:
        return dict(list_skill_gates(agent_name) or {})
    except Exception:
        logger.exception("[SkillGate] gate map unreadable for %s — refusing", agent_name)
        raise SkillGateRefused(
            503, "gate_unavailable",
            f"Could not check whether this request to {agent_name} needs approval; "
            "nothing was run. Try again shortly.")


def _skill_list(names: List[str]) -> str:
    """Skill names WITHOUT a slash — this text is echoed into rows, events and
    channel replies, and must never read as a request to run a skill."""
    return "the skill " + ", ".join(names) if len(names) == 1 else "the skills " + ", ".join(names)


def _json_bytes(text: str) -> int:
    return len(json.dumps(text).encode("utf-8"))


def _request_id(agent_name: str, requester_key: str, occurrence_key: Optional[str]) -> str:
    """One id per occurrence of one requester's request. The requester is part
    of it so another caller re-using the same Idempotency-Key on this agent
    cannot land on someone else's request."""
    key = occurrence_key or uuid.uuid4().hex
    raw = f"{agent_name}\x00{requester_key}\x00{key}".encode("utf-8")
    return "gate-" + hashlib.sha256(raw).hexdigest()[:40]


def _check_caps(agent_name: str, requester: Requester) -> None:
    """Rate first (it spends a token, so a refusal below cannot be repeated for
    free), then the pending caps. A count-then-insert, so two workers racing at
    the limit can each admit one: the caps bound a flood, not an exact number."""
    rate = rate_limiter.check(f"skill_gate:{agent_name}:{requester.key}",
                              RATE_PER_REQUESTER, RATE_WINDOW_SECONDS)
    if not rate.allowed:
        raise SkillGateRefused(
            429, "approval_rate_limited",
            "Too many gated requests in a short time; try again in a minute.")
    if db.count_pending_gate_requests(agent_name, requester_key=requester.key) >= MAX_PENDING_PER_REQUESTER:
        raise SkillGateRefused(
            429, "approval_queue_full",
            f"{MAX_PENDING_PER_REQUESTER} of your requests to {agent_name} are already "
            "waiting for approval; ask again once one is decided.",
            limit=MAX_PENDING_PER_REQUESTER, scope="requester")
    if db.count_pending_gate_requests(agent_name) >= MAX_PENDING_PER_EXECUTOR:
        raise SkillGateRefused(
            429, "approval_queue_full",
            f"{agent_name} already has {MAX_PENDING_PER_EXECUTOR} requests waiting for "
            "approval; ask again once some are decided.",
            limit=MAX_PENDING_PER_EXECUTOR, scope="agent")


async def _fingerprints_or_refuse(agent_name: str, names: List[str]) -> Dict[str, str]:
    found = await read_skill_fingerprints(agent_name, names)
    if found is None:
        raise SkillGateRefused(
            409, "agent_unavailable",
            f"Could not read the gated skill on {agent_name} (is it running?); nothing was run.")
    out = {}
    for name in names:
        entry = found.get(name) or {"error": "not_found"}
        error = entry.get("error")
        if error == "not_found":
            raise SkillGateRefused(409, "gated_skill_not_installed",
                                   f"The skill {name} is not installed on {agent_name}.", skill=name)
        if error == "ambiguous":
            raise SkillGateRefused(409, "gated_skill_ambiguous",
                                   f"More than one skill on {agent_name} answers to {name}.",
                                   skill=name)
        if error or not entry.get("fingerprint"):
            raise SkillGateRefused(409, "gated_skill_unreadable",
                                   f"The skill {name} on {agent_name} could not be read safely.",
                                   skill=name)
        out[name] = entry["fingerprint"]
    return out


def _ask_body(agent_name, request_id, names, clean_text, requester, fingerprints, role, expires_at):
    """The card: the WHOLE request in `proposal.input` (what Approve runs), a
    short preview in the question."""
    skills = _skill_list(names)
    who = requester.display()
    preview = clean_text if len(clean_text) <= QUESTION_PREVIEW_CHARS \
        else clean_text[:QUESTION_PREVIEW_CHARS] + "… (the full request is shown below)"
    return {
        "request_id": request_id,
        "type": "approval",
        "priority": "high",
        "title": f"Approve {skills} on {agent_name}"[:300],
        "question": (f"{who} asked {agent_name} to run {skills}.\n\nRequest:\n{preview}\n\n"
                     f"{APPROVE} runs it once, exactly as written. {REJECT} runs nothing."),
        "options": list(OPTIONS),
        "to": role,
        "expires_at": expires_at,
        "proposal": {
            "kind": "gated_skill",
            "agent": agent_name,
            "skills": names,
            "input": clean_text,
            "requester": who,
            "fingerprints": fingerprints,
        },
    }


def _raise_ask(agent_name: str, ask: Dict[str, Any]) -> Dict[str, Any]:
    """Raise through the one ask sink. After a timeout an identical proposal
    must name the expired ask it supersedes (C6); the gate links it itself."""
    from services import ask_service

    try:
        return ask_service.raise_ask(agent_name, ask, raised_by="gate", channel="gate")
    except ask_service.AskRejected as e:
        expired = (e.extra or {}).get("expired_request_id")
        if e.code != "reask_requires_link" or not expired:
            raise
        return ask_service.raise_ask(agent_name, {**ask, "supersedes_expired": expired},
                                     raised_by="gate", channel="gate")


def _answer_for_record(record: Dict[str, Any], role: str):
    """The answer for a retry of an occurrence that already has a record: still
    waiting → pending; decided → that outcome, by name (never "pending")."""
    state = record.get("state")
    if state in ("pending", "dispatching"):
        return _pending_from_record(record, role)
    return SkillGateRefused(
        409, f"request_{state}",
        f"This request ({record['request_id']}) was already decided: {state}.",
        request_id=record["request_id"], state=state)


def _pending_from_record(record: Dict[str, Any], role: str) -> SkillApprovalRequired:
    expires_at = None
    if record.get("ask_item_id"):
        row = db.get_operator_queue_item_for_agent_by_request_id(record["agent_name"], record["request_id"])
        expires_at = (row or {}).get("expires_at")
    return SkillApprovalRequired(request_id=record["request_id"], agent_name=record["agent_name"],
                                 skills=record.get("skills") or [], approver_role=role,
                                 expires_at=expires_at)


# ---------------------------------------------------------------------------
# Fingerprint — what the runtime would actually load, read inside the executor
# ---------------------------------------------------------------------------

# Run as root through the image's interpreter with `-I -S`, so the agent's own
# Python startup files (`sitecustomize`, a `.pth`, PYTHON* env) do not run inside
# it. This guards against ACCIDENTAL interference, not an adversarial executor:
# the agent user has passwordless sudo in the base image and could replace the
# interpreter itself. Capability confinement is the boundary against that. Resolves a name by directory, then by frontmatter `name:`; refuses
# an ambiguous name (two candidates, or a skill plus a `.claude/commands/<n>.md`)
# and any symlink. Hashes sorted (relpath, exec bit, sha256) — the platform's
# own `.trinity-skill.json` excluded.
_FINGERPRINT_SCRIPT = r'''
import hashlib, json, os, stat, sys
req = json.loads(sys.argv[1])
home = req.get("home") or "/home/developer/.claude"
skills_root = os.path.join(home, "skills")
cmds_root = os.path.join(home, "commands")
MAX_FILES, MAX_BYTES = 2000, 20 * 1024 * 1024

def listdir(p):
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []

def fm_name(d):
    try:
        with open(os.path.join(skills_root, d, "SKILL.md"), "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    for line in head.splitlines()[1:]:
        if line.strip() == "---":
            return None
        if line.lower().startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def hash_dir(root):
    entries, total = [], 0
    if os.path.islink(root):
        return {"error": "symlink"}
    for cur, dirs, files in os.walk(root, followlinks=False):
        for d in dirs:
            if os.path.islink(os.path.join(cur, d)):
                return {"error": "symlink"}
        for fn in files:
            p = os.path.join(cur, fn)
            rel = os.path.relpath(p, root)
            if rel == ".trinity-skill.json":
                continue
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                return {"error": "symlink"}
            total += st.st_size
            if len(entries) >= MAX_FILES or total > MAX_BYTES:
                return {"error": "too_large"}
            entries.append("%s\0%d\0%s" % (rel, 1 if st.st_mode & 0o111 else 0, sha(p)))
    entries.sort()
    return {"fingerprint": hashlib.sha256("\n".join(entries).encode()).hexdigest()}

dirs = [d for d in listdir(skills_root) if os.path.isfile(os.path.join(skills_root, d, "SKILL.md"))]
cmds = [c for c in listdir(cmds_root) if c.endswith(".md")]
out = {}
for name in req["names"]:
    key = name.casefold()
    cands = [d for d in dirs if d.casefold() == key]
    cands += [d for d in dirs if d not in cands and (fm_name(d) or "").casefold() == key]
    cmd = [c for c in cmds if c[:-3].casefold() == key]
    if len(cands) + len(cmd) == 0:
        out[name] = {"error": "not_found"}
    elif len(cands) + len(cmd) > 1:
        out[name] = {"error": "ambiguous"}
    elif cmd:
        p = os.path.join(cmds_root, cmd[0])
        out[name] = {"error": "symlink"} if os.path.islink(p) else {"fingerprint": sha(p), "kind": "command"}
    else:
        r = hash_dir(os.path.join(skills_root, cands[0]))
        r["kind"] = "library" if os.path.isfile(os.path.join(skills_root, cands[0], ".trinity-skill.json")) else "own"
        out[name] = r
print(json.dumps(out))
'''


async def read_skill_fingerprints(agent_name: str, names: List[str]) -> Optional[Dict[str, Dict]]:
    """`{name: {"fingerprint", "kind"} | {"error"}}` read inside the agent, or
    None when the agent could not be read at all. One exec for every name."""
    if not names or any(not SKILL_NAME_RE.match(n) for n in names):
        return None
    from services.docker_service import execute_command_in_container

    try:
        result = await execute_command_in_container(
            container_name=f"agent-{agent_name}",
            command=["/usr/local/bin/python3", "-I", "-S", "-c", _FINGERPRINT_SCRIPT,
                     json.dumps({"names": list(names)})],
            timeout=FINGERPRINT_TIMEOUT_SECONDS,
            user="root",
        )
    except Exception:
        logger.warning("[SkillGate] fingerprint exec failed for %s", agent_name, exc_info=True)
        return None
    if result.get("exit_code") != 0:
        logger.warning("[SkillGate] fingerprint exec on %s exited %s", agent_name, result.get("exit_code"))
        return None
    try:
        parsed = json.loads((result.get("output") or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Who asked — built from what the entry authenticated, never from a header
# ---------------------------------------------------------------------------

_CHANNEL_TRIGGERS = frozenset({"slack", "telegram", "whatsapp"})
_SCHEDULE_TRIGGERS = frozenset({"schedule", "retry", "reminder"})


def _own_execution(execution_id: Optional[str], agent_name: str) -> Optional[str]:
    """`execution_id` iff it is one of `agent_name`'s own executions — the
    platform-injected `X-Trinity-Execution-Id` (#2392), checked like ask_service
    checks it. Never raises."""
    if not execution_id or execution_id == "manual":
        return None
    from services.idempotency_service import resolve_and_validate_execution
    try:
        return execution_id if resolve_and_validate_execution(execution_id, agent_name) else None
    except Exception:  # noqa: BLE001 — provenance never fails the gate
        return None


def requester_from_principal(current_user, *, source_agent: Optional[str] = None,
                             execution_id: Optional[str] = None) -> Requester:
    """The requester an HTTP entry authenticated. `source_agent` must already
    be resolved (`dependencies.resolve_source_agent`). Only a principal that
    `is_person_principal` accepts — never an agent key, a connector, a portal
    delegate, a system key or the event loopback — may self-approve."""
    from dependencies import is_person_principal

    key_id = getattr(current_user, "mcp_key_id", None)
    username = getattr(current_user, "username", None) or "unknown"
    agent = getattr(current_user, "agent_name", None) or source_agent
    if agent:
        return Requester(kind=KIND_AGENT, key=f"agent:{agent}", agent_name=agent,
                         execution_id=_own_execution(execution_id, agent), mcp_key_id=key_id)
    if getattr(current_user, "connector_agent", None):
        return Requester(kind=KIND_CONNECTOR, key=f"connector:{key_id or username}",
                         mcp_key_id=key_id)
    if getattr(current_user, "is_event_loopback", False) is True:
        return Requester(kind=KIND_OTHER, key="event-loopback", label="An event subscription")
    email = getattr(current_user, "email", None) or None
    if is_person_principal(current_user):
        return Requester(kind=KIND_PERSON, key=f"person:{(email or username).casefold()}",
                         email=email, mcp_key_id=key_id, is_person=True)
    return Requester(kind=KIND_OTHER, key=f"principal:{key_id or username}", mcp_key_id=key_id)


def requester_for_dispatch(*, triggered_by: str, source_user_email: Optional[str] = None,
                           source_agent_name: Optional[str] = None,
                           source_mcp_key_id: Optional[str] = None,
                           source_channel: Optional[str] = None,
                           source_channel_chat_id: Optional[str] = None,
                           schedule_id: Optional[str] = None,
                           schedule_name: Optional[str] = None,
                           loop_id: Optional[str] = None) -> Requester:
    """The requester for a producer that calls `execute_task` directly. Never a
    proven person: these producers pass identities they did not authenticate
    as a person session (a channel sender, a public visitor, a schedule)."""
    if source_agent_name:
        return Requester(kind=KIND_AGENT, key=f"agent:{source_agent_name}",
                         agent_name=source_agent_name, mcp_key_id=source_mcp_key_id)
    if triggered_by in _SCHEDULE_TRIGGERS:
        # The name is free text (an agent can create schedules) and the label sits
        # above the request on the approver's card: one line, bounded, so it
        # cannot forge lines of its own.
        name = " ".join(str(schedule_name or "").split())[:80]
        return Requester(kind=KIND_SCHEDULE, key=f"schedule:{schedule_id or triggered_by}",
                         label=f"The schedule \"{name}\"" if name else None)
    if triggered_by == "paid":
        return Requester(kind=KIND_PAID, key="paid")
    if source_channel == PORTAL_SOURCE_CHANNEL and source_user_email:
        # The Workspace: a signed-in person with an Inbox, not an anonymous
        # channel user. Unproven here — only an entry that authenticated the
        # person passes `gate_requester`, the one way to self-approve.
        return Requester(kind=KIND_PERSON, key=f"person:{source_user_email.casefold()}",
                         email=source_user_email)
    if triggered_by in _CHANNEL_TRIGGERS or source_channel:
        who = source_user_email or source_channel_chat_id or "unknown"
        return Requester(kind=KIND_CHANNEL, key=f"channel:{source_channel or triggered_by}:{who}",
                         email=source_user_email)
    if triggered_by == "public":
        return Requester(kind=KIND_PUBLIC, key=f"public:{(source_user_email or 'anonymous').casefold()}",
                         email=source_user_email)
    if triggered_by == "loop" and loop_id:
        return Requester(kind=KIND_OTHER, key=f"loop:{loop_id}", email=source_user_email,
                         label="A loop")
    if source_user_email:
        return Requester(kind=KIND_PERSON, key=f"person:{source_user_email.casefold()}",
                         email=source_user_email, mcp_key_id=source_mcp_key_id)
    return Requester(kind=KIND_OTHER, key=f"trigger:{triggered_by}")


async def audit_self_approved(agent_name: str, decision: GateDecision, *, current_user,
                              endpoint: str, execution_id: Optional[str]) -> None:
    """The gate let an approver's own request through — one row per execution
    it let through. At the `/chat` and `/task` seams it is written once the row
    exists; at the `execute_task` backstop (the Workspace) before admission, so
    a capacity refusal after it, or the Workspace's cold retry, leaves a row
    whose execution says what actually happened. Best effort."""
    if decision.ungated or not decision.self_approved_by:
        return
    from services.platform_audit_service import AuditEventType, platform_audit_service
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="skill_gate_self_approved",
            source="api",
            actor_user=current_user,
            actor_email=decision.self_approved_by,
            mcp_key_id=getattr(current_user, "mcp_key_id", None),
            mcp_key_name=getattr(current_user, "mcp_key_name", None),
            mcp_scope=getattr(current_user, "mcp_scope", None),
            target_type="agent",
            target_id=agent_name,
            endpoint=endpoint,
            request_id=None,
            details={"skills": list(decision.skills), "execution_id": execution_id},
        )
    except Exception:  # noqa: BLE001 — never fail a dispatch that already began
        logger.warning("[SkillGate] self-approval audit failed for %s", agent_name, exc_info=True)


# ---------------------------------------------------------------------------
# The frozen dispatch — what an approved run re-uses of the request
# ---------------------------------------------------------------------------

# Every `execute_task` keyword, classified (a parity test fails on a new one
# that is not). FREEZE: recorded at request time and passed to the approved run.
# REPLACE: `message` becomes the requester's own text (`request_text`), never a
# composed prompt with history. RESET: per-run plumbing the approved run gets
# fresh. DROP: tied to the requesting turn — a session to resume, a batch or a
# loop the run must not rejoin, an attachment, a canvas, the scheduler's
# seat-memory prompt — and wrong to replay hours later.
FREEZE, REPLACE, RESET, DROP, RECORD = "freeze", "replace", "reset", "drop", "record"
DISPATCH_FIELDS: Dict[str, str] = {
    "agent_name": RECORD,
    "message": REPLACE,
    "triggered_by": FREEZE,
    "source_user_id": FREEZE,
    "source_user_email": FREEZE,
    "source_agent_name": FREEZE,
    "source_mcp_key_id": FREEZE,
    "source_mcp_key_name": FREEZE,
    "model": FREEZE,
    "timeout_seconds": FREEZE,
    "allowed_tools": FREEZE,
    "subscription_id": FREEZE,
    "chain_depth": FREEZE,
    "source_channel": FREEZE,
    "source_channel_chat_id": FREEZE,
    "source_channel_thread": FREEZE,
    "source_channel_client": FREEZE,
    "system_prompt": DROP,
    "resume_session_id": DROP,
    "persist_session": DROP,
    "fan_out_id": DROP,
    "loop_id": DROP,
    "parent_activity_id": DROP,
    "collaboration_activity_id": DROP,
    "extra_activity_details": DROP,
    "schedule_context": DROP,
    "images": DROP,
    "open_canvas_id": DROP,
    "conversation_key": DROP,
    "execution_id": RESET,
    "slot_already_held": RESET,
    "attempt": RESET,
    "dispatch_gate_checked": RESET,
    "request_text": RESET,
    "gate_checked": RESET,
    "gate_requester": DROP,
}


def frozen_dispatch(**fields: Any) -> Dict[str, Any]:
    """The FREEZE subset of `fields`, None values left out. Refuses an
    unclassified keyword so a new one cannot be dropped silently."""
    unknown = [k for k in fields if k not in DISPATCH_FIELDS]
    if unknown:
        raise ValueError(f"frozen_dispatch: unclassified execute_task fields {unknown}")
    return {k: v for k, v in fields.items() if DISPATCH_FIELDS[k] == FREEZE and v is not None}


# ---------------------------------------------------------------------------
# Ending → dispatch: the approval runs the frozen request exactly once
# ---------------------------------------------------------------------------

# The gate's own notices (to a person who asked) are gate rows too; their
# endings are acknowledgements, never decisions.
NOTE_PREFIX = "gate-note-"
LOST_IN_DISPATCH_MINUTES = 5

_inflight: set = set()   # strong refs to spawned approved runs (asyncio GC footgun)

# Surfaces whose caller is outside the operator's team — their approved run
# keeps the owner's public-channel caller prompt.
_PUBLIC_FACING_TRIGGERS = frozenset({"public", "paid", "slack", "telegram", "whatsapp"})


def register_ending_observer() -> None:
    """Called once at startup (`main.py`), in every worker: an approval may be
    answered on any of them. Idempotent."""
    from services import ask_service
    ask_service.register_ending_observer(on_ending)


def on_ending(event) -> None:
    """Ask-sink observer. Runs in the ending caller's thread — possibly a worker
    thread with no loop (the Workspace answer route) — so the work hops onto
    the loop. At-most-once by contract; the sweep catches what this misses."""
    ids = [r.get("request_id") for r in (event.rows or ())
           if r.get("raised_by") == "gate"
           and str(r.get("request_id") or "").startswith("gate-")
           and not str(r.get("request_id")).startswith(NOTE_PREFIX)]
    if not ids:
        return
    from services.operator_resume_service import spawn_on_loop
    for rid in ids:
        spawn_on_loop(lambda rid=rid: resolve(rid))


async def sweep() -> None:
    """The reconcile pass, run from the operator-queue poll cycle after expiry.

    1. Gate asks that ended while their record is still `pending` — an observer
       never saw them (a worker died, the observer raised). Resolved now.
    2. Records claimed for dispatch whose run row never appeared after a few
       minutes — the process died between the claim and the run. Recorded
       `unknown` and reported; NEVER re-run, because a run may have started.

    Both are compare-and-sets, so every worker may run this; a second pass
    writes nothing.
    """
    for record in db.list_gate_requests_with_ended_asks():
        await resolve(record["request_id"])
    cutoff = to_utc_iso(utc_now() - timedelta(minutes=LOST_IN_DISPATCH_MINUTES))
    for record in db.list_gate_requests_lost_in_dispatch(cutoff):
        if db.transition_gate_request(record["request_id"], "unknown",
                                      detail="the approved run never started"):
            await _notify(record, "unknown", decider=_decider_of(record), notify_decider=True)
    # 3. The run started but the record never said so (the process died in
    #    between): record it and send the notice the requester never got.
    for record in db.list_gate_requests_dispatched_unrecorded(cutoff):
        if db.transition_gate_request(record["request_id"], "dispatched"):
            await _notify(record, "approved", decider=_decider_of(record),
                          execution_id=record.get("dispatched_execution_id"))
    # 4. A pending record with no ask behind it: raised but never attached
    #    (attach it — the next pass consumes its ending), or the ask is gone
    #    (cleared, retention) and nothing will ever decide it.
    for record in db.list_gate_requests_without_live_ask(cutoff):
        row = db.get_operator_queue_item_for_agent_by_request_id(
            record["agent_name"], record["request_id"])
        if row and not record.get("ask_item_id"):
            db.attach_gate_ask(record["request_id"], row["id"])
        elif db.transition_gate_request(record["request_id"], "cancelled",
                                        detail="approval_ask_missing"):
            await _notify(record, "cancelled",
                          detail="the approval request was removed before anyone decided")


def _decider_of(record: Dict[str, Any]) -> Optional[str]:
    """Who ended the record's ask, read from the ask row (never stored twice)."""
    row = db.get_operator_queue_item_for_agent_by_request_id(record["agent_name"], record["request_id"])
    return (row or {}).get("responded_by_email") or (row or {}).get("disposed_by_email")


async def resolve(request_id: str) -> Optional[str]:
    """Consume the ending of one gate ask. Returns the record's new state, or
    None when there was nothing to do (already consumed, ask still open)."""
    record = db.get_gate_request(request_id)
    if not record or record.get("state") != "pending" or not record.get("ask_item_id"):
        return None
    row = db.get_operator_queue_item_for_agent_by_request_id(record["agent_name"], request_id)
    if not row or row.get("status") == "pending":
        return None
    disposition = row.get("disposition") or {
        "responded": "answered", "acknowledged": "answered",
        "cancelled": "cancelled", "expired": "expired",
    }.get(row.get("status"), "cancelled")
    decider = row.get("responded_by_email") or row.get("disposed_by_email")

    if disposition == "answered" and row.get("response") == APPROVE:
        # Belt and braces over the sink's `may_end`: an approval counts only
        # from a person the ask was addressed to.
        if not _approved_by_addressee(row, decider):
            if db.transition_gate_request(request_id, "denied", detail="approver_not_addressed"):
                await _notify(record, "denied", decider=decider)
            return "denied"
        return await _approve(record, decider)

    target = {"answered": "denied", "cancelled": "cancelled", "expired": "expired"}.get(
        disposition, "cancelled")
    if db.transition_gate_request(request_id, target, detail=disposition):
        await _notify(record, target, decider=decider)
        return target
    return None


def _approved_by_addressee(row: Dict[str, Any], decider: Optional[str]) -> bool:
    if row.get("disposed_by") not in (None, "person") or not decider:
        return False
    resolved = row.get("resolved_to") or []
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except ValueError:
            resolved = []
    return decider.strip().casefold() in {str(p).strip().casefold() for p in resolved}


async def _approve(record: Dict[str, Any], decider: Optional[str]) -> Optional[str]:
    import secrets as _secrets

    request_id, agent = record["request_id"], record["agent_name"]
    execution_id = _secrets.token_urlsafe(16)
    if not db.claim_gate_request_for_dispatch(request_id, execution_id):
        return None   # another worker / the sweep won the claim

    # Re-verify after the claim, against the world as it is now.
    if not db.get_agent_owner(agent):
        return await _end_not_run(record, decider, f"{agent} no longer exists")
    requester_agent = record.get("source_agent")
    if record.get("requester_kind") == KIND_AGENT and requester_agent and requester_agent != agent \
            and not db.get_agent_owner(requester_agent):
        return await _end_not_run(record, decider, f"the requesting agent {requester_agent} no longer exists")
    current = await read_skill_fingerprints(agent, record.get("skills") or [])
    if current is None:
        return await _end_not_run(record, decider, f"{agent} could not be reached (is it running?)")
    frozen = record.get("fingerprints") or {}
    changed = [n for n in (record.get("skills") or [])
               if (current.get(n) or {}).get("fingerprint") != frozen.get(n)]
    if changed:
        if db.transition_gate_request(request_id, "stale", detail=",".join(changed)):
            await _notify(record, "stale", decider=decider)
        return "stale"

    try:
        await _dispatch_approved(record, execution_id)
    except Exception as exc:  # noqa: BLE001 — the run did not start; say so, never retry
        logger.warning("[SkillGate] approved run of %s on %s did not start: %s", request_id, agent, exc)
        return await _end_not_run(record, decider, str(exc) or type(exc).__name__)
    if db.transition_gate_request(request_id, "dispatched"):
        await _notify(record, "approved", decider=decider, execution_id=execution_id)
    return "dispatched"


async def _end_not_run(record, decider, why: str) -> str:
    if db.transition_gate_request(record["request_id"], "not_run", detail=why[:500]):
        await _notify(record, "not_run", decider=decider, detail=why)
    return "not_run"


async def _dispatch_approved(record: Dict[str, Any], execution_id: str) -> str:
    """Start the approved run the way an async `/task` starts: the row (under
    the id the claim wrote), the capacity slot with the PERSISTENT overflow —
    a busy executor queues it instead of losing it — then the run itself in the
    background. Returns "admitted" or "queued"."""
    import asyncio

    from db.write_params import TaskExecutionFields
    from models import ParallelTaskRequest, TaskExecutionStatus
    from db.write_params import ExecutionResult
    from services import chat_execution_service
    from services.capacity_manager import CapacityFull, PersistentTaskPayload, get_capacity_manager
    from services.task_execution_service import dispatch_breaker_active

    agent = record["agent_name"]
    frozen = dict(record.get("dispatch") or {})
    triggered_by = frozen.get("triggered_by") or record.get("triggered_by") or "manual"
    # A request from a public-facing surface keeps its caller prompt (the
    # owner's public-channel instructions, #1205) — re-derived now rather than
    # frozen, so no per-person memory is stored on the record.
    system_prompt = None
    if triggered_by in _PUBLIC_FACING_TRIGGERS:
        from services.platform_prompt_service import build_public_channel_caller_prompt
        system_prompt = build_public_channel_caller_prompt(agent)
    request = ParallelTaskRequest(
        message=record["request_text"],
        model=frozen.get("model"),
        allowed_tools=frozen.get("allowed_tools"),
        system_prompt=system_prompt,
        timeout_seconds=frozen.get("timeout_seconds"),
        async_mode=True,
    )
    row = db.create_task_execution(
        agent_name=agent,
        message=record["request_text"],
        triggered_by=triggered_by,
        fields=TaskExecutionFields(
            source_user_id=frozen.get("source_user_id"),
            source_user_email=frozen.get("source_user_email"),
            source_agent_name=frozen.get("source_agent_name"),
            source_mcp_key_id=frozen.get("source_mcp_key_id"),
            source_mcp_key_name=frozen.get("source_mcp_key_name"),
            model_used=frozen.get("model"),
            subscription_id=frozen.get("subscription_id"),
            chain_depth=frozen.get("chain_depth"),
            source_channel=frozen.get("source_channel"),
            source_channel_chat_id=frozen.get("source_channel_chat_id"),
            source_channel_thread=frozen.get("source_channel_thread"),
            source_channel_client=frozen.get("source_channel_client"),
        ),
        execution_id=execution_id,
    )
    if row is None:
        raise RuntimeError("could not create the execution record")

    timeout = frozen.get("timeout_seconds") or db.get_execution_timeout(agent)
    capacity = get_capacity_manager()
    try:
        admitted = await capacity.acquire(
            agent_name=agent,
            execution_id=execution_id,
            max_concurrent=db.get_max_parallel_tasks(agent),
            message_preview=record["request_text"][:100],
            timeout_seconds=timeout,
            overflow_policy="queue_persistent",
            breaker_enabled=dispatch_breaker_active(agent),
            overflow_payload=PersistentTaskPayload(
                request=request,
                effective_timeout=timeout,
                user_id=frozen.get("source_user_id"),
                user_email=frozen.get("source_user_email"),
                subscription_id=frozen.get("subscription_id"),
                x_source_agent=frozen.get("source_agent_name"),
                triggered_by=triggered_by,
                collaboration_activity_id=None,
                is_self_task=False,
                self_task_activity_id=None,
            ),
        )
    except Exception as exc:
        error = f"Approved run could not be admitted: {exc}"
        won = db.update_execution_status(
            execution_id=execution_id, status=TaskExecutionStatus.FAILED,
            result=ExecutionResult(error=error))
        if won:
            # #1804: the CAS winner closes the dispatch activity — none exists
            # yet on this path (it is opened by execute_task), so this is a
            # no-op kept for the contract, never a reason to skip it.
            from services.activity_service import activity_service
            await activity_service.close_execution_activity(
                execution_id, TaskExecutionStatus.FAILED, error=error)
        if isinstance(exc, CapacityFull):
            raise RuntimeError(f"{agent} is at capacity and its queue is full") from exc
        raise
    if getattr(admitted, "state", None) != "admitted":
        return "queued"   # the backlog drain runs it (`run_async_task`, gate already checked)

    task = asyncio.create_task(chat_execution_service.run_async_task(
        agent_name=agent,
        request=request,
        execution_id=execution_id,
        collaboration_activity_id=None,
        x_source_agent=frozen.get("source_agent_name"),
        user_id=frozen.get("source_user_id"),
        user_email=frozen.get("source_user_email"),
        subscription_id=frozen.get("subscription_id"),
        triggered_by_override=triggered_by,
    ))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return "admitted"


# ---------------------------------------------------------------------------
# Telling the requester — mechanically, on every ending (AC5)
# ---------------------------------------------------------------------------

_OUTCOME = {
    "approved": "was approved{by} and is running as execution {eid}",
    "denied": "was rejected{by}; nothing was run",
    "expired": "expired before anyone decided; nothing was run",
    "cancelled": "was cancelled{by}; nothing was run",
    "stale": ("was approved{by}, but the skill changed on {agent} after you asked, so "
              "nothing was run. Ask again to have the current version approved"),
    "not_run": "was approved{by}, but it could not run: {detail}",
    "unknown": ("was approved{by}, but the run could not be confirmed to have started. It "
                "was not retried — check {agent}'s executions before asking again"),
}


def outcome_text(record: Dict[str, Any], outcome: str, *, decider: Optional[str] = None,
                 execution_id: Optional[str] = None, detail: Optional[str] = None,
                 for_person: bool = False) -> str:
    """One sentence a requester can act on. Skill names without a slash, so the
    notice itself never reads as a request to run one. An agent's copy carries
    the platform's `[Trinity]` marker; a person's Inbox copy does not."""
    skills = ", ".join(record.get("skills") or []) or "a gated skill"
    body = _OUTCOME.get(outcome, "ended ({outcome})").replace("{outcome}", outcome)
    body = (body.replace("{by}", f" by {decider}" if decider else "")
                .replace("{eid}", execution_id or "?")
                .replace("{agent}", record["agent_name"])
                .replace("{detail}", sanitize_text(detail or "unknown reason")))
    origin = record.get("requester_execution_id")
    tail = f" (Your request came from execution {origin}.)" if origin else ""
    marker = "" if for_person else "[Trinity] "
    return (f"{marker}Your request {record['request_id']} to run the skill {skills} on "
            f"{record['agent_name']} {body}.{tail}")


# Outcomes that are exactly what the decider did. A person who decided their own
# request is not told of it; any other outcome (it could not run) is news.
_SELF_EVIDENT_OUTCOMES = frozenset({"approved", "denied", "cancelled"})


async def _notify(record: Dict[str, Any], outcome: str, *, decider: Optional[str] = None,
                  execution_id: Optional[str] = None, detail: Optional[str] = None,
                  notify_decider: bool = False) -> None:
    """Agent requester → a platform task to that agent; person requester → an
    Inbox notice addressed to them. Schedules, channels, public visitors, paid
    callers and connectors have no channel after the request — their record
    and the executor's Executions carry it. Once per record."""
    if not db.mark_gate_request_notified(record["request_id"]):
        return
    await _audit_outcome(record, outcome, decider=decider, execution_id=execution_id)
    text = outcome_text(record, outcome, decider=decider, execution_id=execution_id,
                        detail=detail, for_person=True)
    kind = record.get("requester_kind")
    asker = (record.get("requester_email") or "").strip().casefold() if kind == KIND_PERSON else ""
    decided_own = bool(asker) and asker == (decider or "").strip().casefold()
    try:
        if kind == KIND_AGENT and record.get("source_agent"):
            # #715: an agent learns the outcome, never which person decided it.
            await _wake_requester_agent(record, outcome_text(
                record, outcome, execution_id=execution_id, detail=detail))
        elif asker and not (decided_own and outcome in _SELF_EVIDENT_OUTCOMES):
            _note_to_person(record, record["requester_email"], outcome, text)
        if notify_decider and decider and not decided_own:   # one notice per person
            _note_to_person(record, decider, f"{outcome}-decider", text)
    except Exception:  # noqa: BLE001 — the outcome stands on the record either way
        logger.warning("[SkillGate] could not notify for %s", record["request_id"], exc_info=True)


async def _audit_outcome(record: Dict[str, Any], outcome: str, *, decider: Optional[str],
                         execution_id: Optional[str]) -> None:
    """One audit row per gated request's outcome — the run it started, or why
    nothing ran. The raise and the ending are audited by the ask sink. Ids and
    enums only (the request text stays on the record). Best effort."""
    from services.platform_audit_service import AuditEventType, platform_audit_service
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.EXECUTION,
            event_action="skill_gate_outcome",
            source="system",
            actor_email=decider,
            target_type="agent",
            target_id=record["agent_name"],
            endpoint=None,
            request_id=None,
            details={
                "request_id": record["request_id"],
                "outcome": outcome,
                "skills": list(record.get("skills") or []),
                "execution_id": execution_id,
                "origin_execution_id": record.get("origin_execution_id"),
                "requester_kind": record.get("requester_kind"),
            },
        )
    except Exception:  # noqa: BLE001 — the outcome stands on the record either way
        logger.warning("[SkillGate] outcome audit failed for %s", record["request_id"], exc_info=True)


async def _wake_requester_agent(record: Dict[str, Any], text: str) -> None:
    import asyncio
    from services.task_execution_service import get_task_execution_service

    frozen = record.get("dispatch") or {}

    async def _run():
        try:
            await get_task_execution_service().execute_task(
                agent_name=record["source_agent"],
                message=text,
                triggered_by=SKILL_GATE_TRIGGER,
                chain_depth=frozen.get("chain_depth"),
            )
        except Exception:  # noqa: BLE001 — a stopped requester misses it; the record stands
            logger.info("[SkillGate] requester %s not woken for %s",
                        record["source_agent"], record["request_id"], exc_info=True)

    task = asyncio.create_task(_run())
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


def _note_to_person(record: Dict[str, Any], email: str, outcome: str, text: str) -> None:
    from services import ask_service
    suffix = hashlib.sha256(f"{record['request_id']}\x00{outcome}\x00{email}".encode()).hexdigest()[:32]
    ask_service.raise_ask(
        record["agent_name"],
        {
            "request_id": f"{NOTE_PREFIX}{suffix}",
            "type": "alert",
            "priority": "medium",
            "title": f"Your gated request on {record['agent_name']}: {outcome.split('-')[0]}"[:300],
            "question": text,
        },
        raised_by="gate", channel="gate", addressee=email,
    )


async def cancel_pending_for_agent(agent_name: str, *, actor_email: Optional[str],
                                   actor_user=None, reason: str) -> int:
    """An agent was deleted or renamed: its pending gated requests can no
    longer run against the agent they were approved for. Each record is
    `cancelled` (so a later approval runs nothing) and its requester told; the
    ask itself is ended through the sink as the acting person when they may end
    it, and otherwise stays open but inert. Best effort; returns how many."""
    from dependencies import is_person_principal
    from services import ask_service

    # Only a PERSON's ending is recorded as one: an agent key carries its
    # owner's email, and an ask ended under it would be a person ending in the
    # ledger that no person made. The record is cancelled either way.
    if actor_user is not None and not is_person_principal(actor_user):
        actor_email = None
    cancelled = 0
    for record in db.list_pending_gate_requests(agent_name):
        if not db.transition_gate_request(record["request_id"], "cancelled", detail=reason):
            continue
        cancelled += 1
        if record.get("ask_item_id") and actor_email:
            try:
                ask_service.cancel(record["ask_item_id"],
                                   actor=ask_service.Actor(email=actor_email, user=actor_user),
                                   reason=reason)
            except Exception:  # noqa: BLE001 — the record's state already decides
                logger.info("[SkillGate] ask %s left open (inert) after %s",
                            record["ask_item_id"], reason)
        await _notify(record, "cancelled", detail=reason)
    return cancelled
