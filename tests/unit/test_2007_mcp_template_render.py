"""#2007 — `github:` templates never rendered `.mcp.json.template`.

`TRINITY_COMPATIBLE_AGENT_GUIDE.md` promised Trinity replaces `${VAR}` in
`.mcp.json.template` with credential-store values. Nothing did, for a `github:`
template: the backend renderer (`template_service.generate_credential_files`)
reads `.mcp.json` — not the `.template` — and is `local:`-only, while a
`github:` agent's files only exist after `startup.sh` clones the repo *inside*
the container. The only writer of `~/.mcp.json` was
`inject_trinity_mcp_if_configured()`, so every declared server was silently
absent — a freshly-seeded Cornelius shipped three servers and ran with none.

The renderer therefore lives in the container. These tests pin the four
properties that make it safe to run unattended on every boot:

  1. substitution is confined to `env` — the only form `mcp_validator` accepts,
     and the reason a `${VAR}` must never reach `command` (#590);
  2. an unresolvable placeholder **withholds** the server with a reason, it
     never blanks to `""` (#1929's defect, and what hid the #2006 residue);
  3. merge-only-missing, so an entry already installed — `trinity`, or one an
     owner edited — survives every restart;
  4. one bad server never costs the agent the good ones.

Loaded standalone by path: the agent server ships in its own image and cannot
import `src/backend` (the `test_1965_agent_server_safe_yaml.py` idiom).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"
_MODULE = _AGENT_SERVER / "mcp_template.py"
_CANON_VALIDATOR = _ROOT / "src" / "backend" / "services" / "mcp_validator.py"
_VENDORED_VALIDATOR = _AGENT_SERVER / "mcp_validator.py"

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def mod():
    """Import the renderer with its vendored validator resolvable.

    The module prefers the package-relative import and falls back to a flat
    one; the fallback is what a `python3 -m` run inside the image exercises, so
    the vendored validator is put on `sys.path` here rather than stubbed.
    """
    sys.path.insert(0, str(_AGENT_SERVER))
    try:
        spec = importlib.util.spec_from_file_location("_mcp_template_2007", _MODULE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(_AGENT_SERVER))


@pytest.fixture
def home(tmp_path):
    return tmp_path


def _paths(home):
    return home / ".mcp.json.template", home / ".mcp.json", home / ".env"


def _render(mod, home, template: dict | str, env: str = "", existing=None):
    tpl, cfg, envf = _paths(home)
    tpl.write_text(template if isinstance(template, str) else json.dumps(template))
    envf.write_text(env)
    if existing is not None:
        cfg.write_text(json.dumps(existing))
    result = mod.render(template_file=tpl, config_file=cfg, env_file=envf)
    written = json.loads(cfg.read_text()) if cfg.exists() else None
    return result, written


# ---------------------------------------------------------------------------
# Vendored-mirror parity (Invariant #5)
# ---------------------------------------------------------------------------

def test_validator_copies_are_byte_identical():
    """The container validates with the SAME rules as the backend, or the two
    surfaces accept different configs for the same file."""
    assert _VENDORED_VALIDATOR.exists(), "mcp_validator is not vendored (#2007)"
    assert _CANON_VALIDATOR.read_bytes() == _VENDORED_VALIDATOR.read_bytes(), (
        "mcp_validator.py drifted between backend and agent-server — re-copy "
        "the canonical file over the vendored one."
    )


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

class TestDeclaredServersAppear:

    def test_a_declared_server_reaches_mcp_json(self, mod, home):
        """AC #1 — the whole point: a github: template's declared server is
        actually configured."""
        result, written = _render(
            mod,
            home,
            {"mcpServers": {"mermaid-diagram": {
                "command": "npx", "args": ["-y", "@peng-shawn/mermaid-mcp-server"]}}},
        )
        assert result["added"] == ["mermaid-diagram"]
        assert written["mcpServers"]["mermaid-diagram"]["command"] == "npx"

    def test_env_placeholder_is_substituted_from_the_credential_store(self, mod, home):
        """Cornelius's `aistudio` server, verbatim."""
        result, written = _render(
            mod,
            home,
            {"mcpServers": {"aistudio": {
                "command": "npx", "args": ["-y", "aistudio-mcp-server"],
                "env": {"GEMINI_API_KEY": "${GEMINI_API_KEY}"}}}},
            env='GEMINI_API_KEY="real-key-value"\n',
        )
        assert result["added"] == ["aistudio"]
        assert written["mcpServers"]["aistudio"]["env"] == {
            "GEMINI_API_KEY": "real-key-value"
        }

    def test_default_form_is_honoured(self, mod, home):
        result, written = _render(
            mod, home,
            {"mcpServers": {"x": {"command": "npx", "args": ["-y", "pkg"],
                                  "env": {"P": "${MISSING:-/opt/fallback}"}}}},
        )
        assert result["added"] == ["x"]
        assert written["mcpServers"]["x"]["env"]["P"] == "/opt/fallback"

    def test_output_passes_the_real_validator(self, mod, home):
        """AC #2 — whatever is written must be a config the platform accepts."""
        from services.mcp_validator import validate_mcp_config

        _, written = _render(
            mod, home,
            {"mcpServers": {"ok": {"command": "python3", "args": ["-m", "srv"],
                                   "env": {"K": "${K}"}}}},
            env="K=v\n",
        )
        validate_mcp_config(json.dumps(written))  # raises on rejection


