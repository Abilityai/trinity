"""ent#926 — a public visitor refused by the agent's runtime is told so, not "try again".

Found live 2026-10-19: a Gemini agent whose owner turned the visitor switch off
refuses a public-link turn before the CLI starts (`turn_scope`, a typed 403
`turn_refused`). The public chat answered the generic 502 "Failed to process
your request. Please try again." on both paths, telling the visitor to retry
something that can never succeed.

Both paths now answer a fixed, visitor-facing line that names no setting:
  * sync  `run_public_chat`: the result carries `error_code` TURN_REFUSED -> 403;
  * async the status poll the real page uses (#3461): a failed row whose error is
    the agent's refusal (a pull terminal's `[turn_refused]` prefix, or a push
    row's text) -> that line instead of the generic one.

The push row stores only the agent's sentence, so the status read recognises
it; the parity test below fails if the agent server's wording drifts.
"""
import asyncio
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _refusals():
    from agent_server.services import turn_scope
    out = []
    for runtime in ("Gemini CLI", "Codex"):
        out.append(turn_scope.REFUSED.format(runtime=runtime))
        out.append(turn_scope.REFUSED_VISITOR.format(runtime=runtime))
    return out


@pytest.mark.parametrize("text", _refusals())
def test_every_runtime_refusal_sentence_is_recognised(text):
    """Parity with docker/base-image/agent_server/services/turn_scope.py."""
    from services import public_chat_service as pcs
    assert pcs.is_turn_refused(None, text)
    assert pcs.is_turn_refused(None, f"[turn_refused] {text}")


def test_a_typed_code_is_enough_and_other_failures_are_not_refusals():
    from services import public_chat_service as pcs
    from services.execution_envelope import TaskExecutionErrorCode as C
    assert pcs.is_turn_refused(C.TURN_REFUSED, "anything")
    assert not pcs.is_turn_refused(C.AUTH, "usage limit")
    assert not pcs.is_turn_refused(None, "Execution failed with no output (exit code 1)")
    assert not pcs.is_turn_refused(None, None)


def _status(monkeypatch, error):
    import routers.public as pub
    row = SimpleNamespace(id="e1", agent_name="g", status="failed", triggered_by="public", response=None, error=error)
    monkeypatch.setattr(pub, "_get_client_ip", lambda request: "1.2.3.4")
    monkeypatch.setattr(pub, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(pub, "_validate_public_link", lambda token: {"agent_name": "g"})
    monkeypatch.setattr(pub.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(pub.db, "get_gate_requests_by_origin_executions", lambda ids: {})
    return asyncio.run(pub.public_execution_status("tok", "e1", SimpleNamespace()))


@pytest.mark.parametrize("prefix", ["", "[turn_refused] "])
def test_the_status_poll_tells_the_visitor_plainly(monkeypatch, prefix):
    from services import public_chat_service as pcs
    out = _status(monkeypatch, prefix + _refusals()[1])
    assert out["error"] == pcs.PUBLIC_TURN_REFUSED_MESSAGE
    assert "try again" not in out["error"].lower()
    assert "Guardrails" not in out["error"]           # no internal setting named to a visitor


def test_any_other_failure_keeps_the_fixed_line(monkeypatch):
    out = _status(monkeypatch, "Execution failed with no output (exit code 1)")
    assert out["error"] == "Failed to process your request. Please try again."


def test_the_sync_path_answers_403_with_the_same_line():
    from services import public_chat_service as pcs
    from services.execution_envelope import TaskExecutionErrorCode as C
    result = SimpleNamespace(status="failed", error=_refusals()[1], error_code=C.TURN_REFUSED)
    with pytest.raises(pcs.PublicChatError) as exc:
        pcs.raise_for_failed_public_result("g", result)
    assert exc.value.status_code == 403
    assert exc.value.detail == pcs.PUBLIC_TURN_REFUSED_MESSAGE
