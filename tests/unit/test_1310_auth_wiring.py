"""#1310 — static AST guard against inline auth-wiring drift (INV-8).

The extraction is behavior-preserving, so the durable risk reduction lives HERE:
this guard stops a future edit from re-introducing an inline auth gate (a
404-then-403 oracle, an omitted connector fence, a divergent second convention)
instead of the shared ``dependencies`` helpers.

It flags, in ``src/backend/routers/*.py``:

  (1) an inline agent gate — a ``db.can_user_access_agent(`` /
      ``db.can_user_share_agent(`` Call that sits in an ``if`` **test** whose body
      raises ``HTTPException`` (the ``if not db.can_user_*: raise`` shape); and
  (2) an inline admin deny — an ``if`` whose test compares ``<x>.role`` to
      ``"admin"`` and whose body raises ``HTTPException``.

It deliberately does NOT flag the benign shapes (proven by the self-tests):
assignments (``can_share = db.can_user_share_agent(...)``), ``role == "admin"``
allow/filter branches (no raise), capability flags, and WebSocket ``close(4003)``
dict handlers (they never ``raise HTTPException``).

Escape hatches: a per-``(file, function)`` allowlist for the intentional
consolidated-404 designs, and a general line-level ``# noqa: inv8`` marker for a
future, individually-reviewed exception. The ``slack.py`` sites that once used
the marker were migrated onto the shared helpers (#1710), so no ``# noqa: inv8``
marker remains in the tree — a re-introduced inline gate now trips unless it
carries a freshly-added, reviewable marker.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_ROUTERS = Path(__file__).resolve().parents[2] / "src" / "backend" / "routers"

# Per-(filename, function) allowlist — intentional, documented designs that keep
# their own inline check (enumeration-safe by construction; INV-8 §2.7 / #186):
#   * reports._report_or_404        — 404-not-403 to avoid a report-id oracle;
#     the ONE gate shared by the detail / rows / export routes (#1838 review)
#   * nevermined._require_*_access  — the payment-config uniform-404 helpers
#   * chat.execute_parallel_task    — the resume-session owner check is a compound
#     `role != "admin" AND NOT db.resume_session_belongs_to_user(...)` raising an
#     intentional 404 (session-id enumeration safety, Invariant #8 session
#     pattern). No shared helper fits: `assert_owns_or_admin` takes an owner_id
#     and raises 403, which would leak session existence. Stays inline (#1083).
#   * a2a._authorize_inbound        — the public A2A inbound gate (ent#157): a
#     uniform-404 helper (non-exposed OR inaccessible → the SAME 404, so the
#     public /a2a/{name} surface is not an enumeration oracle, Invariant #8).
#     A shared `AuthorizedAgentByName` dependency can't express the exposure
#     pre-check + the allow-list 403 that only fires AFTER access is proven.
_ALLOWLIST: set[tuple[str, str]] = {
    ("reports.py", "_report_or_404"),
    ("nevermined.py", "_require_read_access"),
    ("nevermined.py", "_require_write_access"),
    ("chat.py", "execute_parallel_task"),
    ("a2a.py", "_authorize_inbound"),
}

_CAN_USER = {"can_user_access_agent", "can_user_share_agent"}


# --------------------------------------------------------------------------- #
# AST predicates
# --------------------------------------------------------------------------- #
def _is_httpexception_raise(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and (
            (isinstance(node.exc.func, ast.Name) and node.exc.func.id == "HTTPException")
            or (isinstance(node.exc.func, ast.Attribute) and node.exc.func.attr == "HTTPException")
        )
    )


def _body_raises_http(body: list[ast.stmt]) -> bool:
    return any(_is_httpexception_raise(n) for stmt in body for n in ast.walk(stmt))


def _test_calls_can_user(test: ast.AST) -> bool:
    for n in ast.walk(test):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _CAN_USER
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "db"
        ):
            return True
    return False


def _test_compares_role_to_admin(test: ast.AST) -> bool:
    for n in ast.walk(test):
        if not isinstance(n, ast.Compare):
            continue
        parts = [n.left, *n.comparators]
        has_role = any(isinstance(p, ast.Attribute) and p.attr == "role" for p in parts)
        has_admin = any(isinstance(p, ast.Constant) and p.value == "admin" for p in parts)
        if has_role and has_admin:
            return True
    return False


class _Finder(ast.NodeVisitor):
    """Collect (function, lineno, kind) for every inline-auth violation."""

    def __init__(self) -> None:
        self.func_stack: list[str] = ["<module>"]
        self.violations: list[tuple[str, int, str]] = []

    def _enter(self, node):
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter

    def visit_If(self, node: ast.If) -> None:
        if _body_raises_http(node.body):
            if _test_calls_can_user(node.test):
                self.violations.append((self.func_stack[-1], node.lineno, "agent-gate"))
            elif _test_compares_role_to_admin(node.test):
                self.violations.append((self.func_stack[-1], node.lineno, "admin-gate"))
        self.generic_visit(node)


def _line_has_noqa(src_lines: list[str], start: int, end: int) -> bool:
    for ln in range(start, end + 1):
        if 0 < ln <= len(src_lines) and "# noqa: inv8" in src_lines[ln - 1]:
            return True
    return False


def find_violations(source: str, filename: str) -> list[tuple[str, int, str]]:
    """Return the non-allowlisted, non-noqa inline-auth violations in `source`."""
    tree = ast.parse(source)
    src_lines = source.splitlines()
    finder = _Finder()
    finder.visit(tree)

    # Map each violating `if` lineno to its end for noqa scanning.
    if_bounds = {n.lineno: getattr(n, "end_lineno", n.lineno) for n in ast.walk(tree) if isinstance(n, ast.If)}

    out = []
    for func, lineno, kind in finder.violations:
        if (filename, func) in _ALLOWLIST:
            continue
        if _line_has_noqa(src_lines, lineno, if_bounds.get(lineno, lineno)):
            continue
        out.append((func, lineno, kind))
    return out


# --------------------------------------------------------------------------- #
# The guard over the live tree
# --------------------------------------------------------------------------- #
def _router_sources() -> list[Path]:
    """Every router source, INCLUDING those in a package subdirectory.

    `glob("*.py")` was correct while `routers/` was flat. #1028 made
    `routers/settings/` the first subdirectory ever created there, and a
    non-recursive glob silently stopped scanning its ten modules — 110 auth-gate
    tokens dropping out of an Invariant #8 guard with the guard still green. It
    fails open, which is why nothing caught it.

    Files are keyed by path relative to `routers/` (`settings/generic.py`), not
    by bare name: two modules in different packages can share a filename, and an
    allowlist keyed on the ambiguous half would exempt both. Top-level files are
    unaffected — their relative path IS their name — so the allowlist needs no
    edit.
    """
    return sorted(
        p for p in _ROUTERS.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def test_no_inline_auth_gates_in_routers():
    """No router carries an inline agent/admin gate that a shared helper should
    own — except the allowlisted intentional-404 designs and any individually
    reviewed `# noqa: inv8`-marked line (none remain in-tree after #1710)."""
    offenders: dict[str, list[tuple[str, int, str]]] = {}
    for path in _router_sources():
        name = path.relative_to(_ROUTERS).as_posix()
        v = find_violations(path.read_text(), name)
        if v:
            offenders[name] = v
    assert not offenders, (
        "Inline auth gate(s) that must move behind a dependencies.py helper "
        "(assert_admin / assert_agent_access / assert_agent_owner / "
        "assert_owns[_or_admin]) — or be allowlisted / `# noqa: inv8`-marked:\n"
        + "\n".join(f"  {f}: {v}" for f, v in offenders.items())
    )


# --------------------------------------------------------------------------- #
# Self-tests: the matcher flags the raising shapes and spares the benign ones.
# --------------------------------------------------------------------------- #
_PLANTED_VIOLATIONS = [
    # inline access → raise
    ("agent-gate",
     "def h(current_user, agent_name):\n"
     "    if not db.can_user_access_agent(current_user.username, agent_name):\n"
     "        raise HTTPException(status_code=403, detail='Access denied')\n"),
    # inline owner → raise (composite negation)
    ("agent-gate",
     "def h(u, a):\n"
     "    if not (db.can_user_share_agent(u.username, a)):\n"
     "        raise HTTPException(status_code=403, detail='no')\n"),
    # inline admin → raise
    ("admin-gate",
     "def h(current_user):\n"
     "    if current_user.role != 'admin':\n"
     "        raise HTTPException(status_code=403, detail='Admin access required')\n"),
    # admin as part of a composite Shape-F test → raise
    ("admin-gate",
     "def h(current_user, session):\n"
     "    if current_user.role != 'admin' and session.user_id != current_user.id:\n"
     "        raise HTTPException(status_code=403, detail='nope')\n"),
]

_BENIGN = [
    # capability-flag assignment
    "def h(current_user, agent_name):\n"
    "    can_share = db.can_user_share_agent(current_user.username, agent_name)\n"
    "    return can_share\n",
    # data-filter loop (append, no raise)
    "def h(current_user, items):\n"
    "    out = []\n"
    "    for a in items:\n"
    "        if db.can_user_access_agent(current_user.username, a):\n"
    "            out.append(a)\n"
    "    return out\n",
    # admin allow-branch (data selection, no raise)
    "def h(current_user):\n"
    "    if current_user.role == 'admin':\n"
    "        keys = db.list_all()\n"
    "    else:\n"
    "        keys = db.list_mine(current_user.username)\n"
    "    return keys\n",
    # admin filter-branch (no raise)
    "def h(current_user, names):\n"
    "    if current_user.role != 'admin':\n"
    "        names = [n for n in names if n in accessible]\n"
    "    return names\n",
    # WebSocket close(4003) dict handler — raw dict, no HTTPException raise
    "async def h(user, session, websocket):\n"
    "    if user['id'] != session.user_id and user.get('role') != 'admin':\n"
    "        await websocket.close(code=4003, reason='Not authorized')\n"
    "        return\n",
]


@pytest.mark.parametrize("kind,src", _PLANTED_VIOLATIONS, ids=[f"{k}-{i}" for i, (k, _) in enumerate(_PLANTED_VIOLATIONS)])
def test_matcher_flags_planted_violation(kind, src):
    v = find_violations(src, "planted.py")
    assert v and v[0][2] == kind, f"planted {kind} violation not flagged: {v}"


@pytest.mark.parametrize("src", _BENIGN, ids=[f"benign-{i}" for i in range(len(_BENIGN))])
def test_matcher_spares_benign_shape(src):
    assert find_violations(src, "benign.py") == [], "benign shape wrongly flagged"


def test_allowlist_suppresses_intentional_404():
    """A `can_user_*`-guarded raise in an allowlisted (file, function) is not a
    violation — the intentional-404 designs keep their inline check."""
    src = (
        "def _report_or_404(report_id, current_user):\n"
        "    report = db.get_report(report_id)\n"
        "    if not report or not db.can_user_access_agent(current_user.username, report['agent_name']):\n"
        "        raise HTTPException(status_code=404, detail='Report not found')\n"
    )
    assert find_violations(src, "reports.py") == []
    # same body in a non-allowlisted file/function IS flagged
    assert find_violations(src.replace("_report_or_404", "leak", 1), "reports.py")


def test_noqa_inv8_suppresses_line():
    src = (
        "def h(current_user, name):\n"
        "    if not db.can_user_access_agent(current_user.username, name):  # noqa: inv8\n"
        "        raise HTTPException(status_code=403, detail='Access denied')\n"
    )
    assert find_violations(src, "slack.py") == []
    # without the marker it IS flagged
    assert find_violations(src.replace("  # noqa: inv8", ""), "slack.py")


# --------------------------------------------------------------------------- #
# trinity-enterprise#840: no hand-copied agent-key self-gate, anywhere in the
# backend. `dependencies.assert_own_agent` is the one spelling the route census
# recognises (AGENT_CALLABLE_SELF); an inline copy is correct and invisible, and
# seat_decisions shipped without one for that reason (trinity-enterprise#839).
# The matcher lives in `_route_census.py` so the private submodule's twin runs
# the same one over its own tree.
# --------------------------------------------------------------------------- #
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _route_census as _rc  # noqa: E402

# (path under src/backend, qualname) -> why it keeps its own comparison.
_SELF_GATE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("routers/aauth_person.py", "assert_own_key"): (
        "stricter than assert_own_agent: the agent's OWN key only, so a person is "
        "refused too (403 aauth_agent_only, trinity-enterprise#878)"
    ),
}


