"""trinity-enterprise#1153 — one people-search seam behind every people picker.

Every route that returns a list of people goes through
`services/people_search_service.search`. The only part a caller supplies is
WHO MAY APPEAR, a scope built in the db layer. The seam applies the rest, so no
picker can drift into a fleet-wide account dump:

  1. the shared match: a prefix of the email, the username or a word of the name,
     case-insensitive, wildcards as text;
  2. the shared exclusions: suspended accounts, accounts with no email, the
     person asking;
  3. the bounds: a minimum query that costs nothing, a cut query, 8 rows, and a
     per-person rate limit per scope;
  4. the wiring: the room picker, the Workspace chat picker and the Access tab's
     candidates each reach the seam, and each route turns its limit into a named
     429;
  5. the guard: a route that returns people without reaching the seam fails CI.

Real SQLite built by the platform's own `init_schema`; the rows are the point.
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
SEAM = BACKEND / "services" / "people_search_service.py"

OWNER = "olga@example.com"
SHARED = "sam@example.com"
ADMIN = "ada@example.com"
OUTSIDER = "otto@example.com"
SUSPENDED = "sue@example.com"
AGENT = "scout"


@pytest.fixture()
def people_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-ent1153.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema

    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()

    from db.engine import get_engine
    from sqlalchemy import text
    from services import rate_limiter

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    eng = get_engine()
    with eng.begin() as conn:
        people = [
            (1, "olga", OWNER, "Olga Owner", "user", None),
            (2, "sam", SHARED, "Sam Sharer", "user", None),
            (3, "ada", ADMIN, "Ada Admin", "admin", None),
            (4, "otto", OUTSIDER, "Otto Outer", "user", None),
            (5, "sue", SUSPENDED, "Sue Stopped", "user", "2026-09-01T00:00:00Z"),
            (6, "noemail", None, "Nora Nomail", "user", None),
        ]
        for uid, username, email, name, role, suspended in people:
            conn.execute(text(
                "INSERT INTO users (id, username, email, name, role, created_at, updated_at, suspended_at) "
                "VALUES (:id, :u, :e, :n, :r, 't', 't', :s)"
            ), {"id": uid, "u": username, "e": email, "n": name, "r": role, "s": suspended})
        conn.execute(text(
            "INSERT INTO agent_ownership (agent_name, owner_id, created_at) VALUES (:a, 1, 't')"), {"a": AGENT})
        for email in (SHARED, SUSPENDED):
            conn.execute(text(
                "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
                "VALUES (:a, :e, 1, 't')"), {"a": AGENT, "e": email})
    yield eng
    rate_limiter.clear_inprocess()


def _search(scope, q, who=OWNER, **kw):
    from services import people_search_service as pss
    return pss.search(scope, q, requester_email=who, rate_key=who, rate_limit=kw.pop("rate_limit", 100), **kw)


def _emails(rows):
    return sorted(r["email"] for r in rows)


def _everyone():
    from database import db
    return db.people_scope_share_candidates("no-such-agent")


# ===========================================================================
# 1–2. The shared match and exclusions
# ===========================================================================

class TestTheMatch:
    def test_email_username_and_a_word_of_the_name_all_match(self, people_db):
        scope = _everyone()
        assert _emails(_search(scope, "otto@")) == [OUTSIDER]
        assert _emails(_search(scope, "ott")) == [OUTSIDER]
        assert _emails(_search(scope, "outer")) == [OUTSIDER]

    def test_the_middle_of_a_word_does_not_match(self, people_db):
        assert _search(_everyone(), "tto") == []

    def test_it_is_case_insensitive(self, people_db):
        assert _emails(_search(_everyone(), "OTTO")) == [OUTSIDER]

    @pytest.mark.parametrize("wild", ["%", "_", "%o", "_t"])
    def test_wildcards_are_text(self, people_db, wild):
        assert _search(_everyone(), wild) == []

    def test_suspended_no_email_and_the_asker_are_never_listed(self, people_db):
        scope = _everyone()
        assert _search(scope, "sue") == []
        assert _search(scope, "nora") == []
        assert _search(scope, "olga", who=OWNER) == []
        assert _emails(_search(scope, "olga", who=ADMIN)) == [OWNER]

    def test_a_row_carries_the_label_the_email_and_the_role(self, people_db):
        row = _search(_everyone(), "otto")[0]
        assert row == {"email": OUTSIDER, "username": "otto", "label": "Otto Outer", "role": "user"}


class TestTheScopes:
    def test_agent_reach_is_owner_sharees_and_admins(self, people_db):
        from database import db
        scope = db.people_scope_agent_reach([AGENT])
        assert _emails(_search(scope, "o", who=SHARED)) == [OWNER]
        assert _emails(_search(scope, "s", who=OWNER)) == [SHARED]
        assert _emails(_search(scope, "ada")) == [ADMIN]
        assert _search(scope, "otto") == []

    def test_share_candidates_are_everyone_but_the_owner_and_the_roster(self, people_db):
        from database import db
        scope = db.people_scope_share_candidates(AGENT)
        assert _search(scope, "olga", who=ADMIN) == []
        assert _search(scope, "sam") == []
        assert _emails(_search(scope, "otto")) == [OUTSIDER]

    def test_a_scope_with_no_agents_lists_no_one_and_asks_no_one(self, people_db, monkeypatch):
        from database import db
        from services import people_search_service as pss
        monkeypatch.setattr(pss.db, "search_people", lambda *a, **k: pytest.fail("asked the DB"))
        assert _search(db.people_scope_agent_reach([]), "otto") == []


# ===========================================================================
# 3. The bounds
# ===========================================================================

class TestTheBounds:
    @pytest.mark.parametrize("q", ["", "  "])
    def test_an_empty_query_lists_no_one_and_costs_nothing(self, people_db, monkeypatch, q):
        from services import people_search_service as pss
        monkeypatch.setattr(pss.rate_limiter, "check", lambda *a, **k: pytest.fail("spent the rate limit"))
        assert _search(_everyone(), q) == []

    def test_a_query_under_the_callers_minimum_costs_nothing(self, people_db, monkeypatch):
        from services import people_search_service as pss
        monkeypatch.setattr(pss.rate_limiter, "check", lambda *a, **k: pytest.fail("spent the rate limit"))
        assert _search(_everyone(), "o", min_query=2) == []

    def test_a_long_query_is_cut(self, people_db, monkeypatch):
        from services import people_search_service as pss
        seen = []
        monkeypatch.setattr(pss.db, "search_people", lambda scope, q, **k: seen.append(q) or [])
        _search(_everyone(), "a" * 500)
        assert seen == ["a" * pss.MAX_QUERY]

    def test_at_most_eight(self, people_db):
        from sqlalchemy import text
        with people_db.begin() as conn:
            for i in range(12):
                conn.execute(text(
                    "INSERT INTO users (username, email, name, role, created_at, updated_at) "
                    "VALUES (:u, :e, NULL, 'user', 't', 't')"), {"u": f"many{i}", "e": f"many{i}@example.com"})
        assert len(_search(_everyone(), "many")) == 8

    def test_the_rate_limit_is_per_person_and_per_scope(self, people_db):
        from database import db
        from services import people_search_service as pss
        share = db.people_scope_share_candidates(AGENT)
        assert _search(share, "otto", rate_limit=1) is not None
        with pytest.raises(pss.PeopleSearchLimited) as e:
            _search(share, "otto", rate_limit=1)
        assert e.value.retry_after >= 1
        # another person is not limited by Olga's lookups
        _search(share, "otto", who=ADMIN, rate_limit=1)
        # and another kind of picker is a separate budget
        _search(db.people_scope_agent_reach([AGENT]), "sam", rate_limit=1)


# ===========================================================================
# 4. The wiring — each picker reaches the seam
# ===========================================================================

class _Recorder:
    def __init__(self):
        self.kinds = []

    def __call__(self, scope, query, **kw):
        self.kinds.append(scope.kind)
        return [{"email": OUTSIDER, "username": "otto", "label": "Otto Outer", "role": "user"}]


def _user(username, email, role="user"):
    from models import User
    return User(id=1, username=username, role=role, email=email)


def test_the_access_tab_candidates_reach_the_seam(people_db, monkeypatch):
    from services import access_candidate_service, people_search_service as pss
    rec = _Recorder()
    monkeypatch.setattr(pss, "search", rec)
    rows = access_candidate_service.suggest(AGENT, "otto", _user("olga", OWNER))
    assert rec.kinds == ["share_candidates"]
    assert [r.email for r in rows] == [OUTSIDER]
    assert rows[0].role is None          # role only for an admin


def test_the_room_picker_reaches_the_seam(people_db, monkeypatch):
    from shared_sessions import service
    from services import people_search_service as pss
    monkeypatch.setattr(service, "_broadcast", lambda *a, **k: None)
    room = service.create_room(_user("olga", OWNER), "Pricing", [AGENT])
    rec = _Recorder()
    monkeypatch.setattr(pss, "search", rec)
    rows = service.room_people(_user("olga", OWNER), room["id"], "otto")
    assert rec.kinds == ["agent_reach"]
    assert rows == [{"email": OUTSIDER, "label": "Otto Outer"}]


def test_the_workspace_chat_picker_reaches_the_seam(people_db, monkeypatch):
    from client_portal import service
    from client_portal.portal_auth import PortalPrincipal
    from services import people_search_service as pss
    rec = _Recorder()
    monkeypatch.setattr(pss, "search", rec)
    rows = service.chat_people(PortalPrincipal(email=OWNER, is_platform=True, is_person=True), AGENT, "otto")
    assert rec.kinds == ["agent_reach"]
    assert rows == [{"email": OUTSIDER, "label": "Otto Outer"}]


def _limited(*_a, **_k):
    from services import people_search_service as pss
    raise pss.PeopleSearchLimited(7)


def test_the_room_route_turns_the_limit_into_a_named_429(people_db, monkeypatch):
    from fastapi import HTTPException
    from shared_sessions import router, service
    monkeypatch.setattr(service, "room_people", _limited)
    with pytest.raises(HTTPException) as e:
        asyncio.run(router.room_people("r1", q="otto", current_user=_user("olga", OWNER)))
    assert e.value.status_code == 429
    assert e.value.detail["code"] == "rate_limited"
    assert e.value.headers["Retry-After"] == "7"


def test_the_workspace_route_turns_the_limit_into_a_named_429(people_db, monkeypatch):
    from fastapi import HTTPException
    from client_portal import router, service
    from client_portal.portal_auth import PortalPrincipal
    monkeypatch.setattr(router, "_require_roster", lambda *a, **k: None)
    monkeypatch.setattr(service, "chat_people", _limited)
    with pytest.raises(HTTPException) as e:
        router.portal_people(AGENT, q="otto", principal=PortalPrincipal(email=OWNER, is_platform=True, is_person=True))
    assert e.value.status_code == 429
    assert e.value.detail["code"] == "rate_limited"


# ===========================================================================
# 5. The guard — no route returns people without the seam
# ===========================================================================

_HTTP_VERBS = {"get", "post", "put", "patch", "delete"}


def _person_row_models() -> set[str]:
    """Models whose instances are picker rows: they carry an `email` and a `label`."""
    tree = ast.parse((BACKEND / "models.py").read_text())
    out = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            fields = {s.target.id for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
            if {"email", "label"} <= fields:
                out.add(node.name)
    return out


def _source_files():
    for path in BACKEND.rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts or "node_modules" in path.parts:
            continue
        yield path


def _response_model_names(call: ast.Call) -> set[str]:
    for kw in call.keywords:
        if kw.arg == "response_model":
            return {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)} | \
                   {n.attr for n in ast.walk(kw.value) if isinstance(n, ast.Attribute)}
    return set()


def people_routes(tree: ast.Module, person_models: set[str]):
    """(handler name) for every route whose response_model names a picker row."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in _HTTP_VERBS and _response_model_names(dec) & person_models):
                    yield node


