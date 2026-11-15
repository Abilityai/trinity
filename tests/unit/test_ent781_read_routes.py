"""ent#781 on dev: who reads which grader, through the router's own projection
and through all three evaluation GET routes.

Supplements `test_937_projection.py` (the adopted issue regression, kept
byte-identical with the evaluation integration branch) with the cases this
backport is answerable for on its own:

  * the evaluator output contract, row by row, through the router helper,
    including the stored row being left untouched;
  * a human OPERATOR (owner or admin, by JWT or their own `user`-scoped key)
    keeps the full grader, while a machine that carries the same operator
    membership does not;
  * a shared non-operator and every machine scope read only the kind, and a
    failed operator lookup redacts rather than discloses;
  * the three GET routes, end to end over HTTP, on the real router.

Identities are placeholders; storage is never touched (every DB method the
router resolves is patched on the object the router bound at import).
"""
import copy
import os
import sys

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

AGENT = "ent781-agent"
GRADER = "grader@example.com"
OWNER = "owner@example.com"
ADMIN = "admin-reader@example.com"
SHARED = "client@example.com"
FULL = f"admin:{GRADER}"

# Neutral contents: the point is that these survive unchanged, not what they say.
_NEUTRAL = {
    "quality": 0.25,
    "completion": 1,
    "target_kind": "message",
    "target_id": "msg_1",
    "checks": {"format_ok": True},
    "judge": {"dimension": "accuracy"},
}


def _row(evaluator=FULL, **extra):
    return {"id": "eval_ent781", "agent_name": AGENT, "evaluator": evaluator,
            "created_at": "2026-10-21T00:00:00Z", "comment": None, **_NEUTRAL, **extra}


def _ownership(monkeypatch, evaluations):
    """The ownership facts, stated: OWNER owns the agent and ADMIN is an admin,
    so `can_user_share_agent` (owner-or-admin) is True for exactly those two.
    Being shared the agent is access, never operatorship."""
    monkeypatch.setattr(evaluations.db, "can_user_share_agent",
                        lambda username, agent: agent == AGENT and username in (OWNER, ADMIN))


# --- the output contract, through the router helper ----------------------------

CONTRACT = [
    ("admin:grader@example.com", "admin"),
    ("workspace:grader@example.com", "workspace"),
    ("operator:grader@example.com", "operator"),
    ("future:grader@example.com:detail", "future"),
    ("future:grader-name", "future"),
    ("platform:tier0", "platform:tier0"),
    ("platform:tier0:pair", "platform:tier0:pair"),
    ("platform:tier0:other", "platform"),
    ("platform:tier1:model-name", "platform:tier1"),
    ("platform:tier1", "platform"),
    ("tier0", "unknown"),
    ("grader@example.com", "unknown"),
    ("", "unknown"),
    (":detail", "unknown"),
    (42, "unknown"),
    (None, "unknown"),
]

_CONTRACT_READERS = {
    "agent-key": dict(username=OWNER, role="creator", agent_name=AGENT, mcp_scope="agent"),
    "shared-human": dict(username=SHARED, role="user"),
}


@pytest.mark.parametrize("reader", sorted(_CONTRACT_READERS))
@pytest.mark.parametrize("stored, shown", CONTRACT, ids=[repr(c[0]) for c in CONTRACT])
def test_evaluator_output_contract(stored, shown, reader):
    from models import User
    from routers import evaluations
    row = _row(stored)
    before = copy.deepcopy(row)
    out = evaluations._redact_for_agent_principal(row, User(id=1, **_CONTRACT_READERS[reader]),
                                                  frozenset())
    assert out["evaluator"] == shown
    assert row == before                             # the stored row is never rewritten
    for key, value in _NEUTRAL.items():
        assert out[key] == value, key


# A kind is only shown when it is an identity-free token (lowercase letters,
# digits, `_`, `-`). Anything else before the colon could itself be the person,
# so it reads `unknown` (ent#781 security review: `grader@example.com:admin`
# reached machine readers as `grader@example.com`). The writers on dev all fix
# their prefix, so this holds the rule at the read boundary for ANY stored value.
MALFORMED_KINDS = [
    ("grader@example.com:admin", "unknown"),         # an address before the colon
    ("grader@example:detail", "unknown"),            # `@` in the kind
    ("Admin:grader@example.com", "unknown"),         # uppercase kind
    ("Grader Name:workspace", "unknown"),            # a display name as the kind
    ("admin x:grader@example.com", "unknown"),       # whitespace inside the kind
    (" admin:grader@example.com", "unknown"),        # leading whitespace
    ("admin\t:grader@example.com", "unknown"),       # trailing tab
    ("grader.name:detail", "unknown"),               # a dotted handle
    # controls: well-formed kinds keep reading as the kind
    ("admin:grader@example.com", "admin"),
    ("future-kind_2:grader@example.com", "future-kind_2"),
]


@pytest.mark.parametrize("reader", sorted(_CONTRACT_READERS))
@pytest.mark.parametrize("stored, shown", MALFORMED_KINDS, ids=[repr(c[0]) for c in MALFORMED_KINDS])
def test_a_kind_that_is_not_an_identity_free_token_reads_unknown(stored, shown, reader):
    from models import User
    from routers import evaluations
    user = User(id=1, **_CONTRACT_READERS[reader])
    row = _row(stored)
    assert evaluations._redact_for_agent_principal(row, user, frozenset())["evaluator"] == shown
    out = evaluations._to_response(row, user, frozenset())
    assert out.evaluator == shown
    assert "grader" not in out.evaluator and "@" not in out.model_dump_json()


# --- operators keep the grader; a machine with the same membership does not ----

