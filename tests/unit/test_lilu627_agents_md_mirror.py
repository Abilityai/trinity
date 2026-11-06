"""Abilityai/lilu#627 — AGENTS.md is a refreshed copy of CLAUDE.md, not a link.

ent#819 forbids the platform creating links in an agent home, so the lilu#257
symlink became a plain copy that Codex and OpenCode refresh at the start of
every turn (``agent_server/services/agents_md_mirror.py``). A provenance
record (sha256 of the last copy we wrote) decides whether a regular AGENTS.md
is ours to refresh or authored and never to be touched.

The three cases the decision named: no file, our stale copy, an authored file.
"""
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = (
    Path(__file__).resolve().parents[2]
    / "docker" / "base-image" / "agent_server" / "services" / "agents_md_mirror.py"
)
_spec = importlib.util.spec_from_file_location("agents_md_mirror_627", _MODULE)
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("v1\n")
    return tmp_path


def _refresh(home):
    return mirror.refresh_agents_md(home=str(home), record_path=str(home / ".tmp" / "rec"))


def test_no_file_gets_a_regular_copy_and_a_record(home):
    assert _refresh(home) == "refreshed"
    agents = home / "AGENTS.md"
    assert agents.is_file() and not agents.is_symlink()
    assert agents.read_text() == "v1\n"
    assert (home / ".tmp" / "rec").read_text().strip() == mirror._sha256(b"v1\n")


def test_our_stale_copy_is_refreshed(home):
    _refresh(home)
    (home / "CLAUDE.md").write_text("v2 — edited by a git sync\n")
    assert _refresh(home) == "refreshed"
    assert (home / "AGENTS.md").read_text() == "v2 — edited by a git sync\n"
    assert _refresh(home) == "current"  # no churn once in step


def test_authored_file_is_never_overwritten(home):
    (home / "AGENTS.md").write_text("hand-written codex instructions\n")
    assert _refresh(home) == "authored"
    assert (home / "AGENTS.md").read_text() == "hand-written codex instructions\n"


def test_an_edited_copy_becomes_authored(home):
    """The author edited our copy: its hash no longer matches the record."""
    _refresh(home)
    (home / "AGENTS.md").write_text("v1\nplus an author's line\n")
    (home / "CLAUDE.md").write_text("v2\n")
    assert _refresh(home) == "authored"
    assert (home / "AGENTS.md").read_text() == "v1\nplus an author's line\n"


def test_a_leftover_symlink_is_replaced_by_a_copy(home):
    (home / "AGENTS.md").symlink_to("CLAUDE.md")
    assert _refresh(home) == "refreshed"
    assert not (home / "AGENTS.md").is_symlink()
    assert (home / "CLAUDE.md").read_text() == "v1\n"


def test_no_claude_md_writes_nothing(tmp_path):
    assert _refresh(tmp_path) == "no-source"
    assert not (tmp_path / "AGENTS.md").exists()


def test_both_runtimes_refresh_at_turn_start():
    services = _MODULE.parent
    for name in ("codex_runtime.py", "opencode_runtime.py"):
        assert "refresh_agents_md()" in (services / name).read_text(), name
