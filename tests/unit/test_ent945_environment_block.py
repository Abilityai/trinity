"""The `### Your environment` block of the platform prompt (trinity-enterprise#945).

An agent could not infer its own container from tool signatures: who it runs
as, which disk survives a recreate, that `/tmp` is a small noexec RAM mount,
which folders the platform reads, and which paths it must never write. The
block states those facts once, at every prompt tier, on every runtime.

Each fact here has an owner elsewhere in the tree, so the suite pins the block
to that owner rather than to its own text: a renamed folder constant, a new
`path_deny` entry or a moved TMPDIR fails here instead of shipping a prompt
that is quietly wrong. The owners are read without importing them (the router
and `agent_service` package pull in FastAPI, Docker and the database at import
time); each read fails, never skips, when its file or symbol is missing.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.platform_prompt_service import (
    _ALWAYS_SECTIONS,
    _KNOWN_SECTION_HEADINGS,
    _MINIMAL_DROP_SECTIONS,
    _adapt_instructions_for_runtime,
    _iter_sections,
    get_platform_system_prompt,
    render_platform_instructions,
)
from services.prompt_tier import PromptTier

pytestmark = pytest.mark.unit

# getattr, so a missing copy fails the parity test below on behaviour, not at import.
_GUARDRAIL_PATH_DENY = getattr(platform_prompt_service, "_GUARDRAIL_PATH_DENY", ())

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "src/backend"
GUARDRAILS_JSON = REPO_ROOT / "docker/base-image/hooks/guardrails-baseline.json"
STARTUP_SH = REPO_ROOT / "docker/base-image/startup.sh"

HEADING = "Your environment"
HOME = "/home/developer"

# Ships on every turn of every agent (the #1535 / ent#536 budget discipline).
MAX_BLOCK_CHARS = 2600

TIERS = (PromptTier.VERBOSE, PromptTier.MINIMAL)
HOOKED_RUNTIMES = ("claude-code", "claude", "")
UNHOOKED_RUNTIMES = ("codex", "opencode", "gemini", "some-future-runtime")


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


def _render(tier: PromptTier, runtime: str) -> str:
    return _adapt_instructions_for_runtime(render_platform_instructions(tier), runtime)


def _section(text: str, heading: str = HEADING) -> str:
    sections = dict(_iter_sections(text))
    assert heading in sections, f"section '### {heading}' missing from the rendered prompt"
    return sections[heading]


def _module_constant(relpath: str, name: str) -> str:
    """Read a module-level string constant from its owner, without importing it."""
    path = BACKEND / relpath
    assert path.is_file(), f"owner of {name} moved: {path}"
    for node in ast.parse(path.read_text()).body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = ast.literal_eval(node.value)
                assert isinstance(value, str)
                return value
    pytest.fail(f"{name} is no longer a module-level constant in {relpath}")


def _home_relative(path: str) -> str:
    assert path.startswith(HOME + "/"), path
    return path[len(HOME) + 1:].strip("/")


# T1 — present at every tier, through the production composition root, in place.
class TestPresence:
    @pytest.mark.parametrize("runtime", HOOKED_RUNTIMES + UNHOOKED_RUNTIMES)
    def test_live_prompt_carries_the_block(self, runtime):
        assert f"### {HEADING}" in get_platform_system_prompt(runtime=runtime)

    @pytest.mark.parametrize("tier", TIERS)
    def test_every_tier_renders_it(self, tier):
        _section(render_platform_instructions(tier))

    def test_always_class_not_droppable(self):
        assert HEADING in _KNOWN_SECTION_HEADINGS
        assert HEADING not in _MINIMAL_DROP_SECTIONS
        assert HEADING in _ALWAYS_SECTIONS

    @pytest.mark.parametrize("tier", TIERS)
    def test_sits_directly_before_package_persistence(self, tier):
        headings = [h for h, _ in _iter_sections(render_platform_instructions(tier)) if h]
        assert headings[headings.index(HEADING) + 1] == "Package Persistence"


# T2 — each fact token an agent acts on.
class TestFacts:
    @pytest.mark.parametrize(
        "token",
        ["`developer`", "`/home/developer`", "durable volume", "recreate", "`/tmp`",
         "noexec", "`$TMPDIR`", "`~/.tmp`", "UTC", "`.trinity/persistent-state.yaml`",
         "untracked", "`memory/`", "`setup.sh`", "never put secrets in it",
         "`template.yaml`", "refresh_metric_definitions",
         "People cannot edit it from the Files tab; you can."],
    )
    def test_fact_present(self, token):
        assert token in _section(_render(PromptTier.VERBOSE, "claude-code"))


# T3 — no marker ever ships unfilled.
@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("runtime", HOOKED_RUNTIMES + UNHOOKED_RUNTIMES)
def test_no_unfilled_marker(tier, runtime):
    leftover = re.findall(r"__[A-Z][A-Z_]+__", _render(tier, runtime))
    assert not leftover, f"unfilled markers for runtime={runtime!r}: {leftover}"


# T4 — the protected-path list is the agent image's own path_deny, exactly.
class TestPathDeny:
    def test_backend_copy_matches_the_agent_image(self):
        assert GUARDRAILS_JSON.is_file(), f"guardrails baseline moved: {GUARDRAILS_JSON}"
        path_deny = json.loads(GUARDRAILS_JSON.read_text())["path_deny"]
        assert list(_GUARDRAIL_PATH_DENY) == path_deny, (
            "guardrails-baseline.json path_deny changed: update _GUARDRAIL_PATH_DENY "
            "in platform_prompt_service.py so the prompt names the same paths"
        )

    @pytest.mark.parametrize("runtime", HOOKED_RUNTIMES + UNHOOKED_RUNTIMES)
    def test_every_entry_is_in_the_block(self, runtime):
        block = _section(_render(PromptTier.VERBOSE, runtime))
        for entry in _GUARDRAIL_PATH_DENY:
            assert f"`{entry}`" in block, entry


# T4b — the reset's default keep-set is the agent server's own default, exactly.
AGENT_FILES_ROUTER = REPO_ROOT / "docker/base-image/agent_server/routers/files.py"


def _agent_default_persistent_state() -> list[str]:
    """`_DEFAULT_PERSISTENT_STATE` from the agent server, read without importing it."""
    assert AGENT_FILES_ROUTER.is_file(), f"owner moved: {AGENT_FILES_ROUTER}"
    for node in ast.parse(AGENT_FILES_ROUTER.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_DEFAULT_PERSISTENT_STATE" for t in node.targets
        ):
            return list(ast.literal_eval(node.value))
    pytest.fail("_DEFAULT_PERSISTENT_STATE is no longer a module-level constant in files.py")


def test_reset_default_set_matches_the_agent_server():
    block = _section(_render(PromptTier.VERBOSE, "claude-code"))
    match = re.search(r"without that file, a default set: ([^)]*)\)", block)
    assert match, "the reset sentence no longer names the default keep-set"
    named = re.findall(r"`([^`]+)`", match.group(1))
    expected = [p[: -len("**")] if p.endswith("/**") else p for p in _agent_default_persistent_state()]
    assert named == expected, (
        "files.py _DEFAULT_PERSISTENT_STATE changed: update the reset sentence in "
        "platform_prompt_service.py so the prompt names the same paths"
    )


# T5 — reserved directories, each against the constant that owns it.
class TestReservedDirectories:
    @pytest.mark.parametrize(
        ("relpath", "name"),
        [
            ("routers/agent_data.py", "_DATA_CONTAINER_PATH"),
            ("services/agent_shared_files_service.py", "PUBLISH_DIR"),
            ("services/upload_service.py", "UPLOAD_BASE"),
            ("services/agent_service/helpers.py", "_SHARED_IN_PREFIX"),
        ],
    )
    def test_owning_constant_is_named(self, relpath, name):
        folder = _home_relative(_module_constant(relpath, name))
        block = _section(_render(PromptTier.VERBOSE, "claude-code"))
        assert f"`{folder}/" in block, f"{name} -> {folder}/ not named in the block"

    @pytest.mark.parametrize(
        "relpath", ["services/agent_service/crud.py", "services/agent_service/lifecycle.py"]
    )
    def test_shared_out_bind_is_named(self, relpath):
        source = (BACKEND / relpath).read_text()
        assert "'bind': '/home/developer/shared-out'" in source, f"shared-out bind moved in {relpath}"
        assert "`shared-out/`" in _section(_render(PromptTier.VERBOSE, "claude-code"))

    def test_content_folder_is_created_by_startup(self):
        assert STARTUP_SH.is_file()
        assert "mkdir -p /home/developer/content/" in STARTUP_SH.read_text()
        assert "`content/`" in _section(_render(PromptTier.VERBOSE, "claude-code"))

    @pytest.mark.parametrize("name", ["setup.sh", "plugins.yaml"])
    def test_trinity_files_are_read_by_startup(self, name):
        assert f"/home/developer/.trinity/{name}" in STARTUP_SH.read_text()
        assert f"`{name}`" in _section(_render(PromptTier.VERBOSE, "claude-code"))

    def test_tmpdir_matches_the_container_default(self):
        tmpdir = _module_constant("services/agent_service/capabilities.py", "AGENT_DEFAULT_TMPDIR")
        assert f"`~/{_home_relative(tmpdir)}`" in _section(_render(PromptTier.VERBOSE, "claude-code"))


# T6 — context budget.
def test_block_stays_within_the_context_budget():
    block = _section(_render(PromptTier.VERBOSE, "claude-code")).strip()
    assert len(block) <= MAX_BLOCK_CHARS, (
        f"environment block grew to {len(block)} chars (cap {MAX_BLOCK_CHARS}); "
        "it ships on every turn of every agent"
    )


# T7 — never claim a block the runtime does not have.
class TestRuntimeHonesty:
    @pytest.mark.parametrize("runtime", HOOKED_RUNTIMES)
    def test_claude_code_names_its_hook_and_its_gap(self, runtime):
        block = _section(get_platform_system_prompt(runtime=runtime))
        assert "Your Write and Edit tools refuse them" in block
        assert "shell commands do not" in block
        assert "Nothing in your runtime blocks" not in block

    @pytest.mark.parametrize("runtime", UNHOOKED_RUNTIMES)
    def test_other_runtimes_are_told_nothing_blocks(self, runtime):
        block = _section(get_platform_system_prompt(runtime=runtime))
        assert "Nothing in your runtime blocks these writes" in block
        assert "refuse them" not in block


# T8 — the user-memory warning keeps its substance under the corrected name.
class TestRememberingWording:
    SECTION = "Remembering Things About Users (Public & Channel Sessions)"

    @pytest.mark.parametrize("tier", TIERS)
    def test_wording(self, tier):
        text = render_platform_instructions(tier)
        section = _section(text, self.SECTION)
        assert "Claude Code's auto-memory (under `~/.claude/projects/`)" in section
        assert "shared across all users" in section
        assert "write_user_memory" in section
        assert "agent memory directory" not in text
        assert "~/.claude/projects/memory/" not in text


# T9 — no /tmp size typed into the block (it is operator-tunable).
def test_no_tmp_size_number():
    block = _section(_render(PromptTier.VERBOSE, "claude-code"))
    assert not re.search(r"\b\d+\s*(MB|MiB|GB|m\b|g\b)", block), "a /tmp size crept into the block"


# T10 — Codex is oriented to the tool the block names, and sees no Claude prefix.
def test_codex_orientation_names_refresh_metric_definitions():
    rendered = get_platform_system_prompt(runtime="codex")
    orientation = rendered.split("\n\n---\n\n", 1)[0]
    assert "`refresh_metric_definitions`" in orientation
    assert "mcp__trinity__" not in rendered
    assert "`refresh_metric_definitions`" in _section(rendered)


# T10b — the OpenCode twin (#2981): `trinity_`-prefixed, no Claude prefix.
def test_opencode_orientation_names_refresh_metric_definitions():
    rendered = get_platform_system_prompt(runtime="opencode")
    orientation = rendered.split("\n\n---\n\n", 1)[0]
    assert "`trinity_refresh_metric_definitions`" in orientation
    assert "mcp__trinity__" not in rendered
    assert "trinity_refresh_metric_definitions" in _section(rendered)


# T11 — a force-replace pull is the one path that deletes untracked files
# (agent_server git router: `git reset --hard` then `git clean -fd`).
def test_force_replace_pull_is_named_as_the_exception():
    block = _section(_render(PromptTier.VERBOSE, "claude-code"))
    assert "and never touches untracked ones. A force-replace pull" in block
    assert "deletes untracked files that are not gitignored" in block
    assert "through `git_pull`" in block


# T12 — `data/` is gitignored only once `data_paths` is declared; it is not in
# the base `.gitignore` patterns, so the block must not say it always is.
def test_data_folder_ignore_is_conditional():
    block = _section(_render(PromptTier.VERBOSE, "claude-code"))
    assert "- `data/` — runtime data; the export/import boundary, gitignored once you declare `data_paths`" in block
    assert not re.search(r"`data/`[^\n]*gitignored$", block, re.M), "data/ cited as always gitignored"


# T13 — the agent server treats `claude` as Claude Code too (runtime_adapter,
# execution_env), so it runs the guardrail hook and gets the hooked sentence.
def test_claude_alias_gets_the_hooked_sentence():
    hooked = platform_prompt_service._PATH_DENY_SENTENCE_HOOKED
    unhooked = platform_prompt_service._PATH_DENY_SENTENCE_UNHOOKED
    rendered = _render(PromptTier.VERBOSE, "claude")
    assert hooked in rendered
    assert unhooked not in rendered