def _hand_copies() -> list[tuple[str, str, int]]:
    out = []
    for path in _rc.backend_files():
        rel = path.relative_to(_rc.BACKEND).as_posix()
        for qual, lineno in _rc.hand_copied_self_gates(ast.parse(path.read_text(encoding="utf-8"))):
            out.append((rel, qual, lineno))
    return out


def test_no_hand_copied_agent_self_gate():
    found = _hand_copies()
    offenders = [f"  {rel}:{line} {qual}" for rel, qual, line in found
                 if (rel, qual) not in _SELF_GATE_ALLOWLIST]
    assert not offenders, (
        "Hand-copied agent-key self-gate(s) — call dependencies.assert_own_agent("
        "current_user, <path agent>, detail=...) as the handler's FIRST statement and "
        "list the route in AGENT_CALLABLE_SELF (tests/unit/_route_census.py):\n"
        + "\n".join(offenders)
    )
    stale = set(_SELF_GATE_ALLOWLIST) - {(rel, qual) for rel, qual, _ in found}
    assert not stale, f"stale _SELF_GATE_ALLOWLIST entries: {sorted(stale)}"


_SELF_GATE_SPELLINGS = [
    # the original inline copy
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # reversed operands
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and agent_name != current_user.agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # getattr, bound to a local first (share_agent_file's spelling)
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    actor = getattr(current_user, 'agent_name', None)\n"
    "    if actor and actor != agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # getattr inline
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    if getattr(current_user, 'agent_name', None) and "
    "getattr(current_user, 'agent_name', None) != agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # the predicate the helper uses, copied
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    caller = agent_principal_name(current_user)\n"
    "    if caller is not None and caller != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # `not ==`
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and not current_user.agent_name == name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # a module helper the handler calls (reminders' `_self_gate`)
    "def _self_gate(current_user: User, name: str) -> None:\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # a CurrentUser principal
    "def h(agent_name: str, current_user: CurrentUser):\n"
    "    if current_user.agent_name and current_user.agent_name != agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # an unannotated helper (the house style of many module helpers)
    "def _gate(current_user, name):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # Optional[User], Annotated[User, ...] and string annotations
    "def _gate(current_user: Optional[User], name: str):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    "def h(name: str, current_user: Annotated[User, Depends(get_current_user)]):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    "def _gate(current_user: 'User', name: str):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # `not in (None, name)`
    "def _gate(current_user: User, name: str):\n"
    "    if current_user.agent_name not in (None, name):\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # a factory's exception, and a prebuilt one
    "def _gate(current_user: User, name: str):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise _forbidden()\n",
    "def _gate(current_user: User, name: str):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        raise FORBIDDEN\n",
    # a hand-built 403 response
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        return JSONResponse(status_code=403, content={'detail': 'no'})\n",
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={})\n",
    # the path agent aliased to a local first
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    target = agent_name\n"
    "    if current_user.agent_name and current_user.agent_name != target:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # equal-or-else-refuse
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if not current_user.agent_name or current_user.agent_name == name:\n"
    "        pass\n"
    "    else:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # the agent name as an operand of `or`, and of a conditional expression (review of #3929)
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if (current_user.agent_name or name) != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if (current_user.agent_name if current_user.agent_name else name) != name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
]

