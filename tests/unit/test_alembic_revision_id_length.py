"""Guard: every Alembic revision id must fit ``alembic_version.version_num`` (#1420).

Alembic auto-creates ``version_num`` at a fixed width and PostgreSQL enforces it,
so a revision id longer than the column truncation-fails the version stamp on
boot — a P0 outage that SQLite (no VARCHAR enforcement) never surfaces. Trinity's
descriptive ``NNNN_<table>_<change>`` slugs are long, so this is a live hazard.

This lint runs in the SQLite-only test lane and catches the class cheaply. The
column was widened to 255 (#1420: env.py ``version_table_column_type`` +
``0001_baseline`` downgrade DDL + the ``0008a_widen_alembic_version`` migration
for existing DBs); keep all three in lockstep with ``_COLUMN_WIDTH`` below.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "backend" / "migrations" / "versions"
)
_ENV_PY = _VERSIONS_DIR.parent / "env.py"
_BASELINE = _VERSIONS_DIR / "0001_baseline.py"

# The width alembic_version.version_num is created at (#1420). If you change this,
# change env.py's _VERSION_TABLE_COLUMN_TYPE and 0001_baseline's downgrade DDL too.
_COLUMN_WIDTH = 255

_REVISION_RE = re.compile(r'^revision\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _revision_ids() -> list[tuple[str, str]]:
    ids = []
    for path in sorted(_VERSIONS_DIR.glob("*.py")):
        m = _REVISION_RE.search(path.read_text())
        if m:
            ids.append((path.name, m.group(1)))
    return ids


def test_migrations_dir_is_discovered():
    ids = _revision_ids()
    assert ids, f"no Alembic revisions found under {_VERSIONS_DIR}"


@pytest.mark.parametrize("filename,revision_id", _revision_ids())
def test_revision_id_fits_version_column(filename, revision_id):
    assert len(revision_id) <= _COLUMN_WIDTH, (
        f"{filename}: revision id '{revision_id}' is {len(revision_id)} chars, "
        f"exceeds alembic_version.version_num VARCHAR({_COLUMN_WIDTH}) — it will "
        f"truncation-fail the version stamp on PostgreSQL (#1420). Shorten the id "
        f"or widen the column in env.py + 0001_baseline + a new ALTER migration."
    )


def test_column_width_kept_in_lockstep():
    """The three places that must agree on the width (#1420). A drift here is how
    the outage would silently return (e.g. someone narrows one but not the ids)."""
    env_src = _ENV_PY.read_text()
    assert f"String({_COLUMN_WIDTH})" in env_src, (
        f"env.py must configure version_table_column_type=String({_COLUMN_WIDTH})"
    )
    baseline_src = _BASELINE.read_text()
    assert f"VARCHAR({_COLUMN_WIDTH})" in baseline_src, (
        f"0001_baseline downgrade must create alembic_version at VARCHAR({_COLUMN_WIDTH})"
    )
