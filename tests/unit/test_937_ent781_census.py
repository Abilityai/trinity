"""ent#781 census: every evaluation writer that can name a person is driven
through the machine projection with an email-shaped identity.

The AST walk finds every `create_agent_evaluation(...)` call in the OSS backend
that passes `evaluator=`. Each site must be listed in `DRIVERS` with a driver
that runs the REAL writer; a new site fails here until someone writes its
driver — which is when the question "does this evaluator name a person?" gets
asked. Each driver's stored row is then read back through
`routers.evaluations._to_response` as a machine principal, and no `@` may
survive.
"""
import ast
import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

IDENTITY = "grader-census@example.com"
AGENT = "census-agent"


def _call_sites():
    sites = set()
    for path in _BACKEND.rglob("*.py"):
        rel = path.relative_to(_BACKEND).as_posix()
        if rel.startswith(("enterprise/", "tests/")) or "/node_modules/" in rel:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "create_agent_evaluation"
                        and any(k.arg == "evaluator" for k in node.keywords)):
                    sites.add(f"{rel}::{fn.name}")
    return sites


def _drive_admin_route(monkeypatch):
    from models import EvaluationCreate, User
    from routers import evaluations
    stored = {}

    def fake_create(agent_name, **kw):
        stored.update({"id": "eval_c", "agent_name": agent_name,
                       "created_at": "2026-10-18T00:00:00Z", **kw})
        return dict(stored)

    monkeypatch.setattr(evaluations.db, "create_agent_evaluation", fake_create)
    monkeypatch.setattr(evaluations.db, "can_user_share_agent", lambda u, a: True)
    admin = User(id=2, username=IDENTITY, email=IDENTITY, role="admin")
    asyncio.run(evaluations.create_evaluation(AGENT, EvaluationCreate(quality=1.0), admin))
    return stored


DRIVERS = {
    "routers/evaluations.py::create_evaluation": _drive_admin_route,
}


def test_every_call_site_has_a_driver():
    sites = _call_sites()
    assert sites, "census found no writer: the walk is broken, not the code clean"
    assert sites == set(DRIVERS), (
        f"unlisted: {sorted(sites - set(DRIVERS))}; stale: {sorted(set(DRIVERS) - sites)}")


@pytest.mark.parametrize("site", sorted(DRIVERS))
@pytest.mark.parametrize("reader", [{"agent_name": AGENT}, {"mcp_scope": "system"},
                                    {"mcp_scope": "connector"}])
def test_no_writer_names_its_grader_to_a_machine(monkeypatch, site, reader):
    from models import User
    from routers import evaluations
    row = DRIVERS[site](monkeypatch)
    assert IDENTITY in str(row["evaluator"])      # precondition: the writer names a person
    machine = User(id=1, username="owner", role="creator", **reader)
    out = evaluations._to_response(row, machine, frozenset({AGENT}))
    assert "@" not in out.model_dump_json()
