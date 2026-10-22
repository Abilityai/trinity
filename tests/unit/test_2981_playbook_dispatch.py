"""OpenCode playbook dispatch (#2981, checkpoint E).

``/<skill> [args]`` on the first non-blank line of a chat or headless prompt
runs as ``opencode run --command <skill> -- <args>``, against ``command:``
entries rendered per turn into the private ``opencode.json`` from the agent's
skills (ruling T2: config entries, no workspace shim directory).

Schema verified against the pinned OpenCode v1.18.32 source — cited in
``agent_server/services/opencode_commands.py``: one entry is
``{template (required), description?, agent?, model?, variant?, subtask?}``
and the handler substitutes ``$ARGUMENTS`` with the raw argument string.
"""

from __future__ import annotations

import json
import logging
import os

import pytest

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_commands import (  # noqa: E402
    discover_playbook_commands,
    split_playbook_prefix,
)
from agent_server.services.opencode_runtime import OpenCodeRuntime  # noqa: E402

# Checkpoint A's fake subprocess + stream builders + autouse provider keys,
# loaded by path (the test_opencode_controls.py idiom).
import importlib.util as _ilu  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "_opencode_runtime_suite_e", _Path(__file__).with_name("test_opencode_runtime.py")
)
_suite = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_suite)
_install_fake_subprocess = _suite._install_fake_subprocess
_provider_keys_present = _suite._provider_keys_present
_step = _suite._step


