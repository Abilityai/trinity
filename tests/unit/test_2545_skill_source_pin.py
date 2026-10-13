"""The bundled community-catalog pin moves in lockstep with the library (#2545).

`config.DEFAULT_SKILL_SOURCE_REF` is the tag a FRESH install's default skills
source is seeded at (ent#237 AC#3/AC#5). Instances only ever see tagged states
of `abilityai/trinity-skills`, so until the pin moves a fresh install seeds a
catalog that is releases stale — the code comment says "bump this in lockstep
with the catalog's releases", and the library had been ahead of the pin since
v0.1.1 by the time #2545 moved it.

Four properties, none of them a literal that the next bump must edit:

* **Floor, not literal.** The pin is at least v0.3.0 — the release that added
  `update-dashboard`, the playbook the metrics tiles point users at (#3123;
  #2545 had set the floor at v0.2.0, the `project-management` release). A floor
  fails on a regression (a bad merge or revert resetting the pin) and stays
  valid across every future bump; an exact-literal assertion would just be a
  change-detector.
* **Parity with the documented example.** `.env.example` shows the default as
  the value of `TRINITY_DEFAULT_SKILL_SOURCE_REF`. A stale example is exactly
  the "docs mention of the default pin" line item the bump's AC carries.
* **Parity with the user doc** (#3123). `docs/user-docs/automation/
  skills-and-playbooks.md` states the current pin in prose; #2551 had to
  remember it by hand. With both parity checks the next bump is a three-file
  change that cannot leave one behind.
* **The seed note reports the configured ref** (AC#1) — the one line an
  operator sees at boot that says which catalog their fresh install got.

All three static checks parse SOURCE, not the imported module: `config.py`
honours the env var, so a developer's shell override would otherwise leak into
the assertion.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = _REPO / "src" / "backend" / "config.py"
_ENV_EXAMPLE = _REPO / ".env.example"
_USER_DOC = _REPO / "docs" / "user-docs" / "automation" / "skills-and-playbooks.md"

# The three files that state the bundled pin; they move together.
_PIN_FILES = (
    "src/backend/config.py, .env.example and "
    "docs/user-docs/automation/skills-and-playbooks.md"
)

# Why v0.3.0 (#3123): it is the first catalog that lets an agent get, from the
# Library, the `update-dashboard` playbook that
# `metric_read_service.EMPTY_METRIC_MESSAGE` and the Dashboard tile
# (`DashboardPanel.vue`, "Run /update-dashboard playbook") name. That copy says
# "if the agent has that playbook", so the floor is about availability, not a
# broken promise. (v0.2.0, #2545's floor, added the `project-management`
# category.) Raise only when a later release becomes the new floor an install
# cannot do without.
#
# The limit: this guards the VERSION, not whether `update-dashboard` is in the
# tag. Before moving the pin, check membership against the real upstream tag —
# it must print the path:
#
#   git clone --bare https://github.com/abilityai/trinity-skills /tmp/ts && \
#     git -C /tmp/ts ls-tree --name-only -r <tag> skills/update-dashboard/SKILL.md
#
# And review what the new tag brings into a fleet (skills carry executables):
#
#   git -C /tmp/ts diff --stat <old> <new> -- ':!*.md'      # non-instruction files
#   git -C /tmp/ts diff --name-status <old> <new>          # added / modified files
#   git -C /tmp/ts diff <old> <new> | grep -E '^\+.*https?://'   # URLs in added lines
_FLOOR = (0, 3, 0)


def _config_default_ref() -> str:
    m = re.search(
        r'DEFAULT_SKILL_SOURCE_REF\s*=\s*os\.getenv\(\s*"TRINITY_DEFAULT_SKILL_SOURCE_REF"\s*,\s*"([^"]+)"\s*\)',
        _CONFIG.read_text(),
    )
    assert m, "DEFAULT_SKILL_SOURCE_REF default not found in config.py"
    return m.group(1)


def _env_example_ref() -> str:
    m = re.search(r"^#?\s*TRINITY_DEFAULT_SKILL_SOURCE_REF=(\S+)", _ENV_EXAMPLE.read_text(), re.M)
    assert m, "TRINITY_DEFAULT_SKILL_SOURCE_REF example not found in .env.example"
    return m.group(1)


def _user_doc_refs() -> list[str]:
    return re.findall(r"`(v\d+\.\d+\.\d+)` of `trinity-skills`", _USER_DOC.read_text())


def _semver(tag: str) -> tuple[int, ...]:
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    assert m, f"bundled pin {tag!r} is not a vMAJOR.MINOR.PATCH tag (ent#237 AC#5: tag-pinned)"
    return tuple(int(x) for x in m.groups())


def test_default_pin_is_at_least_the_floor_release():
    ref = _config_default_ref()
    floor = "v" + ".".join(map(str, _FLOOR))
    assert _semver(ref) >= _FLOOR, (
        f"DEFAULT_SKILL_SOURCE_REF={ref!r} is below the floor {floor}. A fresh install "
        "would seed a catalog without `update-dashboard`, the playbook "
        "metric_read_service.EMPTY_METRIC_MESSAGE and the Dashboard tile send users to "
        f"(#3123). Fix: move the pin in {_PIN_FILES} together, then re-run the "
        "membership check in the _FLOOR comment against the new tag."
    )


def test_env_example_documents_the_same_default():
    assert _env_example_ref() == _config_default_ref(), (
        ".env.example's TRINITY_DEFAULT_SKILL_SOURCE_REF example is stale — the "
        f"pin is bumped in {_PIN_FILES} together (#2545, #3123)"
    )


def test_user_doc_states_the_same_default():
    refs = _user_doc_refs()
    assert len(refs) == 1, (
        f"expected exactly one \"`vX.Y.Z` of `trinity-skills`\" in {_USER_DOC.name}, "
        f"found {refs!r} — the pin sentence was reworded? update this regex"
    )
    assert refs[0] == _config_default_ref(), (
        f"{_USER_DOC.name} states the bundled pin as {refs[0]!r} but config.py "
        f"defaults to {_config_default_ref()!r} — the pin is bumped in {_PIN_FILES} "
        "together (#3123)"
    )


def test_seed_note_reports_the_configured_ref(tmp_path, monkeypatch, capsys):
    """AC#1: the note printed on a fresh install shows the ref that was seeded."""
    import sqlite3

    import config
    import database as D
    from db.schema import init_schema

    db_path = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = sqlite3.connect(db_path)
    init_schema(conn.cursor(), conn)
    conn.commit()

    monkeypatch.setattr(config, "DEFAULT_SKILL_SOURCE_REF", "v9.9.9")
    D._seed_fresh_install_skill_source(conn.cursor(), conn)

    out = capsys.readouterr().out
    assert "seeded default skills source" in out
    assert "@ v9.9.9" in out, out
    assert conn.execute("SELECT ref FROM skill_sources").fetchone()[0] == "v9.9.9"
