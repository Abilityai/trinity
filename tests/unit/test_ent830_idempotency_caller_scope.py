"""trinity-enterprise#830 — a client-chosen Idempotency-Key is scoped to its caller.

`make_agent_scope` returns `agent:{name}`, and the boundaries that accept a raw
`Idempotency-Key` header claimed it under that scope alone, without the
authenticated caller. A client-chosen key only identifies a request together
with the client that chose it, and a header key shared the namespace that
platform-derived keys use.

Now every header-key boundary claims under `make_caller_scope(agent, user)`,
`agent:{name}:caller:{principal}`; platform-derived keys keep `agent:{name}`,
which a header key can no longer reach.
"""
import ast
import sqlite3
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _user(uid, *, agent_name=None, mcp_scope=None, mcp_key_id=None):
    return SimpleNamespace(id=uid, username=f"u{uid}", agent_name=agent_name,
                           mcp_scope=mcp_scope, mcp_key_id=mcp_key_id)


ALICE = _user(1)                                                   # browser JWT
ALICE_KEY = _user(1, mcp_scope="user", mcp_key_id="k-alice")       # her user MCP key
BOB = _user(2)
AGENT_A = _user(1, agent_name="agent-a", mcp_scope="agent", mcp_key_id="k-a")  # owned by alice
AGENT_B = _user(1, agent_name="agent-b", mcp_scope="agent", mcp_key_id="k-b")  # owned by alice
SYSTEM = _user(9, mcp_scope="system", mcp_key_id="k-sys")


@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent830.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db
    return db


class TestCallerScope:
    def test_two_people_on_one_agent_get_two_scopes(self):
        from services.idempotency_service import make_caller_scope
        assert make_caller_scope("shared", ALICE) != make_caller_scope("shared", BOB)

    def test_two_agents_of_one_owner_get_two_scopes(self):
        # Both agent keys resolve to the same owner id — the owner id alone
        # would collapse them (the #2323 a2a note).
        from services.idempotency_service import make_caller_scope
        assert make_caller_scope("target", AGENT_A) != make_caller_scope("target", AGENT_B)

    def test_one_person_is_one_principal_across_their_browser_and_user_key(self):
        from services.idempotency_service import make_caller_scope
        assert make_caller_scope("t", ALICE) == make_caller_scope("t", ALICE_KEY)

    def test_a_machine_credential_is_its_own_principal(self):
        from services.idempotency_service import make_caller_scope
        assert make_caller_scope("t", SYSTEM) not in {
            make_caller_scope("t", u) for u in (ALICE, BOB, AGENT_A, AGENT_B)}

    def test_a_caller_scope_never_equals_the_platform_scope(self):
        from services.idempotency_service import make_agent_scope, make_caller_scope
        for u in (ALICE, BOB, AGENT_A, SYSTEM):
            assert make_caller_scope("t", u) != make_agent_scope("t")
            assert make_caller_scope("t", u).startswith(make_agent_scope("t") + ":")


class TestTheStore:
    def test_two_principals_same_key_two_claims_and_no_shared_snapshot(self, real_db):
        from services import idempotency_service as svc
        agent, key = f"shared-{uuid.uuid4().hex[:6]}", "same-key"
        a = svc.begin(svc.make_caller_scope(agent, ALICE), key)
        svc.complete(a, "exec-alice", {"response": "alice's private answer"})
        b = svc.begin(svc.make_caller_scope(agent, BOB), key)
        assert a.enabled and not a.replay
        assert b.enabled and not b.replay      # Bob's request runs ...
        assert b.snapshot is None              # ... and never sees Alice's answer

    def test_the_same_principal_still_replays(self, real_db):
        from services import idempotency_service as svc
        agent = f"shared-{uuid.uuid4().hex[:6]}"
        a = svc.begin(svc.make_caller_scope(agent, ALICE), "k")
        svc.complete(a, "exec-1", {"response": "r"})
        again = svc.begin(svc.make_caller_scope(agent, ALICE), "k")
        assert again.replay and again.snapshot == {"response": "r"}

    def test_a_header_key_cannot_pre_claim_a_derived_key(self, real_db):
        from services import idempotency_service as svc
        agent = f"a-{uuid.uuid4().hex[:6]}"
        derived = "capture_feedback:abc"
        svc.begin(svc.make_caller_scope(agent, BOB), derived)   # Bob sends it as a header
        platform = svc.begin(svc.make_agent_scope(agent), derived)
        assert platform.enabled and not platform.replay


