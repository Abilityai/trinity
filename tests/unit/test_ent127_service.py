"""Report assembly: sources, tri-states and the degraded precedence (ent#127 §2).

The load-bearing test in this file is `TestDegradedDominates`. A degraded lookup
and a genuinely credential-free agent produce a textually identical empty
requirement set, and "Ready — this agent needs no credentials" is the one state
a user will never investigate.

`tests/unit/pytest.ini` does not inherit pyproject's `asyncio_mode = "auto"`, so
every async test here carries an explicit marker.
"""

import pytest

from services import credential_requirements_service as crs


# --------------------------------------------------------------------------
# fixtures / doubles
# --------------------------------------------------------------------------

TEMPLATE_WITH_SETUP = """
name: acme-recon
credentials:
  env_file:
    - OPENAI_API_KEY
    - NOTION_TOKEN
  mcp_servers:
    slack:
      env_vars:
        - SLACK_BOT_TOKEN
credential_setup:
  - name: OPENAI_API_KEY
    title: OpenAI API key
    description: Used by the research MCP server.
    setup_url: https://platform.openai.com/api-keys
  - name: SLACK_BOT_TOKEN
    required: false
    secret: true
"""

TEMPLATE_PLATFORM_ONLY = """
name: injected-only
credentials:
  env_file:
    - TRINITY_MCP_API_KEY
    - GITHUB_PAT
"""

TEMPLATE_NO_CREDENTIALS = "name: bare\ndescription: nothing to configure\n"


def _facts(**over):
    base = {
        "template_present": True,
        "template_text": TEMPLATE_WITH_SETUP,
        "template_truncated": False,
        "mcp_template_text": None,
        "env_example_text": None,
        "env_file_present": True,
        "env_keys_nonempty": [],
        "output_capped": False,
    }
    base.update(over)
    return base


def _probe(status="ok", **over):
    return {"status": status, "facts": _facts(**over) if status == "ok" else None}


def _install_probe(monkeypatch, result):
    async def fake(_agent_name):
        return result

    monkeypatch.setattr(crs, "collect_agent_credential_facts", fake)


def _install_catalog(monkeypatch, *, label="local:acme", entry=None):
    class _Container:
        labels = {"trinity.template": label} if label else {}

    monkeypatch.setattr(crs, "get_agent_container", lambda _n: _Container())
    monkeypatch.setattr(crs, "get_local_template", lambda _i: entry)
    monkeypatch.setattr(crs, "get_github_template", lambda _i: entry)


def _by_name(report):
    return {row["name"]: row for row in report["requirements"]}


# --------------------------------------------------------------------------


class TestLiveWorkspace:
    @pytest.mark.asyncio
    async def test_fresh_agent_every_variable_missing(self, monkeypatch):
        """A fresh `local:` agent's generated `.env` is `KEY=` for every declared
        variable, so the naive "the key is present" predicate reports SET for an
        agent nobody has configured."""
        _install_probe(monkeypatch, _probe(env_keys_nonempty=[]))
        report = await crs.build_report("acme")

        assert report["state"] == "ok"
        assert report["requirements_source"] == "live_workspace"
        assert report["status_source"] == "live"
        assert {r["status"] for r in report["requirements"]} == {"missing"}
        assert report["summary"]["missing"] == 3
        assert report["summary"]["set"] == 0

    @pytest.mark.asyncio
    async def test_github_agent_has_no_env_at_all(self, monkeypatch):
        """The dominant case. `_stage_config_files` guards on `template_data`,
        which only the `local:` arm populates, so a `github:` agent — the whole
        ent#123 tokenless seeded fleet — never gets a generated `.env`.
        Absent must be a definite `missing`, never `unknown`."""
        _install_probe(monkeypatch, _probe(env_file_present=False, env_keys_nonempty=[]))
        report = await crs.build_report("cornelius")
        assert {r["status"] for r in report["requirements"]} == {"missing"}
        assert report["summary"]["unknown"] == 0

    @pytest.mark.asyncio
    async def test_partially_configured(self, monkeypatch):
        _install_probe(monkeypatch, _probe(env_keys_nonempty=["OPENAI_API_KEY"]))
        rows = _by_name(await crs.build_report("acme"))
        assert rows["OPENAI_API_KEY"]["status"] == "set"
        assert rows["NOTION_TOKEN"]["status"] == "missing"

    @pytest.mark.asyncio
    async def test_undeclared_env_key_never_appears(self, monkeypatch):
        """The probe's raw key list is projected onto the DECLARED set before it
        can reach a response: `CLIENT_ACME_PROD_TOKEN` leaks a customer
        relationship."""
        _install_probe(
            monkeypatch,
            _probe(env_keys_nonempty=["OPENAI_API_KEY", "CLIENT_ACME_PROD_TOKEN"]),
        )
        report = await crs.build_report("acme")
        assert "CLIENT_ACME_PROD_TOKEN" not in str(report)