_OPERATORS = {
    "owner-jwt": dict(username=OWNER, role="creator"),
    "owner-user-key": dict(username=OWNER, role="creator", mcp_scope="user"),
    "admin-jwt": dict(username=ADMIN, role="admin"),
    "admin-user-key": dict(username=ADMIN, role="admin", mcp_scope="user"),
}


@pytest.mark.parametrize("who", sorted(_OPERATORS))
def test_human_operator_keeps_the_full_grader(monkeypatch, who):
    from models import User
    from routers import evaluations
    _ownership(monkeypatch, evaluations)
    human = User(id=1, **_OPERATORS[who])
    row = _row()
    operators = evaluations._operator_for([row], human)
    assert operators == {AGENT}
    assert evaluations._to_response(row, human, operators).evaluator == FULL

    # The same person's agent key, handed the same operator membership on
    # purpose: software never reads the grader, whoever owns it.
    machine = User(id=1, **{**_OPERATORS[who], "agent_name": AGENT, "mcp_scope": "agent"})
    out = evaluations._to_response(row, machine, frozenset({AGENT}))
    assert out.evaluator == "admin"
    assert GRADER not in out.model_dump_json()


_NON_OPERATORS = {
    # people the agent is shared with: access, not operatorship
    "shared-jwt": (dict(username=SHARED, role="user"), None),
    "shared-user-key": (dict(username=SHARED, role="creator", mcp_scope="user"), None),
    # machines, each deliberately GIVEN operator membership
    "agent-key": (dict(username=OWNER, role="creator", agent_name=AGENT, mcp_scope="agent"), {AGENT}),
    "system": (dict(username=ADMIN, role="admin", mcp_scope="system"), {AGENT}),
    "connector": (dict(username=OWNER, role="creator", mcp_scope="connector", connector_agent=AGENT), {AGENT}),
    "unknown-scope": (dict(username=OWNER, role="creator", mcp_scope="some-future-scope"), {AGENT}),
    "agent-identity-user-scope": (dict(username=OWNER, role="admin", agent_name=AGENT, mcp_scope="user"), {AGENT}),
}


@pytest.mark.parametrize("who", sorted(_NON_OPERATORS))
def test_non_operator_and_machine_readers_get_only_the_kind(monkeypatch, who):
    from models import User
    from routers import evaluations
    _ownership(monkeypatch, evaluations)
    fields, forced = _NON_OPERATORS[who]
    reader = User(id=1, **fields)
    row = _row()
    operators = frozenset(forced) if forced is not None else evaluations._operator_for([row], reader)
    if forced is None:
        assert operators == set()                    # shared is not an operator
    out = evaluations._to_response(row, reader, operators)
    assert out.evaluator == "admin"
    assert GRADER not in out.model_dump_json()


def test_operator_lookup_error_redacts_the_grader(monkeypatch):
    from models import User
    from routers import evaluations

    def boom(username, agent):
        raise RuntimeError("ownership lookup unavailable")

    monkeypatch.setattr(evaluations.db, "can_user_share_agent", boom)
    admin = User(id=1, username=ADMIN, role="admin")
    row = _row()
    operators = evaluations._operator_for([row], admin)
    assert operators == set()                        # fail-closed
    out = evaluations._to_response(row, admin, operators)
    assert out.evaluator == "admin"
    assert GRADER not in out.model_dump_json()


# --- the three GET routes, over HTTP on the real router --------------------------

PATHS = [f"/api/agents/{AGENT}/evaluations", "/api/evaluations", "/api/evaluations/eval_ent781"]


@pytest.fixture
def http_as(monkeypatch):
    """A fresh app per call over the REAL `routers.evaluations` router.

    Overrides are keyed on the dependency objects the router bound at import
    (`evaluations.get_current_user`): the unit harness can drop `dependencies`
    from `sys.modules` between tests, after which a fresh import is a different
    object and the override silently stops matching (every read then 401s)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import evaluations

    monkeypatch.setattr(evaluations.db, "list_agent_evaluations", lambda agent, limit: [_row()])
    monkeypatch.setattr(evaluations.db, "list_fleet_evaluations", lambda names, limit: [_row()])
    monkeypatch.setattr(evaluations.db, "get_agent_evaluation", lambda eval_id: _row())
    monkeypatch.setattr(evaluations, "accessible_agent_names", lambda user: [AGENT])
    _ownership(monkeypatch, evaluations)

    def make(user):
        app = FastAPI()
        app.include_router(evaluations.router)
        app.dependency_overrides[evaluations.get_current_user] = lambda: user
        app.dependency_overrides[evaluations.get_authorized_agent_by_name] = lambda: AGENT
        return TestClient(app)

    return make


def _evaluators(response):
    body = response.json()
    return [r["evaluator"] for r in body] if isinstance(body, list) else [body["evaluator"]]


_HTTP_READERS = {
    "agent-key": dict(username=OWNER, role="creator", agent_name=AGENT, mcp_scope="agent"),
    "system": dict(username=ADMIN, role="admin", mcp_scope="system"),
    "shared-human": dict(username=SHARED, role="user"),
}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", sorted(_HTTP_READERS))
def test_read_routes_hide_admin_identity(http_as, who, path):
    from models import User
    r = http_as(User(id=1, **_HTTP_READERS[who])).get(path)
    assert r.status_code == 200, r.text
    assert _evaluators(r) == ["admin"], r.text
    assert GRADER not in r.text


@pytest.mark.parametrize("path", PATHS)
def test_read_routes_keep_operator_identity(http_as, path):
    from models import User
    r = http_as(User(id=1, username=OWNER, role="creator", mcp_scope="user")).get(path)
    assert r.status_code == 200, r.text
    assert _evaluators(r) == [FULL]
