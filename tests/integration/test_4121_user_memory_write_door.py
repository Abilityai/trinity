"""The write_user_memory door over the network, with a real agent key (#4121).

Runs on verify-local's sibling stack (stage 6 is `pytest tests/integration`
against `TRINITY_API_URL`); run it against a stack you can afford to mutate, never
a working instance. tests/unit/test_4121_user_memory_write_door.py carries the
full matrix on every PR; this proves the deployed image holds the rule at the
network edge: real TCP, the real auth stack, a real `trinity_mcp_…` agent key.

Why the rows are seeded inside the backend: no API returns an agent key
(`POST /api/mcp/keys` refuses `scope=agent`) and a `--skip-agent` run has no agent
container to read one from, so the key comes from the function agent creation
calls, and the runs from the one the task path calls. The container is found by
the host port of `TRINITY_API_URL`, so a seed can only land in the stack the
requests go to. Needs the Docker socket of the host running that stack: without
it this FAILS naming the requirement, because a skip here reads as coverage.

Everything seeded is under a `pytest-ephemeral-` agent name that has no container,
and the runs are ended and the key and ownership row removed at teardown.
"""
from __future__ import annotations

import json
import textwrap
import uuid
from urllib.parse import urlparse

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import assert_json_response

pytestmark = pytest.mark.integration

ANN, BOB = "ann@example.com", "bob@example.com"
NOTE = "Prefers invoices as PDF."
_RESULT = "SEED-RESULT "
_PRELUDE = f"""
import json, os
from database import db
from db.write_params import ExecutionResult, TaskExecutionFields
args = json.loads(os.environ["SEED_ARGS"])
def done(value):
    print({_RESULT!r} + json.dumps(value))
"""


def _backend_container(base_url: str):
    """The backend container publishing the host port the API client talks to.
    `ignore_removed`: parallel stacks come and go during the listing."""
    port = str(urlparse(base_url).port or 80)
    try:
        import docker  # noqa: WPS433 — test-only dependency, declared in requirements-test.txt
        containers = docker.from_env().containers.list(
            ignore_removed=True, filters={"label": "com.docker.compose.service=backend"})
    except Exception as e:  # noqa: BLE001 — turn any SDK failure into the finding
        raise AssertionError(
            f"#4121's live test needs the Docker socket of the host running the stack, to seed "
            f"an agent key inside its backend ({type(e).__name__}). Run it where the stack runs."
        ) from e
    for container in containers:
        for bindings in (container.ports or {}).values():
            if any(b.get("HostPort") == port for b in bindings or []):
                return container
    raise AssertionError(f"no backend container on this Docker host publishes port {port} ({base_url})")


class Door:
    """One agent with a real key, its runs, and the door. Never reprs the key."""

    def __init__(self, client: TrinityApiClient):
        self.client = client
        self.container = _backend_container(client.config.base_url)
        self.name = f"pytest-ephemeral-4121-{uuid.uuid4().hex[:8]}"
        self.runs: list[str] = []
        self.key = self._backend("""
            db.register_agent_owner(args["name"], "admin")
            done(db.create_agent_mcp_api_key(args["name"], "admin").api_key)
        """, name=self.name)

    def __repr__(self) -> str:
        return f"Door({self.name})"

    def _backend(self, code: str, **seed_args):
        script = _PRELUDE + textwrap.dedent(code)
        exit_code, output = self.container.exec_run(
            ["python3", "-c", script], environment={"SEED_ARGS": json.dumps(seed_args)})
        text = output.decode(errors="replace")
        line = next((ln for ln in reversed(text.splitlines()) if ln.startswith(_RESULT)), None)
        assert exit_code == 0 and line, f"seeding in the backend failed (exit {exit_code}): {text[-600:]}"
        return json.loads(line[len(_RESULT):])

    def run(self, *, email: str = ANN, triggered_by: str = "public") -> str:
        """A live run of this agent, as the task path records one. Marked dispatched,
        as a real turn is, or the no-session sweep fails it after a minute."""
        execution_id = self._backend("""
            row = db.create_task_execution(args["name"], "hello", args["trigger"],
                                           fields=TaskExecutionFields(source_user_email=args["email"]))
            db.mark_execution_dispatched(row.id)
            done(row.id)
        """, name=self.name, trigger=triggered_by, email=email)
        self.runs.append(execution_id)
        return execution_id

    def end(self, *execution_ids: str) -> None:
        self._backend("""
            for execution_id in args["ids"]:
                db.update_execution_status(execution_id, "success", ExecutionResult(response="done"))
            done(True)
        """, ids=list(execution_ids))

    def notes(self, email: str = ANN) -> str:
        return self._backend("""
            done(db.get_public_user_memory_agent_notes(args["name"], args["email"]))
        """, name=self.name, email=email)

    def write(self, execution_id: str, *, text: str = NOTE, token: str | None = None):
        return self.client.post(
            f"/api/agents/{self.name}/user-memory",
            json={"execution_id": execution_id, "memory_text": text},
            auth=False,
            headers={"Authorization": f"Bearer {token or self.key}"},
        )

    def close(self) -> None:
        self._backend("""
            for execution_id in args["ids"]:
                db.update_execution_status(execution_id, "success", ExecutionResult(response="done"))
            db.delete_agent_mcp_api_key(args["name"])
            db.delete_agent_ownership(args["name"])
            done(True)
        """, name=self.name, ids=self.runs)


@pytest.fixture
def door(api_client: TrinityApiClient):
    opened = Door(api_client)
    yield opened
    opened.close()


def _refused(resp, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["detail"]["code"] == code, resp.text


def test_the_live_turn_writes_its_person(door: Door):
    resp = door.write(door.run())
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_email"] == ANN
    assert door.notes() == NOTE


def test_a_finished_run_of_another_person_cannot_be_named(door: Door):
    """The reported write: the agent's own key, in any later turn, names a run
    that ended and replaces that person's notes."""
    theirs = door.run()
    assert door.write(theirs, text="Ann's real notes.").status_code == 200
    door.end(theirs)

    _refused(door.write(theirs, text="Planted by someone else's turn."), 422, "memory_turn_not_live")

    assert door.notes() == "Ann's real notes."


def test_two_people_live_on_the_agent_refuse_each_other(door: Door):
    ann, bob = door.run(), door.run(email=BOB)
    for named in (ann, bob):
        _refused(door.write(named), 422, "memory_turn_ambiguous")
    assert door.notes() == "" and door.notes(BOB) == ""

    door.end(bob)
    assert door.write(ann).status_code == 200
    assert door.notes() == NOTE


def test_a_human_session_is_sent_away(door: Door, api_client: TrinityApiClient):
    """The admin's own JWT, over the wire: this is the agent's door."""
    ann = door.run()
    resp = api_client.post(f"/api/agents/{door.name}/user-memory",
                           json={"execution_id": ann, "memory_text": NOTE})
    _refused(resp, 403, "agent_only")
    assert door.notes() == ""


def test_a_user_scoped_key_is_sent_away(door: Door, api_client: TrinityApiClient,
                                        test_mcp_key_name: str, resource_tracker):
    key_data = assert_json_response(api_client.post("/api/mcp/keys", json={"name": test_mcp_key_name}))
    resource_tracker.track_mcp_key(key_data["id"])
    _refused(door.write(door.run(), token=key_data["api_key"]), 403, "agent_only")
    assert door.notes() == ""