# ---------------------------------------------------------------------------
# Refuse, never blank (AC #3 — the shared contract with #1929)
# ---------------------------------------------------------------------------

class TestRefuseNeverBlank:

    def test_unresolved_placeholder_withholds_the_server_with_a_reason(self, mod, home):
        result, written = _render(
            mod, home,
            {"mcpServers": {"aistudio": {"command": "npx", "args": ["-y", "p"],
                                         "env": {"GEMINI_API_KEY": "${GEMINI_API_KEY}"}}}},
            env="",
        )
        assert result["added"] == []
        assert "GEMINI_API_KEY" in result["skipped"]["aistudio"]
        assert written is None, "nothing should be written when nothing rendered"

    def test_an_empty_credential_counts_as_unset(self, mod, home):
        """A key present but blank is the operator-hasn't-filled-it-in case —
        exactly the one that must not silently produce env={'K': ''}."""
        result, _ = _render(
            mod, home,
            {"mcpServers": {"x": {"command": "npx", "args": ["-y", "p"],
                                  "env": {"K": "${K}"}}}},
            env='K=""\n',
        )
        assert result["added"] == []
        assert "K" in result["skipped"]["x"]

    def test_placeholder_in_command_is_refused_not_substituted(self, mod, home):
        """The #590 class: a credential value becoming the executed command.
        `/api/credentials/update`'s whole-text `str.replace` did exactly this
        (#2008); this renderer must not.

        Asserted against the renderer's OWN named reason, not merely "the
        server was withheld": an unsubstituted `${SHELL_PATH}` is also refused
        by the validator (not in COMMAND_ALLOWLIST), so a withheld-only
        assertion passes with the explicit guard deleted and tells the operator
        the wrong thing to fix. To be honest about what protects what: the
        boundary is "never substitute outside `env`" plus the validator; this
        guard exists to name the actual cause.
        """
        result, written = _render(
            mod, home,
            {"mcpServers": {"evil": {"command": "${SHELL_PATH}", "args": []}}},
            env='SHELL_PATH="/bin/sh -c whatever"\n',
        )
        assert result["added"] == []
        assert "Trinity substitutes" in result["skipped"]["evil"], (
            "expected the named placeholder-in-command reason, got: "
            f"{result['skipped']['evil']}"
        )
        assert written is None

    def test_a_substituted_command_never_appears_in_the_output(self, mod, home):
        """The property that actually matters, stated independently of the
        error text: whatever happens, the credential value must not end up as
        the executed command."""
        _, written = _render(
            mod, home,
            {"mcpServers": {
                "evil": {"command": "${SHELL_PATH}", "args": []},
                "ok": {"command": "npx", "args": ["-y", "p"]},
            }},
            env='SHELL_PATH="/bin/sh -c whatever"\n',
        )
        assert "/bin/sh" not in json.dumps(written)

    def test_placeholder_in_args_is_refused(self, mod, home):
        """Cornelius's `ebook-mcp` shape. The validator rejects `${VAR}` in args
        (bare `$` is a shell metachar), so expanding there could only produce a
        config the platform refuses."""
        result, _ = _render(
            mod, home,
            {"mcpServers": {"ebook-mcp": {
                "command": "uvx",
                "args": ["--directory", "${EBOOK_MCP_PATH:-/opt/mcp/ebook-mcp}",
                         "run", "ebook-mcp"]}}},
            env="",
        )
        assert result["added"] == []
        assert "args" in result["skipped"]["ebook-mcp"]

    def test_a_command_outside_the_allowlist_is_withheld_with_the_validator_reason(
        self, mod, home
    ):
        """Cornelius's real `ebook-mcp` uses `uv`, which is not in
        COMMAND_ALLOWLIST — the withholding reason is the validator's own, so
        the operator is told what to change (`uvx`)."""
        result, _ = _render(
            mod, home,
            {"mcpServers": {"ebook-mcp": {"command": "uv", "args": ["run", "x"]}}},
        )
        assert result["added"] == []
        assert "allowlist" in result["skipped"]["ebook-mcp"]

    def test_one_bad_server_does_not_cost_the_good_ones(self, mod, home):
        """AC #5's shape: the Cornelius trio — two install, one is withheld
        with a documented reason."""
        result, written = _render(
            mod, home,
            {"mcpServers": {
                "mermaid-diagram": {"command": "npx", "args": ["-y", "mermaid"]},
                "aistudio": {"command": "npx", "args": ["-y", "aistudio"],
                             "env": {"GEMINI_API_KEY": "${GEMINI_API_KEY}"}},
                "ebook-mcp": {"command": "uv", "args": ["run", "ebook-mcp"]},
            }},
            env="GEMINI_API_KEY=k\n",
        )
        assert sorted(result["added"]) == ["aistudio", "mermaid-diagram"]
        assert list(result["skipped"]) == ["ebook-mcp"]
        assert sorted(written["mcpServers"]) == ["aistudio", "mermaid-diagram"]


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------

