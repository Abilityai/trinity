"""The Tier-0 write fence (trinity-enterprise#935, checkpoint B; epic ent#205 AC).

The load-bearing rule of the eval epic: **the graded agent never writes its
own grade.** ent#206 fenced the admin write route; ent#366 added a Workspace
rating path under a `workspace:` evaluator. Tier 0 adds a platform-written row
(`evaluator='platform:tier0'`, `target_kind='execution'`) and three delegation
columns, so the fence now has to hold for THOSE too: no agent-key, MCP or user
route may create, update or delete a `platform:tier0` row or set the delegation
columns. Four properties, each pinned by driving the path an agent could reach:

1. the admin write route refuses an agent principal (and a connector), with a
   Tier-0-shaped body;
2. a human admin's write cannot be dressed up as a Tier-0 row — evaluator,
   target and delegation columns in the body are ignored, and the row it
   writes never answers the Tier-0 read;
3. the Workspace rating path cannot address a `target_kind='execution'` row,
   and its evaluator is always prefixed, so it can never collide with the
   Tier-0 key;
4. there is no other writer: every DML on `agent_evaluations` lives in the db
   layer, every caller of a writer is one of three known sites, the MCP server
   has no evaluations surface, and the evaluator + sweep import no FastAPI /
   auth type (so no route can hand them a caller to trust).

Plus the positive half: the sweep's writer path still writes.
"""
from __future__ import annotations

import ast
import os
import sys
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import count, db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

TIER0 = "platform:tier0"
OWNER = "owner"

# What an agent that wanted to grade itself would send: a Tier-0-shaped row
# plus every column the fence protects.
TIER0_SHAPED_BODY = {
    "execution_id": "ex-1",
    "archetype": "playbook",
    "completion": True,
    "checks": {"schema": 1, "headline": {"passed": True, "gate_fail": 0}, "checks": []},
    "evaluator": TIER0,
    "target_kind": "execution",
    "target_id": "ex-1",
    "caller_agent": "agent-a",
    "parent_execution_id": "parent-1",
    "delegation_mode": "fan_out",
}


def _ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _seed(exec_id="ex-1", agent="agent-a"):
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import schedule_executions
    with get_engine().begin() as conn:
        conn.execute(insert(schedule_executions).values(
            id=exec_id, schedule_id="sched-1", agent_name=agent, status="success",
            triggered_by="schedule", message="/report-weekly", response="Done",
            started_at=_ago(300), completed_at=_ago(120)))


def _user(username, *, role="user", agent_name=None, mcp_scope=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name, mcp_scope=mcp_scope)


@pytest.fixture
def client(db_backend, monkeypatch):
    from database import db
    from routers import evaluations

    principal = {"user": _user(OWNER, role="admin")}
    app = FastAPI()
    app.include_router(evaluations.router)
    # Keyed on the ROUTER's binding, not `dependencies.get_current_user`: the
    # unit conftest's sys.modules restore re-imports `dependencies` between
    # tests, so the module attribute is a fresh object while the router
    # (cached) still depends on the one it imported.
    app.dependency_overrides[evaluations.get_current_user] = lambda: principal["user"]
    monkeypatch.setattr(db, "get_agent_owner", lambda name: OWNER)
    monkeypatch.setattr(db, "can_user_access_agent", lambda user, name: True)
    tc = TestClient(app)
    tc.as_user = lambda u: principal.__setitem__("user", u)
    return tc


WRITE = "/api/agents/agent-a/evaluations"


# --- 1. the write route refuses machine principals with a Tier-0 body -------------------

def test_the_graded_agents_own_key_cannot_write_a_tier0_row(client):
    """An agent-scoped key carries its OWNER's role — admin on a default
    install — so the role check alone would let it through. The gate rejects
    the principal kind, whatever the body says."""
    _seed()
    client.as_user(_user(OWNER, role="admin", agent_name="agent-a", mcp_scope="agent"))
    r = client.post(WRITE, json=TIER0_SHAPED_BODY)
    assert r.status_code == 403, r.text
    assert count("agent_evaluations") == 0


def test_a_connector_key_cannot_write_a_tier0_row(client):
    _seed()
    client.as_user(_user(OWNER, role="admin", mcp_scope="connector"))
    assert client.post(WRITE, json=TIER0_SHAPED_BODY).status_code == 403
    assert count("agent_evaluations") == 0


