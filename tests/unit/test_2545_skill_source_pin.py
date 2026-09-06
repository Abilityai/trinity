"""The bundled community-catalog pin moves in lockstep with the library (#2545).

`config.DEFAULT_SKILL_SOURCE_REF` is the tag a FRESH install's default skills
source is seeded at (ent#237 AC#3/AC#5). Instances only ever see tagged states
of `abilityai/trinity-skills`, so until the pin moves a fresh install seeds a
catalog that is releases stale — the code comment says "bump this in lockstep
with the catalog's releases", and the library had been ahead of the pin since
v0.1.1 by the time #2545 moved it.

Three properties, none of them a literal that the next bump must edit:

* **Floor, not literal.** The pin is at least v0.2.0 — the release that added
  the `project-management` category. A floor fails on a regression (a bad merge
  or revert resetting the pin) and stays valid across every future bump; an
  exact-literal assertion would just be a change-detector.
* **Parity with the documented example.** `.env.example` shows the default as
  the value of `TRINITY_DEFAULT_SKILL_SOURCE_REF`. A stale example is exactly
  the "docs mention of the default pin" line item the bump's AC carries; this
  makes the next bump a two-file change that cannot leave one behind.
* **The seed note reports the configured ref** (AC#1) — the one line an
  operator sees at boot that says which catalog their fresh install got.

Both static checks parse SOURCE, not the imported module: `config.py` honours
the env var, so a developer's shell override would otherwise leak into the
assertion.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = _REPO / "src" / "backend" / "config.py"
_ENV_EXAMPLE = _REPO / ".env.example"

# The release that introduced the `project-management` category (14 runtime
# skills) — the reason #2545 moved the pin. Raise only when a later release
# becomes the new floor an install cannot do without.
_FLOOR = (0, 2, 0)


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


def _semver(tag: str) -> tuple[int, ...]:
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    assert m, f"bundled pin {tag!r} is not a vMAJOR.MINOR.PATCH tag (ent#237 AC#5: tag-pinned)"
    return tuple(int(x) for x in m.groups())


def test_default_pin_is_at_least_the_project_management_release():
    ref = _config_default_ref()
    assert _semver(ref) >= _FLOOR, (
        f"DEFAULT_SKILL_SOURCE_REF={ref!r} is below v{'.'.join(map(str, _FLOOR))}: a fresh "
        "install would seed a catalog without the project-management skills (#2545)"
    )


def test_env_example_documents_the_same_default():
    assert _env_example_ref() == _config_default_ref(), (
        ".env.example's TRINITY_DEFAULT_SKILL_SOURCE_REF example is stale — the "
        "pin is bumped in config.py AND .env.example together (#2545)"
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