class TestMergeNeverClobber:

    def test_the_trinity_entry_survives(self, mod, home):
        """`inject_trinity_mcp_if_configured()` may write .mcp.json before OR
        after this runs; either way its entry must be intact."""
        trinity = {"type": "http", "url": "http://mcp-server:8080/mcp",
                   "headers": {"Authorization": "Bearer trinity_mcp_x"}}
        _, written = _render(
            mod, home,
            {"mcpServers": {"new": {"command": "npx", "args": ["-y", "p"]}}},
            existing={"mcpServers": {"trinity": trinity}},
        )
        assert written["mcpServers"]["trinity"] == trinity
        assert "new" in written["mcpServers"]

    def test_an_owner_edited_entry_is_not_reverted_on_restart(self, mod, home):
        edited = {"command": "npx", "args": ["-y", "pkg@2.0.0"]}
        result, written = _render(
            mod, home,
            {"mcpServers": {"pkg": {"command": "npx", "args": ["-y", "pkg@1.0.0"]}}},
            existing={"mcpServers": {"pkg": edited}},
        )
        assert result["added"] == []
        assert written["mcpServers"]["pkg"] == edited

    def test_rendering_twice_changes_nothing(self, mod, home):
        tpl, cfg, envf = _paths(home)
        tpl.write_text(json.dumps(
            {"mcpServers": {"x": {"command": "npx", "args": ["-y", "p"]}}}))
        envf.write_text("")

        first = mod.render(template_file=tpl, config_file=cfg, env_file=envf)
        snapshot = cfg.read_text()
        second = mod.render(template_file=tpl, config_file=cfg, env_file=envf)

        assert first["added"] == ["x"]
        assert second["added"] == []
        assert cfg.read_text() == snapshot

    def test_an_unparseable_existing_config_is_left_alone(self, mod, home):
        """Not ours to repair, and merging into it blind would destroy it."""
        tpl, cfg, envf = _paths(home)
        tpl.write_text(json.dumps({"mcpServers": {"x": {"command": "npx"}}}))
        envf.write_text("")
        cfg.write_text("{ not json")

        result = mod.render(template_file=tpl, config_file=cfg, env_file=envf)

        assert result["status"] == "existing_unreadable"
        assert cfg.read_text() == "{ not json"