def _module_file(dotted: str, here: Path, level: int) -> Path | None:
    base = here.parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    root = base if level else BACKEND
    parts = dotted.split(".") if dotted else []
    cand = root.joinpath(*parts)
    if cand.with_suffix(".py").is_file():
        return cand.with_suffix(".py")
    if (cand / "__init__.py").is_file():
        return cand / "__init__.py"
    return None


def _imports(tree: ast.Module, here: Path) -> dict[str, tuple[Path, str | None]]:
    """local name -> (file, function name or None for a module alias)."""
    out: dict[str, tuple[Path, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                local = a.asname or a.name
                as_module = _module_file(f"{node.module}.{a.name}" if node.module else a.name, here, node.level)
                if as_module:
                    out[local] = (as_module, None)
                else:
                    mod = _module_file(node.module or "", here, node.level)
                    if mod:
                        out[local] = (mod, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                mod = _module_file(a.name, here, 0)
                if mod:
                    out[a.asname or a.name.split(".")[0]] = (mod, None)
    return out


_TREES: dict[Path, ast.Module] = {}


def _tree(path: Path) -> ast.Module:
    if path not in _TREES:
        _TREES[path] = ast.parse(path.read_text())
    return _TREES[path]


def _function(path: Path, name: str):
    for node in ast.walk(_tree(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def reaches_seam(path: Path, fn, depth: int = 5, seen=None) -> bool:
    """Does `fn` (in `path`) call `people_search_service.search`, directly or through
    the functions it calls (module aliases, from-imports and same-file helpers)?"""
    seen = seen if seen is not None else set()
    if depth < 0 or (path, fn.name) in seen:
        return False
    seen.add((path, fn.name))
    if path == SEAM and fn.name == "search":
        return True
    names = _imports(_tree(path), path)
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
        target = None
        if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
            mod = names.get(call.func.value.id)
            if mod and mod[1] is None:
                target = (mod[0], call.func.attr)
        elif isinstance(call.func, ast.Name):
            if call.func.id in names and names[call.func.id][1]:
                target = names[call.func.id]
            elif _function(path, call.func.id):
                target = (path, call.func.id)
        if target:
            callee = _function(*target)
            if callee and reaches_seam(target[0], callee, depth - 1, seen):
                return True
    return False


def test_every_route_that_returns_people_goes_through_the_seam():
    models = _person_row_models()
    assert {"TaggablePerson", "AccessCandidate"} <= models
    found, strays = [], []
    for path in _source_files():
        for fn in people_routes(_tree(path), models):
            found.append(f"{path.relative_to(BACKEND)}::{fn.name}")
            if not reaches_seam(path, fn):
                strays.append(found[-1])
    # Not vacuous: the three pickers this seam was built for are all seen.
    assert {"shared_sessions/router.py::room_people", "client_portal/router.py::portal_people",
            "routers/sharing.py::get_access_candidates_endpoint"} <= set(found), found
    assert strays == [], (
        f"These routes return people without going through people_search_service.search: {strays}. "
        "Build a scope in the db layer and call the seam (trinity-enterprise#1153).")


def test_the_guard_catches_a_route_that_dumps_the_fleet(tmp_path):
    """The guard is a guard: a route returning picker rows from `db.list_users()`."""
    stray = tmp_path / "stray_router.py"
    stray.write_text(
        "from fastapi import APIRouter\nfrom database import db\nfrom models import TaggablePerson\n"
        "router = APIRouter()\n\n@router.get('/everyone', response_model=list[TaggablePerson])\n"
        "def everyone():\n    return [{'email': u['email'], 'label': u['username']} for u in db.list_users()]\n")
    try:
        fn = next(people_routes(_tree(stray), _person_row_models()))
        assert not reaches_seam(stray, fn)
    finally:
        stray.unlink()
        _TREES.pop(stray, None)


def test_only_the_seam_runs_the_people_query():
    callers = []
    for path in _source_files():
        if path in (SEAM, BACKEND / "database.py", BACKEND / "db" / "users.py"):
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in {
                    "search_people", "list_taggable_people", "list_share_candidates"}:
                callers.append(f"{path.relative_to(BACKEND)}:{node.lineno}:{node.attr}")
    assert callers == []
