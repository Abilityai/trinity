"""Who sees which grader on an evaluation (trinity-enterprise#781).

The grader-identity projection, one helper in
`services/evaluation_read_service.py`: an allowlist. `platform:tier0` (and its
pair name) passes through; a Tier-1 row reads as `platform:tier1` (no model
name); any other `kind:rest` reads as `kind`; a bare name reads `unknown`. The
route's own `admin:<email>` writer is driven to prove it (the ent#781 failing
test, adopted verbatim below). Operators still read the full evaluator.

Everything below the docstring is copied byte-for-byte from the evaluation
integration branch's file of the same name (its ent#781 slice, ending before
its Tier-1 read-guard block), so when that branch reaches dev its version is a
strict superset of this one.
"""
import os
import sys

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

AGENT = "scribe-retro"
GRADER = "alice-retro@example.com"


# --- ent#781: adopted verbatim ---------------------------------------------------

def _written_by_the_real_route(monkeypatch):
    """Drive POST /api/agents/{name}/evaluations as an email-login admin (username ==
    email) and capture the row the route stores."""
    import asyncio
    from models import EvaluationCreate, User
    from routers import evaluations

    stored = {}

    def fake_create(agent_name, **kw):
        stored.update({"id": "eval_retro", "agent_name": agent_name, "created_at": "2026-10-03T00:00:00Z", **kw})
        return dict(stored)

    monkeypatch.setattr(evaluations.db, "create_agent_evaluation", fake_create)
    monkeypatch.setattr(evaluations.db, "can_user_share_agent", lambda u, a: True)
    admin = User(id=2, username=GRADER, email=GRADER, role="admin")
    asyncio.run(evaluations.create_evaluation(AGENT, EvaluationCreate(quality=0.0), admin))
    assert GRADER in stored["evaluator"]            # precondition: the row names the person
    return stored


@pytest.mark.parametrize("reader", [
    {"agent_name": AGENT},                          # the graded agent's own key
    {"mcp_scope": "system"},                        # the orchestrator
])
def test_a_machine_never_learns_which_admin_graded_it(monkeypatch, reader):
    from models import User
    from routers import evaluations

    row = _written_by_the_real_route(monkeypatch)
    machine = User(id=1, username="owner", role="user", **reader)
    out = evaluations._to_response(row, machine, frozenset())
    assert GRADER not in out.model_dump_json(), out.evaluator


# --- ent#781: the rest of the AC -------------------------------------------------

def test_admin_grade_reads_as_the_kind_to_a_non_operator_human(monkeypatch):
    from models import User
    from routers import evaluations
    row = _written_by_the_real_route(monkeypatch)
    shared = User(id=3, username="client@example.com", role="creator")
    out = evaluations._to_response(row, shared, frozenset())
    assert out.evaluator == "admin"


@pytest.mark.parametrize("reader", [
    {"role": "admin"},                              # admin JWT
    {"role": "creator"},                            # owner JWT
    {"role": "creator", "mcp_scope": "user"},       # owner's own user key
])
def test_an_operator_still_reads_the_full_evaluator(monkeypatch, reader):
    from models import User
    from routers import evaluations
    row = _written_by_the_real_route(monkeypatch)
    op = User(id=4, username="op", **reader)
    out = evaluations._to_response(row, op, frozenset({AGENT}))
    assert out.evaluator == f"admin:{GRADER}"


@pytest.mark.parametrize("stored, shown", [
    ("platform:tier0", "platform:tier0"),
    ("platform:tier1:some-model-v2", "platform:tier1"),
    ("admin:alice@example.com", "admin"),
    ("workspace:bob@example.com", "workspace"),
    ("operator:carol@example.com", "operator"),
    ("reviewer:dave@example.com", "reviewer"),      # a fourth kind, anonymised on day one
    ("platform:tier9:x", "platform"),               # an unknown platform pass is not allowlisted
    ("dave@example.com", "unknown"),                # no kind at all
    ("", "unknown"),
    (None, "unknown"),
])
def test_redact_evaluator_is_an_allowlist(stored, shown):
    from services.evaluation_read_service import redact_evaluator
    assert redact_evaluator(stored) == shown
