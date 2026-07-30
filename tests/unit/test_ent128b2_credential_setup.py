"""ent#128 PR-B — the credential declaration standard.

Covers the four defects PR-A deferred here plus the `credential_setup:`
declaration standard itself:

  * `crud._resolve_local_template` reaching straight through `credentials:`
    (a malformed block silently cost the agent its `runtime:` and
    `shared_folders:` config too);
  * the same uncaught crash on the agent image's `GET /api/template/info`,
    with a BEHAVIOURAL parity guard over the duplicated accessor (W10 — no
    parity test covered `info.py`, so the two copies could diverge freely);
  * MCP-server precedence (`credentials:` outranked the template's own
    `mcp_servers:`, Defect D) and the dead `required_credentials` badge
    (Defect C / W6 — `platform_injected` vars must not be counted);
  * `normalize_credential_requirements()` — base-set-plus-overlay, never
    raises, never mutates its input.

Harness notes:
  * `services.agent_service.crud` is imported lazily inside each test body —
    same rationale as `test_1793_unknown_local_template.py`: importing the crud
    chain at collection time trips the documented tests/utils-shadows-
    backend-utils `sys.modules` race.
  * Local-template roots are monkeypatched via the module attribute, so no
    `sys.modules[...] =` assignment is introduced (keeps
    `tests/lint_sys_modules.py` green).

Target: src/backend/services/template_service.py,
        src/backend/services/agent_service/crud.py,
        docker/base-image/agent_server/routers/info.py
Issue:  Abilityai/trinity-enterprise#128 (AC #1-4)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent128b2.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


# ===========================================================================
# Defect 2 — crud.py's raw reach-through through `credentials:`
# ===========================================================================
#
# The reach-through sat FIRST in a run of `config` mutations wrapped in one
# broad `except Exception`, so an AttributeError there skipped every mutation
# AFTER it. A malformed `credentials:` therefore cost the agent its `runtime:`
# and `shared_folders:` config as well — three unrelated features lost to one
# bad key, with only a WARNING to show for it.


def _config(template: str):
    from models import AgentConfig

    return AgentConfig(name="t-ent128b", template=template)


def _write_template(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(body)


def _patch_roots(monkeypatch, curated: Path, deployed: Path) -> None:
    from services.agent_service import crud

    monkeypatch.setattr(
        crud, "_LOCAL_TEMPLATE_ROOTS", (curated.resolve(), deployed.resolve())
    )


_MALFORMED_CREDENTIALS = [
    pytest.param('credentials: "OPENAI_API_KEY"', id="string"),
    pytest.param("credentials:", id="null"),
    pytest.param("credentials:\n  - OPENAI_API_KEY", id="list"),
    pytest.param("credentials:\n  mcp_servers: nope", id="mcp_servers-string"),
    pytest.param(
        "credentials:\n  mcp_servers:\n    - a\n    - b", id="mcp_servers-list"
    ),
]


@pytest.mark.parametrize("credentials_block", _MALFORMED_CREDENTIALS)
def test_malformed_credentials_does_not_cost_runtime_and_shared_folders(
    monkeypatch, tmp_path, credentials_block
):
    """The mutations AFTER the credentials read must still be applied."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    _write_template(
        curated,
        "cred-shape",
        "name: cred-shape\n"
        "type: research-agent\n"
        "resources:\n  cpu: '4'\n  memory: '8g'\n"
        f"{credentials_block}\n"
        "runtime:\n  type: codex\n  model: gpt-5.5\n"
        "shared_folders:\n  expose: true\n  consume: false\n",
    )
    _patch_roots(monkeypatch, curated, deployed)

    config = _config("local:cred-shape")
    template_data, shared_folders = crud._resolve_local_template(config)

    # The template still resolves and the agent is still created...
    assert template_data["name"] == "cred-shape"
    # ...and the two settings that used to be collateral damage survive.
    assert config.runtime == "codex", "runtime: lost to the credentials reach-through"
    assert config.runtime_model == "gpt-5.5"
    assert shared_folders == {
        "expose": True,
        "consume": False,
    }, "shared_folders: lost to the credentials reach-through"


