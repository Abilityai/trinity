"""trinity-enterprise#754 — the agent server says where each skill came from.

The merged Skills tab splits an agent's skills into **Own** (the agent's own
`.claude/skills/`) and **Shared** (library skills the platform delivered), and
warns the owner about a skill whose author recommends approval. Two facts the
agent server's `GET /api/skills` did not report:

- `source`: the platform marks every library skill it delivers with
  `.trinity-skill.json` (`skill_packaging.META_FILENAME`); a directory without
  it is the agent's own (#2914). `dir` is the directory name, which the gate
  fingerprint resolves BEFORE the frontmatter `name` (the two can differ).
- `approval`: the library contract's `approval: recommended` (ent#753), read
  with the SAME precedence the backend uses — a `trinity:` block first, then
  the flat key — and the same closed value set.

Both are informational: the agent can write the marker and the frontmatter,
so neither decides access; the platform's gate map stays the authority.

The scanner ships in the agent base image; these tests run against the source
tree through the `agent_server` namespace shim in `tests/unit/conftest.py`, and
the parity table runs the SAME documents through the backend's real parser.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_server.routers import skills as skills_mod
from agent_server.routers.skills import scan_skills_directory


@pytest.fixture(autouse=True)
def _fresh_warned_set(monkeypatch):
    monkeypatch.setattr(skills_mod, "_SKIPPED_FIELD_WARNED", set())


def _write_skill(root: Path, dirname: str, frontmatter: str, *, marker: bool = False) -> Path:
    skill_dir = root / dirname
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n# {dirname}\n\nBody.\n", encoding="utf-8")
    if marker:
        (skill_dir / ".trinity-skill.json").write_text('{"version": "abc"}', encoding="utf-8")
    return skill_dir


def _by_name(skills):
    return {s.name: s for s in skills}


# ---------------------------------------------------------------------------
# source + dir
# ---------------------------------------------------------------------------


def test_marker_present_is_platform_absent_is_agent(tmp_path):
    _write_skill(tmp_path, "from-library", "name: from-library\ndescription: d", marker=True)
    _write_skill(tmp_path, "my-own", "name: my-own\ndescription: d")

    got = _by_name(scan_skills_directory(tmp_path))

    assert got["from-library"].source == "platform"
    assert got["my-own"].source == "agent"


def test_dir_is_reported_even_when_frontmatter_name_differs(tmp_path):
    """The gate fingerprint resolves a name by DIRECTORY first; a gate keyed on
    the directory must still find its card when the frontmatter says otherwise."""
    _write_skill(tmp_path, "weekly-report", "name: Weekly Report\ndescription: d")

    (only,) = scan_skills_directory(tmp_path)

    assert only.name == "Weekly Report"
    assert only.dir == "weekly-report"


def test_unreadable_skill_fallback_still_reports_source_and_dir(tmp_path):
    """The last-resort record (an unreadable SKILL.md) keeps the two facts the
    tab needs to place the card: they come from the filesystem, not the file."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"\xff\xfe\x00not utf8")
    (d / ".trinity-skill.json").write_text("{}", encoding="utf-8")

    (only,) = scan_skills_directory(tmp_path)

    assert only.name == "broken"
    assert only.source == "platform"
    assert only.dir == "broken"


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frontmatter, expected", [
    ("name: s\napproval: recommended", "recommended"),
    ("name: s\napproval: ' Recommended '", "recommended"),
    ("name: s\ntrinity:\n  approval: recommended", "recommended"),
    # the trinity: block wins whichever value it holds, as on the backend
    ("name: s\napproval: recommended\ntrinity:\n  approval: other", None),
    ("name: s\napproval: other\ntrinity:\n  approval: recommended", "recommended"),
    # YAML 1.1 turns these into a bool / a date — never "recommended"
    ("name: s\napproval: yes", None),
    ("name: s\napproval: 2026-10-01", None),
    ("name: s\napproval: [recommended]", None),
    ("name: s\napproval: required", None),
    ("name: s", None),
])
def test_approval_is_the_closed_set_with_backend_precedence(tmp_path, frontmatter, expected):
    _write_skill(tmp_path, "s", frontmatter)

    (only,) = scan_skills_directory(tmp_path)

    assert only.approval == expected


def test_a_bad_approval_value_keeps_the_rest_of_the_record(tmp_path):
    """#2850's rule: one malformed field never nulls the record."""
    _write_skill(tmp_path, "s", "name: s\ndescription: keeps me\napproval: [1, 2]\nautomation: manual")

    (only,) = scan_skills_directory(tmp_path)

    assert only.approval is None
    assert only.description == "keeps me"
    assert only.automation == "manual"


# ---------------------------------------------------------------------------
# Parity with the backend's contract parser — one table, both parsers
# ---------------------------------------------------------------------------

_PARITY_DOCS = [
    "name: s\napproval: recommended",
    "name: s\napproval: RECOMMENDED",
    "name: s\napproval: '  recommended\t'",
    "name: s\ntrinity:\n  approval: recommended",
    "name: s\napproval: recommended\ntrinity:\n  approval: nope",
    "name: s\napproval: nope\ntrinity:\n  approval: recommended",
    "name: s\napproval: recommended\ntrinity:\n  approval: null",
    "name: s\napproval: yes",
    "name: s\napproval: true",
    "name: s\napproval: 3",
    "name: s\napproval: 2026-10-01",
    "name: s\napproval: {x: 1}",
    "name: s\napproval: ''",
    "name: s",
]


@pytest.mark.parametrize("frontmatter", _PARITY_DOCS)
def test_agent_server_and_backend_agree_on_approval(tmp_path, frontmatter):
    from services import skill_packaging

    text = f"---\n{frontmatter}\n---\n\n# s\n"
    parsed, _warn = skill_packaging.parse_frontmatter(text)
    backend_contract, _warnings = skill_packaging.extract_contract(parsed)

    _write_skill(tmp_path, "s", frontmatter)
    (only,) = scan_skills_directory(tmp_path)

    assert only.approval == backend_contract["approval"], frontmatter


def test_vendored_constants_match_the_backend():
    """The agent server cannot import `src/backend`; the marker name and the
    approval value set are duplicated, and this keeps the copies equal."""
    from services import skill_packaging

    assert skills_mod.PLATFORM_MARKER_FILENAME == skill_packaging.META_FILENAME
    assert set(skills_mod.APPROVAL_VALUES) == set(skill_packaging.APPROVAL_VALUES)