# ---------------------------------------------------------------------------
# Never breaks the boot
# ---------------------------------------------------------------------------

class TestStartupSafety:

    @pytest.mark.parametrize("template,status", [
        ("{ not json", "invalid_json"),
        ({"mcpServers": {}}, "no_servers"),
        ({"no_servers_key": 1}, "no_servers"),
        ('"a string"', "no_servers"),
    ])
    def test_malformed_template_degrades_quietly(self, mod, home, template, status):
        result, written = _render(mod, home, template)
        assert result["status"] == status
        assert written is None

    def test_absent_template_is_a_no_op(self, mod, home):
        tpl, cfg, envf = _paths(home)
        result = mod.render(template_file=tpl, config_file=cfg, env_file=envf)
        assert result["status"] == "no_template"
        assert not cfg.exists()

    def test_main_always_reports_success(self, mod, monkeypatch):
        """startup.sh must continue whatever happens here."""
        monkeypatch.setattr(mod, "TEMPLATE_FILE", Path("/nonexistent/.mcp.json.template"))
        assert mod.main() == 0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_startup_sh_invokes_the_renderer():
    """The module is only a fix if the boot path runs it."""
    startup = (_ROOT / "docker" / "base-image" / "startup.sh").read_text()
    assert "agent_server.mcp_template" in startup, (
        "startup.sh does not run the .mcp.json.template renderer (#2007)"
    )


def test_the_renderer_runs_after_credentials_are_available():
    """Ordering: `.env` arrives either from the /generated-creds copy or from
    the decrypt-and-inject fallback. Rendering before the latter would resolve
    placeholders against a file that is not there yet."""
    startup = (_ROOT / "docker" / "base-image" / "startup.sh").read_text()
    assert startup.index("decrypt-and-inject") < startup.index("agent_server.mcp_template"), (
        "the renderer runs before the credential auto-import — placeholders "
        "would be unresolvable on that path"
    )


# ---------------------------------------------------------------------------
# Review follow-ups (#2013 review)
# ---------------------------------------------------------------------------

