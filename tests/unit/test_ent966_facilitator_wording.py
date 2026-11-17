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

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
MODULES = (
    BACKEND / "routers" / "nevermined_delegation.py",
    BACKEND / "services" / "nevermined_delegation_service.py",
)
#: Text a person reads that is not Python: the MCP tool descriptions and
#: refusals (their live consumer is the MCP client / the model) and the two
#: docs pages that describe the card-delegation surface.
TEXT_SURFACES = (
    REPO / "src" / "mcp-server" / "src" / "tools" / "nevermined.ts",
    REPO / "docs" / "memory" / "feature-flows" / "nevermined-payments.md",
    REPO / "docs" / "user-docs" / "integrations" / "nevermined-payments.md",
    # The ent#967 spending panel and organization form: the copy constants and both SFCs.
    REPO / "src" / "frontend" / "src" / "utils" / "neverminedSpending.js",
    REPO / "src" / "frontend" / "src" / "components" / "NeverminedSpendingPanel.vue",
    REPO / "src" / "frontend" / "src" / "components" / "settings" / "NeverminedOrganizationPanel.vue",
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


@pytest.mark.parametrize("path", TEXT_SURFACES, ids=lambda p: p.name if p.suffix == ".ts" else "/".join(p.parts[-2:]))
def test_mcp_tools_and_docs_never_say_trinity_holds_a_card_or_settles(path):
    text = " ".join(path.read_text().split())  # a phrase wrapped across lines still matches
    hit = FORBIDDEN.search(text)
    assert hit is None, f"{path.name}: {hit.group(0)!r}"


def test_text_surfaces_describe_the_card_mandate_as_nevermined_held():
    """Not vacuous: each surface does describe the card mandate, by Nevermined's name."""
    for path in TEXT_SURFACES:
        text = " ".join(path.read_text().split())
        assert re.search(r"Nevermined card mandate|Nevermined holds the card", text), path.name


def test_the_text_scan_catches_the_forbidden_phrasing():
    """The pattern the surfaces are scanned with does match the ent#932 phrasings."""
    for bad in ("Trinity charges your card", "Trinity holds the card", "Trinity stores your card",
                "Trinity settles every purchase"):
        assert FORBIDDEN.search(" ".join(bad.split()))
