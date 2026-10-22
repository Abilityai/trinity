"""
Facilitator wording (trinity-enterprise#932 AC, carried by ent#966).

Every message a person can read from the card-delegation surface names
Nevermined, and none reads as Trinity holding a card or settling money.

Two layers: the refusals are raised for real and their messages checked, and
every `DelegationError` message in the module set is read by AST so a refusal
added later is covered without someone remembering to add a case here (the
live consumer of those literals is the API response body).
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
MODULES = (
    BACKEND / "routers" / "nevermined_delegation.py",
    BACKEND / "services" / "nevermined_delegation_service.py",
)
FORBIDDEN = re.compile(
    r"trinity\s+(charges|holds|stores|keeps|saves)\s+(the|your|a)\s+card"
    r"|trinity\s+settles"
    r"|trinity\s+(is|acts as)\s+(the\s+)?merchant",
    re.IGNORECASE,
)


def _string_constants(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _error_messages(path):
    """The message argument of every DelegationError(...) call, literal parts joined."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        # DelegationError(status, code, message) and the `_rejected(status, message)`
        # helper that forwards its message into one.
        position = {"DelegationError": 2, "_rejected": 1}.get(name)
        if position is None or len(node.args) <= position:
            continue
        arg = node.args[position]
        if isinstance(arg, ast.Name):
            continue  # a forwarded parameter — its literal is read at the helper's call sites
        parts = [
            n.value for n in ast.walk(arg)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        yield node.lineno, "".join(parts)


def test_no_string_says_trinity_holds_a_card_or_settles():
    for path in MODULES:
        for s in _string_constants(path):
            assert not FORBIDDEN.search(s), f"{path.name}: {s!r}"


def test_every_refusal_names_nevermined():
    found = 0
    for path in MODULES:
        for lineno, message in _error_messages(path):
            found += 1
            assert "Nevermined" in message, f"{path.name}:{lineno}: {message!r}"
    assert found >= 4  # the guard is not vacuous


def test_raised_refusals_name_nevermined(monkeypatch):
    """Executed: the refusals a person actually receives."""
    from services import nevermined_delegation_service as svc

    monkeypatch.setattr(svc.db, "get_nevermined_organization", lambda: None)
    monkeypatch.setattr(svc.db, "count_active_agent_delegations", lambda: 2)
    errors = []
    for call in (
        lambda: svc.save_organization("o", "live", "w", "sandbox:k", "ada"),
        lambda: svc.save_organization("o", "sandbox", None, None, "ada"),
        lambda: svc.delete_organization(),
    ):
        with pytest.raises(svc.DelegationError) as exc:
            call()
        errors.append(exc.value)
    errors.extend([svc._not_configured(), svc._busy(), svc._unreachable()])
    for err in errors:
        assert "Nevermined" in err.message, err.code
        assert not FORBIDDEN.search(err.message)