class TestTriStates:
    @pytest.mark.asyncio
    async def test_required_unknown_survives_and_never_blocks(self, monkeypatch):
        """A bare `- FOO` carries no authorial intent. Rendering it as required
        cries wolf on every template whose author never opted in."""
        _install_probe(monkeypatch, _probe())
        rows = _by_name(await crs.build_report("acme"))
        # Declared but not decorated by `credential_setup:`.
        assert rows["NOTION_TOKEN"]["required"] == "unknown"
        # Decorated with no explicit `required:` => the author meant required.
        assert rows["OPENAI_API_KEY"]["required"] is True
        # Explicitly opted out.
        assert rows["SLACK_BOT_TOKEN"]["required"] is False

    @pytest.mark.asyncio
    async def test_blocking_counts_only_required_true_and_missing(self, monkeypatch):
        _install_probe(monkeypatch, _probe(env_keys_nonempty=[]))
        report = await crs.build_report("acme")
        assert report["summary"]["total"] == 3
        assert report["summary"]["blocking"] == 1  # OPENAI_API_KEY only

    @pytest.mark.asyncio
    async def test_secret_defaults_to_true(self, monkeypatch):
        _install_probe(monkeypatch, _probe())
        rows = _by_name(await crs.build_report("acme"))
        assert all(row["secret"] is True for row in rows.values())

    @pytest.mark.asyncio
    async def test_setup_url_carries_display_facts(self, monkeypatch):
        _install_probe(monkeypatch, _probe())
        row = _by_name(await crs.build_report("acme"))["OPENAI_API_KEY"]
        assert row["setup_url_display_host"] == "platform.openai.com"
        assert row["setup_url_registrable"] == "openai.com"
        assert row["setup_url_verified"] is True

    @pytest.mark.asyncio
    async def test_no_setup_url_is_not_verified(self, monkeypatch):
        _install_probe(monkeypatch, _probe())
        row = _by_name(await crs.build_report("acme"))["NOTION_TOKEN"]
        assert row["setup_url"] is None
        assert row["setup_url_display_host"] is None
        assert row["setup_url_verified"] is False


class TestPlatformInjected:
    @pytest.mark.asyncio
    async def test_excluded_from_rows_and_counted(self, monkeypatch):
        """Never render a platform-injected var as "missing" — it is not the
        operator's to set, and doing so is a pure false-alarm class."""
        _install_probe(monkeypatch, _probe(template_text=TEMPLATE_PLATFORM_ONLY))
        report = await crs.build_report("acme")
        assert report["requirements"] == []
        assert report["summary"]["platform_injected_excluded"] == 2
        # An explicit declaration of only-injected variables IS an opt-in.
        assert report["state"] == "no_credentials_required"


class TestEmptyState:
    @pytest.mark.parametrize(
        "template",
        [
            TEMPLATE_NO_CREDENTIALS,
            "name: x\ncredentials:\n",           # present but null
            "name: x\ncredentials: {}\n",        # present but empty
        ],
    )
    @pytest.mark.asyncio
    async def test_no_credentials_required_is_first_class(self, monkeypatch, template):
        _install_probe(monkeypatch, _probe(template_text=template))
        report = await crs.build_report("acme")
        assert report["state"] == "no_credentials_required"
        assert report["requirements"] == []
        assert report["degraded_reason"] is None


