"""Unit tests for the Alembic single-head guard (#2068).

The guard asserts that an Alembic version-line resolves to **exactly one head**.
Two revisions sharing a parent are two heads; `alembic upgrade head` (singular)
then raises `CommandError: Multiple head revisions are present`, which on the
OSS track crash-loops boot and on the enterprise track is swallowed by
`register_enterprise`'s broad `except` — the instance silently degrades to
OSS-only (#2068).

Coverage mirrors the acceptance criteria:

  (a) the REAL OSS graph resolves to one head — a regression lock, not a fixture;
  (b) a synthetic two-head graph FAILS;
  (c) a synthetic **merge revision** (tuple `down_revision`) PASSES — the guard
      ships beside exactly such a revision, so mishandling tuples would fail the
      very fix it guards;
  (d) an ABSENT version directory exits 0 with "skipped" (uninitialised submodule);
  (e) an existing-but-EMPTY version directory exits 0 with "skipped";
  (f) a directory holding `*.py` files that parse to ZERO revisions FAILS —
      a guard that reports green when it understood nothing is worse than none.
"""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Load the guard from scripts/ci/ (not an importable package) — same technique
# tests/unit/test_alembic_parity_guard.py uses for its sibling script.
_GUARD_PATH = REPO_ROOT / "scripts" / "ci" / "check_alembic_heads.py"
_spec = importlib.util.spec_from_file_location("check_alembic_heads", _GUARD_PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

OSS_VERSIONS = REPO_ROOT / "src" / "backend" / "migrations" / "versions"


def _revision_file(
    directory: Path,
    filename: str,
    revision: str,
    down_revision,
    *,
    docstring: str = "",
) -> Path:
    """Write a minimal Alembic revision file into `directory`."""
    path = directory / filename
    path.write_text(
        f'"""{docstring or filename}\n\n'
        f"Revision ID: {revision}\n"
        '"""\n'
        "from alembic import op\n\n"
        f"revision = {revision!r}\n"
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    return path


def _linear_chain(directory: Path) -> None:
    """0001 → 0002 → 0003, a healthy single-head line."""
    _revision_file(directory, "0001_base.py", "0001_base", None)
    _revision_file(directory, "0002_two.py", "0002_two", "0001_base")
    _revision_file(directory, "0003_three.py", "0003_three", "0002_two")


# --- parsing ------------------------------------------------------------------


def test_parses_single_string_down_revision():
    source = 'revision = "0002_two"\ndown_revision = "0001_base"\n'
    assert guard.parse_revision_source(source) == ("0002_two", ("0001_base",))


def test_parses_none_down_revision_as_base():
    source = 'revision = "0001_base"\ndown_revision = None\n'
    assert guard.parse_revision_source(source) == ("0001_base", ())


def test_parses_tuple_down_revision_with_all_members():
    """A merge revision's tuple contributes EVERY member as a parent."""
    source = 'revision = "0015_merge"\ndown_revision = ("0010_a", "0014_b")\n'
    assert guard.parse_revision_source(source) == ("0015_merge", ("0010_a", "0014_b"))


def test_parses_list_down_revision():
    source = "revision = '0015_merge'\ndown_revision = ['0010_a', '0014_b']\n"
    assert guard.parse_revision_source(source) == ("0015_merge", ("0010_a", "0014_b"))


def test_parses_multiline_tuple_down_revision():
    source = (
        'revision = "0015_merge"\n'
        "down_revision = (\n"
        '    "0010_a",\n'
        '    "0014_b",\n'
        ")\n"
    )
    assert guard.parse_revision_source(source) == ("0015_merge", ("0010_a", "0014_b"))


def test_parses_annotated_assignment_form():
    """Alembic's newer template emits `revision: str = '...'`."""
    source = (
        "from typing import Sequence, Union\n"
        "revision: str = '0002_two'\n"
        "down_revision: Union[str, Sequence[str], None] = ('0001_base',)\n"
    )
    assert guard.parse_revision_source(source) == ("0002_two", ("0001_base",))


def test_docstring_prose_mentioning_revision_is_not_an_assignment():
    """`0008a_widen_alembic_version` has a docstring line starting with 'revision'."""
    source = (
        '"""widen the version column\n\n'
        "revision exists so an *existing* PG deployment keeps its stamped\n"
        'down_revision = "not-a-real-assignment" prose.\n'
        '"""\n'
        'revision = "0008a_widen"\n'
        'down_revision = "0008_prior"\n'
    )
    assert guard.parse_revision_source(source) == ("0008a_widen", ("0008_prior",))


def test_unparseable_file_yields_no_revision():
    assert guard.parse_revision_source("def broken(:\n") == (None, ())


def test_non_literal_revision_yields_no_revision():
    assert guard.parse_revision_source("revision = SOME_CONSTANT\n") == (None, ())


# --- head computation ---------------------------------------------------------


def test_find_heads_linear_chain(tmp_path):
    _linear_chain(tmp_path)
    assert guard.find_heads(guard.collect_graph(tmp_path)) == ["0003_three"]


def test_find_heads_reports_both_forks(tmp_path):
    _linear_chain(tmp_path)
    _revision_file(tmp_path, "0003_fork.py", "0003_fork", "0002_two")
    assert sorted(guard.find_heads(guard.collect_graph(tmp_path))) == [
        "0003_fork",
        "0003_three",
    ]


def test_merge_revision_collapses_two_heads_to_one(tmp_path):
    """(c) The exact shape of the #2068 fix — tuple parent, one head."""
    _linear_chain(tmp_path)
    _revision_file(tmp_path, "0003_fork.py", "0003_fork", "0002_two")
    _revision_file(
        tmp_path, "0004_merge.py", "0004_merge", ("0003_fork", "0003_three")
    )
    assert guard.find_heads(guard.collect_graph(tmp_path)) == ["0004_merge"]


def test_shared_parent_of_two_heads(tmp_path):
    _linear_chain(tmp_path)
    _revision_file(tmp_path, "0003_fork.py", "0003_fork", "0002_two")
    graph = guard.collect_graph(tmp_path)
    by_id = {rev.revision: rev for rev in graph}
    assert guard.shared_parent(by_id, guard.find_heads(graph)) == "0002_two"


def test_init_py_is_not_a_revision_file(tmp_path):
    _linear_chain(tmp_path)
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    assert [p.name for p in guard.revision_files(tmp_path)] == [
        "0001_base.py",
        "0002_two.py",
        "0003_three.py",
    ]


# --- check_directory: exit codes ---------------------------------------------


def test_single_head_directory_passes(tmp_path, capsys):
    _linear_chain(tmp_path)
    assert guard.check_directory(tmp_path) == 0
    assert "1 head" in capsys.readouterr().out


def test_two_head_directory_fails(tmp_path, capsys):
    """(b) The #2068 condition itself."""
    _linear_chain(tmp_path)
    _revision_file(tmp_path, "0003_fork.py", "0003_fork", "0002_two")
    assert guard.check_directory(tmp_path) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Every head is named, with its filename, plus the shared parent.
    assert "0003_fork" in combined
    assert "0003_three" in combined
    assert "0003_fork.py" in combined
    assert "0002_two" in combined
    assert "2 heads" in combined


def test_merge_revision_directory_passes(tmp_path, capsys):
    """(c) end-to-end: the merge-revision fix shape exits 0."""
    _linear_chain(tmp_path)
    _revision_file(tmp_path, "0003_fork.py", "0003_fork", "0002_two")
    _revision_file(
        tmp_path, "0004_merge.py", "0004_merge", ("0003_fork", "0003_three")
    )
    assert guard.check_directory(tmp_path) == 0
    assert "0004_merge" in capsys.readouterr().out


def test_absent_directory_skips(tmp_path, capsys):
    """(d) Uninitialised submodule — the deep versions/ path does not exist."""
    missing = tmp_path / "enterprise" / "backend" / "migrations" / "versions"
    assert guard.check_directory(missing) == 0
    assert "skipped" in capsys.readouterr().out


def test_empty_directory_skips(tmp_path, capsys):
    """(e) Partially-initialised checkout — directory present, no revisions."""
    empty = tmp_path / "versions"
    empty.mkdir()
    assert guard.check_directory(empty) == 0
    assert "skipped" in capsys.readouterr().out


def test_directory_with_only_init_py_skips(tmp_path, capsys):
    empty = tmp_path / "versions"
    empty.mkdir()
    (empty / "__init__.py").write_text("", encoding="utf-8")
    assert guard.check_directory(empty) == 0
    assert "skipped" in capsys.readouterr().out


def test_unparseable_revisions_fail_closed(tmp_path, capsys):
    """(f) `*.py` present but zero parseable revisions → FAIL, never a green skip."""
    (tmp_path / "0001_broken.py").write_text("def nope(:\n", encoding="utf-8")
    (tmp_path / "0002_alsobroken.py").write_text("x = 1\n", encoding="utf-8")
    assert guard.check_directory(tmp_path) == 1
    combined = capsys.readouterr()
    assert "skipped" not in (combined.out + combined.err)


def test_cycle_reports_zero_heads(tmp_path, capsys):
    _revision_file(tmp_path, "0001_a.py", "0001_a", "0002_b")
    _revision_file(tmp_path, "0002_b.py", "0002_b", "0001_a")
    assert guard.check_directory(tmp_path) == 1
    combined = capsys.readouterr()
    assert "0 heads" in (combined.out + combined.err)


# --- main() -------------------------------------------------------------------


def test_main_requires_at_least_one_directory(capsys):
    assert guard.main(["check_alembic_heads.py"]) == 2


def test_main_checks_every_directory_even_after_a_failure(tmp_path, capsys):
    good = tmp_path / "good"
    good.mkdir()
    _linear_chain(good)
    bad = tmp_path / "bad"
    bad.mkdir()
    _linear_chain(bad)
    _revision_file(bad, "0003_fork.py", "0003_fork", "0002_two")

    # Bad first: the good directory must still be reported (no short-circuit).
    assert guard.main(["check_alembic_heads.py", str(bad), str(good)]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(bad) in combined
    assert str(good) in combined


def test_main_passes_when_every_directory_is_clean(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    _linear_chain(good)
    missing = tmp_path / "absent"
    assert guard.main(["check_alembic_heads.py", str(good), str(missing)]) == 0


# --- (a) the real OSS graph ---------------------------------------------------


def test_real_oss_graph_has_exactly_one_head():
    """Regression lock on the live OSS version-line (Architectural Invariant #3)."""
    assert OSS_VERSIONS.is_dir(), f"expected {OSS_VERSIONS} to exist"
    graph = guard.collect_graph(OSS_VERSIONS)
    assert len(graph) >= 30, "OSS revisions failed to parse — guard is blind"
    heads = guard.find_heads(graph)
    assert heads == sorted(heads)
    assert len(heads) == 1, f"OSS Alembic line has {len(heads)} heads: {heads}"


def test_real_oss_graph_passes_check_directory():
    assert guard.check_directory(OSS_VERSIONS) == 0


def test_every_real_oss_revision_parses():
    """No revision file may be silently unreadable — that would hide a head."""
    files = guard.revision_files(OSS_VERSIONS)
    parsed = {rev.filename for rev in guard.collect_graph(OSS_VERSIONS)}
    unparsed = sorted(p.name for p in files if p.name not in parsed)
    assert unparsed == [], f"unparseable OSS revision files: {unparsed}"


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_revision_id_is_not_a_revision(bad):
    source = f'revision = "{bad}"\ndown_revision = None\n'
    assert guard.parse_revision_source(source) == (None, ())