_SELF_GATE_BENIGN = [
    # the helper itself, in use
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    assert_own_agent(current_user, name, detail='no')\n",
    # a ROW's agent, not the principal's (execution ownership)
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    execution = db.get_execution('x')\n"
    "    if execution.agent_name != agent_name:\n"
    "        raise HTTPException(status_code=403, detail='not this agent')\n",
    # a header checked against the principal, not the path agent
    "def h(x_source_agent: str, current_user: User = Depends(get_current_user)):\n"
    "    if x_source_agent != current_user.agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # the same comparison deciding a branch, not refusing
    "def h(agent_name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and current_user.agent_name != agent_name:\n"
    "        return None\n",
    # refusing every agent key is a different gate (canvas `_gate_human_only`)
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name:\n"
    "        raise HTTPException(status_code=403, detail='humans only')\n",
    # a body field, not a principal
    "def h(agent_name: str, body: NotificationCreate):\n"
    "    if body.agent_name != agent_name:\n"
    "        raise HTTPException(status_code=403, detail='no')\n",
    # the helper's own shape: an equality that returns, with no refusing `else`
    "def gate(current_user: User, agent_name: str):\n"
    "    caller = agent_principal_name(current_user)\n"
    "    if caller is None or caller == agent_name:\n"
    "        return\n"
    "    raise HTTPException(status_code=403, detail='no')\n",
    # an unannotated parameter that is not named like a principal
    "def h(execution, agent_name):\n"
    "    if execution.agent_name != agent_name:\n"
    "        raise HTTPException(status_code=404, detail='Execution not found')\n",
    # a 200 response is not a refusal
    "def h(name: str, current_user: User = Depends(get_current_user)):\n"
    "    if current_user.agent_name and current_user.agent_name != name:\n"
    "        return JSONResponse(status_code=200, content={'delivered': False})\n",
]


@pytest.mark.parametrize("src", _SELF_GATE_SPELLINGS, ids=[f"spelling-{i}" for i in range(len(_SELF_GATE_SPELLINGS))])
def test_the_self_gate_matcher_finds_every_spelling(src):
    assert _rc.hand_copied_self_gates(ast.parse(src)), "hand-copied self-gate not found"


@pytest.mark.parametrize("src", _SELF_GATE_BENIGN, ids=[f"benign-{i}" for i in range(len(_SELF_GATE_BENIGN))])
def test_the_self_gate_matcher_spares_other_comparisons(src):
    assert _rc.hand_copied_self_gates(ast.parse(src)) == []
