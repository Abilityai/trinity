"""
#3461 — the public execution-status route does not hand a visitor the operator's error.

`GET /api/public/executions/{token}/{execution_id}/status` is unauthenticated:
the link token is the only credential, and the page polling it is read by
anonymous visitors. For a `failed` row it returned `execution.error` verbatim —
the agent server's own diagnosis ("Execution failed with no output (exit code
1): …") followed by remediation addressed to the operator. The synchronous
public path never did this: `run_public_chat` logs the detail and answers a
fixed line. The async status read now answers that same line.

Unchanged, and pinned here so the fix stays narrow: a `cancelled` row's reason
(#679) and a gate's notice on a `skipped` row (trinity#3274) are written FOR the
visitor and still pass through.

Driven: `routers.public.public_execution_status`, with the link check, the rate
limit and the row read stubbed.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

RAW = (
    "Execution failed with no output (exit code 1): the model credential is "
    "exhausted. Check the provider console, then update the key under Settings."
)


def _status(monkeypatch, *, status, error, triggered_by="public", record=None):
    import routers.public as pub

    row = SimpleNamespace(id="e1", agent_name="finance", status=status,
                          triggered_by=triggered_by, response=None, error=error)
    monkeypatch.setattr(pub, "_get_client_ip", lambda request: "1.2.3.4")
    monkeypatch.setattr(pub, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(pub, "_validate_public_link", lambda token: {"agent_name": "finance"})
    monkeypatch.setattr(pub.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(pub.db, "get_gate_requests_by_origin_executions",
                        lambda ids: {"e1": record} if record else {})
    return asyncio.run(pub.public_execution_status("tok", "e1", SimpleNamespace()))


def test_a_failed_row_answers_without_the_operators_error_text(monkeypatch):
    out = _status(monkeypatch, status="failed", error=RAW)

    assert out["status"] == "failed"
    assert "exit code" not in out["error"]
    assert "Settings" not in out["error"]
    assert out["error"] == "Failed to process your request. Please try again."


def test_a_failed_row_with_no_error_text_still_answers_the_fixed_line(monkeypatch):
    """The poller always gets a reason for a terminal failure, never a null."""
    out = _status(monkeypatch, status="failed", error=None)

    assert out["error"] == "Failed to process your request. Please try again."


def test_a_cancelled_rows_reason_still_passes_through(monkeypatch):
    out = _status(monkeypatch, status="cancelled", error="Execution cancelled by user")

    assert out["error"] == "Execution cancelled by user"


def test_a_gates_notice_still_passes_through(monkeypatch):
    notice = "Not run: the skill pay-invoice on finance needs approval before it can run."
    out = _status(monkeypatch, status="skipped", error=notice, record={"state": "pending"})

    assert (out["error"], out["gate"]) == (notice, "held")
