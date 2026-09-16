"""#2850 — a comma-separated `allowed-tools` nulled the whole skill record.

`docker/base-image/agent_server/routers/skills.py` fed the raw frontmatter value
into `SkillInfo.allowed_tools: Optional[List[str]]`. Claude Code's canonical form
is the comma-separated string (`allowed-tools: Read, Bash`), so Pydantic rejected
the record, the outer `except` caught it, and the fallback re-added the skill with
only its directory name — description, argument hint, automation all gone, and
the warning re-fired on every `GET /api/skills`.

Three properties pinned here:

1. Both spellings of the same tools normalize to the same list, including
   entries with parentheses (`Bash(git:*)`) and spaces inside them.
2. One malformed field never nulls the record — the other parsed fields survive
   and only the offending field drops to `None`.
3. A skipped field is warned once per (file, field, value), not once per scan.

The scanner ships in the agent base image; these tests run against the source
tree via the `agent_server` namespace shim installed by `tests/unit/conftest.py`.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_server.routers import skills as skills_mod
from agent_server.routers.skills import (
    FieldSkipped,
    normalize_allowed_tools,
    scan_skills_directory,
)


@pytest.fixture(autouse=True)
def _fresh_warned_set(monkeypatch):
    """The warn-once ledger is module-global; give every test an empty one so
    a warning asserted here is one *this* test caused."""
    monkeypatch.setattr(skills_mod, "_SKIPPED_FIELD_WARNED", set())


def _write_skill(root: Path, name: str, frontmatter: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n\nBody.\n", encoding="utf-8")
    return skill_md


def _only(skills):
    assert len(skills) == 1, [s.name for s in skills]
    return skills[0]


# ---------------------------------------------------------------------------
# AC 1 + 2 — the string form renders, and both forms agree
# ---------------------------------------------------------------------------


def test_string_form_keeps_description_and_hint(tmp_path):
    _write_skill(
        tmp_path,
        "brief",
        "name: brief\n"
        "description: Write the morning brief\n"
        "argument-hint: <topic>\n"
        "automation: gated\n"
        "allowed-tools: Read, Write, Edit, Bash, Glob",
    )
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.name == "brief"
    assert skill.description == "Write the morning brief"
    assert skill.argument_hint == "<topic>"
    assert skill.automation == "gated"
    assert skill.user_invocable is True
    assert skill.allowed_tools == ["Read", "Write", "Edit", "Bash", "Glob"]


@pytest.mark.parametrize(
    "string_form, list_form",
    [
        ("Read, Bash", "[Read, Bash]"),
        ("Read,Bash,Glob", "[Read, Bash, Glob]"),
        ("Read, Bash(git:*), Grep", "[Read, 'Bash(git:*)', Grep]"),
        ("Bash(git *), Grep", "['Bash(git *)', Grep]"),
        ("Bash(ssh *), Bash(curl *), Read", "['Bash(ssh *)', 'Bash(curl *)', Read]"),
    ],
)
def test_string_and_list_forms_yield_identical_allowed_tools(tmp_path, string_form, list_form):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _write_skill(tmp_path / "a", "s", f"description: d\nallowed-tools: {string_form}")
    _write_skill(tmp_path / "b", "s", f"description: d\nallowed-tools: {list_form}")
    from_string = _only(scan_skills_directory(tmp_path / "a"))
    from_list = _only(scan_skills_directory(tmp_path / "b"))
    assert from_string.allowed_tools == from_list.allowed_tools
    assert from_string.allowed_tools is not None
    assert from_string.description == from_list.description == "d"


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        (",", None),
        (" , , ", None),
        ([], None),
        ("Read", ["Read"]),
        ("Read, Bash", ["Read", "Bash"]),
        ("Read,Bash", ["Read", "Bash"]),
        ("  Read ,  Bash  ", ["Read", "Bash"]),
        ("Bash(git *), Grep", ["Bash(git *)", "Grep"]),
        ("Bash(git:*), Read", ["Bash(git:*)", "Read"]),
        # a comma INSIDE a group is not a separator
        ("Bash(npm run lint, npm test), Read", ["Bash(npm run lint, npm test)", "Read"]),
        ("Bash(ls {a,b}), Read", ["Bash(ls {a,b})", "Read"]),
        ('Bash(git commit -m "a, b"), Read', ['Bash(git commit -m "a, b")', "Read"]),
        ("Bash(echo 'x, y'), Read", ["Bash(echo 'x, y')", "Read"]),
        # quotes are NOT tracked — an apostrophe in shell text must not drop the field
        ("Bash(echo it's), Read", ["Bash(echo it's)", "Read"]),
        ('Bash(echo "), Read', ['Bash(echo ")', "Read"]),
        (["Read", " Bash ", ""], ["Read", "Bash"]),
        (["Read", "Bash(git *)"], ["Read", "Bash(git *)"]),
        ("mcp__trinity__report, Skill", ["mcp__trinity__report", "Skill"]),
    ],
)
def test_normalize_allowed_tools_table(value, expected):
    assert normalize_allowed_tools(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        True,  # YAML 1.1: `allowed-tools: yes`
        42,
        {"Read": True},
        "Bash(git *",  # unbalanced group — silently merging the tail is the bug class
        "Bash(git *)), Read",
        [True],  # `allowed-tools: [yes]` — a bool is not a tool named "True"
        ["Read", ["nested"]],
    ],
)
def test_normalize_allowed_tools_rejects_unrepresentable(value):
    with pytest.raises(FieldSkipped):
        normalize_allowed_tools(value)


# ---------------------------------------------------------------------------
# AC 3 — one bad field never nulls the record
# ---------------------------------------------------------------------------


def test_malformed_allowed_tools_keeps_every_other_field(tmp_path):
    _write_skill(
        tmp_path,
        "deploy",
        "name: deploy\n"
        "description: Ship it\n"
        "argument-hint: <env>\n"
        "automation: autonomous\n"
        "user-invocable: false\n"
        "allowed-tools: {Read: yes}",
    )
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.allowed_tools is None
    assert skill.description == "Ship it"
    assert skill.argument_hint == "<env>"
    assert skill.automation == "autonomous"
    assert skill.user_invocable is False


def test_list_valued_description_drops_only_description(tmp_path):
    _write_skill(
        tmp_path,
        "odd",
        "description: [not, a, string]\n"
        "argument-hint: <x>\n"
        "allowed-tools: Read, Bash",
    )
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.description is None
    assert skill.argument_hint == "<x>"
    assert skill.allowed_tools == ["Read", "Bash"]


@pytest.mark.parametrize(
    "hint_yaml, expected",
    [
        ("[file]", "[file]"),  # Claude Code's documented idiom parses as a YAML list
        ("[issue-number, priority]", "[issue-number, priority]"),
        ("'[file]'", "[file]"),
        ("add [tagId] | remove [tagId] | list", "add [tagId] | remove [tagId] | list"),
        ("[]", None),  # an empty hint is no hint
        ("[file, ~]", "[file]"),  # a null element is dropped, not rendered as "None"
    ],
)
def test_argument_hint_bracket_form_is_restored_to_a_string(tmp_path, hint_yaml, expected):
    _write_skill(tmp_path, "s", f"description: d\nargument-hint: {hint_yaml}")
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.argument_hint == expected


def test_yaml_scalars_coerce_to_strings(tmp_path):
    # YAML 1.1 resolves an unquoted date and a bare number; neither must null the field.
    _write_skill(tmp_path, "s", "description: 2026-01-01\nargument-hint: 42\nallowed-tools: Read")
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.description == "2026-01-01"
    assert skill.argument_hint == "42"
    assert skill.allowed_tools == ["Read"]


def test_null_allowed_tools_means_unrestricted(tmp_path):
    _write_skill(tmp_path, "s", "description: d\nallowed-tools:")
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.allowed_tools is None
    assert skill.description == "d"


def test_path_outside_home_does_not_crash_the_scan(tmp_path):
    """The container root is `/home/developer`; a tmp_path (or a mount outside
    HOME) used to raise inside `relative_to` — in the fallback too."""
    skill_md = _write_skill(tmp_path, "s", "description: d")
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.path.endswith("s/SKILL.md")
    assert skill.description == "d"
    assert Path(skill.path).name == skill_md.name


def test_unreadable_skill_still_yields_minimal_record(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").mkdir()  # exists, but read_text raises
    skill = _only(scan_skills_directory(tmp_path))
    assert skill.name == "broken"
    assert skill.description is None


# ---------------------------------------------------------------------------
# AC 3 — warned once, not once per request
# ---------------------------------------------------------------------------


def _skipped_warnings(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "allowed-tools" in r.getMessage()
    ]


def test_skipped_field_is_warned_once_across_scans(tmp_path, caplog):
    _write_skill(tmp_path, "s", "description: d\nallowed-tools: {Read: yes}")
    with caplog.at_level(logging.DEBUG, logger=skills_mod.logger.name):
        scan_skills_directory(tmp_path)
        scan_skills_directory(tmp_path)
        scan_skills_directory(tmp_path)
    warnings = _skipped_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    msg = warnings[0].getMessage()
    assert "SKILL.md" in msg and "allowed-tools" in msg


def test_a_changed_malformed_value_warns_again(tmp_path, caplog):
    skill_md = _write_skill(tmp_path, "s", "description: d\nallowed-tools: {Read: yes}")
    with caplog.at_level(logging.WARNING, logger=skills_mod.logger.name):
        scan_skills_directory(tmp_path)
        skill_md.write_text("---\ndescription: d\nallowed-tools: yes\n---\n", encoding="utf-8")
        scan_skills_directory(tmp_path)
        scan_skills_directory(tmp_path)
    assert len(_skipped_warnings(caplog)) == 2


def test_warning_truncates_the_offending_value(tmp_path, caplog):
    big = "{" + ", ".join(f"k{i}: v{i}" for i in range(400)) + "}"
    _write_skill(tmp_path, "s", f"description: d\nallowed-tools: {big}")
    with caplog.at_level(logging.WARNING, logger=skills_mod.logger.name):
        scan_skills_directory(tmp_path)
    (record,) = _skipped_warnings(caplog)
    assert len(record.getMessage()) < 400


def test_healthy_skill_emits_no_warning(tmp_path, caplog):
    _write_skill(tmp_path, "s", "description: d\nallowed-tools: Read, Bash(git *)")
    with caplog.at_level(logging.WARNING, logger=skills_mod.logger.name):
        scan_skills_directory(tmp_path)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