class TestDeclarationIncomplete:
    @pytest.mark.asyncio
    async def test_undeclared_but_referenced_is_not_green(self, monkeypatch):
        """12 of 25 bundled templates declare `credentials: {}` and 13 declare
        nothing. A legacy template with `${SLACK_BOT_TOKEN}` in
        `.mcp.json.template` must not render "Ready — needs no credentials"."""
        _install_probe(
            monkeypatch,
            _probe(
                template_text=TEMPLATE_NO_CREDENTIALS,
                mcp_template_text='{"mcpServers":{"slack":{"env":{"T":"${SLACK_BOT_TOKEN}"}}}}',
            ),
        )
        report = await crs.build_report("legacy")
        assert report["state"] == "declaration_incomplete"
        assert [r["name"] for r in report["requirements"]] == ["SLACK_BOT_TOKEN"]

    @pytest.mark.asyncio
    async def test_advisory_rows_are_never_blocking(self, monkeypatch):
        _install_probe(
            monkeypatch,
            _probe(
                template_text=TEMPLATE_NO_CREDENTIALS,
                env_example_text="SLACK_BOT_TOKEN=xoxb-example\n",
            ),
        )
        report = await crs.build_report("legacy")
        row = report["requirements"][0]
        assert row["advisory"] is True
        assert row["required"] == "unknown"
        assert report["summary"]["blocking"] == 0
        assert report["summary"]["advisory"] == 1

    @pytest.mark.asyncio
    async def test_platform_injected_refs_do_not_trigger_it(self, monkeypatch):
        _install_probe(
            monkeypatch,
            _probe(
                template_text=TEMPLATE_NO_CREDENTIALS,
                mcp_template_text='{"env":{"K":"${TRINITY_MCP_API_KEY}"}}',
            ),
        )
        report = await crs.build_report("legacy")
        assert report["state"] == "no_credentials_required"

    @pytest.mark.asyncio
    async def test_a_real_declaration_suppresses_advisory_rows(self, monkeypatch):
        """`.mcp.json.template` never becomes a declaration authority."""
        _install_probe(
            monkeypatch,
            _probe(mcp_template_text='{"env":{"K":"${SOMETHING_ELSE}"}}'),
        )
        report = await crs.build_report("acme")
        assert report["state"] == "ok"
        assert "SOMETHING_ELSE" not in _by_name(report)

    @pytest.mark.asyncio
    async def test_advisory_rows_are_capped(self, monkeypatch):
        refs = "".join('"${{VAR_{0}}}"'.format(i) for i in range(300))
        _install_probe(
            monkeypatch,
            _probe(template_text=TEMPLATE_NO_CREDENTIALS, mcp_template_text=refs),
        )
        report = await crs.build_report("legacy")
        assert len(report["requirements"]) == crs._MAX_ADVISORY


class TestDegradedDominates:
    """`degraded` beats `no_credentials_required` unconditionally."""

    @pytest.mark.asyncio
    async def test_stopped_agent_renders_catalog_with_unknown_status(self, monkeypatch):
        _install_probe(monkeypatch, {"status": "not_running", "facts": None})
        _install_catalog(
            monkeypatch,
            entry={
                "credential_requirements": [
                    {"name": "OPENAI_API_KEY", "required": True, "secret": True,
                     "platform_injected": False, "source": "template:env_file"}
                ],
                "credential_errors": [],
            },
        )
        report = await crs.build_report("stopped")
        assert report["state"] == "degraded"
        assert report["degraded_reason"] == "agent_not_running"
        assert report["requirements_source"] == "catalog"
        assert report["status_source"] == "unavailable"
        assert [r["status"] for r in report["requirements"]] == ["unknown"]

    @pytest.mark.asyncio
    async def test_empty_catalog_result_is_degraded_not_ready(self, monkeypatch):
        """`get_github_template` returns `_build_template(repo, {})` — EMPTY
        requirements, not None — when the repo is gone or the fetch failed. With
        no PAT, GitHub's 60-req/hr anon limit makes that the expected outcome."""
        _install_probe(monkeypatch, {"status": "not_running", "facts": None})
        _install_catalog(
            monkeypatch,
            label="github:Abilityai/cornelius",
            entry={"credential_requirements": [], "credential_errors": []},
        )
        report = await crs.build_report("cornelius")
        assert report["state"] == "degraded"
        assert report["requirements"] == []

    @pytest.mark.asyncio
    async def test_missing_template_label_is_degraded(self, monkeypatch):
        _install_probe(monkeypatch, {"status": "not_running", "facts": None})
        _install_catalog(monkeypatch, label=None)
        report = await crs.build_report("nolabel")
        assert report["state"] == "degraded"
        assert report["degraded_reason"] == "agent_not_running"

    @pytest.mark.asyncio
    async def test_label_names_a_template_not_in_the_catalog(self, monkeypatch):
        _install_probe(monkeypatch, {"status": "not_running", "facts": None})
        _install_catalog(monkeypatch, entry=None)
        report = await crs.build_report("gone")
        assert report["state"] == "degraded"

    @pytest.mark.asyncio
    async def test_docker_unavailable_is_degraded(self, monkeypatch):
        _install_probe(monkeypatch, {"status": "unavailable", "facts": None})
        monkeypatch.setattr(crs, "get_agent_container", lambda _n: None)
        report = await crs.build_report("unreachable")
        assert report["state"] == "degraded"
        assert report["degraded_reason"] == "agent_unreachable"
        assert report["requirements_source"] == "none"

    @pytest.mark.asyncio
    async def test_unparseable_template_is_degraded_with_catalog_fallback(self, monkeypatch):
        _install_probe(monkeypatch, _probe(template_text="\t not: [ yaml"))
        _install_catalog(
            monkeypatch,
            entry={
                "credential_requirements": [
                    {"name": "A", "required": True, "platform_injected": False}
                ],
                "credential_errors": [],
            },
        )
        report = await crs.build_report("broken")
        assert report["degraded_reason"] == "template_unreadable"
        assert report["state"] == "degraded"
        assert report["requirements_source"] == "catalog"
        # The probe DID answer, so per-variable status is still live.
        assert report["status_source"] == "live"
        assert report["requirements"][0]["status"] == "missing"

    @pytest.mark.asyncio
    async def test_absent_template_reports_no_template(self, monkeypatch):
        _install_probe(monkeypatch, _probe(template_present=False, template_text=None))
        _install_catalog(monkeypatch, entry=None)
        report = await crs.build_report("notemplate")
        assert report["degraded_reason"] == "no_template"
        assert report["state"] == "degraded"


