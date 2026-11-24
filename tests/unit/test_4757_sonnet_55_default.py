"""#4757 — Claude Sonnet 5.5 is the platform default model (was Sonnet 4.6).

What moves, and what must not:

* A fresh install, and an install that never chose a default, resolves
  ``claude-sonnet-5-5`` — in the backend setting and in the agent image's own
  safety-net fallbacks. An admin's explicit ``platform_default_model`` row wins.
* An agent still running an older base image cannot run Sonnet 5.5
  (``min_claude_code`` 2.1.284). Its turns must not start failing
  ``model_unsupported`` because the default moved: the platform-default rung
  steps down to a model every CLI accepts (Sonnet 4.6) for an agent whose
  container does not prove a new enough Claude Code. The proof is the base
  image's ``trinity.claude-code-version`` label; an image built before it
  carries none, so it is treated as unable — the safe direction.
* An agent's own ``default_model`` and an explicit per-turn model are not
  touched by the step-down; non-Claude runtimes still get no Claude id.

Driven through the real ``agent_model_service.resolve_default_model``, the real
``settings_service.get_platform_default_model`` and the real agent-image
``_setup_headless_command`` / ``AgentState.get_default_model``.
"""
from __future__ import annotations

import re
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.settings_service as ss
from services import agent_model_service as ams
from services import model_catalog

pytestmark = pytest.mark.unit

NEW = "claude-sonnet-5-5"
OLD = "claude-sonnet-4-6"
_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "docker" / "base-image" / "Dockerfile"


def _settings(default):
    return SimpleNamespace(get_platform_default_model=lambda runtime=None: default)


def _db(stored=None):
    return SimpleNamespace(get_default_model_raw=lambda name: stored)


def _resolve(*, stored=None, default=NEW, cli=None, runtime="claude-code"):
    return ams.resolve_default_model(
        "agent-x", runtime=runtime, db_ops=_db(stored), settings=_settings(default),
        cli_version=lambda name: cli,
    )


# --------------------------------------------------------------------------- #
# AC1 / AC2 — the default itself
# --------------------------------------------------------------------------- #
class TestPlatformDefaultIsSonnet55:
    def _fresh(self, monkeypatch, row=None):
        monkeypatch.setattr(ss, "db", types.SimpleNamespace(
            get_setting_value=lambda key, default=None: (
                row if (row is not None and key == "platform_default_model") else default
            )
        ))
        monkeypatch.setattr(ss, "_platform_model_cache", None)
        monkeypatch.setattr(ss, "_platform_model_cache_ts", 0.0)

    def test_install_that_never_chose_a_default_gets_sonnet_5_5(self, monkeypatch):
        self._fresh(monkeypatch)
        assert ss.SettingsService().get_platform_default_model() == NEW

    def test_an_admin_choice_survives_the_upgrade(self, monkeypatch):
        self._fresh(monkeypatch, row=OLD)
        assert ss.SettingsService().get_platform_default_model() == OLD

    def test_the_catalog_recommends_the_default(self):
        recommended = [m.id for m in model_catalog.MODEL_CATALOG if m.recommended]
        assert recommended == [ss.PLATFORM_DEFAULT_MODEL_VALUE]

    def test_the_step_down_model_runs_on_every_cli(self):
        floor = model_catalog.OLD_CLI_DEFAULT_MODEL
        entry = {m.id: m for m in model_catalog.MODEL_CATALOG}[floor]
        assert entry.min_claude_code == "" and entry.admin_default_selectable


