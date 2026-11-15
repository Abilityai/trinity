"""ent#781 census: every evaluation writer that can name a person is driven
through the machine projection with an email-shaped identity.

The AST walk finds every statically named call to the three evaluation writer
APIs (`create_agent_evaluation`, `upsert_workspace_rating`, `create_evaluation`)
in the OSS backend, keyword shape regardless, and gives each one a site id
`<path>::<qualified function>::<method>#<ordinal>` (`<module>` for a
module-level call), so a SECOND call inside an already-registered function is a
new site too. Each site must be listed in `DRIVERS` with a driver that runs the
REAL writer down to the stored-row boundary; a new site fails here until
someone writes its driver, which is when the question "does this evaluator
name a person?" gets asked. Each driver's stored row is then read back through
`routers.evaluations._to_response` as a machine principal, and neither the
identity nor any `@` may survive.

What this does NOT see: a dynamically dispatched call, a writer API under a new
name, or a new SQL writer. The read projection is the guarantee for those (an
allowlist: an unknown evaluator collapses to its kind or `unknown`); this
census is the tripwire for the known doors.
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

WRITER_METHODS = frozenset({"create_agent_evaluation", "upsert_workspace_rating", "create_evaluation"})
_EXCLUDED_PARTS = frozenset({"enterprise", "tests", "node_modules", "__pycache__"})


class CensusError(RuntimeError):
    """The census could not read a source file. Never a silent skip: a file the
    walk cannot parse is a file whose writers it cannot see."""


class _SiteVisitor(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.scope: list[str] = []
        self.counts: dict[tuple[str, str], int] = {}
        self.sites: list[str] = []

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped
    visit_ClassDef = _scoped

    def visit_Call(self, node: ast.Call):
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None)
        if name in WRITER_METHODS:
            scope = ".".join(self.scope) or "<module>"
            key = (scope, name)
            self.counts[key] = self.counts.get(key, 0) + 1
            self.sites.append(f"{self.rel}::{scope}::{name}#{self.counts[key]}")
        self.generic_visit(node)


def _scan_source(rel: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as e:
        raise CensusError(f"census could not parse {rel}: {e}") from e
    visitor = _SiteVisitor(rel)
    visitor.visit(tree)
    return visitor.sites


def _call_sites(root: Path = _BACKEND) -> set:
    sites: set = set()
    for path in sorted(root.rglob("*.py")):
        rel_path = path.relative_to(root)
        if _EXCLUDED_PARTS.intersection(rel_path.parts):
            continue
        rel = rel_path.as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CensusError(f"census could not read {rel}: {e}") from e
        sites.update(_scan_source(rel, source))
    return sites


def _census_problems(sites: set, registered) -> list[str]:
    problems = []
    if not sites:
        problems.append("census found no writer: the walk is broken, not the code clean")
    missing = sorted(sites - set(registered))
    stale = sorted(set(registered) - sites)
    if missing:
        problems.append(f"unregistered (write a driver): {missing}")
    if stale:
        problems.append(f"stale (site gone): {stale}")
    return problems


# --- drivers: run the real writer, capture at the store boundary ---------------

def _evaluation_stores():
    """Every distinct `_evaluation_ops` the exercised paths can resolve: the one
    bound into the router at import, and the one `from database import db`
    returns at call time (the portal's import). Patched on each, so a write can
    never reach a real SQLite file even if the harness re-imported `database`."""
    import database
    from routers import evaluations
    stores = []
    for manager in (evaluations.db, database.db):
        if not any(manager._evaluation_ops is s for s in stores):
            stores.append(manager._evaluation_ops)
    return stores


def _capture(monkeypatch, method: str) -> list:
    captured: list = []

    def fake(agent_name, **kw):
        row = {"id": f"eval_census_{len(captured)}", "agent_name": agent_name,
               "created_at": "2026-10-21T00:00:00Z", "updated_at": "2026-10-21T00:00:00Z",
               "quality": None, "completion": None, "checks": None, "judge": None,
               "target_kind": None, "target_id": None, "comment": None, **kw}
        captured.append(row)
        return dict(row)

    for store in _evaluation_stores():
        monkeypatch.setattr(store, method, fake)
    return captured


def _drive_admin_route(monkeypatch):
    """The real admin POST handler through the real `DatabaseManager`
    `create_agent_evaluation` facade; captured only at the ops layer."""
    from models import EvaluationCreate, User
    from routers import evaluations
    captured = _capture(monkeypatch, "create_evaluation")
    monkeypatch.setattr(evaluations.db, "can_user_share_agent", lambda u, a: True)
    admin = User(id=2, username=IDENTITY, email=IDENTITY, role="admin")
    asyncio.run(evaluations.create_evaluation(AGENT, EvaluationCreate(quality=1.0), admin))
    assert len(captured) == 1
    return [("admin", captured[0])]


def _drive_workspace_rating(monkeypatch):
    """The real `submit_rating` (real roster check, real target check, real
    `workspace_evaluator`) through the real `upsert_workspace_rating` facade,
    once as a client and once as an operator preview (`include_owned`)."""
    from client_portal import service
    captured = _capture(monkeypatch, "upsert_workspace_rating")
    monkeypatch.setattr(service, "roster_agent_names", lambda email, include_owned: {AGENT})
    monkeypatch.setattr(service.db, "get_portal_message", lambda message_id: {
        "id": message_id, "agent_name": AGENT, "client_email": IDENTITY, "role": "assistant"})
    out = []
    for include_owned, kind in ((False, "workspace"), (True, "operator")):
        service.submit_rating(AGENT, IDENTITY, target_kind="message", target_id="msg_census",
                              rating="down", comment="neutral words", include_owned=include_owned)
        out.append((kind, captured[-1]))
    assert len(captured) == 2
    return out


# Several sites may share one driver when that driver executes the whole
# forwarding path (route -> facade -> ops).
DRIVERS = {
    "routers/evaluations.py::create_evaluation::create_agent_evaluation#1": _drive_admin_route,
    "database.py::DatabaseManager.create_agent_evaluation::create_evaluation#1": _drive_admin_route,
    "client_portal/service.py::submit_rating::upsert_workspace_rating#1": _drive_workspace_rating,
    "database.py::DatabaseManager.upsert_workspace_rating::upsert_workspace_rating#1": _drive_workspace_rating,
}


def test_every_call_site_has_a_driver():
    problems = _census_problems(_call_sites(), DRIVERS)
    assert not problems, "; ".join(problems)


_MACHINE_READERS = {
    "agent-key": dict(username="owner", role="creator", agent_name=AGENT, mcp_scope="agent"),
    "system": dict(username="admin", role="admin", mcp_scope="system"),
    "connector": dict(username="owner", role="creator", mcp_scope="connector", connector_agent=AGENT),
    # A machine whose owner is an admin AND an operator of the agent: neither
    # the role nor operator membership makes software a person.
    "admin-owned-agent-key": dict(username="admin", role="admin", agent_name=AGENT, mcp_scope="agent"),
}


@pytest.mark.parametrize("site", sorted(DRIVERS))
@pytest.mark.parametrize("reader", sorted(_MACHINE_READERS))
def test_no_writer_names_its_grader_to_a_machine(monkeypatch, site, reader):
    from models import User
    from routers import evaluations
    cases = DRIVERS[site](monkeypatch)
    assert cases
    machine = User(id=1, **_MACHINE_READERS[reader])
    for kind, row in cases:
        assert row["evaluator"] == f"{kind}:{IDENTITY}"   # precondition: the writer names a person
        out = evaluations._to_response(row, machine, frozenset({AGENT}))
        body = out.model_dump_json()
        assert out.evaluator == kind, (site, kind, out.evaluator)
        assert IDENTITY not in body and "@" not in body, (site, kind, out.evaluator)


# --- the census's own teeth (negative controls) ----------------------------------

_FIXTURE = '''
from database import db

db.create_agent_evaluation("a", evaluator="admin:x")          # module level


class Store:
    def write(self, **kw):
        return self.create_evaluation("a", **kw)               # no evaluator= keyword


def route(row):
    db.upsert_workspace_rating("a", evaluator=row["e"], target_kind="message",
                               target_id="m", quality=1.0)
    upsert_workspace_rating("a", evaluator="x")                # a bare-name call
    db.upsert_workspace_rating("a", evaluator="y")             # a second call, same function


def unrelated():
    db.get_agent_evaluation("e")
'''


def test_the_scanner_names_every_shape_of_call():
    assert _scan_source("pkg/fixture.py", _FIXTURE) == [
        "pkg/fixture.py::<module>::create_agent_evaluation#1",
        "pkg/fixture.py::Store.write::create_evaluation#1",
        "pkg/fixture.py::route::upsert_workspace_rating#1",
        "pkg/fixture.py::route::upsert_workspace_rating#2",
        "pkg/fixture.py::route::upsert_workspace_rating#3",
    ]


def test_an_unregistered_site_fails_the_census():
    sites = set(_scan_source("pkg/fixture.py", _FIXTURE))
    registered = sites - {"pkg/fixture.py::<module>::create_agent_evaluation#1"}
    problems = _census_problems(sites, registered)
    assert any("unregistered" in p and "<module>::create_agent_evaluation#1" in p for p in problems)


def test_a_second_call_in_a_registered_function_fails_the_census():
    one = "def route():\n    db.upsert_workspace_rating('a', evaluator='x')\n"
    two = one + "    db.upsert_workspace_rating('a', evaluator='y')\n"
    registered = set(_scan_source("r.py", one))
    assert _census_problems(set(_scan_source("r.py", one)), registered) == []
    problems = _census_problems(set(_scan_source("r.py", two)), registered)
    assert any("r.py::route::upsert_workspace_rating#2" in p for p in problems)


def test_a_removed_site_is_reported_stale():
    registered = set(_scan_source("pkg/fixture.py", _FIXTURE))
    problems = _census_problems(registered - {"pkg/fixture.py::Store.write::create_evaluation#1"}, registered)
    assert any("stale" in p for p in problems)


def test_an_empty_census_fails():
    assert _census_problems(set(), {}) != []


def test_malformed_or_unreadable_source_raises_instead_of_skipping(tmp_path):
    (tmp_path / "ok.py").write_text("db.create_evaluation('a')\n", encoding="utf-8")
    (tmp_path / "enterprise").mkdir()
    (tmp_path / "enterprise" / "skipped.py").write_text("def (:\n", encoding="utf-8")
    assert _call_sites(tmp_path) == {"ok.py::<module>::create_evaluation#1"}

    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(CensusError, match="broken.py"):
        _call_sites(tmp_path)
    (tmp_path / "broken.py").unlink()

    (tmp_path / "latin.py").write_bytes(b"x = '\xff\xfe'\n")
    with pytest.raises(CensusError, match="latin.py"):
        _call_sites(tmp_path)