def test_a_non_admin_person_cannot_write_one_either(client):
    _seed()
    client.as_user(_user("creator-person", role="creator"))
    assert client.post(WRITE, json=TIER0_SHAPED_BODY).status_code == 403
    assert count("agent_evaluations") == 0


# --- 2. a human write cannot impersonate the platform ------------------------------------

def test_an_admin_write_cannot_become_a_tier0_row(client, monkeypatch):
    """The body names the Tier-0 evaluator, the execution target and every
    delegation column. None of it lands: the row is filed under the admin's
    own identity, carries no target and no delegation, and the Tier-0 read for
    that execution still answers "not evaluated".

    ent#937 (ent#781): the grader reads in full only to an OPERATOR of the
    agent. This fixture's admin has no users row, so the operator check is
    stated here rather than resolved — otherwise the echo reads `admin`."""
    from database import db
    monkeypatch.setattr(db, "can_user_share_agent", lambda u, a: True)
    _seed()
    r = client.post(WRITE, json=TIER0_SHAPED_BODY)
    assert r.status_code == 200, r.text
    row = db.get_agent_evaluation(r.json()["id"])
    assert row["evaluator"] == f"admin:{OWNER}"
    assert row["target_kind"] is None and row["target_id"] is None
    assert row["caller_agent"] is None and row["parent_execution_id"] is None
    assert row["delegation_mode"] is None
    assert row["execution_id"] == "ex-1"                 # the honest field survives
    assert db.latest_tier0_for_execution("ex-1") is None
    assert r.json()["evaluator"] == f"admin:{OWNER}"


def test_the_request_model_has_no_field_for_the_fenced_columns():
    """Pydantic drops unknown keys, which is what the test above relies on.
    Pinned at the model so a field added for convenience fails here first."""
    from models import EvaluationCreate
    fenced = {"evaluator", "target_kind", "target_id", "caller_agent",
              "parent_execution_id", "delegation_mode"}
    assert not fenced & set(EvaluationCreate.model_fields)


# --- 3. the Workspace rating path cannot reach the Tier-0 key ---------------------------

@pytest.fixture
def portal(monkeypatch):
    from client_portal import service as mod
    monkeypatch.setattr(mod, "agent_on_roster", lambda a, e, include_owned=False: True)
    return mod


def test_a_rating_cannot_address_an_execution_row(portal):
    """`target_kind` is a closed set of the two things a person can rate; an
    `execution` target is refused before any visibility check runs — so a
    rating can never UPDATE a Tier-0 row through the upsert's key."""
    with pytest.raises(portal.ClientPortalError) as e:
        portal.submit_rating("agent-a", "client@example.com", target_kind="execution",
                             target_id="ex-1", rating="up")
    assert e.value.status_code == 422
    assert "execution" not in portal.RATING_TARGETS


def test_a_rating_evaluator_is_always_prefixed_so_it_cannot_be_the_platform(portal):
    """Even a person whose address IS the platform evaluator string files
    under `workspace:`/`operator:` — the prefix is unconditional."""
    for is_platform in (False, True):
        ev = portal.workspace_evaluator(TIER0, is_platform=is_platform)
        assert ev != TIER0
        assert ev.startswith(("workspace:", "operator:"))


# --- 4. there is no other writer --------------------------------------------------------

_SRC = _BACKEND


def _py_files():
    for path in _SRC.rglob("*.py"):
        if "/enterprise/" in str(path) or "/migrations/" in str(path):
            continue
        yield path


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def test_every_dml_on_agent_evaluations_lives_in_the_db_layer():
    """insert/update/delete over the table object: `db/evaluations.py` (the
    three writers) and `db/agent_cleanup.py` (the owner-only agent-delete
    cascade) and nowhere else — a service or router cannot reach the table."""
    allowed = {"db/evaluations.py", "db/agent_cleanup.py"}
    offenders = set()
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            fn = call.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name in ("insert", "update", "delete") and call.args:
                arg = call.args[0]
                if isinstance(arg, ast.Name) and arg.id == "agent_evaluations":
                    offenders.add(str(path.relative_to(_SRC)))
    assert offenders <= allowed, offenders - allowed


