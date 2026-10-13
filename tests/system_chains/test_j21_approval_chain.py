"""J21 — an approval rule is enforced end to end (trinity-enterprise#795).

A gated skill on one agent; another agent asks for it; nothing runs until the
right person approves; it then runs exactly once with the input that was
approved, and the requester is told how it ended.

Every check reads a record the system wrote:

* the gate request row (``skill_gate_requests``) — its state, its requester,
  the execution it dispatched — read where the stack runs (``stack_reads``);
* the executor's execution list — how many runs carried the request's input;
* the approver's OWN Workspace asks read — never the admin's broad listing
  (ent#815: an approval counts as *reached the approver* only there);
* the requester agent's execution list — the platform's notice, by request id.

The people are real accounts that sign in by the real email-code flow; the
approver role resolves to exactly ONE person (ent#816: a role reaching two or
more people reaches nobody's Inbox today).

**Not run, on purpose:**

* *The rule comes from the canon* and *the approver changes in the canon*: the
  canon approval map (trinity-enterprise#757) does not exist, so the gate is
  set on the instance and the canon half is recorded as not run — the chain is
  ``partial`` until it does.
* *Nobody answers*: the shortest deadline is one hour and there is no clock
  override (none is added — a test door in the gate would be a door). Set
  ``CHAIN_GATE_EXPIRY=1`` to wait it out for real.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest
import requests

from journeys.conftest import (
    add_edge,
    agent_executions,
    agent_mcp_key,
    create_agent_and_wait,
    poll_until,
)

from .conftest import model_key_available, not_run, release_agent, wait_agent_server
from .stack_reads import API, Person, email_sign_in_enabled, gate_request, sign_in_person

RUN = uuid.uuid4().hex[:6]
SKILL = f"chain-gated-{RUN}"
#: Each request carries its own marker, so a run is counted by its input alone.
INPUT_APPROVE = f"approve-me-{RUN}"
INPUT_REJECT = f"reject-me-{RUN}"
INPUT_EXPIRE = f"expire-me-{RUN}"
INPUT_MOVED = f"moved-approver-{RUN}"
DONE_MARK = "CHAIN-GATED-DONE"
RUN_DEADLINE_S = float(os.getenv("CHAIN_GATE_RUN_DEADLINE_S", "300"))
EXPIRY_ENABLED = os.getenv("CHAIN_GATE_EXPIRY", "").strip() == "1"
#: One hour of deadline, plus the sweep that ends it.
EXPIRY_DEADLINE_S = float(os.getenv("CHAIN_GATE_EXPIRY_DEADLINE_S", str(75 * 60)))

SKILL_FILE = f"""---
name: {SKILL}
description: A chain-test skill. Replies with a fixed mark and the input it was given.
---

