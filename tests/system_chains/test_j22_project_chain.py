"""J22 — a project is run across agents and people (trinity-enterprise#795).

A steward opens a project with an agent and two people; the agent works its
task and cannot close it itself; a person verifies and closes it; the log says
who did what, to a member who took no part; the project links to a room and an
agent's message about the project is in that room.

Every check reads a record the system wrote — the project, its task and its
append-only log (``/api/enterprise/projects``), and the room's messages
(``/api/rooms``). The agent acts with its OWN key through the agent door
(``/api/enterprise/projects/agent/...``), the people through their own sessions.

**Not run, on purpose — no platform writer exists yet:**

* *Tasks reach their owners as playbook calls*: nothing dispatches a project
  task to its agent; the agent finds it only by reading its task list (which
  this chain checks instead).
* *Running the steward again does not hand the same task out twice*: there is
  no steward run that hands tasks out to repeat.

Both arrive with the project skills' platform mode (trinity-enterprise#788).
"""
from __future__ import annotations

import uuid

import pytest
import requests

from journeys.conftest import agent_mcp_key, create_agent_and_wait, poll_until

from .conftest import not_run, release_agent, wait_agent_server
from .stack_reads import API, Person, email_sign_in_enabled, sign_in_person

RUN = uuid.uuid4().hex[:6]
PROJECTS = "/api/enterprise/projects"
TASK_TITLE = f"Chase the overdue invoices ({RUN})"
DONE_CLAIM = f"Reminders sent to the three overdue accounts ({RUN})."
ROOM_MESSAGE = f"Project update ({RUN}): the overdue-invoice reminders are out."


#: The turn id the platform defines for an agent's call made outside any turn
#: (`projects/audience.py::MANUAL_EXECUTION_ID`) — what the agent's own tools
#: send when no execution carries the call. A value the product accepts, not a
#: door added for the test.
MANUAL_TURN = "manual"


class AgentKey:
    """The agent acting with its own key, outside a turn."""

    def __init__(self, key: str):
        self._headers = {"Authorization": f"Bearer {key}", "X-Trinity-Execution-Id": MANUAL_TURN}

    def get(self, path, **kw):
        return requests.get(f"{API}{path}", headers=self._headers, timeout=30, **kw)

    def post(self, path, **kw):
        return requests.post(f"{API}{path}", headers=self._headers, timeout=30, **kw)

    def patch(self, path, **kw):
        return requests.patch(f"{API}{path}", headers=self._headers, timeout=30, **kw)


def _ok(r, what: str):
    assert r.status_code in (200, 201), f"{what} answered {r.status_code}: {r.text[:300]}"
    return r.json() if r.content else None


def _provision(client, email: str) -> int:
    r = client.post("/api/enterprise/user-management/members", json={"email": email, "role": "user"})
    if r.status_code == 404:
        not_run("the user-management module is not installed on this build; the chain needs real people")
    return int(_ok(r, f"provisioning {email}")["id"])


@pytest.fixture
def worker(chain_client):
    name = f"pytest-ephemeral-chain-{uuid.uuid4().hex[:8]}"
    try:
        create_agent_and_wait(chain_client, name)
        wait_agent_server(chain_client, name)
        yield name
    finally:
        release_agent(chain_client, name)