def test_wellformed_credentials_still_drive_mcp_servers(monkeypatch, tmp_path):
    """The happy path is unchanged — declared servers still reach `config`."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    _write_template(
        curated,
        "cred-ok",
        "name: cred-ok\n"
        "resources:\n  cpu: '2'\n  memory: '4g'\n"
        "credentials:\n"
        "  mcp_servers:\n"
        "    stripe:\n      env_vars: [STRIPE_API_KEY]\n"
        "    linear:\n      env_vars: [LINEAR_API_KEY]\n",
    )
    _patch_roots(monkeypatch, curated, deployed)

    config = _config("local:cred-ok")
    crud._resolve_local_template(config)

    assert sorted(config.mcp_servers) == ["linear", "stripe"]


# ===========================================================================
# Defect 1 — the agent image's `GET /api/template/info`, and its parity guard
# ===========================================================================
#
# W10: the agent server cannot import `src/backend`, so the tolerant reader is
# DUPLICATED into the base image. The two in-repo precedents for that
# (`credential_paths.py`, `model_context.py`) are both vendored byte-identically
# WITH a parity test; a 6-line reader does not earn a whole vendored module, but
# it does earn the same guard — before ent#128 no parity test covered
# `agent_server/routers/info.py` at all, so a fix on one side could silently not
# reach the other. This is the `test_1713_scheduler_utils_parity.py` shape: one
# shared table of malformed shapes driven through BOTH implementations, asserting
# they agree on OUTPUT (the copies are textually divergent by design — the agent
# copy folds `_credentials_mapping` inline — so a source diff cannot verify them).

# (block, expected) — every shape a hostile or slipshod template.yaml can present.
_CREDENTIAL_SHAPE_TABLE = [
    (None, []),
    ({}, []),
    ([], []),
    ("OPENAI_API_KEY", []),
    (0, []),
    (True, []),
    ({"env_file": ["A"]}, []),
    ({"mcp_servers": None}, []),
    ({"mcp_servers": "stripe"}, []),
    ({"mcp_servers": ["stripe", "linear"]}, []),
    ({"mcp_servers": 7}, []),
    ({"mcp_servers": {}}, []),
    ({"mcp_servers": {"stripe": {"env_vars": ["STRIPE_API_KEY"]}}}, ["stripe"]),
    ({"mcp_servers": {"stripe": None}}, ["stripe"]),
    ({"mcp_servers": {"stripe": "nope"}}, ["stripe"]),
    ({"mcp_servers": {"a": {}, "b": {}}}, ["a", "b"]),
    # A non-string key is a legal YAML mapping key; both sides must stringify.
    ({"mcp_servers": {1: {}}}, ["1"]),
]


def _agent_side_reader():
    """Load the base-image reader without executing `agent_server/__init__.py`.

    `tests/unit/conftest.py` already registers `docker/base-image/agent_server`
    as a namespace package, so a plain import resolves to the real base-image
    file (not the `tests/agent_server/` helper package) — no
    `sys.modules[...] =` assignment, so `tests/lint_sys_modules.py` stays green.
    """
    from agent_server.routers.info import _credential_mcp_server_names

    return _credential_mcp_server_names


@pytest.mark.parametrize("block,expected", _CREDENTIAL_SHAPE_TABLE)
def test_agent_and_backend_credential_readers_agree(block, expected):
    """The duplicated accessor must never diverge from the backend canonical."""
    from services.template_service import credential_mcp_server_names as backend

    agent = _agent_side_reader()

    assert sorted(backend(block)) == expected
    assert sorted(agent(block)) == expected, (
        "the agent-image copy diverged from services.template_service — "
        "fix both or the Info tab and the catalog will disagree"
    )


def test_template_info_endpoint_survives_a_malformed_credentials_block(
    monkeypatch, tmp_path
):
    """`GET /api/template/info` must still answer, not 500.

    The endpoint's own `try/except` wraps only the YAML load, so the reach-through
    crash escaped it entirely.
    """
    import asyncio

    from agent_server.routers import info

    workspace = tmp_path / "template.yaml"
    workspace.write_text(
        "name: broken\n"
        "display_name: Broken Agent\n"
        'credentials: "OPENAI_API_KEY"\n'
    )
    monkeypatch.setattr(info, "get_template_path", lambda: workspace)

    payload = asyncio.run(info.get_template_info())

    assert payload["has_template"] is True
    assert payload["name"] == "broken"
    # Degraded to empty, not crashed.
    assert payload["mcp_servers"] == []


# ===========================================================================
# Defect D + Defect C / W6 — MCP precedence and the credentials badge
# ===========================================================================

def _local_entry(tmp_path, body: str, name: str = "fixture") -> dict:
    from services import template_service as ts

    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(body)
    return ts._build_local_template(d)


_HEAD = "name: fixture\nresources:\n  cpu: '2'\n  memory: '4g'\n"


def test_templates_own_mcp_servers_outranks_the_credentials_block(tmp_path):
    """Defect D: the operands were the other way round. Must fail on baseline.

    `agent_server/routers/info.py` has always read them in this order, so the
    catalog and the agent's own Info tab disagreed for any template declaring both.
    """
    entry = _local_entry(
        tmp_path,
        _HEAD
        + "mcp_servers: [mermaid-diagram, aistudio, ebook-mcp]\n"
        + "credentials:\n  mcp_servers:\n    aistudio:\n      env_vars: [AISTUDIO_KEY]\n",
    )
    assert entry["mcp_servers"] == ["mermaid-diagram", "aistudio", "ebook-mcp"]


def test_credentials_block_is_still_the_fallback(tmp_path):
    """Flipping precedence must not delete the legacy path."""
    entry = _local_entry(
        tmp_path,
        _HEAD + "credentials:\n  mcp_servers:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n",
    )
    assert entry["mcp_servers"] == ["stripe"]


def test_github_builder_gains_the_same_fallback():
    """W14: the GitHub builder had NO credentials fallback, so all three
    surfaces disagreed. Must fail on baseline."""
    from services import template_service as ts

    entry = ts._build_template(
        "Owner/repo",
        {"credentials": {"mcp_servers": {"stripe": {"env_vars": ["STRIPE_API_KEY"]}}}},
    )
    assert entry["mcp_servers"] == ["stripe"]


def test_badge_counts_declared_credentials(tmp_path):
    """Defect C: `required_credentials` read a top-level key NO template defines,
    so the catalog badge rendered 0 for everything. Must fail on baseline."""
    entry = _local_entry(
        tmp_path,
        _HEAD
        + "credentials:\n"
        + "  mcp_servers:\n    stripe:\n      env_vars: [STRIPE_API_KEY]\n"
        + "  env_file: [VAULT_BASE_PATH]\n",
    )
    assert entry["required_credentials"] == ["STRIPE_API_KEY", "VAULT_BASE_PATH"]


def test_badge_excludes_platform_injected_vars(tmp_path):
    """W6/A1: the badge means "credentials you must supply".

    Fixture is the seeded-cornelius shape — 5 declared, 3 platform-injected, so an
    operator supplies 2. Before this, the badge said 5.
    """
    entry = _local_entry(
        tmp_path,
        _HEAD
        + "credentials:\n"
        + "  mcp_servers:\n"
        + "    google:\n      env_vars: [GEMINI_API_KEY]\n"
        + "    trinity:\n      env_vars: [TRINITY_MCP_API_KEY]\n"
        + "  env_file: [GITHUB_PAT, VAULT_BASE_PATH, EBOOK_MCP_PATH]\n",
    )
    assert entry["required_credentials"] == ["VAULT_BASE_PATH", "EBOOK_MCP_PATH"]


def test_badge_is_empty_for_a_zero_credential_template(tmp_path):
    """An explicit `credentials: {}` means "needs nothing", not "unknown"."""
    assert _local_entry(tmp_path, _HEAD + "credentials: {}\n")["required_credentials"] == []
    assert _local_entry(tmp_path, _HEAD)["required_credentials"] == []


@pytest.mark.parametrize("block", ['credentials: "OOPS"', "credentials:\n  - A", "credentials:"])
def test_badge_degrades_on_a_malformed_block(tmp_path, block):
    """A malformed declaration costs the badge, never the catalog entry."""
    entry = _local_entry(tmp_path, _HEAD + block + "\n")
    assert entry["required_credentials"] == []
    assert entry["mcp_servers"] == []


# ===========================================================================
# AC #1-2 — `normalize_credential_requirements()`
# ===========================================================================

def _norm(data, trust="bundled"):
    from services.template_service import normalize_credential_requirements

    return normalize_credential_requirements(data, source_trust=trust)


_DECLARED = {
    "credentials": {
        "mcp_servers": {"stripe": {"env_vars": ["STRIPE_API_KEY"]}},
        "env_file": ["VAULT_BASE_PATH"],
    }
}


def test_base_set_is_derived_from_credentials_alone():
    """One record per declared variable, with `template:`-prefixed provenance."""
    records, errors = _norm(_DECLARED)
    assert errors == []
    assert [r["name"] for r in records] == ["STRIPE_API_KEY", "VAULT_BASE_PATH"]
    assert [r["source"] for r in records] == ["template:mcp:stripe", "template:env_file"]


def test_legacy_names_only_records_carry_no_authorial_intent():
    """`required == "unknown"` is the tri-state AND the enriched discriminator.

    A bare `- FOO` says nothing about whether an operator must supply it, so
    reading it as `True` makes a guided checklist cry wolf. It must never be `True`.
    """
    records, _ = _norm(_DECLARED)
    for record in records:
        assert record["required"] == "unknown"
        assert record["secret"] is True, "secret must default fail-safe"
        assert record["title"] == record["name"]
        assert record["description"] is None


def test_credential_setup_decorates_a_declared_variable():
    data = {
        **_DECLARED,
        "credential_setup": [
            {
                "name": "STRIPE_API_KEY",
                "title": "Stripe secret key",
                "description": "Powers the stripe MCP server.",
                "required": True,
                "secret": True,
                "setup_url": "https://dashboard.stripe.com/apikeys",
            },
            {
                "name": "VAULT_BASE_PATH",
                "title": "Obsidian vault path",
                "required": False,
                "secret": False,
                "format": "filepath",
                "default": "./Brain",
            },
        ],
    }
    records, errors = _norm(data)
    assert errors == []
    stripe, vault = records
    assert stripe["title"] == "Stripe secret key"
    assert stripe["required"] is True
    assert stripe["setup_url"] == "https://dashboard.stripe.com/apikeys"
    assert vault["required"] is False
    assert vault["secret"] is False
    assert vault["format"] == "filepath"
    assert vault["default"] == "./Brain"


def test_enriched_but_omitted_required_defaults_to_true():
    """An author who described a variable and left `required` off meant it."""
    data = {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "title": "K"}]}
    records, errors = _norm(data)
    assert errors == []
    assert records[0]["required"] is True
    # The un-decorated sibling keeps the tri-state.
    assert records[1]["required"] == "unknown"


def test_setup_cannot_declare_only_decorate():
    """The mandatory cross-reference — the condition that makes a sibling key safe.

    An entry naming nothing in `credentials:` is a NAMED error and is dropped;
    valid siblings survive. Drift is impossible by construction.
    """
    data = {
        **_DECLARED,
        "credential_setup": [
            {"name": "SMUGGLED_KEY", "title": "not declared anywhere"},
            {"name": "STRIPE_API_KEY", "title": "fine"},
        ],
    }
    records, errors = _norm(data)
    assert [r["name"] for r in records] == ["STRIPE_API_KEY", "VAULT_BASE_PATH"]
    assert len(errors) == 1
    # Problem, cause and FIX — the no-drift guarantee rests on the author
    # understanding that this key decorates.
    assert "SMUGGLED_KEY" in errors[0]
    assert "not declared in `credentials:`" in errors[0]
    assert "does not declare them" in errors[0]
    assert records[0]["title"] == "fine", "a valid sibling must survive"


@pytest.mark.parametrize(
    "block", [None, {}, {"credentials": None}, {"credentials": {}}, "nope", [], 7]
)
def test_absent_or_empty_means_zero_credentials_not_an_error(block):
    records, errors = _norm(block)
    assert records == []
    assert errors == []


def test_absent_credential_setup_is_not_an_error():
    records, errors = _norm(_DECLARED)
    assert len(records) == 2 and errors == []


def test_platform_injected_is_flagged_on_the_record():
    """A2: a consumer needs to know which rows an operator cannot fill."""
    data = {"credentials": {"env_file": ["GEMINI_API_KEY", "VAULT_BASE_PATH"]}}
    records, _ = _norm(data)
    assert records[0]["platform_injected"] is True
    assert records[1]["platform_injected"] is False


# --- error table (§4) -------------------------------------------------------

@pytest.mark.parametrize(
    "setup,fragment",
    [
        ({"a": 1}, "expected a list of variable descriptors, got mapping"),
        (["bare string"], "expected a mapping with a 'name' key, got string"),
        ([{"title": "no name"}], "missing required key 'name'"),
        ([{"name": "my-key"}], "invalid variable name"),
        ([{"name": {"K": "v"}}], "expected a variable name (string), got mapping"),
        ([{"name": "STRIPE_API_KEY", "required": "yes"}], "expected true or false"),
        ([{"name": "STRIPE_API_KEY", "secret": 1}], "expected true or false"),
        ([{"name": "STRIPE_API_KEY", "format": "token"}], "unknown format 'token'"),
        ([{"name": "STRIPE_API_KEY", "title": ["a"]}], "title: expected a string"),
        ([{"name": "STRIPE_API_KEY", "description": 5}], "description: expected a string"),
        ([{"name": "STRIPE_API_KEY", "default": {}}], "default: expected a string"),
        ([{"name": "STRIPE_API_KEY", "is_required": True}], "unknown key 'is_required'"),
    ],
)
def test_named_error_per_malformed_descriptor(setup, fragment):
    _records, errors = _norm({**_DECLARED, "credential_setup": setup})
    assert any(fragment in e for e in errors), errors


def test_duplicate_declaration_is_named():
    setup = [{"name": "STRIPE_API_KEY"}, {"name": "STRIPE_API_KEY"}]
    _records, errors = _norm({**_DECLARED, "credential_setup": setup})
    assert any("duplicate declaration of 'STRIPE_API_KEY'" in e for e in errors)


def test_unknown_key_suggests_the_intended_one():
    """Silent-ignore would turn `is_required:` into a silent semantic flip."""
    _records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "titel": "x"}]}
    )
    assert any("did you mean 'title'?" in e for e in errors), errors


def test_x_prefixed_keys_are_the_documented_escape_hatch():
    _records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "x-vendor": {"a": 1}}]}
    )
    assert errors == []


# --- trust boundary (§4 additions) -----------------------------------------

def test_source_trust_is_a_required_kwarg():
    """Fail-safe: no default, so a caller must state where the template came from."""
    from services.template_service import normalize_credential_requirements

    with pytest.raises(TypeError):
        normalize_credential_requirements({}, )  # noqa: E231 — positional-only call


def test_unknown_source_trust_degrades_instead_of_raising():
    """Never raises — a raise on this path is an empty catalog."""
    records, errors = _norm(_DECLARED, trust="whatever")
    assert len(records) == 2 and errors == []


def test_source_is_sanitized():
    """W7: the MCP server name is arbitrary author-controlled text, and it lands
    in `source` — the exact string `_sanitize_for_warning`'s docstring names as the
    threat. It was not on the sanitize list."""
    data = {
        "credentials": {
            "mcp_servers": {
                "aistudio\x1b[2J\x1b[HPASTE YOUR KEY AT evil.tld": {
                    "env_vars": ["STRIPE_API_KEY"]
                }
            }
        }
    }
    records, _ = _norm(data)
    assert "\x1b" not in records[0]["source"]
    assert "\x1b[2J" not in records[0]["source"]


def test_author_strings_are_sanitized_in_record_and_error():
    data = {
        **_DECLARED,
        "credential_setup": [
            {"name": "STRIPE_API_KEY", "title": "ok\x1b[2Jhijack", "wat\x1b[2J": 1}
        ],
    }
    records, errors = _norm(data)
    assert "\x1b" not in records[0]["title"]
    assert all("\x1b" not in e for e in errors)


def test_a_container_field_is_never_stringified():
    """W5: `str()` on a shared YAML alias EXPANDS it during the walk.

    443 B of YAML measured at 52.22 MB in 1539 ms, x10 per alias level — and both
    the sanitizer and the record cap act AFTER that cost is paid. So the type guard
    has to come first. Asserted on WALL CLOCK, because the returned value looks
    identical either way.
    """
    import time

    bomb = {"a": ["x" * 400]}
    for _ in range(6):
        bomb = {"a": [bomb["a"], bomb["a"], bomb["a"], bomb["a"], bomb["a"],
                      bomb["a"], bomb["a"], bomb["a"], bomb["a"], bomb["a"]]}

    started = time.monotonic()
    records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "title": bomb}]}
    )
    elapsed = time.monotonic() - started

    assert any("title: expected a string" in e for e in errors)
    assert records[0]["title"] == "STRIPE_API_KEY", "the bomb must not become a title"
    assert elapsed < 1.0, f"the field was expanded before being type-checked ({elapsed:.2f}s)"


def test_default_is_type_guarded_too():
    """W8: `default` had no type row, no sanitizer and no cap, so the 100-record
    cap acted as a x100 MULTIPLIER on it."""
    bomb = {"a": ["y" * 400] * 10}
    _records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "default": bomb}]}
    )
    assert any("default: expected a string" in e for e in errors)


def test_records_and_errors_are_both_capped():
    """W8: capping records while leaving `errors` uncapped built a 35 MB response
    out of the cap that was supposed to prevent it."""
    from services.template_service import (
        _MAX_CREDENTIAL_ERRORS,
        _MAX_CREDENTIAL_RECORDS,
    )

    many = {"credentials": {"env_file": [f"VAR_{i}" for i in range(5000)]}}
    records, errors = _norm(many)
    assert len(records) == _MAX_CREDENTIAL_RECORDS
    assert len(errors) <= _MAX_CREDENTIAL_ERRORS

    hostile = {
        **many,
        "credential_setup": [{"name": f"NOPE_{i}"} for i in range(5000)],
    }
    records, errors = _norm(hostile)
    assert len(records) == _MAX_CREDENTIAL_RECORDS
    assert len(errors) <= _MAX_CREDENTIAL_ERRORS


def test_long_fields_are_capped_but_not_at_the_terminal_default():
    """W9: reusing `_sanitize_for_warning`'s 80-char default truncated a realistic
    159-char description into uselessness."""
    description = "A" * 159
    records, errors = _norm(
        {
            **_DECLARED,
            "credential_setup": [{"name": "STRIPE_API_KEY", "description": description}],
        }
    )
    assert errors == []
    assert records[0]["description"] == description, "a realistic description was truncated"


# --- setup_url (§4 addition 5) ---------------------------------------------

_URL_REJECTS = [
    pytest.param("https://google.com@evil.tld/apikey", "userinfo", id="userinfo"),
    pytest.param("http://example.com/k", "expected an https URL", id="http"),
    pytest.param("javascript:alert(1)", "expected an https URL", id="javascript"),
    pytest.param("data:text/html;base64,AAAA", "expected an https URL", id="data"),
    pytest.param("https:///nohost", "no host", id="no-host"),
    pytest.param("https://x.tld/" + "a" * 4096, "characters (limit", id="too-long"),
    pytest.param("https://x.tld/‮key", "non-printable", id="rtl-override"),
    pytest.param(["https://x.tld"], "expected an https URL (string)", id="not-a-string"),
]


@pytest.mark.parametrize("url,fragment", _URL_REJECTS)
def test_setup_url_rejects(url, fragment):
    records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "setup_url": url}]}
    )
    assert any(fragment in e for e in errors), errors
    assert records[0]["setup_url"] is None, "a rejected URL must not reach the record"


def test_setup_url_accepts_an_uppercase_scheme():
    """`HTTPS://` is a legitimate author today and scheme-lowercasing rejected it."""
    _records, errors = _norm(
        {
            **_DECLARED,
            "credential_setup": [
                {"name": "STRIPE_API_KEY", "setup_url": "HTTPS://dashboard.stripe.com/apikeys"}
            ],
        }
    )
    assert errors == []


def test_a_real_ninety_character_console_url_survives_intact():
    """Never run a URL through a truncator."""
    url = (
        "https://console.cloud.google.com/apis/credentials?project=my-project-123456"
        "&authuser=1"
    )
    assert len(url) >= 85
    records, errors = _norm(
        {**_DECLARED, "credential_setup": [{"name": "STRIPE_API_KEY", "setup_url": url}]}
    )
    assert errors == []
    assert records[0]["setup_url"] == url


def test_idn_homograph_is_a_documented_residual():
    """Stated honestly rather than claimed closed.

    `isprintable()` is False for Cf/Cc, so RTL and ANSI are rejected — but a
    lookalike host is printable and SURVIVES. A consumer must render the parsed
    hostname beside the link, which is why the schema carries that requirement.
    """
    records, errors = _norm(
        {
            **_DECLARED,
            "credential_setup": [
                {"name": "STRIPE_API_KEY", "setup_url": "https://dashboard.striрe.com/x"}
            ],
        }
    )
    assert errors == []
    assert records[0]["setup_url"] is not None


# --- W11 non-mutation ------------------------------------------------------

def test_normalizer_never_mutates_its_input():
    """`_metadata_cache` holds the parsed dict for 600s and YAML aliases genuinely
    share nodes, so one in-place normalize rewrites both fields and persists."""
    import copy

    data = {
        **copy.deepcopy(_DECLARED),
        "credential_setup": [
            {"name": "STRIPE_API_KEY", "title": "T", "setup_url": "https://x.tld/k"},
            {"name": "NOPE"},
        ],
    }
    snapshot = copy.deepcopy(data)
    _norm(data)
    assert data == snapshot


def test_aliased_nodes_are_not_rewritten_through_the_shared_reference():
    """The alias case specifically — the reason non-mutation is load-bearing."""
    import copy

    import yaml

    data = yaml.safe_load(
        "credentials:\n"
        "  env_file: &shared [STRIPE_API_KEY]\n"
        "aliased: *shared\n"
        "credential_setup:\n"
        "  - name: STRIPE_API_KEY\n"
        "    title: decorated\n"
    )
    snapshot = copy.deepcopy(data)
    records, _ = _norm(data)
    assert records[0]["title"] == "decorated"
    assert data == snapshot
    assert data["aliased"] == ["STRIPE_API_KEY"]


# --- W2: the unfenced GitHub builder ---------------------------------------

def test_a_raising_normalizer_does_not_empty_the_catalog(monkeypatch):
    """W2: `_build_template` runs in bare list comprehensions in
    `get_all_templates()`, OUTSIDE PR-A's per-template fence. A raise there is an
    HTTP 500 with an empty catalog — PR-A's exact bug, reopened by the change that
    surfaces the new metadata. The bad template must still list, with a NAMED error.
    """
    from services import template_service as ts

    def boom(_data, *, source_trust):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(ts, "normalize_credential_requirements", boom)

    entry = ts._build_template("Owner/repo", {"credentials": {"env_file": ["A"]}})

    assert entry["id"] == "github:Owner/repo", "the template must still list"
    assert entry["credential_requirements"] == []
    assert any("could not be read" in e for e in entry["credential_errors"])


def test_ordinary_authoring_slips_do_not_break_the_builder():
    """`title: 123` and a bare `title:` are both W1/W2 triggers and both ordinary."""
    from services import template_service as ts

    for value in ("123", "", "[]", "{}"):
        entry = ts._build_template(
            "Owner/repo",
            {
                "credentials": {"env_file": ["STRIPE_API_KEY"]},
                "credential_setup": [{"name": "STRIPE_API_KEY", "title": value}],
            },
        )
        assert entry["id"] == "github:Owner/repo"
        assert len(entry["credential_requirements"]) == 1


# ===========================================================================
# Test 11 — the forward-compatibility invariant, pinned
# ===========================================================================
#
# This is the whole argument for putting enrichment in a sibling key rather than
# in `credentials.env_file`, so it is asserted rather than reasoned about:
# `generate_credential_files` must produce a BYTE-IDENTICAL `.env` with and
# without a `credential_setup:` block. If that ever stops holding, an
# already-deployed older Trinity reading an enriched template is writing a
# different `.env` than it does today — which is the failure mode the shape choice
# exists to prevent.

def test_env_writer_output_is_byte_identical_with_and_without_credential_setup():
    from services.template_service import generate_credential_files

    credentials = {
        "mcp_servers": {"stripe": {"env_vars": ["STRIPE_API_KEY"]}},
        "env_file": ["STRIPE_API_KEY", "VAULT_BASE_PATH"],
    }
    values = {"STRIPE_API_KEY": "sk-live-xxx", "VAULT_BASE_PATH": "./Brain"}

    plain = generate_credential_files(
        {"name": "fixture", "credentials": credentials}, values, "agent-x"
    )
    enriched = generate_credential_files(
        {
            "name": "fixture",
            "credentials": credentials,
            "credential_setup": [
                {
                    "name": "STRIPE_API_KEY",
                    "title": "Stripe secret key",
                    "description": "Powers the stripe MCP server.",
                    "required": True,
                    "secret": True,
                    "setup_url": "https://dashboard.stripe.com/apikeys",
                },
                {"name": "VAULT_BASE_PATH", "format": "dirpath", "default": "./Brain"},
            ],
        },
        values,
        "agent-x",
    )

    assert plain == enriched
    # And the content is what it always was — not identically broken.
    assert plain[".env"].encode() == (
        b"# Generated by Trinity - Agent credentials\n\n"
        b"STRIPE_API_KEY=sk-live-xxx\nVAULT_BASE_PATH=./Brain"
    )


def test_a_deployed_older_binary_reads_env_file_unchanged():
    """The accessor an older Trinity uses must see a plain list of names.

    `credentials:` stays names-only forever, so this reader — which backs the `.env`
    writer — cannot meet the mapping that would `TypeError` on
    `agent_credentials.get(var_name, "")`.
    """
    from services.template_service import credential_env_file_names

    block = {"env_file": ["STRIPE_API_KEY", "VAULT_BASE_PATH"]}
    names = credential_env_file_names(block)
    assert names == ["STRIPE_API_KEY", "VAULT_BASE_PATH"]
    assert all(isinstance(n, str) for n in names)


def test_credential_setup_is_invisible_to_the_write_path():
    """A malformed `credential_setup:` must not fail agent creation.

    The write path fails loud on a malformed `credentials:` (PR-A's contract), but
    `credential_setup:` is presentation metadata — a bad title must cost a catalog
    error, never an agent.
    """
    from services.template_service import generate_credential_files

    files = generate_credential_files(
        {
            "name": "fixture",
            "credentials": {"env_file": ["STRIPE_API_KEY"]},
            "credential_setup": "not even a list",
        },
        {"STRIPE_API_KEY": "v"},
        "agent-x",
    )
    assert files[".env"].endswith("STRIPE_API_KEY=v")
