"""
#1483 §7 — every WebSocket-manager setter imported into main.py must actually be
invoked at startup wiring.

The split moved the chat broadcasts (agent_collaboration, self_task,
chat_response_ready) out of routers.chat into chat_execution_service /
chat_persistence_service, each with its own ``set_websocket_manager``. A missed
setter call leaves the module global ``None`` → the broadcast becomes a **silent
no-op**, invisible to the OpenAPI diff and to unit tests that patch the manager.
This static (AST) guard fails if any ``set_*_ws_manager`` / ``set_*_websocket_manager``
alias imported into main.py is never called — catching that exact class.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MAIN = Path(__file__).resolve().parent.parent.parent / "src" / "backend" / "main.py"
_SETTER_RE = re.compile(r"^set_.*(?:ws|websocket)_manager$")


def _imported_ws_setter_aliases(tree) -> set[str]:
    """Names bound in main.py via `from X import Y as <setter>` (or plain
    `import Y`) that look like a WebSocket-manager setter."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if _SETTER_RE.match(bound):
                    aliases.add(bound)
    return aliases


def _called_names(tree) -> set[str]:
    """Every bare-name call target `foo(...)` in main.py."""
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def test_every_imported_ws_setter_is_invoked():
    tree = ast.parse(_MAIN.read_text(), filename=str(_MAIN))
    aliases = _imported_ws_setter_aliases(tree)
    # Sanity: the split's two new setters must be among them.
    assert "set_chat_execution_ws_manager" in aliases, (
        "main.py must import chat_execution_service.set_websocket_manager "
        "(agent_collaboration + self_task broadcasts, #1483 §7)"
    )
    assert "set_chat_persistence_ws_manager" in aliases, (
        "main.py must import chat_persistence_service.set_websocket_manager "
        "(chat_response_ready broadcast, #1483 §7)"
    )

    called = _called_names(tree)
    missing = sorted(a for a in aliases if a not in called)
    assert not missing, (
        f"WebSocket-manager setters imported into main.py but never invoked "
        f"(their broadcasts would be silent no-ops): {missing}"
    )
