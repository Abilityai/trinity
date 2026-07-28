"""
Regression for trinity-enterprise#128 PR-A — a malformed `credentials:` block
must cost its own template, never the catalog, and must never reach the agent's
`.env` as silent corruption.

Three live bugs on `dev`, all reproduced before the fix:

A1  One template whose `credentials:` is a list / string / null — or whose
    `credentials.mcp_servers` is — raised an uncaught `AttributeError` out of
    `_build_local_template` → `get_local_templates()` → `GET /api/templates`,
    returning **HTTP 500 with an empty catalog**: every other template
    disappeared because one was malformed. The `github:` builder was worse
    still: no `isinstance` guard at all on untrusted repo metadata.

A2  `env_file: "OPENAI_API_KEY"` (an ordinary authoring typo — the list dash
    forgotten) was iterated CHARACTER BY CHARACTER into the generated `.env`,
    producing fifteen single-letter variables, never writing the real
    credential, and raising nothing. No error, no warning, no crash: the agent
    booted and its MCP server failed at first use with nothing pointing at the
    cause.

A3  `credentials.config_files[].path` was joined onto the staging directory and
    opened for write with no normalization, so an absolute path or a `..`
    escape was an arbitrary-file-write primitive — reachable by any
    authenticated user, since `deploy_local_agent_logic` accepts an uploaded
    template archive.

`template_service` is loaded standalone (the `test_data_paths_template_surface.py`
pattern: `importlib.util` + `monkeypatch.setitem(sys.modules, ...)`, the
monkeypatch form so `tests/lint_sys_modules.py` stays green). The crud-side sink
barrier is imported lazily inside the test body (the `test_1793_unknown_local_template.py`
pattern).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_template_service(monkeypatch, fake_templates_dir: Path | None = None):
    if "config" not in sys.modules:
        config_mod = types.ModuleType("config")
        config_mod.DEFAULT_GITHUB_TEMPLATE_REPOS = []
        config_mod.GITHUB_PAT_CREDENTIAL_ID = "test-pat"
        monkeypatch.setitem(sys.modules, "config", config_mod)

    spec = importlib.util.spec_from_file_location(
        "ts_under_test_ent128a", _BACKEND / "services" / "template_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if fake_templates_dir is not None:
        monkeypatch.setattr(module, "_local_templates_dir", lambda: fake_templates_dir)
    return module


def _seed(parent: Path, name: str, body: str) -> Path:
    tdir = parent / name
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "template.yaml").write_text(body)
    return tdir


_VALID = "name: {name}\ndisplay_name: {name}\n"

# Every shape that raised an uncaught AttributeError out of the catalog.
_MALFORMED_BLOCKS = pytest.mark.parametrize(
    "credentials_yaml, expect_error",
    [
        pytest.param("credentials:\n  - GEMINI_API_KEY\n", True, id="block-is-list"),
        pytest.param("credentials: GEMINI_API_KEY\n", True, id="block-is-string"),
        pytest.param("credentials: 7\n", True, id="block-is-scalar"),
        # A bare `credentials:` is a *present* key with a null value, so the
        # `.get("credentials", {})` default never applies.
        pytest.param("credentials:\n", False, id="block-is-null"),
        pytest.param(
            "credentials:\n  mcp_servers:\n    - aistudio\n", True, id="mcp-is-list"
        ),
        pytest.param("credentials:\n  mcp_servers:\n", False, id="mcp-is-null"),
        pytest.param(
            "credentials:\n  mcp_servers: aistudio\n", True, id="mcp-is-string"
        ),
        pytest.param(
            "credentials:\n  env_file: GEMINI_API_KEY\n", True, id="env-is-string"
        ),
        pytest.param("credentials:\n  env_file:\n", False, id="env-is-null"),
        pytest.param(
            "credentials:\n  config_files: oops\n", True, id="config-is-string"
        ),
    ],
)


# ---------------------------------------------------------------------------
# A1 — one malformed template must not empty the catalog
# ---------------------------------------------------------------------------


@_MALFORMED_BLOCKS
def test_malformed_credentials_never_empties_the_catalog(
    tmp_path, monkeypatch, credentials_yaml, expect_error
):
    """The headline bug: 1 malformed + 2 valid templates must list THREE."""
    _seed(tmp_path, "alpha", _VALID.format(name="alpha"))
    _seed(tmp_path, "beta", _VALID.format(name="beta"))
    _seed(tmp_path, "broken", _VALID.format(name="broken") + credentials_yaml)

    ts = _load_template_service(monkeypatch, tmp_path)

    entries = ts.get_local_templates()

    by_id = {e["id"]: e for e in entries}
    assert set(by_id) == {"local:alpha", "local:beta", "local:broken"}
    # The valid siblings are untouched — no collateral error.
    assert by_id["local:alpha"]["credential_errors"] == []
    assert by_id["local:beta"]["credential_errors"] == []

    if expect_error:
        # AC #5: a NAMED error on the offending template, and it still lists.
        errors = by_id["local:broken"]["credential_errors"]
        assert errors, "malformed block must produce a named error"
        assert all(e.startswith("credentials") for e in errors), errors
    else:
        # An absent/null section means "no credentials" — not an error. A
        # template that comments its block out must not acquire a warning.
        assert by_id["local:broken"]["credential_errors"] == []

    # Whatever the shape, the derived field degrades to empty rather than raising.
    assert by_id["local:broken"]["mcp_servers"] == []


def test_valid_credentials_block_still_surfaces_mcp_servers(tmp_path, monkeypatch):
    """The guard must not regress the happy path."""
    _seed(
        tmp_path,
        "cornelius",
        _VALID.format(name="cornelius") + "credentials:\n"
        "  mcp_servers:\n"
        "    aistudio:\n"
        "      env_vars: [GEMINI_API_KEY]\n"
        "    ebook-mcp:\n"
        "      env_vars: [EBOOK_MCP_PATH]\n",
    )
    ts = _load_template_service(monkeypatch, tmp_path)

    entry = ts.get_local_template("local:cornelius")

    assert entry["mcp_servers"] == ["aistudio", "ebook-mcp"]
    assert entry["credential_errors"] == []


def test_unparseable_yaml_drops_only_its_own_template(tmp_path, monkeypatch):
    """RecursionError is a RuntimeError, so it escaped `except yaml.YAMLError`.

    Deeply nested YAML blew straight past the parse handler and out of the
    catalog. Verified: `issubclass(RecursionError, yaml.YAMLError)` is False.
    """
    _seed(tmp_path, "alpha", _VALID.format(name="alpha"))
    depth = sys.getrecursionlimit()
    _seed(tmp_path, "deep", "name: deep\nx: " + "[" * depth + "]" * depth + "\n")

    ts = _load_template_service(monkeypatch, tmp_path)

    entries = ts.get_local_templates()

    assert [e["id"] for e in entries] == ["local:alpha"]


def test_per_template_fence_survives_an_unguarded_field(tmp_path, monkeypatch):
    """Last-resort fence: a raise from the builder costs one template, not all.

    This pins the *property* — one bad template can never empty the catalog —
    independently of which fields happen to be guarded today.
    """
    _seed(tmp_path, "alpha", _VALID.format(name="alpha"))
    _seed(tmp_path, "boom", _VALID.format(name="boom"))

    ts = _load_template_service(monkeypatch, tmp_path)
    real_builder = ts._build_local_template

    def exploding_builder(template_dir: Path):
        if template_dir.name == "boom":
            raise RuntimeError("a future field reached through without a guard")
        return real_builder(template_dir)

    monkeypatch.setattr(ts, "_build_local_template", exploding_builder)

    entries = ts.get_local_templates()

    assert [e["id"] for e in entries] == ["local:alpha"]


@pytest.mark.parametrize("metadata", [[], "a-string", 7, None], ids=repr)
def test_github_builder_tolerates_non_mapping_metadata(monkeypatch, metadata):
    """`_build_local_template` always had an isinstance guard; this path had none.

    `_fetch_template_yaml` returns `yaml.safe_load(content) or {}` — a top-level
    list or scalar is truthy, so a hostile/typo'd repo template.yaml arrives as
    a non-mapping and every `metadata.get()` raised AttributeError.
    """
    if "services.git_service" not in sys.modules:
        git_mod = types.ModuleType("services.git_service")
        git_mod.DEFAULT_PERSISTENT_STATE = []
        services_pkg = sys.modules.get("services") or types.ModuleType("services")
        monkeypatch.setitem(sys.modules, "services", services_pkg)
        monkeypatch.setitem(sys.modules, "services.git_service", git_mod)

    ts = _load_template_service(monkeypatch)

    entry = ts._build_template("owner/repo", metadata)

    assert entry["id"] == "github:owner/repo"
    assert entry["display_name"] == "repo"
    assert entry["mcp_servers"] == []
    assert entry["credential_errors"] == []


# ---------------------------------------------------------------------------
# A2 — a string `env_file` must never be written character by character
# ---------------------------------------------------------------------------


def test_string_env_file_is_a_named_error_not_silent_corruption(monkeypatch):
    """The worst failure class here: wrong output, zero signal."""
    ts = _load_template_service(monkeypatch)
    template = {"credentials": {"env_file": "OPENAI_API_KEY"}}

    with pytest.raises(ts.CredentialDeclarationError) as exc:
        ts.generate_credential_files(template, {"OPENAI_API_KEY": "sk-real"}, "agent")

    message = str(exc.value)
    assert "credentials.env_file" in message
    # problem + cause + fix — the author needs to know what to type.
    assert "list" in message and "- OPENAI_API_KEY" in message


def test_string_env_file_never_produces_character_wise_env_lines(monkeypatch):
    """Pin the exact corrupt output that used to ship, so it cannot come back."""
    ts = _load_template_service(monkeypatch)
    template = {"credentials": {"env_file": "OPENAI_API_KEY"}}

    try:
        files = ts.generate_credential_files(
            template, {"OPENAI_API_KEY": "sk-real"}, "agent"
        )
    except ts.CredentialDeclarationError:
        return  # refused outright — the strongest possible outcome

    env = files.get(".env", "")
    assert "O=\nP=\nE=\nN=" not in env
    for letter in "OPENAI_KY":
        assert f"\n{letter}=" not in env, f"single-character variable {letter}= emitted"


def test_non_string_env_file_entry_is_a_named_error(monkeypatch):
    """A mapping entry used to raise `TypeError: unhashable type: 'dict'`.

    `credentials.env_file` stays a names-only list; the enriched per-variable
    declaration lands under its own top-level key (PR-B), precisely so an older
    Trinity reading a newer template is structurally untouched.
    """
    ts = _load_template_service(monkeypatch)
    template = {"credentials": {"env_file": [{"name": "VAULT_BASE_PATH"}]}}

    with pytest.raises(ts.CredentialDeclarationError) as exc:
        ts.generate_credential_files(template, {}, "agent")

    assert "credentials.env_file[0]" in str(exc.value)


def test_valid_env_file_writes_the_env_in_authored_order(monkeypatch):
    """The happy path is byte-identical to before the guard."""
    ts = _load_template_service(monkeypatch)
    template = {"credentials": {"env_file": ["GEMINI_API_KEY", "VAULT_BASE_PATH"]}}

    files = ts.generate_credential_files(
        template, {"GEMINI_API_KEY": "k", "VAULT_BASE_PATH": "/vault"}, "agent"
    )

    assert files[".env"] == (
        "# Generated by Trinity - Agent credentials\n"
        "\n"
        "GEMINI_API_KEY=k\n"
        "VAULT_BASE_PATH=/vault"
    )


@pytest.mark.parametrize(
    "block",
    [None, {}, {"credentials": None}, {"mcp_servers": {}}],
    ids=["null", "empty", "nested-null", "empty-section"],
)
def test_empty_credentials_declaration_is_never_an_error(monkeypatch, block):
    """Zero credentials is a valid contract — the ent#124 starter trio ships it."""
    ts = _load_template_service(monkeypatch)

    assert ts.credential_shape_errors(block) == []
    assert ts.generate_credential_files({"credentials": block}, {}, "agent") == {}