Reply with exactly one line: `{DONE_MARK} <the text you were given after the skill name>`.
Do nothing else and call no tools.
"""

#: The states a gate request ends a run in (trinity#3520).
RAN_TERMINAL = ("succeeded", "failed")


# --------------------------------------------------------------------------- helpers

def _provision(client, email: str) -> int:
    r = client.post("/api/enterprise/user-management/members", json={"email": email, "role": "user"})
    if r.status_code == 404:
        not_run("the user-management module is not installed on this build; the chain "
                "needs real people (provision a member)")
    assert r.status_code in (200, 201), f"provisioning {email} answered {r.status_code}: {r.text[:300]}"
    return int(r.json()["id"])


def _assign(client, agent: str, user_id: int, kind: str) -> None:
    r = client.post(f"/api/enterprise/assignments/agents/{agent}",
                    json={"user_id": user_id, "kind": kind})
    if r.status_code == 404 and "not found" in r.text.lower() and "agent" not in r.text.lower():
        not_run("the assignments module is not installed on this build")
    assert r.status_code in (200, 201), f"assigning {kind} on {agent} answered {r.status_code}: {r.text[:300]}"


def _share(client, agent: str, email: str) -> None:
    """Share the agent with a person — their access; an assignment is only
    their role. A Workspace shows an ask only for an agent on the viewer's
    roster (`client_portal/asks/service.py::_on_roster`), so a company that
    names an approver also shares the agent with them. Without the share the
    approval is still addressed to them and silently reaches nobody
    (trinity-enterprise#881)."""
    r = client.post(f"/api/agents/{agent}/share", json={"email": email})
    assert r.status_code in (200, 201), f"sharing {agent} with {email} answered {r.status_code}: {r.text[:300]}"


def _set_gate(client, agent: str, **changes) -> None:
    r = client.put(f"/api/agents/{agent}/skill-gates/{SKILL}", json=changes)
    assert r.status_code == 200, f"setting the gate answered {r.status_code}: {r.text[:300]}"


def _request_as_agent(requester_key: str, executor: str, text: str) -> dict:
    """The requester agent asks the executor, with ITS OWN key — the path a
    `chat_with_agent` call takes. A held request answers 202."""
    r = requests.post(f"{API}/api/agents/{executor}/task",
                      headers={"Authorization": f"Bearer {requester_key}"},
                      json={"message": f"/{SKILL} {text}"}, timeout=60)
    assert r.status_code == 202, (
        f"a request for the gated skill answered {r.status_code}, not 202 (held): {r.text[:300]}")
    body = r.json()
    body = body.get("detail", body) if isinstance(body.get("detail"), dict) else body
    assert body.get("status") == "pending_approval" and body.get("request_id"), \
        f"the held answer names no pending request: {body}"
    return body


def _runs_with(client, agent: str, marker: str) -> list:
    """Executions on `agent` that carried `marker` and actually ran — the
    SKIPPED row a held request leaves behind is not a run."""
    return [e for e in agent_executions(client, agent)
            if marker in (e.get("message") or "") and e.get("status") not in ("skipped",)]


def _card_for(person: Person, executor: str, marker: str):
    r = person.get("/api/enterprise/client-portal/asks", params={"agent_name": executor})
    assert r.status_code in (200, 404), f"{person.email}'s asks read answered {r.status_code}: {r.text[:300]}"
    if r.status_code == 404:
        return None
    for ask in r.json():
        proposal = ask.get("proposal") or {}
        if proposal.get("kind") == "gated_skill" and marker in (proposal.get("input") or ""):
            return ask
    return None


def _answer(person: Person, ask_id: str, response: str):
    return person.post(f"/api/enterprise/client-portal/asks/{ask_id}/answer",
                       json={"response": response})


def _state(request_id: str) -> str:
    return (gate_request(request_id) or {}).get("state")


def _wait_state(request_id: str, states, deadline_s: float, what: str) -> dict:
    return poll_until(lambda: (lambda r: r if r and r.get("state") in states else None)(
        gate_request(request_id)), deadline_s=deadline_s, interval_s=3, describe=what)


def _told(client, requester: str, request_id: str, headline: str):
    """The requester agent received the platform's notice for `request_id`."""
    def found():
        for e in agent_executions(client, requester):
            msg = e.get("message") or ""
            if request_id in msg and msg.startswith(f"[Trinity] {headline}"):
                return e
        return None
    return poll_until(found, deadline_s=120, interval_s=3,
                      describe=f"{requester} was never told '{headline}' for {request_id}")


# --------------------------------------------------------------------------- fixtures

@pytest.fixture
def cast(chain_client):
    """The executor, the requester, and two people the executor is shared
    with — P1 fills `primary`, P2 holds no role on the executor yet."""
    client = chain_client
    if not email_sign_in_enabled(client):
        not_run("email-code sign-in is off on this target; the chain needs two real people")
    names = []
    try:
        executor = f"pytest-ephemeral-chain-{uuid.uuid4().hex[:8]}"
        requester = f"pytest-ephemeral-chain-{uuid.uuid4().hex[:8]}"
        for n in (executor, requester):
            create_agent_and_wait(client, n)
            names.append(n)
        for n in names:
            wait_agent_server(client, n)
        r = client.put(f"/api/agents/{executor}/files",
                       params={"path": f".claude/skills/{SKILL}/SKILL.md"},
                       json={"content": SKILL_FILE})
        assert r.status_code in (200, 201), f"installing the test skill answered {r.status_code}: {r.text[:300]}"
        add_edge(client, requester, executor)
        people = []
        for tag in ("approver", "other"):
            email = f"chain-{tag}-{RUN}@example.com"
            uid = _provision(client, email)
            people.append(Person(email, uid, sign_in_person(email)))
            # Both people can reach the executor: the check that P2 is not
            # shown the approval is then about the role, not about access.
            _share(client, executor, email)
        _assign(client, executor, people[0].user_id, "primary")
        yield {"executor": executor, "requester": requester, "p1": people[0], "p2": people[1]}
    finally:
        for n in names:
            release_agent(client, n)


# --------------------------------------------------------------------------- the chain

@pytest.mark.chain("J21", "An approval rule is enforced end to end")
def test_an_approval_rule_is_enforced_end_to_end(chain, chain_client, cast):
    client = chain_client
    executor, requester = cast["executor"], cast["requester"]
    p1, p2 = cast["p1"], cast["p2"]
    try:
        requester_key = agent_mcp_key(requester)
    except Exception as e:  # noqa: BLE001 — a missing Docker socket is a precondition
        not_run(f"the requester's own key could not be read ({type(e).__name__}); the run "
                f"needs the Docker socket of the host running the stack")

    chain.not_run_step(
        "the rule in the canon is applied to the instance by the orchestrator, and drift is reported",
        "the canon approval map does not exist yet (trinity-enterprise#757) — the gate is set "
        "on the instance instead")

    with chain.step("the gate is on the instance: the skill needs the primary's approval"):
        _set_gate(client, executor, approver="primary")
        gates = client.get(f"/api/agents/{executor}/skill-gates").json()
        assert SKILL in str(gates), f"the gate map does not list {SKILL}: {gates}"

    with chain.step("a request from another agent does not run"):
        held = _request_as_agent(requester_key, executor, INPUT_APPROVE)
        rid = held["request_id"]
        record = gate_request(rid)
        assert record and record["state"] == "pending", f"the gate request is not pending: {record}"
        assert record.get("source_agent") == requester, \
            f"the request is not recorded as {requester}'s: {record.get('source_agent')!r}"
        assert not _runs_with(client, executor, INPUT_APPROVE), "the gated skill ran before anyone approved"

    with chain.step("the approval reaches the approver's own Inbox, naming agent, skill, input and requester"):
        card = poll_until(lambda: _card_for(p1, executor, INPUT_APPROVE), deadline_s=60,
                          describe="the approval never reached the approver's own asks read")
        proposal = card["proposal"]
        assert proposal.get("agent") == executor, f"the card names agent {proposal.get('agent')!r}"
        assert SKILL in (proposal.get("skills") or []), f"the card names skills {proposal.get('skills')!r}"
        assert INPUT_APPROVE in proposal.get("input", ""), "the card does not carry the input"
        assert requester in (proposal.get("requester") or ""), \
            f"the card names requester {proposal.get('requester')!r}, not {requester}"
        assert _card_for(p2, executor, INPUT_APPROVE) is None, \
            "a person who is not the approver was shown the approval"

    with chain.step("a person who is not the approver cannot approve"):
        r = _answer(p2, card["id"], "Approve")
        assert r.status_code >= 400, f"a non-approver's Approve answered {r.status_code}: {r.text[:200]}"
        assert _state(rid) == "pending", "a non-approver's answer moved the request"
        assert not _runs_with(client, executor, INPUT_APPROVE), "a non-approver's answer ran the skill"

    can_run = model_key_available(client)
    if not can_run:
        for name in ("approve: the action runs exactly once, with the input that was approved",
                     "the requester is told the outcome",
                     "a second approve does not run the action twice"):
            chain.not_run_step(name, "the target has no model credential, so an approved run cannot run")
    else:
        with chain.step("approve: the action runs exactly once, with the input that was approved"):
            r = _answer(p1, card["id"], "Approve")
            assert r.status_code == 200, f"the approver's Approve answered {r.status_code}: {r.text[:300]}"
            record = _wait_state(rid, RAN_TERMINAL, RUN_DEADLINE_S,
                                 "the approved request never finished running")
            assert record["state"] == "succeeded", \
                f"the approved run ended {record['state']}: {record.get('state_detail')}"
            runs = _runs_with(client, executor, INPUT_APPROVE)
            assert len(runs) == 1, f"the approved request ran {len(runs)} times, not once"
            assert runs[0].get("id") == record.get("dispatched_execution_id"), \
                "the run on record is not the one the gate dispatched"

        with chain.step("the requester is told the outcome"):
            _told(client, requester, rid, "Finished successfully")

        with chain.step("a second approve does not run the action twice"):
            _answer(p1, card["id"], "Approve")   # any answer code: what matters is what ran
            time.sleep(15)
            assert len(_runs_with(client, executor, INPUT_APPROVE)) == 1, \
                "a second Approve ran the action again"

    with chain.step("refuse: nothing runs and the requester is told"):
        rid2 = _request_as_agent(requester_key, executor, INPUT_REJECT)["request_id"]
        card2 = poll_until(lambda: _card_for(p1, executor, INPUT_REJECT), deadline_s=60,
                           describe="the second approval never reached the approver")
        r = _answer(p1, card2["id"], "Reject")
        assert r.status_code == 200, f"the approver's Reject answered {r.status_code}: {r.text[:300]}"
        _wait_state(rid2, ("denied",), 60, "the rejected request never read denied")
        assert not _runs_with(client, executor, INPUT_REJECT), "a rejected request ran"
        _told(client, requester, rid2, "Rejected")

    if not EXPIRY_ENABLED:
        chain.not_run_step(
            "nobody answers: at the deadline nothing runs, the requester is told, and asking "
            "again raises one request linked to the expired one",
            "the shortest deadline is one hour; set CHAIN_GATE_EXPIRY=1 to wait it out")
    else:
        with chain.step("nobody answers: at the deadline nothing runs, the requester is told, "
                        "and asking again raises one request linked to the expired one"):
            _set_gate(client, executor, deadline_hours=1)
            rid3 = _request_as_agent(requester_key, executor, INPUT_EXPIRE)["request_id"]
            _wait_state(rid3, ("expired",), EXPIRY_DEADLINE_S, "the unanswered request never expired")
            assert not _runs_with(client, executor, INPUT_EXPIRE), "an expired request ran"
            _told(client, requester, rid3, "Expired")
            rid4 = _request_as_agent(requester_key, executor, INPUT_EXPIRE)["request_id"]
            assert rid4 != rid3, "asking again after the expiry answered the expired request"
            expired_ask = (gate_request(rid3) or {}).get("ask_item_id")
            link = gate_link(executor, rid4)
            assert link and link == expired_ask, \
                f"the new request's ask is linked to {link!r}, not the expired ask {expired_ask!r}"

    with chain.step("the approver changes: the next request reaches the new approver, not the old one"):
        _assign(client, executor, p2.user_id, "approver")
        _set_gate(client, executor, approver="approver")
        rid5 = _request_as_agent(requester_key, executor, INPUT_MOVED)["request_id"]
        card5 = poll_until(lambda: _card_for(p2, executor, INPUT_MOVED), deadline_s=60,
                           describe="the request never reached the new approver")
        assert _card_for(p1, executor, INPUT_MOVED) is None, "the old approver was still shown the request"
        # Leave nothing pending behind.
        _answer(p2, card5["id"], "Reject")
        _wait_state(rid5, ("denied",), 60, "the cleanup Reject never ended the request")

    chain.not_run_step(
        "the approver changes in the canon, and after it is applied the next request reaches the new one",
        "the canon approval map does not exist yet (trinity-enterprise#757); the change was made "
        "on the instance in the step above")


def gate_link(executor: str, request_id: str):
    """The expired ask the gate linked a re-ask to, read from the ask row."""
    from .stack_reads import backend_json

    row = backend_json(
        "import json\n"
        "from database import db\n"
        f"r = db.get_operator_queue_item_for_agent_by_request_id({executor!r}, {request_id!r}) or {{}}\n"
        "print(json.dumps(r.get('supersedes_expired')))\n")
    return row