def test_the_writers_have_exactly_four_callers():
    """The db facade forwards; past it, a Tier-0/pair/admin/rating row is
    written from four places only. A fifth caller is a new write path and
    must be fenced here before it exists."""
    writers = {"create_agent_evaluation", "upsert_platform_evaluation",
               "upsert_workspace_rating", "create_evaluation"}
    allowed = {
        "database.py",                     # facade → EvaluationOperations
        "routers/evaluations.py",          # admin:<user>, human-only gate
        "client_portal/service.py",        # workspace:<email>, closed target set
        "services/tier0_evaluator.py",     # platform:tier0, execution id only
        # ent#940: platform:tier0:pair, execution id only; the writer refuses
        # any evaluator outside PLATFORM_EVALUATORS (test_940_pair_checks.py)
        "services/tier0_pair_evaluator.py",
    }
    callers = set()
    for path in _py_files():
        rel = str(path.relative_to(_SRC))
        if rel == "db/evaluations.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls(tree):
            if isinstance(call.func, ast.Attribute) and call.func.attr in writers:
                callers.add(rel)
    assert callers == allowed, (callers - allowed, allowed - callers)


def test_the_evaluator_and_the_sweep_take_no_principal():
    """Decision 18: the writer takes an execution id and nothing else, and the
    modules import no FastAPI, no `dependencies`, no `models` — there is no
    type through which a route could hand them a caller to trust."""
    import inspect
    from services import tier0_evaluator, tier0_pair_evaluator, tier0_sweep_service
    assert list(inspect.signature(tier0_evaluator.evaluate_execution).parameters) == ["execution_id"]
    assert list(inspect.signature(tier0_evaluator.record_timeout).parameters) == ["execution_id"]
    # ent#940: the pair evaluator's two writer entry points, same shape; its
    # sweep takes bounds only (all keyword), never a caller.
    for fn in (tier0_pair_evaluator.evaluate_pair, tier0_pair_evaluator.record_timeout):
        assert list(inspect.signature(fn).parameters) == ["execution_id"], fn.__name__
    sweep_params = inspect.signature(tier0_pair_evaluator.sweep).parameters
    assert set(sweep_params) == {"limit", "lookback_hours", "timeout_seconds", "concurrency"}
    assert all(p.kind is p.KEYWORD_ONLY for p in sweep_params.values())
    forbidden = ("fastapi", "starlette", "dependencies", "models")
    for mod in (tier0_evaluator, tier0_pair_evaluator, tier0_sweep_service):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        imported = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imported.update(a.name.split(".")[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                imported.add(n.module.split(".")[0])
        assert not imported & set(forbidden), (mod.__name__, imported & set(forbidden))


def test_the_write_route_is_the_only_non_get_route_in_the_module():
    """No PUT/PATCH/DELETE on an evaluation exists; the one POST is the
    human-admin write the ent#206 tests already fence."""
    from routers import evaluations
    non_get = {(tuple(sorted(r.methods)), r.endpoint.__name__)
               for r in evaluations.router.routes if r.methods - {"GET", "HEAD"}}
    assert non_get == {(("POST",), "create_evaluation")}


_WRITE_VERB = re.compile(
    r"""["'`](POST|PUT|PATCH|DELETE)["'`]"""            # request("POST", …) / method: "POST"
    r"""|\.(post|put|patch|delete)\s*(<[^>]*>)?\s*\(""",  # axios-style .post(…)
    re.I,
)
_HELPER_DEF = re.compile(r"^\s*(?:public\s+|private\s+)?async\s+(\w+)\s*(?:<[^>]*>)?\(", re.M)


def _statements(text: str):
    """TypeScript statements, split on `;` — every call site here is one."""
    return text.split(";")


def _evaluations_writes(text: str):
    """The statements of one file that put a write verb and an `/evaluations`
    path in the same call."""
    return [s.strip()[:160] for s in _statements(text)
            if "/evaluations" in s and _WRITE_VERB.search(s)]


def _evaluations_write_helpers(client_text: str):
    """Names of `TrinityClient` methods whose body issues such a write."""
    defs = list(_HELPER_DEF.finditer(client_text))
    names = set()
    for i, m in enumerate(defs):
        end = defs[i + 1].start() if i + 1 < len(defs) else len(client_text)
        if _evaluations_writes(client_text[m.start():end]):
            names.add(m.group(1))
    return names


def _mcp_evaluations_write_sites(root: Path):
    """Every write against an `/evaluations` path anywhere under
    `src/mcp-server/src`: a literal verb + path call, or a call of a client
    helper that makes one."""
    files = {p: p.read_text(encoding="utf-8") for p in sorted(root.rglob("*.ts"))}
    client = root / "client.ts"
    helpers = _evaluations_write_helpers(files.get(client, ""))
    sites = []
    for p, text in files.items():
        sites += [(p.name, s) for s in _evaluations_writes(text)]
        sites += [(p.name, h) for h in helpers
                  if p != client and re.search(rf"\.{h}\s*\(", text)]
    return sites


def test_the_mcp_server_exposes_no_evaluations_write_surface():
    """The third surface (Invariant #13) is TypeScript, so this is a text pin
    by design — and it pins the PROPERTY, not a file list: no `.ts` file under
    `src/mcp-server/src` issues POST/PUT/PATCH/DELETE (directly, or through a
    client helper that does) against an `/evaluations` path. Imports of the
    `tools/evaluations.js` module and GET reads (ent#937's
    `get_evaluation_summary`) pass; an MCP write would be a write path
    reachable by an agent key."""
    root = Path(__file__).resolve().parents[2] / "src" / "mcp-server" / "src"
    assert (root / "client.ts").exists()
    assert _mcp_evaluations_write_sites(root) == []
    header = (_SRC / "routers" / "evaluations.py").read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("# mcp: src/mcp-server/src/tools/evaluations.ts")
    assert "never exposed" in header


@pytest.mark.parametrize("client_ts, tool_ts", [
    ('async addEval(n: string) {\n    return this.request("POST", `/api/agents/${n}/evaluations`, {})\n  }',
     'await client.addEval("a")'),
    ('async delEval(id: string) {\n    return this.request<void>(\n      "DELETE",\n      `/api/evaluations/${id}`\n    )\n  }',
     'x'),
    ('async getEval(n: string) {\n    return this.request("GET", `/api/agents/${n}/evaluations/summary`)\n  }',
     'await this.http.put(`/api/evaluations/1`, {})'),
])
def test_the_write_surface_property_catches_a_write(tmp_path, client_ts, tool_ts):
    """The detector itself: a POST/DELETE helper or a raw `.put(` against
    `/evaluations` is found; the shipped GET-plus-import shape is not."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "client.ts").write_text(client_ts + ";\n")
    (tmp_path / "tools" / "x.ts").write_text(
        'import { x } from "./evaluations.js";\n' + tool_ts + ";\n")
    assert _mcp_evaluations_write_sites(tmp_path) != []


def test_the_write_surface_property_passes_a_get_and_an_import(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "client.ts").write_text(
        'async getEval(n: string) {\n    return this.request(\n      "GET",\n'
        '      `/api/agents/${n}/evaluations/summary`\n    );\n  }\n')
    (tmp_path / "tools" / "x.ts").write_text(
        'import { r } from "./tools/evaluations.js";\nawait client.getEval("a");\n')
    assert _mcp_evaluations_write_sites(tmp_path) == []


# --- the positive half: the sweep's path still writes --------------------------------

def test_the_sweeps_writer_path_still_writes(db_backend, monkeypatch):
    import asyncio
    from database import db
    from services import tier0_evaluator as t0
    monkeypatch.setattr(t0, "_staged_values", lambda: [])
    _seed("ex-1")
    result = asyncio.new_event_loop().run_until_complete(t0.sweep())
    assert result["evaluated"] == 1
    row = db.latest_tier0_for_execution("ex-1")
    assert row and row["evaluator"] == TIER0 and row["target_kind"] == "execution"
    assert row["agent_name"] == "agent-a" and row["checks"]["schema"] == 1


def test_an_admin_row_and_the_tier0_row_coexist_for_one_execution(client, monkeypatch):
    """Different evaluators, different keys: a human's note about a run and
    the platform's grade of it are two rows, and the Tier-0 read returns only
    the platform's."""
    from database import db
    from services import tier0_evaluator as t0
    monkeypatch.setattr(t0, "_staged_values", lambda: [])
    _seed("ex-1")
    admin_id = client.post(WRITE, json=TIER0_SHAPED_BODY).json()["id"]
    graded = t0.evaluate_execution("ex-1")
    assert graded["id"] != admin_id
    assert count("agent_evaluations") == 2
    assert db.latest_tier0_for_execution("ex-1")["id"] == graded["id"]