class TestTaskAdmission:
    def test_two_principals_same_key_on_task_dispatch_twice(self, real_db):
        from services import dispatch_admission_service as das
        from services import idempotency_service as svc
        agent = f"shared-{uuid.uuid4().hex[:6]}"
        first, replay1 = das.begin_task_idempotency(name=agent, idempotency_key="k", current_user=ALICE)
        svc.complete(first, "exec-alice", {"status": "accepted", "execution_id": "exec-alice", "async_mode": True})
        second, replay2 = das.begin_task_idempotency(name=agent, idempotency_key="k", current_user=BOB)
        assert replay1 is None and replay2 is None


# Every boundary that claims a CLIENT-supplied key, and the function it lives in.
HEADER_SITES = [
    ("services/dispatch_admission_service.py", "admit_chat_request"),
    ("services/dispatch_admission_service.py", "begin_task_idempotency"),
    ("routers/fan_out.py", "fan_out"),
    ("routers/voip.py", "place_voip_call"),
    ("routers/reminders.py", "create_reminder_endpoint"),
    ("routers/agent_brain_orb.py", "post_brain_orb_action"),
    ("routers/agent_data.py", "import_agent_data"),
    ("routers/git.py", "bind_agent_to_own_repo"),
]


def _calls(fn, name):
    return [c for c in ast.walk(fn) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and c.func.attr == name]


@pytest.mark.parametrize("path,func", HEADER_SITES)
def test_every_header_key_boundary_claims_under_the_caller_scope(path, func):
    tree = ast.parse((_BACKEND / path).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func)
    assert _calls(fn, "make_caller_scope"), f"{path}::{func} claims a client key without the caller"


# Boundaries that take the header but deliberately claim under the agent scope.
AGENT_SCOPE_ALLOWED = {
    # Agent-to-backend, no client principal: the key comes from the platform.
    "routers/internal.py": "agent-to-backend boundary",
    # The key is always derived from (payment-signature, message); the header
    # is accepted for contract compliance and never claimed (#1018).
    "routers/paid.py": "header key never participates",
}


def _takes_header_key(fn):
    args = fn.args.args + fn.args.kwonlyargs
    defaults = dict(zip(reversed(fn.args.args), reversed(fn.args.defaults)))
    defaults.update({a: d for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None})
    for a in args:
        if a.arg != "idempotency_key":
            continue
        d = defaults.get(a)
        if isinstance(d, ast.Call) and getattr(d.func, "id", getattr(d.func, "attr", None)) == "Header":
            return True
    return False


def test_no_header_key_boundary_claims_under_the_agent_scope_alone():
    """Beyond HEADER_SITES: any function that takes an ``Idempotency-Key``
    header and builds ``make_agent_scope`` must also build
    ``make_caller_scope`` (a derived-key fallback, as reminders has, is fine).
    A new boundary that claims the header under the agent scope alone fails
    here even though nobody added it to the list."""
    offenders = []
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith(("enterprise/", "migrations/")) or rel in AGENT_SCOPE_ALLOWED:
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _takes_header_key(fn):
                continue
            if _calls(fn, "make_agent_scope") and not _calls(fn, "make_caller_scope"):
                offenders.append(f"{rel}::{fn.name}")
    assert offenders == [], f"header key claimed without the caller: {offenders}"


def test_the_agent_scope_allowlist_is_live():
    for rel in AGENT_SCOPE_ALLOWED:
        tree = ast.parse((_BACKEND / rel).read_text())
        assert any(
            isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _takes_header_key(fn) and _calls(fn, "make_agent_scope")
            for fn in ast.walk(tree)
        ), f"{rel} no longer needs its allowlist entry"
