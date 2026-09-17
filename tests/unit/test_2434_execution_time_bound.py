"""#2434 — the one duration on this surface a CALLER chooses, not the backend.

#2434 proper was a duration the *backend* fabricated (``now - started_at``)
overflowing PostgreSQL's ``int4`` ceiling — ``2**31 - 1`` ms = 24.855 days —
which aborted a whole sweep transaction and left the fleet's stale rows
``running`` forever. That half is fixed at the writers and proved in
``test_2434_duration_overflow.py``.

``ExecutionResultEnvelope.execution_time_ms`` is the same value class arriving
from *outside*: agent-supplied, on the #1083 fire-and-forget result callback
(``routers/agents.py::agent_execution_result``), and previously unbounded.

**This is a boundary check, not a live overflow path — deliberately.** Today the
value reaches only JSON: the activity ``details`` blob and the #1578 event
payload. ``schedule_executions.duration_ms`` is *recomputed* from ``started_at``,
and the ``execution_time_ms`` int4 columns (``chat_messages``,
``agent_session_messages``) are written from the backend's own in-request
measurement, never from this field. So the bound is not closing a reachable
overflow — it is what keeps the path from becoming one, so a future writer that
*does* persist the field inherits the guarantee instead of rediscovering the
int4 ceiling in production.

Enforcement is at the **contract**, which means at PARSE: FastAPI rejects the
body before ``agent_execution_result`` runs. That is why every case here asserts
on ``ValidationError`` / 422 and never on a handler return — a hostile value
never reaches the endpoint at all, which is strictly stronger than any check
inside it.

Modules under test: ``src/backend/models.py`` (``ExecutionResultEnvelope``),
``src/backend/error_handlers.py`` (the app-wide 422 shape),
``docker/base-image/agent_server/services/result_callback.py`` (the agent's
reaction to that 422 — the cross-surface half, Invariant #5).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Pinned at IMPORT time, not resolved lazily inside a fixture — the precedent is
# test_ent109_validation_error_no_input.py, and the reason is the sys.modules
# leak that makes a sibling module's collection-time Mock this file's problem
# (`error_handlers` imports `utils.credential_sanitizer`, and `utils` is exactly
# the name `tests/utils` shadows at full-suite scale).
from fastapi import FastAPI  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from error_handlers import validation_error_without_input  # noqa: E402
from models import ExecutionResultEnvelope  # noqa: E402

pytestmark = pytest.mark.unit

# 24.855 days in ms. The ceiling every `duration_ms` / `execution_time_ms`
# column carries, because they are all SQLAlchemy `Integer` → PostgreSQL int4.
_PG_INT4_MAX = 2**31 - 1

_RESULT_CALLBACK = (
    _PROJECT_ROOT
    / "docker"
    / "base-image"
    / "agent_server"
    / "services"
    / "result_callback.py"
)


def _envelope_app():
    """A minimal app carrying ONLY the body model and ``main.py``'s real handler.

    Deliberately not ``main.app``: importing it drags in the lifespan, every
    router and a live DB/Redis, none of which this behaviour depends on. The
    handler is registered exactly the way ``main.py:1189`` registers it, so what
    is asserted is the real handler and not a re-implementation of it.

    ``reached`` records whether the route body ever ran. The point of the
    rejection cases is that it does **not** — pinning "the contract stops it"
    rather than the weaker "something stopped it".
    """
    reached: list = []
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_input)

    @app.post("/result")
    def _result(body: ExecutionResultEnvelope):
        reached.append(body.execution_time_ms)
        return {"ok": True}

    return TestClient(app), reached


class TestTheContractRejectsAnUnrepresentableDuration:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(_PG_INT4_MAX + 1, id="one-over-the-ceiling"),
            pytest.param(3_000_000_000, id="the-hostile-value-in-the-plan"),
            pytest.param(2_851_200_000, id="the-33-days-that-wedged-the-sweeps"),
            pytest.param(-1, id="negative-one"),
            pytest.param(-2_851_200_000, id="a-large-negative"),
        ],
    )
    def test_out_of_range_is_rejected(self, value):
        """Both ends. The low end matters as much as the high one: #1832 was a
        NEGATIVE fabricated duration (a future ``started_at``), and ``ge=0`` is
        the same guarantee for the caller-supplied twin."""
        with pytest.raises(ValidationError):
            ExecutionResultEnvelope(status="success", execution_time_ms=value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="zero"),
            pytest.param(1234, id="a-normal-turn"),
            pytest.param(_PG_INT4_MAX, id="exactly-the-ceiling"),
            pytest.param(None, id="absent"),
        ],
    )
    def test_representable_values_still_pass(self, value):
        """The bound must not be off-by-one and must not break a legitimate
        callback. ``None`` above all: the field is optional and an agent that
        never measured a turn omits it — rejecting that would fail every
        callback rather than the malformed ones."""
        envelope = ExecutionResultEnvelope(status="success", execution_time_ms=value)
        assert envelope.execution_time_ms == value


class TestTheRejectionIsACleanWireLevel422:
    def test_an_oversized_value_is_422_and_never_reaches_the_handler(self):
        client, reached = _envelope_app()

        response = client.post(
            "/result", json={"status": "success", "execution_time_ms": 3_000_000_000}
        )

        assert response.status_code == 422, response.text
        assert reached == [], "the handler ran on a body the contract rejects"

    def test_a_representable_value_does_reach_the_handler(self):
        """The vacuity guard for the case above. Without it a bound that
        rejected *everything* — or a route wired to the wrong model — would
        satisfy every rejection assertion in this file."""
        client, reached = _envelope_app()

        response = client.post(
            "/result", json={"status": "success", "execution_time_ms": _PG_INT4_MAX}
        )

        assert response.status_code == 200, response.text
        assert reached == [_PG_INT4_MAX]

    def test_the_422_body_does_not_echo_the_rejected_value(self):
        """ent#109's handler is app-wide, so it covers this field for free —
        pinned here because the 422 body is what an agent's retry path logs, and
        because a future numeric validator on this model could carry a value
        that is not merely a number."""
        client, _ = _envelope_app()

        response = client.post(
            "/result", json={"status": "success", "execution_time_ms": 3_000_000_000}
        )

        assert response.status_code == 422
        for entry in response.json()["detail"]:
            assert "input" not in entry and "ctx" not in entry
        assert "3000000000" not in response.text


class TestTheAgentTreatsThat422AsPermanent:
    """The cross-surface half (Invariant #5), and the reason the 422 is safe.

    ``ExecutionResultEnvelope``'s own docstring records that the **size** caps
    are enforced *inside* the handler as a 413 rather than as a Pydantic 422,
    "the agent's retry logic special-cases status codes". A field constraint
    cannot do that — it fires at parse — so the bound necessarily introduces a
    422 on a surface whose docstring reads as though 422 were being avoided.

    That is safe only because 422 is already in the agent's permanent set: the
    agent gives up instead of re-POSTing the same malformed body until the lease
    deadline. Asserted rather than assumed, because dropping 422 from that set
    would silently convert this hardening into a retry storm — and the two files
    live in different images, so nothing else would catch it.

    Read by AST, never imported: ``agent_server`` is a separate image's package
    and the backend test island cannot import it (Invariant #5).
    """

    def test_422_is_a_permanent_status_for_the_result_callback(self):
        tree = ast.parse(_RESULT_CALLBACK.read_text())
        permanent = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_PERMANENT_STATUSES"
                for t in node.targets
            ):
                # `frozenset({...})` — evaluate the literal argument only.
                permanent = ast.literal_eval(node.value.args[0])

        assert permanent is not None, (
            "_PERMANENT_STATUSES not found in "
            f"{_RESULT_CALLBACK.relative_to(_PROJECT_ROOT)} — if it was renamed, "
            "re-point this guard; do not delete it."
        )
        assert 422 in permanent, (
            "The #2434 bound on execution_time_ms makes a malformed callback a "
            "422. With 422 out of _PERMANENT_STATUSES the agent would retry that "
            "same body until its lease deadline instead of giving up."
        )