def test_non_mapping_template_data_generates_no_files(monkeypatch):
    """A garbage template.yaml declares no credentials; it must not AttributeError."""
    ts = _load_template_service(monkeypatch)

    assert ts.generate_credential_files(["not", "a", "mapping"], {}, "agent") == {}


# ---------------------------------------------------------------------------
# A3 — `config_files[].path` is not an arbitrary-file-write primitive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_fragment",
    [
        ("/tmp/absolute-pwned.txt", "absolute path"),
        ("/etc/cron.d/pwned", "absolute path"),
        ("../../../../../tmp/pwned.txt", "'..' segments"),
        ("nested/../../escape.txt", "'..' segments"),
    ],
)
def test_config_file_path_escape_is_rejected(monkeypatch, path, expected_fragment):
    """An absolute path silently wins the join; `..` walks out of it."""
    ts = _load_template_service(monkeypatch)
    template = {"credentials": {"config_files": [{"path": path, "template": "pwned"}]}}

    with pytest.raises(ts.CredentialDeclarationError) as exc:
        ts.generate_credential_files(template, {}, "agent")

    assert expected_fragment in str(exc.value)


def test_relative_config_file_path_still_works(monkeypatch):
    ts = _load_template_service(monkeypatch)
    template = {
        "credentials": {
            "config_files": [{"path": ".config/app.conf", "template": "key={K}"}]
        }
    }

    files = ts.generate_credential_files(template, {"K": "v"}, "agent")

    assert files[".config/app.conf"] == "key=v"


