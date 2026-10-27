"""trinity-enterprise#858 — `/ws/events` applies the #995 account-state check
every other door applies.

`get_current_user` refuses a key whose owner is suspended, and so does `/ws`
(`resolve_ws_identity`). `/ws/events` authenticates its MCP key itself and never
runs `get_current_user`, so the same check sits in the handler, on the owner row
it resolves, before `accept()`.

The owner check is executed here (`dependencies.owner_is_suspended`); the
placement is a shape check, because `main.py` is not importable in the unit
island — the same split as the ent#837 owner floor in
`test_837_workspace_only_floor.py`.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"


@pytest.mark.parametrize("row,refused", [
    ({"suspended_at": "2026-10-20T10:00:00Z", "role": "admin"}, True),
    ({"suspended_at": "2026-10-20T10:00:00Z", "role": "operator"}, True),
    ({"suspended_at": None, "role": "admin"}, False),
    ({"suspended_at": "", "role": "admin"}, False),
    ({"role": "operator"}, False),
    (None, False),   # unresolvable owner: owner_is_workspace_only refuses it
])
def test_the_owner_check(row, refused):
    import dependencies as deps

    assert deps.owner_is_suspended(row) is refused


def _handler():
    tree = ast.parse((BACKEND / "main.py").read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "websocket_events_endpoint")


def _closes(node):
    """(code, reason) of every `websocket.close(...)` inside `node`'s body."""
    out = []
    for c in ast.walk(ast.Module(body=node.body, type_ignores=[])):
        if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "close":
            kw = {k.arg: getattr(k.value, "value", None) for k in c.keywords}
            out.append((kw.get("code"), kw.get("reason")))
    return out


def _guard_calling(fn, name):
    """The `if <name>(user_data): ...` guard in `fn`, or None."""
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and any(
            isinstance(c, ast.Call)
            and getattr(c.func, "id", getattr(c.func, "attr", None)) == name
            and len(c.args) == 1 and isinstance(c.args[0], ast.Name) and c.args[0].id == "user_data"
            for c in ast.walk(node.test)
        ):
            return node
    return None


def test_the_event_stream_refuses_a_suspended_owner_before_accepting():
    fn = _handler()
    accept_line = min(n.lineno for n in ast.walk(fn)
                      if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "accept")
    owner_lookup = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "user_data" for t in n.targets))
    guard = _guard_calling(fn, "owner_is_suspended")
    assert guard is not None, "websocket_events_endpoint must check owner_is_suspended(user_data)"
    assert owner_lookup.lineno < guard.lineno < accept_line
    assert any(isinstance(s, ast.Return) for s in guard.body)
    assert _closes(guard), "the suspended-owner guard must close the socket"


def test_the_refusal_reads_exactly_like_an_inactive_key():
    """No oracle: a suspended owner's key gets the same close as a dead key."""
    fn = _handler()
    inactive = next(
        node for node in ast.walk(fn)
        if isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not) and getattr(node.test.operand, "id", None) == "key_info"
    )
    assert _closes(_guard_calling(fn, "owner_is_suspended")) == _closes(inactive)