class TestHardening:
    @pytest.mark.asyncio
    async def test_yaml_aliases_are_refused(self, monkeypatch):
        """Alias expansion is a measured 443 B -> 52 MB amplifier in this very
        codebase, and this template.yaml comes from an agent-writable workspace."""
        bomb = "a: &x [z,z,z]\nb: [*x,*x,*x]\ncredentials:\n  env_file: [A]\n"
        _install_probe(monkeypatch, _probe(template_text=bomb))
        _install_catalog(monkeypatch, entry=None)
        report = await crs.build_report("bomb")
        assert report["degraded_reason"] == "template_unreadable"

    @pytest.mark.asyncio
    async def test_anchor_without_alias_is_fine(self, monkeypatch):
        _install_probe(
            monkeypatch,
            _probe(template_text="name: &n ok\ncredentials:\n  env_file: [A]\n"),
        )
        report = await crs.build_report("anchored")
        assert report["state"] == "ok"

    @pytest.mark.asyncio
    async def test_non_mapping_template_is_unreadable(self, monkeypatch):
        _install_probe(monkeypatch, _probe(template_text="- just\n- a\n- list\n"))
        _install_catalog(monkeypatch, entry=None)
        report = await crs.build_report("listy")
        assert report["degraded_reason"] == "template_unreadable"

    @pytest.mark.asyncio
    async def test_normalizer_raise_is_caught_narrowly(self, monkeypatch):
        """Wrapped at the CALL, not around the whole build: the ent#128 lesson is
        that blanket swallowing turns a raise into a verdict indistinguishable
        from a pass."""
        def boom(*_a, **_k):
            raise RuntimeError("normalizer exploded")

        monkeypatch.setattr(crs, "normalize_credential_requirements", boom)
        _install_probe(monkeypatch, _probe())
        report = await crs.build_report("acme")
        assert report["requirements"] == []
        assert report["errors"]
        # It still reports live_workspace: the template WAS read and parsed.
        assert report["requirements_source"] == "live_workspace"

    @pytest.mark.asyncio
    async def test_normalizer_errors_surface(self, monkeypatch):
        _install_probe(
            monkeypatch,
            _probe(
                template_text=(
                    "credentials:\n  env_file: [A]\n"
                    "credential_setup:\n  - name: NOT_DECLARED\n"
                )
            ),
        )
        report = await crs.build_report("acme")
        assert any("NOT_DECLARED" in e for e in report["errors"])

    @pytest.mark.asyncio
    async def test_catalog_lookup_failure_degrades(self, monkeypatch):
        _install_probe(monkeypatch, {"status": "not_running", "facts": None})

        class _Container:
            labels = {"trinity.template": "local:acme"}

        monkeypatch.setattr(crs, "get_agent_container", lambda _n: _Container())

        def boom(_i):
            raise RuntimeError("catalog down")

        monkeypatch.setattr(crs, "get_local_template", boom)
        report = await crs.build_report("acme")
        assert report["state"] == "degraded"

    @pytest.mark.asyncio
    async def test_github_catalog_lookup_is_offloaded(self, monkeypatch):
        """`_get_cached_metadata` uses a SYNCHRONOUS httpx client with a 10s
        timeout; called inline from an async route a cache miss stalls the whole
        worker."""
        seen = {}

        def fake_github(template_id):
            import threading
            seen["thread"] = threading.current_thread().name
            return {"credential_requirements": [], "credential_errors": []}

        _install_probe(monkeypatch, {"status": "not_running", "facts": None})
        _install_catalog(monkeypatch, label="github:Org/repo")
        monkeypatch.setattr(crs, "get_github_template", fake_github)
        await crs.build_report("gh")
        assert seen["thread"] != "MainThread"

    @pytest.mark.asyncio
    async def test_no_fixture_secret_reaches_the_report(self, monkeypatch):
        secret = "sk-ent127-should-never-appear"
        _install_probe(
            monkeypatch,
            _probe(env_keys_nonempty=["OPENAI_API_KEY"], env_example_text="X=" + secret),
        )
        report = await crs.build_report("acme")
        assert secret not in str(report)
