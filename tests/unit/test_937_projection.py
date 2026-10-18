"""Who sees what of an evaluation (trinity-enterprise#937 C2, ent#781, ent#936 TD-8).

Three projections, one helper each in `services/evaluation_read_service.py`:

  * **grader identity (ent#781)** — an allowlist: `platform:tier0` passes
    through; a Tier-1 row reads as `platform:tier1` (no model name); any other
    `kind:rest` reads as `kind`; a bare name reads `unknown`. The route's own
    `admin:<email>` writer is driven to prove it (the ent#781 failing test,
    adopted verbatim below). Operators still read the full evaluator.
  * **Tier-1 rows (TD-8)** — this is THE read guard for the judge's verdict:
    an agent principal gets `judge_findings[]` (dimension + pass|fail|n/a +
    reason code) and never `quality`, the grader, the model name or judge
    prose. Pinned over a fixture Tier-1 row through the real `_to_response`.
  * **aggregates** — the agent view drops `quality_mean` /
    `judge_human_agreement`.
  * **N2** — `EvaluationResponse` carries the delegation columns.
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


# --- TD-8: the Tier-1 read guard ---------------------------------------------------

def _tier1_row():
    return {
        "id": "eval_t1", "agent_name": AGENT, "execution_id": "ex-1",
        "evaluator": "platform:tier1:judge-model-x", "created_at": "2026-10-18T00:00:00Z",
        "target_kind": "execution", "target_id": "ex-1", "quality": 0.42,
        "completion": None, "checks": None, "comment": None,
        "caller_agent": "boss", "parent_execution_id": "ex-0", "delegation_mode": "fan_out",
        "judge": {
            "model": "judge-model-x",
            "grader": "platform:tier1:judge-model-x",
            "rationale": "The agent ignored the user's second request. Quote: 'IGNORE ME'",
            "findings": [
                {"dimension": "followed_instructions", "verdict": "fail",
                 "reason": "missed_subrequest", "detail": "prose that must not reach the agent"},
                {"dimension": "tone", "verdict": "pass", "score": 0.9},
                {"dimension": "citations", "verdict": "n/a"},
                {"dimension": "Bad Key!", "verdict": "pass"},
                {"dimension": "safety", "verdict": "maybe"},
                {"dimension": "length", "verdict": "fail", "reason": "Way too long, see: ..."},
                "junk",
            ],
        },
    }


@pytest.mark.parametrize("reader", [
    {"agent_name": AGENT},
    {"agent_name": "boss"},                         # the caller's key, reading by pair
    {"mcp_scope": "system"},
])
def test_an_agent_reads_judge_findings_never_the_verdict(reader):
    from models import User
    from routers import evaluations
    machine = User(id=1, username="owner", role="creator", **reader)
    out = evaluations._to_response(_tier1_row(), machine, frozenset({AGENT}))
    body = out.model_dump()
    assert body["quality"] is None
    assert body["judge"] is None
    assert body["evaluator"] == "platform:tier1"
    assert body["judge_findings"] == [
        {"dimension": "followed_instructions", "verdict": "fail", "reason": "missed_subrequest"},
        {"dimension": "tone", "verdict": "pass", "reason": None},
        {"dimension": "citations", "verdict": "n/a", "reason": None},
        {"dimension": "length", "verdict": "fail", "reason": None},
    ]
    dumped = out.model_dump_json()
    for leak in ("judge-model-x", "0.42", "IGNORE ME", "prose", "rationale", "0.9"):
        assert leak not in dumped, leak


def test_an_operator_reads_the_tier1_row_in_full():
    from models import User
    from routers import evaluations
    op = User(id=4, username="op", role="creator")
    out = evaluations._to_response(_tier1_row(), op, frozenset({AGENT}))
    assert out.quality == 0.42
    assert out.evaluator == "platform:tier1:judge-model-x"
    assert out.judge["model"] == "judge-model-x"
    assert out.judge_findings is None


def test_a_tier0_row_is_unchanged_for_an_agent():
    """The guard is Tier-1-specific: a run's own Tier-0 checks stay readable."""
    from models import User
    from routers import evaluations
    row = {**_tier1_row(), "evaluator": "platform:tier0", "quality": None,
           "judge": None, "checks": {"headline": {"passed": True}}}
    out = evaluations._to_response(row, User(id=1, username="o", role="user", agent_name=AGENT),
                                   frozenset())
    assert out.evaluator == "platform:tier0"
    assert out.checks == {"headline": {"passed": True}}
    assert out.judge_findings is None