class TestRealCredentialValuesRender:
    """AC #5 must hold for values that look like real credentials.

    `mcp_validator` was written for the UNRENDERED config: it strips `${...}`
    refs and rejects whatever literal remains against `_LITERAL_SECRET_PATTERNS`
    and `_SHELL_METACHARS_RE`. Validating AFTER substitution means the literal
    IS the secret, so every real key was withheld with advice ("store it in
    .env and reference as ${VAR}") that the operator had already followed. CI
    passed only because the fixture used `GEMINI_API_KEY="real-key-value"`,
    which no credential looks like.
    """

    # Each is the real vendor shape, not a lookalike.
    SHAPES = [
        "AIzaSyA1234567890123456789012345678901234",   # Google (AIza[0-9A-Za-z_-]{35})
        "sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # Anthropic
        "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # OpenAI
        "ghp_1234567890abcdefghij1234567890abcdefgh",   # GitHub PAT
        "github_pat_11ABCDEFG0abcdefghijkl_ABCDEFGHI",  # GitHub fine-grained
        "xoxb-123456789012-1234567890123-abcdefghij",   # Slack bot
        "AKIAIOSFODNN7EXAMPLE",                          # AWS
        "pa$$w|rd;with&shell`meta",                      # shell metacharacters
    ]

    @pytest.mark.parametrize("secret", SHAPES)
    def test_the_declared_server_is_rendered_not_withheld(self, mod, home, secret):
        result, written = _render(
            mod, home,
            {"mcpServers": {"aistudio": {
                "command": "npx", "args": ["-y", "@aistudio/mcp"],
                "env": {"GEMINI_API_KEY": "${GEMINI_API_KEY}"},
            }}},
            env=f'GEMINI_API_KEY="{secret}"\n',
        )
        assert result["added"] == ["aistudio"], (
            f"a real-shaped credential was withheld: {result['skipped']}"
        )
        assert written["mcpServers"]["aistudio"]["env"]["GEMINI_API_KEY"] == secret

    def test_the_default_form_still_resolves(self, mod, home):
        """`${VAR:-default}` is a Trinity extension the vendored validator does
        not know, so the probe normalises it to `${VAR}` rather than teaching
        the vendored copy a new syntax (Invariant #5) or validating a rendered
        secret."""
        result, written = _render(
            mod, home,
            {"mcpServers": {"x": {"command": "npx", "args": ["-y", "p"],
                                  "env": {"P": "${MISSING:-/opt/fallback}"}}}},
        )
        assert result["added"] == ["x"], result["skipped"]
        assert written["mcpServers"]["x"]["env"]["P"] == "/opt/fallback"


class TestOneBadServerNeverCostsTheGoodOnes:
    """`render()` documents 'never raises' and the per-server call was unguarded.

    `_resolves_to_private_ip` catches only `socket.gaierror`, but
    `getaddrinfo` raises `UnicodeError` for an over-long hostname label — so a
    single bad entry propagated out of the loop and NOTHING was written,
    losing every valid sibling. `startup.sh`'s `|| echo` saved the boot, not
    the render.
    """

    def test_an_over_long_hostname_label_only_withholds_its_own_entry(self, mod, home):
        result, written = _render(
            mod, home,
            {"mcpServers": {
                "good": {"command": "npx", "args": ["-y", "ok"], "env": {}},
                "bad": {"type": "http", "url": f"https://{'a' * 80}.example.com/mcp"},
            }},
        )
        assert result["added"] == ["good"], (
            "a valid sibling was lost to another entry's failure"
        )
        assert "bad" in result["skipped"]
        assert written is not None and "good" in written["mcpServers"]

    def test_an_unexpected_error_is_reported_not_raised(self, mod, home, monkeypatch):
        """The guard is on the CALL, so any future validator exception type is
        covered — not just the one hostname case that was found."""
        def boom(*a, **k):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr(mod, "render_server", boom)
        result, _ = _render(
            mod, home,
            {"mcpServers": {"x": {"command": "npx", "args": ["-y", "p"], "env": {}}}},
        )
        assert result["status"] == "ok"
        assert "validator exploded" in result["skipped"]["x"]


def test_the_withheld_reason_names_the_server_not_the_probe(mod, home):
    """The named, actionable reason is this feature's core value claim.

    The probe key used to be the literal `"_probe"`, which the validator embeds
    verbatim — so the operator read `withheld MCP server 'aistudio': Server
    '_probe': ...`.
    """
    result, _ = _render(
        mod, home,
        {"mcpServers": {"aistudio": {"command": "/bin/sh", "args": ["-c", "id"]}}},
    )
    reason = result["skipped"]["aistudio"]
    assert "_probe" not in reason, f"the probe key leaked into the reason: {reason}"
    assert "aistudio" in reason, f"the reason does not name the server: {reason}"
