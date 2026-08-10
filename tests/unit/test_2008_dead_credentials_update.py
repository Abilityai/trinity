"""#2008 — `POST /api/credentials/update` was documented but unreachable.

The endpoint had **no callers**: not the backend, not the MCP server, not
`startup.sh`, not the frontend, not the enterprise submodule. Two live places
treated it as the sanctioned path anyway:

  * `agent_server/routers/files.py` blocked BOTH direct-edit paths for
    `.mcp.json` on the strength of a sentence pointing at it —  so the
    documented escape hatch for configuring MCP servers did not exist;
  * `docs/memory/architecture.md` listed it as a live agent endpoint.

It also mattered if anyone re-wired it: the renderer was a whole-text
`str.replace` over `.mcp.json.template`, so unlike every other path it **did**
substitute into `command`, and nothing on that path ran `validate_mcp_config`.
A credential value becoming the executed command is the RCE-by-config class
#590 closed. It was inert only because nothing called it — while the security
comment above actively invited someone to.

Resolved by deletion, because #2007 put the `.mcp.json.template` renderer where
the files actually are (in-container at startup, `env`-only, validated
per-server). The AC's "a test that fails if a `${VAR}` can reach `command`
through this path" is therefore expressed as: the path is gone, and no live
document sends anyone back to it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"
_CREDENTIALS = _AGENT_SERVER / "routers" / "credentials.py"
_FILES = _AGENT_SERVER / "routers" / "files.py"
_MODELS = _AGENT_SERVER / "models.py"

pytestmark = pytest.mark.unit


class TestTheEndpointIsGone:

    def test_no_route_registers_credentials_update(self):
        """AST, not a substring scan: a `@router.post("/api/credentials/update")`
        is what makes it reachable, and a mention of the string in a comment is
        not (this file, and the corrected `files.py` comment, both contain it)."""
        tree = ast.parse(_CREDENTIALS.read_text())
        routes = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                for arg in dec.args:
                    if isinstance(arg, ast.Constant) and arg.value == "/api/credentials/update":
                        routes.append(node.name)
        assert not routes, (
            f"/api/credentials/update is registered again by {routes} (#2008). "
            "It substituted ${VAR} into `command` with no validate_mcp_config — "
            "the #590 RCE-by-config class. If it must come back, it needs the "
            "validator and a refusal for placeholders in `command`."
        )

    def test_its_request_model_is_gone_too(self):
        """A dangling model is how a deleted route quietly comes back."""
        assert "class CredentialUpdateRequest" not in _MODELS.read_text()

    def test_the_whole_text_replace_renderer_is_gone(self):
        """The specific mechanism: a whole-text `str.replace` over the template
        substitutes anywhere, including `command`."""
        src = _CREDENTIALS.read_text()
        assert "mcp_template.read_text()" not in src
        assert "generated_content.replace(placeholder" not in src


class TestNoLiveDocSendsYouThere:
    """AC #2 and #3: the two places that named a path which did not exist."""

    def test_files_py_names_a_path_that_exists(self):
        """The rationale must point at a route that runs today.

        The first draft was `"/api/credentials/inject" in block or
        ".mcp.json.template" in block`. `PROTECTED_PATHS` contains the literal
        `.mcp.json.template` and sits well inside the 1500-character window, so
        the `or` was satisfied by the path list no matter what the comment
        said — deleting the whole rationale, or rewording it to point back at
        the deleted endpoint, both left this green. It now asserts what the AC
        actually claims: the sanctioned path is named, and the deleted route is
        not offered as a destination.
        """
        src = _FILES.read_text()
        # Anchored on the rationale block itself, not a fixed byte window: a
        # 1500-character lookbehind both reaches into `PROTECTED_PATHS` (which
        # is what made the old `or` branch always true) and silently drops the
        # top of the comment as soon as it grows.
        start = src.index("# Paths that cannot be edited via the file-write endpoint.")
        block = src[start:src.index("EDIT_PROTECTED_PATHS = [")]
        # The backend route is `/api/agents/{name}/credentials/inject`; the
        # first draft asserted `/api/credentials/inject`, which is not a path
        # that exists anywhere — and it went unnoticed because the `or` branch
        # carried the test.
        assert "credentials/inject" in block, (
            "the EDIT_PROTECTED_PATHS rationale must name a real path — both "
            "direct-edit blocks rest on it"
        )
        # The one permitted mention is the historical note saying it was
        # removed; anything else is the comment sending an owner to a route
        # that does not exist.
        without_history = block.replace(
            "`/api/credentials/update`, which had no callers anywhere and was removed", ""
        )
        assert "/api/credentials/update" not in without_history, (
            "the rationale points at /api/credentials/update as a live path "
            "again — it was deleted in #2008"
        )
        assert "platform-internal\n# /api/credentials/update flow" not in src

    def test_architecture_md_does_not_list_it_as_live(self):
        arch = (_ROOT / "docs" / "memory" / "architecture.md").read_text()
        assert "`/api/credentials/update` - Hot-reload credentials" not in arch

    @pytest.mark.parametrize("doc", [
        "docs/testing/API_TEST_REQUIREMENTS.md",
        "docs/diagrams/03-agent-container.md",
    ])
    def test_no_live_doc_advertises_the_endpoint(self, doc):
        """Historical records (`docs/archive/**`, `docs/security-reports/**`,
        the 2026-06 meta-analysis) are deliberately NOT in scope: they are
        point-in-time and were true when written."""
        text = (_ROOT / doc).read_text()
        assert "POST /api/credentials/update" not in text
