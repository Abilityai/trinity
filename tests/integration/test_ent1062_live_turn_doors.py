"""The seat-decision and voice-reply doors over the network, with a real agent
key (trinity-enterprise#1062).

Runs on verify-local's sibling stack (stage 6 is `pytest tests/integration`
against `TRINITY_API_URL`); run it against a stack you can afford to mutate,
never a working instance. tests/unit/test_ent1062_live_turn_doors.py carries the
full matrix on every PR; this proves the deployed image holds the rule at the
network edge: real TCP, the real auth stack, a real `trinity_mcp_…` agent key.

Seeded the way tests/integration/test_4121_user_memory_write_door.py seeds: no
API returns an agent key, so the key and the runs come from the functions agent
creation and the task path call, inside the backend this client talks to. Needs
the Docker socket of the host running that stack; without it this FAILS naming
the requirement. Everything is under a `pytest-ephemeral-` name with no
container, ended and removed at teardown.
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
RECORD = {
    "outcome": "approved",
    "decided": "Renew the Acme contract",
    "alternatives": ["let it lapse"],
    "criterion": "renewal cost below the switching cost",
    "reversal": "Acme raises the price",
    "review_by": "2027-06-30",
}
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
            f"trinity-enterprise#1062's live test needs the Docker socket of the host running the stack, to seed "
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
        self.name = f"pytest-ephemeral-1062-{uuid.uuid4().hex[:8]}"
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

    def run(self, *, email: str = ANN, triggered_by: str = "public", chat: str | None = None) -> str:
        """A live run, marked dispatched as a real turn is (or the no-session
        sweep fails it). With ``chat``, a one-to-one Telegram turn."""
        execution_id = self._backend("""
            telegram = args["chat"] is not None
            row = db.create_task_execution(
                args["name"], "hello", "telegram" if telegram else args["trigger"],
                fields=TaskExecutionFields(
                    source_user_email=args["email"],
                    source_channel="telegram" if telegram else None,
                    source_channel_chat_id=args["chat"],
                    source_channel_client=args["email"] if telegram else None))
            db.mark_execution_dispatched(row.id)
            done(row.id)
        """, name=self.name, trigger=triggered_by, email=email, chat=chat)
        self.runs.append(execution_id)
        return execution_id

    def end(self, *execution_ids: str) -> None:
        self._backend("""
            for execution_id in args["ids"]:
                db.update_execution_status(execution_id, "success", ExecutionResult(response="done"))
            done(True)
        """, ids=list(execution_ids))

    def decisions(self, email: str = ANN) -> int:
        return self._backend("""
            done(len(db.list_seat_decisions(args["name"], args["email"], limit=50)))
        """, name=self.name, email=email)

    def record(self, execution_id: str, *, token: str | None = None):
        return self.client.post(f"/api/agents/{self.name}/decisions", json={**RECORD, "execution_id": execution_id},
                                auth=False, headers={"Authorization": f"Bearer {token or self.key}",
                                                     "X-Trinity-Execution-Id": execution_id})

    def read(self, execution_id: str):
        return self.client.get(f"/api/agents/{self.name}/decisions", params={"execution_id": execution_id},
                               auth=False, headers={"Authorization": f"Bearer {self.key}"})

    def speak(self, execution_id: str):
        return self.client.post(f"/api/agents/{self.name}/voice-reply",
                                json={"execution_id": execution_id, "text": "hi"},
                                auth=False, headers={"Authorization": f"Bearer {self.key}"})

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


def test_the_live_turn_records_and_reads_its_seat(door: Door):
    ann = door.run()
    assert door.record(ann).status_code == 200, door.record(ann).text
    body = door.read(ann).json()
    assert [d["decided"] for d in body["decisions"]] == ["Renew the Acme contract"]


def test_a_finished_run_records_and_reads_nothing(door: Door):
    theirs = door.run()
    assert door.record(theirs).status_code == 200
    door.end(theirs)
    _refused(door.record(theirs), 422, "decision_turn_not_live")
    resp = door.read(theirs)
    _refused(resp, 422, "decision_turn_not_live")
    assert "Acme" not in resp.text
    assert door.decisions() == 1


def test_two_people_live_refuse_each_other(door: Door):
    ann, bob = door.run(), door.run(email=BOB)
    _refused(door.record(ann), 422, "decision_turn_ambiguous")
    _refused(door.read(bob), 422, "decision_turn_ambiguous")
    assert door.decisions() == 0
    door.end(bob)
    assert door.record(ann).status_code == 200


def test_a_human_session_is_sent_away(door: Door, api_client: TrinityApiClient):
    ann = door.run()
    _refused(api_client.post(f"/api/agents/{door.name}/decisions", json={**RECORD, "execution_id": ann}),
             403, "agent_only")
    assert door.decisions() == 0


def test_a_user_scoped_key_is_sent_away(door: Door, api_client: TrinityApiClient,
                                        test_mcp_key_name: str, resource_tracker):
    key_data = assert_json_response(api_client.post("/api/mcp/keys", json={"name": test_mcp_key_name}))
    resource_tracker.track_mcp_key(key_data["id"])
    _refused(door.record(door.run(), token=key_data["api_key"]), 403, "agent_only")


def test_voice_never_reaches_a_finished_run(door: Door):
    theirs = door.run(email=BOB, chat="chat-bob")
    door.end(theirs)
    body = door.speak(theirs).json()
    assert body["delivered"] is False and body["reason"] == "turn_not_live", body


def test_voice_from_a_live_turn_passes_both_gates(door: Door):
    """No real channel here, so the note is not delivered — for some later reason
    (no voice configured), never the two this issue adds."""
    body = door.speak(door.run(chat="chat-ann")).json()
    assert body["reason"] not in ("turn_not_live", "turn_ambiguous"), body


def test_voice_with_another_live_conversation_is_ambiguous(door: Door):
    ann = door.run(chat="chat-ann")
    door.run(email=BOB, chat="chat-bob")
    body = door.speak(ann).json()
    assert body["delivered"] is False and body["reason"] == "turn_ambiguous", body
