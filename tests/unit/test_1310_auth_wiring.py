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
consolidated-404 designs, and a line-level ``# noqa: inv8`` marker for the
deferred ``slack.py`` sites — so NEW inline auth added to slack.py still trips.
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
def test_no_inline_auth_gates_in_routers():
    """No router carries an inline agent/admin gate that a shared helper should
    own — except the allowlisted intentional-404 designs and `# noqa: inv8`
    lines (the deferred slack.py sites)."""
    offenders: dict[str, list[tuple[str, int, str]]] = {}
    for path in sorted(_ROUTERS.glob("*.py")):
        v = find_violations(path.read_text(), path.name)
        if v:
            offenders[path.name] = v
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