# --------------------------------------------------------------------------- #
# AC3 — an older base image keeps a model it can run
# --------------------------------------------------------------------------- #
class TestOlderImageKeepsARunnableModel:
    def test_image_without_the_version_label_steps_down(self):
        r = _resolve(cli=None)
        assert (r.model, r.source) == (OLD, ams.SOURCE_PLATFORM_DEFAULT)

    def test_cli_older_than_the_models_minimum_steps_down(self):
        assert _resolve(cli="2.1.278").model == OLD

    @pytest.mark.parametrize("cli", ["2.1.284", "2.1.322", "2.2.0"])
    def test_cli_new_enough_runs_the_new_default(self, cli):
        assert _resolve(cli=cli).model == NEW

    def test_unparseable_label_is_treated_as_unable(self):
        assert _resolve(cli="latest").model == OLD

    def test_a_default_with_no_minimum_never_reads_the_container(self):
        def boom(name):
            raise AssertionError("container read for a model every CLI runs")
        r = ams.resolve_default_model(
            "agent-x", runtime="claude-code", db_ops=_db(None),
            settings=_settings(OLD), cli_version=boom,
        )
        assert r.model == OLD

    def test_a_failed_container_read_steps_down_instead_of_raising(self):
        def boom(name):
            raise RuntimeError("docker unreadable")
        r = ams.resolve_default_model(
            "agent-x", runtime="claude-code", db_ops=_db(None),
            settings=_settings(NEW), cli_version=boom,
        )
        assert r.model == OLD

    def test_the_default_cli_reader_is_the_container_label(self, monkeypatch):
        """Production wiring: with no injected reader the resolver asks Docker
        for the agent's ``trinity.claude-code-version`` label."""
        import services.docker_service as ds

        seen = []
        container = SimpleNamespace(labels={"trinity.claude-code-version": "2.1.322"})
        monkeypatch.setattr(ds, "get_agent_container", lambda name: seen.append(name) or container)
        r = ams.resolve_default_model(
            "agent-x", runtime="claude-code", db_ops=_db(None), settings=_settings(NEW),
        )
        assert (r.model, seen) == (NEW, ["agent-x"])
        container.labels = {}
        assert ams.resolve_default_model(
            "agent-y", runtime="claude-code", db_ops=_db(None), settings=_settings(NEW),
        ).model == OLD

    def test_the_base_image_stamps_its_claude_code_version(self):
        """The label the resolver reads is written by the image, from the pin."""
        text = _DOCKERFILE.read_text()
        assert re.search(
            r'^LABEL trinity\.claude-code-version="\$\{CLAUDE_CODE_VERSION\}"$', text, re.M
        )
        assert text.index("ARG CLAUDE_CODE_VERSION=") < text.index("LABEL trinity.claude-code-version")


# --------------------------------------------------------------------------- #
# AC4 / AC5 — higher rungs and other runtimes are untouched
# --------------------------------------------------------------------------- #
class TestOtherRungsUntouched:
    def test_the_agents_own_model_still_wins(self):
        r = _resolve(stored="claude-opus-5", cli=None)
        assert (r.model, r.source) == ("claude-opus-5", ams.SOURCE_AGENT)

    @pytest.mark.parametrize("runtime", ["codex", "gemini-cli"])
    def test_non_claude_runtime_gets_no_claude_id(self, runtime):
        assert _resolve(runtime=runtime, cli=None).model is None


# --------------------------------------------------------------------------- #
# AC1 — the agent image's own safety-net fallbacks
# --------------------------------------------------------------------------- #
class TestAgentImageFallbacks:
    def test_headless_task_with_no_model_runs_the_new_default(self):
        from agent_server.services import headless_executor as he

        ctx = he._setup_headless_command(
            prompt="p", model=None, allowed_tools=None, system_prompt=None,
            timeout_seconds=60, max_turns=None, execution_id="e-4757",
            resume_session_id=None, persist_session=False, images=None,
        )
        assert ctx.cmd[ctx.cmd.index("--model") + 1] == NEW

    def test_runtime_default_model_is_the_platform_default(self):
        from agent_server.services.claude_code import ClaudeCodeRuntime, DEFAULT_CLAUDE_MODEL

        assert DEFAULT_CLAUDE_MODEL == ss.PLATFORM_DEFAULT_MODEL_VALUE
        assert ClaudeCodeRuntime().get_default_model() == NEW