def _skill(root, dirname, frontmatter=None, body="Do the thing.\n"):
    d = root / ".claude" / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    head = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ""
    (d / "SKILL.md").write_text(head + body, encoding="utf-8")
    return d / "SKILL.md"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A project dir and a separate personal HOME, so both locations are real."""
    project = tmp_path / "project"
    personal = tmp_path / "home"
    project.mkdir()
    personal.mkdir()
    monkeypatch.setenv("HOME", str(personal))
    return project, personal


# ---------------------------------------------------------------------------
# discovery → command entries
# ---------------------------------------------------------------------------

def test_one_entry_per_skill_md_across_locations(homes):
    project, personal = homes
    impl = _skill(project, "implement", "name: implement\ndescription: Build a feature")
    pub = _skill(personal, "canon-publish", "name: canon-publish")
    (project / ".claude" / "skills" / "no-skill-md").mkdir()
    (project / ".claude" / "skills" / "README.md").write_text("not a dir")

    commands = discover_playbook_commands(project)

    assert commands == {
        "implement": {
            "template": f"Follow the skill at {impl} with arguments: $ARGUMENTS",
            "description": "Build a feature",
        },
        "canon-publish": {
            "template": f"Follow the skill at {pub} with arguments: $ARGUMENTS",
        },
    }


def test_template_references_the_path_never_the_body(homes):
    project, _ = homes
    _skill(project, "secretive", "name: secretive", body="BODY-MARKER-XYZ\n")
    entry = discover_playbook_commands(project)["secretive"]
    assert "BODY-MARKER-XYZ" not in json.dumps(entry)
    assert os.path.isabs(entry["template"].split("Follow the skill at ")[1].split(" with")[0])


def test_nameless_frontmatter_falls_back_to_the_directory_name(homes):
    project, _ = homes
    _skill(project, "dir-named", frontmatter=None)
    assert "dir-named" in discover_playbook_commands(project)


def test_description_is_one_line_and_capped(homes):
    project, _ = homes
    long = "word " * 100
    _skill(project, "wordy", f"name: wordy\ndescription: |\n  line one\n  line two {long}")
    desc = discover_playbook_commands(project)["wordy"]["description"]
    assert "\n" not in desc
    assert desc.startswith("line one line two word")
    assert len(desc) <= 200 and desc.endswith("…")


def test_project_beats_personal_on_collision(homes):
    project, personal = homes
    mine = _skill(project, "review", "name: review\ndescription: project copy")
    _skill(personal, "review", "name: review\ndescription: personal copy")
    entry = discover_playbook_commands(project)["review"]
    assert entry["description"] == "project copy"
    assert str(mine) in entry["template"]


def test_unsafe_names_are_skipped_with_one_warning(homes, caplog):
    project, _ = homes
    _skill(project, "a", 'name: "-flag"')
    _skill(project, "b", 'name: "has space"')
    _skill(project, "c", "name: good")
    with caplog.at_level(logging.WARNING):
        commands = discover_playbook_commands(project)
    assert set(commands) == {"good"}
    warnings = [r for r in caplog.records if "not offered as /commands" in r.getMessage()]
    assert len(warnings) == 1
    assert "'-flag'" in warnings[0].getMessage() and "'has space'" in warnings[0].getMessage()


def test_skill_named_like_a_builtin_warns_and_still_registers(homes, caplog):
    """Review I5: OpenCode registers its built-ins (`init`, `review`) first and
    config commands after, so a same-named skill replaces the built-in."""
    project, _ = homes
    _skill(project, "review", "name: review")
    _skill(project, "backlog", "name: backlog")
    with caplog.at_level(logging.WARNING):
        commands = discover_playbook_commands(project)
    assert {"review", "backlog"} <= set(commands)
    warned = [r.getMessage() for r in caplog.records if "built-in" in r.getMessage()]
    assert len(warned) == 1 and "'review'" in warned[0]
    assert not any("backlog" in m for m in warned)


def test_no_skills_no_commands(homes):
    project, _ = homes
    assert discover_playbook_commands(project) == {}


# ---------------------------------------------------------------------------
# split_playbook_prefix
# ---------------------------------------------------------------------------

NAMES = {"implement", "status"}


@pytest.mark.parametrize("prompt,expected", [
    ("/implement plan.md --run x", ("implement", "plan.md --run x")),
    ("/status", ("status", "")),
    ("/status   ", ("status", "")),
    ("/implement plan.md --run x\n\nRiders:\n- one\n  - two",
     ("implement", "plan.md --run x\n\nRiders:\n- one\n  - two")),
    ("/implement\n\nriders below", ("implement", "riders below")),
    ("\n\n  /implement args\nmore", ("implement", "args\nmore")),
])
def test_dispatches_a_known_command(prompt, expected):
    assert split_playbook_prefix(prompt, NAMES) == expected


@pytest.mark.parametrize("prompt", [
    "/unknown args",                    # unknown name → prose
    "please run /implement args",       # not at line start
    "intro\n/implement args",           # not the first non-blank line
    "no slash at all",
    "/implementx args",                 # prefix of a name is not the name
    "",
    "   \n  ",
])
def test_everything_else_passes_through_unchanged(prompt):
    assert split_playbook_prefix(prompt, NAMES) == (None, prompt)


def test_no_commands_means_no_dispatch():
    assert split_playbook_prefix("/implement x", set()) == (None, "/implement x")


def test_shell_shaped_arguments_are_never_dispatched(caplog):
    """OpenCode runs `!`cmd`` in a command template through the shell AFTER
    substituting $ARGUMENTS (prompt.ts:1391-1407, v1.18.32), outside the
    permission layer — so such arguments go as prose instead."""
    prompt = "/implement !`id`\nrest"
    with caplog.at_level(logging.WARNING):
        assert split_playbook_prefix(prompt, NAMES) == (None, prompt)
    assert any("not dispatched" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# _build_command
# ---------------------------------------------------------------------------

def test_build_command_without_a_command_is_unchanged():
    cmd = OpenCodeRuntime()._build_command(model="a/b", session_id=None, directory=None)
    assert cmd == ["opencode", "run", "--format", "json", "--auto", "-m", "a/b"]


def test_build_command_with_a_command():
    cmd = OpenCodeRuntime()._build_command(
        model="a/b", session_id="ses_1", directory="/d", command="implement"
    )
    assert cmd == ["opencode", "run", "--format", "json", "--auto", "-m", "a/b",
                   "--session", "ses_1", "--dir", "/d", "--command", "implement"]


# ---------------------------------------------------------------------------
# the seam: rendered opencode.json
# ---------------------------------------------------------------------------

def _config_of(turn_dir):
    with open(f"{turn_dir}/opencode/opencode.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_rendered_config_carries_the_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    commands = {"implement": {"template": "Follow the skill at /x/SKILL.md with arguments: $ARGUMENTS"}}
    turn_dir = opencode_runtime._prepare_turn_config("exec-c", None, commands=commands)
    assert _config_of(turn_dir)["command"] == commands


def test_per_turn_commands_beat_the_shared_config(monkeypatch, tmp_path):
    shared = tmp_path / "shared" / "opencode"
    shared.mkdir(parents=True)
    (shared / "opencode.json").write_text(json.dumps({"command": {
        "implement": {"template": "operator version"},
        "operator-only": {"template": "kept"},
    }}))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))
    turn_dir = opencode_runtime._prepare_turn_config(
        "exec-s", None, commands={"implement": {"template": "skill version"}}
    )
    assert _config_of(turn_dir)["command"] == {
        "implement": {"template": "skill version"},
        "operator-only": {"template": "kept"},
    }


def test_no_commands_leaves_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    turn_dir = opencode_runtime._prepare_turn_config("exec-n", None, commands={})
    assert "command" not in _config_of(turn_dir)


# ---------------------------------------------------------------------------
# execute path (stubbed subprocess): chat and headless both dispatch
# ---------------------------------------------------------------------------

@pytest.fixture
def runtime_with_skill(monkeypatch, tmp_path, homes):
    project, _ = homes
    skill_md = _skill(project, "implement", "name: implement\ndescription: Build")
    monkeypatch.setattr(opencode_runtime, "_AGENT_HOME", str(project))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path / "oc"))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    rendered = {}
    real = opencode_runtime._prepare_turn_config

    def _spy(*a, **k):
        turn_dir = real(*a, **k)
        rendered.update(_config_of(turn_dir))
        return turn_dir

    monkeypatch.setattr(opencode_runtime, "_prepare_turn_config", _spy)
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    return runtime, rendered, skill_md, tmp_path / "oc"


@pytest.mark.asyncio
async def test_headless_dispatches_the_command(monkeypatch, runtime_with_skill):
    runtime, rendered, skill_md, root = runtime_with_skill
    argv = _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    await runtime.execute_headless(
        "/implement plan.md --run x\n\nriders", model="openrouter/x/y"
    )

    cmd = argv[-1]
    assert cmd[cmd.index("--command") + 1] == "implement"
    assert cmd[cmd.index("--command") + 1:] == ["implement", "--", "plan.md --run x\n\nriders"]
    assert rendered["command"]["implement"]["template"] == (
        f"Follow the skill at {skill_md} with arguments: $ARGUMENTS"
    )
    # Nothing is left on disk after the turn (ruling T2).
    assert not any((root / "turns").iterdir())


@pytest.mark.asyncio
async def test_chat_dispatches_a_bare_command_with_no_message(monkeypatch, runtime_with_skill):
    runtime, _rendered, _skill_md, _root = runtime_with_skill
    argv = _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    await runtime.execute("/implement", model="openrouter/x/y")

    cmd = argv[-1]
    assert cmd[-2:] == ["--command", "implement"]
    assert "--" not in cmd


@pytest.mark.asyncio
async def test_unknown_command_runs_as_prose(monkeypatch, runtime_with_skill):
    runtime, rendered, _skill_md, _root = runtime_with_skill
    argv = _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    await runtime.execute_headless("/nope do it", model="openrouter/x/y")

    cmd = argv[-1]
    assert "--command" not in cmd
    assert cmd[-2:] == ["--", "/nope do it"]
    assert "implement" in rendered["command"]  # still offered, just not invoked