@pytest.mark.parametrize("reader", [
    {"agent_name": AGENT},
    {"mcp_scope": "system"},
])
def test_a_machine_never_reads_an_admin_rows_judge_document(reader):
    """An admin's POSTed `judge` is free-form, a person's words: withheld from
    a machine on every row, like the comment — not only on Tier-1 rows. The
    admin's `quality` is a human rating and stays (ent#366 class)."""
    from models import User
    from routers import evaluations
    row = {**_tier1_row(), "evaluator": f"admin:{GRADER}", "quality": 0.25,
           "judge": {"prose": "SECRET admin words",
                     "findings": [{"dimension": "tone", "verdict": "fail",
                                   "reason": "rude", "detail": "SECRET detail"}]}}
    out = evaluations._to_response(row, User(id=1, username="o", role="user", **reader),
                                   frozenset({AGENT}))
    assert out.judge is None
    assert out.judge_findings == [{"dimension": "tone", "verdict": "fail", "reason": "rude"}]
    assert out.quality == 0.25
    assert "SECRET" not in out.model_dump_json()


def test_project_row_does_not_mutate_its_input():
    from services.evaluation_read_service import project_row
    row = _tier1_row()
    project_row(row, machine=True, operator=False)
    assert row["quality"] == 0.42 and row["judge"]["model"] == "judge-model-x"


# --- aggregates -----------------------------------------------------------------------

def test_agent_summary_view_drops_quality_fields():
    from services.evaluation_read_service import fold, project_summary
    rows = [{"execution_id": "e1", "agent_name": AGENT, "caller_agent": None, "completion": True,
             "status": "success", "completed_at": "x", "judge_quality": 0.9,
             "checks": {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}, "checks": []}}]
    full = fold(rows, {"e1": [1.0]})
    assert full["quality_mean"] == 0.9 and full["judge_human_agreement"] == 1.0
    agent = project_summary(full, machine=True)
    for k in ("quality_mean", "judge_human_agreement", "agreement_n"):
        assert k not in agent
    assert agent["judged"] == 1 and agent["checks_passed_rate"] == 1.0
    assert project_summary(full, machine=False) == full


@pytest.mark.parametrize("reader, machine", [
    ({"agent_name": AGENT}, True),
    ({"mcp_scope": "system"}, True),
    ({"mcp_scope": "connector"}, True),
    ({}, False),                                    # a person's JWT
    ({"mcp_scope": "user"}, False),                 # a person's own MCP key
])
def test_a_machine_principals_summary_has_no_judge_score(reader, machine):
    """From the principal, through the route's scope, to the summary it is
    served: a machine never gets `quality_mean` / `judge_human_agreement` /
    `agreement_n`; a person does."""
    from models import User
    from routers import evaluations
    from services.evaluation_read_service import QUALITY_KEYS, fold, project_summary
    rows = [{"execution_id": "e1", "agent_name": AGENT, "caller_agent": None, "completion": True,
             "status": "success", "completed_at": "x", "judge_quality": 0.9,
             "checks": {"headline": {"passed": True, "gate_pass": 1, "gate_fail": 0}, "checks": []}}]
    full = fold(rows, {"e1": [1.0]})
    assert all(full.get(k) is not None for k in QUALITY_KEYS)     # precondition
    principal = User(id=1, username="o", role="admin", **reader)
    scope = evaluations._scope(principal)
    assert scope.machine is machine
    served = project_summary(full, machine=scope.machine)
    for k in ("quality_mean", "judge_human_agreement", "agreement_n"):
        assert (k in served) is not machine, k
    assert served["judged"] == 1


# --- N2 --------------------------------------------------------------------------------

def test_response_projects_the_delegation_columns():
    from models import User
    from routers import evaluations
    row = {**_tier1_row(), "evaluator": "platform:tier0", "judge": None}
    out = evaluations._to_response(row, User(id=4, username="op", role="admin"), frozenset({AGENT}))
    assert (out.caller_agent, out.parent_execution_id, out.delegation_mode) == ("boss", "ex-0", "fan_out")