def test_crud_sink_rejects_an_escaping_credential_file_path(tmp_path):
    """Defense in depth: the barrier at the write sink, not only at the parse.

    Validate-at-boundary AND at-sink — the boundary check is one refactor away
    from being bypassed, and this is the call that actually opens the file.
    """
    from fastapi import HTTPException

    from services.agent_service import crud

    root = tmp_path / "agent-x-creds"
    root.mkdir()

    assert crud._safe_cred_file_path(".env", root) == (root / ".env").resolve()

    assert crud._safe_cred_file_path(".config/app.conf", root) == (
        root / ".config/app.conf"
    ).resolve()

    # Step 1 (allowlist on the raw string) and step 2 (resolve + containment).
    # Step 1 is what CodeQL recognises as a py/path-injection barrier — without
    # it this helper was flagged high-severity twice.
    hostile = (
        "/tmp/pwned.txt",          # absolute — silently wins the join
        "../../pwned.txt",         # traversal
        "nested/../../escape.txt",  # traversal mid-path
        "",                        # empty
        "-leading-dash",           # outside the allowlist's first-char class
        "has space.txt",           # outside the allowlist
        "semi;colon.txt",          # shell-ish punctuation
    )
    for path in hostile:
        with pytest.raises(HTTPException) as exc:
            crud._safe_cred_file_path(path, root)
        assert exc.value.status_code == 400, path
        assert exc.value.detail["code"] == "INVALID_CREDENTIAL_FILE_PATH", path


# ---------------------------------------------------------------------------
# Parity — the shipped bundle must satisfy its own contract
# ---------------------------------------------------------------------------


def test_every_bundled_template_declares_a_valid_credentials_block(monkeypatch):
    """A malformed bundled template fails CI, not a user's fresh install."""
    ts = _load_template_service(monkeypatch)
    bundle = (
        Path(__file__).resolve().parent.parent.parent / "config" / "agent-templates"
    )
    assert bundle.is_dir(), bundle

    import yaml

    offenders = {}
    for template_yaml in sorted(bundle.glob("*/template.yaml")):
        data = yaml.safe_load(template_yaml.read_text()) or {}
        errors = ts.credential_shape_errors(
            data.get("credentials") if isinstance(data, dict) else data
        )
        if errors:
            offenders[template_yaml.parent.name] = errors

    assert offenders == {}
