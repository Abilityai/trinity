"""
Regression test for #568.

`/api/files/{id}` previously gated downloads behind a `session_token`
check when the owning agent had `require_email=true`. Since
`build_download_url` never appended a `session_token` (and the agent
has no way to learn the recipient's value), file sharing was
permanently broken for those agents.

The 192-bit `sig` token minted at share time is the sole auth
credential. This test pins the post-fix structure of `routers/files.py`
via AST so we don't accidentally re-introduce the gate later.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


_FILES_PY = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "backend" / "routers" / "files.py"
)


@pytest.fixture(scope="module")
def files_ast() -> ast.Module:
    return ast.parse(_FILES_PY.read_text())


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in {_FILES_PY}")


def test_validate_download_request_has_no_session_token_param(files_ast):
    fn = _function(files_ast, "_validate_download_request")
    arg_names = [a.arg for a in fn.args.args]
    assert "session_token" not in arg_names, (
        "_validate_download_request must not accept session_token (#568)"
    )
    assert arg_names == [
        "file_id", "request", "sig", "download_token_alias",
    ], f"unexpected signature: {arg_names}"


def test_get_endpoint_has_no_session_token_param(files_ast):
    fn = _function(files_ast, "download_shared_file")
    arg_names = [a.arg for a in fn.args.args]
    assert "session_token" not in arg_names, (
        "GET /api/files/{id} must not accept session_token (#568)"
    )


def test_head_endpoint_has_no_session_token_param(files_ast):
    fn = _function(files_ast, "head_shared_file")
    arg_names = [a.arg for a in fn.args.args]
    assert "session_token" not in arg_names


def test_does_not_import_agent_requires_email(files_ast):
    """The require_email gate is gone; the import must be too."""
    for node in ast.walk(files_ast):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # Both spellings: #1028 renamed the canonical function to
                # `agent_requires_email` on `public_chat_service`, and a guard
                # that only knows the retired alias would wave the re-import
                # through.
                assert alias.name not in {"_agent_requires_email", "agent_requires_email"}, (
                    f"remove {alias.name} import — gate was deleted (#568)"
                )


def test_validate_does_not_call_session_or_require_email(files_ast):
    """No call to validate_agent_session or _agent_requires_email anywhere."""
    fn = _function(files_ast, "_validate_download_request")
    banned = {"_agent_requires_email", "agent_requires_email", "validate_agent_session"}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            assert name not in banned, (
                f"{name} call must not appear in _validate_download_request (#568)"
            )