@pytest.mark.chain("J22", "A project is run across agents and people")
def test_a_project_is_run_across_agents_and_people(chain, chain_client, worker):
    client = chain_client
    if client.get(PROJECTS).status_code == 404:
        not_run("the projects module is not installed on this build")
    if not email_sign_in_enabled(client):
        not_run("email-code sign-in is off on this target; the chain needs real people")
    try:
        agent = AgentKey(agent_mcp_key(worker))
    except Exception as e:  # noqa: BLE001 — a missing Docker socket is a precondition
        not_run(f"the agent's own key could not be read ({type(e).__name__}); the run needs "
                f"the Docker socket of the host running the stack")
    people = []
    for tag in ("verifier", "reader"):
        email = f"chain-{tag}-{RUN}@example.com"
        uid = _provision(client, email)
        people.append(Person(email, uid, sign_in_person(email)))
    verifier, reader = people
    project_id = None

    try:
        with chain.step("a steward opens a project with a member agent and member people"):
            # The opener stewards it (no steward named) and owns the agent, so
            # the agent is active at once — no owner consent to wait for.
            project = _ok(client.post(PROJECTS, json={
                "name": f"Q4 collections push ({RUN})",
                "goal": "Bring every overdue account current this quarter.",
                "agents": [worker]}), "opening the project")
            project_id = project["id"]
            for p in people:
                _ok(client.post(f"{PROJECTS}/{project_id}/members", json={"email": p.email}),
                    f"adding {p.email} as a member")
            seen = _ok(verifier.get(f"{PROJECTS}/{project_id}"), "the verifier reading the project")
            assert worker in str(seen), f"the project as a member reads it does not name {worker}"

        with chain.step("the agent's task is on the agent's own task list"):
            task = _ok(client.post(f"{PROJECTS}/{project_id}/tasks", json={
                "title": TASK_TITLE, "agent": worker,
                "done_definition": "Every overdue account has had a reminder."}), "creating the task")
            task_id = task.get("task_id") or task.get("id")
            assert task_id, f"the created task carries no id: {task}"
            tasks = _ok(agent.get(f"{PROJECTS}/agent/projects/{project_id}/tasks"),
                        "the agent listing its project tasks")
            assert TASK_TITLE in str(tasks), "the agent's own task list does not hold its task"

        chain.not_run_step(
            "tasks reach their owners as playbook calls",
            "nothing dispatches a project task to its agent yet — the agent finds it only by "
            "reading its task list (checked in the step above); the project skills' platform "
            "mode is abilityai/trinity-enterprise#788")

        with chain.step("the agent finishes its task and cannot mark it done itself"):
            r = agent.patch(f"{PROJECTS}/agent/projects/{project_id}/tasks/{task_id}",
                            json={"status": "done"})
            assert r.status_code == 403, \
                f"the agent marking its own task done answered {r.status_code}, not 403: {r.text[:200]}"
            _ok(agent.patch(f"{PROJECTS}/agent/projects/{project_id}/tasks/{task_id}",
                            json={"status": "pending-verification", "note": DONE_CLAIM}),
                "the agent claiming completion")
            row = _ok(client.get(f"{PROJECTS}/{project_id}/tasks/{task_id}"), "reading the task")
            assert (row.get("task") or row).get("status") == "pending-verification", \
                f"the task is not pending verification after the claim: {row}"

        with chain.step("a person verifies and closes it"):
            _ok(verifier.patch(f"{PROJECTS}/{project_id}/tasks/{task_id}", json={"status": "done"}),
                "the verifier closing the task")
            row = _ok(client.get(f"{PROJECTS}/{project_id}/tasks/{task_id}"), "reading the task")
            assert (row.get("task") or row).get("status") == "done", f"the task is not done: {row}"

        with chain.step("the project's log shows who did what, to a member who took no part"):
            log = _ok(reader.get(f"{PROJECTS}/{project_id}/log"), "the reader's log read")
            entries = log if isinstance(log, list) else (log.get("entries") or log.get("items") or [])
            text = " ".join(f"{e.get('author')} {e.get('body')}" for e in entries)
            assert f"agent:{worker}" in text and "pending-verification" in text, \
                f"the log does not record the agent's done claim: {text[:400]}"
            assert verifier.email in text and f"{task_id} done" in text, \
                f"the log does not record who verified it: {text[:400]}"

        chain.not_run_step(
            "running the steward again does not hand the same task out twice",
            "there is no steward run that hands tasks out yet, so there is nothing to repeat "
            "(abilityai/trinity-enterprise#788)")

        with chain.step("the project links to a room, and an agent's message about it appears there"):
            room = _ok(client.post("/api/rooms", json={"name": f"Collections ({RUN})", "agents": [worker]}),
                       "opening the room")
            room_id = room.get("id") or room.get("room_id")
            _ok(client.post(f"{PROJECTS}/{project_id}/links", json={"kind": "room", "target_id": room_id}),
                "linking the room to the project")
            # Read by someone IN the room: a project shows a room link only to
            # the room's participants (the room is theirs, not the project's).
            by_link = _ok(client.get(f"{PROJECTS}/by-link/room/{room_id}"), "looking the room's project up")
            assert project_id in str(by_link), f"the room does not resolve to the project: {by_link}"
            _ok(agent.post(f"/api/rooms/{room_id}/messages", json={"content": ROOM_MESSAGE}),
                "the agent posting to the room")
            def in_room():
                body = client.get(f"/api/rooms/{room_id}").json()
                for m in body.get("messages") or []:
                    if m.get("content") == ROOM_MESSAGE:
                        return m
                return None
            msg = poll_until(in_room, deadline_s=30, describe="the agent's message never appeared in the room")
            assert worker in str(msg.get("sender") or msg.get("sender_name") or msg), \
                f"the room message is not attributed to {worker}: {msg}"
    finally:
        if project_id:
            client.post(f"{PROJECTS}/{project_id}/archive")
