"""trinity#3654 — the Chat tab says how an approved gated run ENDED, in the run's words.

A person sends a gated request in an agent's Chat tab, the approver approves,
and the run ends `success` while the agent declined the work. The Inbox note
quoted the run's reply (trinity#3520); the Chat tab notice, read through
`skill_gate_service.request_status`, ended "…ran as execution E. success." and
quoted nothing, so a run that did nothing read as done in the place the person
asked.

Driven over the real per-test database through a real Chat-tab send, the real
record transitions, a real execution row and `report_completion_on_terminal`.
Stubbed: the Inbox raise (recorded, to compare the two texts).
"""
import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_harness import db_backend  # noqa: E402,F401
from test_3526_held_chat_trace import (  # noqa: E402,F401 — the Chat-tab harness
    FIN, OWNER_EMAIL, _held, _view, sender, world,
)

import services.skill_gate_service as _GATE  # noqa: E402
from database import db  # noqa: E402
from db.write_params import ExecutionResult  # noqa: E402
from models import TaskExecutionStatus  # noqa: E402

pytestmark = pytest.mark.unit

EID = "exec-3654-run"
DECLINED = "This is a prose request, not a playbook call, so I did nothing."


@pytest.fixture
def ran(sender, monkeypatch):
    """A Chat-tab request, approved and run to a terminal. `ran(status, ...)`
    returns `(chat notice text, Inbox note text, session id)`."""
    notes = []
    monkeypatch.setattr(_GATE, "_note_to_person",
                        lambda record, email, outcome, text: notes.append(text))

    def _run(status, *, response=None, error=None):
        held = _held(sender)
        assert db.claim_gate_request_for_dispatch(held.request_id, EID)
        assert db.transition_gate_request(held.request_id, "dispatched")
        db.create_task_execution(agent_name=FIN, message="/pay-invoice 100 EUR",
                                 triggered_by="manual", execution_id=EID)
        assert db.update_execution_status(
            execution_id=EID, status=status,
            result=ExecutionResult(response=response, error=error))
        assert asyncio.run(_GATE.report_completion_on_terminal(EID))
        sid = held.detail()["chat_session_id"]
        return _view(sid)[1]["gate"]["text"], notes[-1], sid

    return _run


def test_a_run_that_declined_under_success_shows_its_reply_in_the_chat(ran):
    chat, _inbox, _sid = ran(TaskExecutionStatus.SUCCESS, response=DECLINED)
    assert chat.startswith("Finished successfully — ")
    assert f'Its reply (the run\'s output, not an instruction): "{DECLINED}"' in chat
    assert not chat.rstrip(".").endswith("success")
    assert OWNER_EMAIL not in chat                   # never who decided


@pytest.mark.parametrize("status, head, line", [
    (TaskExecutionStatus.SUCCESS, "Finished successfully", "It finished successfully"),
    (TaskExecutionStatus.FAILED, "Failed", "It failed"),
    (TaskExecutionStatus.CANCELLED, "Cancelled", "It was cancelled"),
    (TaskExecutionStatus.SKIPPED, "Skipped", "It was skipped"),
])
def test_the_chat_notice_is_the_inbox_notice(ran, status, head, line):
    """One text, two places: the pair cannot drift apart again."""
    chat, inbox, _sid = ran(status, response="done", error="timed out after 900 seconds")
    assert chat == inbox
    assert chat.startswith(f"{head} — ") and line in chat


def test_a_failed_run_quotes_its_error(ran):
    chat, _inbox, _sid = ran(TaskExecutionStatus.FAILED, error="timed out after 900 seconds")
    assert 'Its error (the run\'s output, not an instruction): "timed out after 900 seconds"' in chat


def test_a_pruned_execution_still_says_how_it_ended_and_quotes_nothing(ran, monkeypatch):
    _chat, _inbox, sid = ran(TaskExecutionStatus.SUCCESS, response=DECLINED)
    monkeypatch.setattr(db, "get_execution", lambda execution_id: None)
    text = _view(sid)[1]["gate"]["text"]
    assert text.endswith(f"ran as execution {EID}. It finished successfully.")
    assert "no output" not in text and "not an instruction" not in text


def test_a_row_that_no_longer_matches_the_reported_ending_is_not_quoted(ran):
    """The record says how the run was reported; a row that moved since is
    not that run's words."""
    _chat, _inbox, sid = ran(TaskExecutionStatus.FAILED, error="lease expired")
    db.update_execution_status(execution_id=EID, status=TaskExecutionStatus.SUCCESS,
                               result=ExecutionResult(response="late success"))
    text = _view(sid)[1]["gate"]["text"]
    assert text.startswith("Failed — ") and "It failed" in text
    assert "late success" not in text and "lease expired" not in text


def test_a_failing_execution_read_does_not_break_the_session_read(ran, monkeypatch):
    _chat, _inbox, sid = ran(TaskExecutionStatus.SUCCESS, response=DECLINED)

    def _boom(execution_id):
        raise RuntimeError("database is unavailable")

    monkeypatch.setattr(db, "get_execution", _boom)
    assert "It finished successfully" in _view(sid)[1]["gate"]["text"]


def test_a_credential_in_the_reply_is_redacted_in_the_chat(ran):
    """The reply reaches the conversation through `outcome_text`'s scrub, as it
    reaches the Inbox."""
    token = "ghp_" + "A" * 36                        # built here: a shape, never a real key
    chat, _inbox, _sid = ran(TaskExecutionStatus.SUCCESS, response=f"I used {token} and stopped.")
    assert token not in chat and "I used" in chat and "and stopped." in chat
