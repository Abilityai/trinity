"""trinity-enterprise#660 — a library skill's `category` reaches the listing.

Every skill in the library carries `category:` in its frontmatter and the
platform parser dropped it, so the Skills surfaces could not narrow by it.
These tests EXECUTE the path, none reads source text:

  * `extract_contract` — the key survives, a `trinity:` block wins, the value
    is one bounded printable line, and garbage reads as no category with no
    warning: a warning would ride every injection of that skill on every agent.
  * `list_skills` / `get_skill` and the REST route — the field reaches the
    listing entry and survives the router's explicit `SkillInfo(...)`.

No real Redis, no real Docker, no real agent.
Related flow: docs/memory/feature-flows/skills-tab.md
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import services.skill_service as skill_service_module
from services import skill_packaging as pkg
from services.skill_service import SkillService
from services.skill_source_clone import SkillSourceClone

_SOURCE_ID = "src_aaaaaaaa"

TAGGED_MD = "---\ndescription: tagged\ncategory: project-management\n---\n# tagged\n"
PLAIN_MD = "---\ndescription: plain\n---\n# plain\n"


# ---- the contract -------------------------------------------------------------

def test_the_contract_carries_a_category():
    contract, warnings = pkg.extract_contract({"category": "project-management"})
    assert contract["category"] == "project-management"
    assert warnings == []


def test_no_category_is_none():
    contract, warnings = pkg.extract_contract({"description": "x"})
    assert contract["category"] is None
    assert warnings == []


def test_a_trinity_block_wins():
    contract, _ = pkg.extract_contract({"category": "flat", "trinity": {"category": "scoped"}})
    assert contract["category"] == "scoped"


@pytest.mark.parametrize("garbage", [["a", "b"], {"a": 1}, 7, True])
def test_garbage_reads_as_no_category_and_never_warns(garbage):
    """A category only narrows a list. `frontmatter_invalid:*` would ride every
    injection of this skill on every agent, for a value nothing else reads."""
    contract, warnings = pkg.extract_contract({"category": garbage})
    assert contract["category"] is None
    assert warnings == []


def test_a_blank_category_is_none():
    contract, _ = pkg.extract_contract({"category": "   "})
    assert contract["category"] is None


def test_the_value_is_one_printable_bounded_line():
    """The author's text reaches a <select> option, so it is bounded here, once."""
    contract, _ = pkg.extract_contract({"category": "a‮b\n  c" + "x" * 200})
    value = contract["category"]
    assert value.startswith("ab c")
    assert "\n" not in value and "‮" not in value
    assert len(value) == pkg.CATEGORY_MAX_CHARS


# ---- harness: one library source on disk --------------------------------------

@pytest.fixture
def service(tmp_path):
    svc = SkillService()
    svc.library_root = tmp_path
    svc.library_path = tmp_path / _SOURCE_ID
    (svc.library_path / ".claude" / "skills").mkdir(parents=True)
    clone = SkillSourceClone(_SOURCE_ID, "https://github.com/owner/repo", "main", "branch", tmp_path)
    svc._clones = lambda enabled_only=True: [clone]
    svc._source_names = lambda: {_SOURCE_ID: "Test library"}
    clone.current_commit = MagicMock(return_value=None)
    for name, md in (("tagged", TAGGED_MD), ("plain", PLAIN_MD)):
        d = svc.library_path / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(md)
    clone.tree_shas = MagicMock(return_value={"tagged": "tree-tagged", "plain": "tree-plain"})
    return svc


# ---- the listing, get, and the REST route -------------------------------------

def test_list_skills_carries_it(service):
    by_name = {s["name"]: s for s in service.list_skills()}
    assert by_name["tagged"]["category"] == "project-management"
    assert by_name["plain"]["category"] is None


def test_a_skill_whose_parse_blows_up_has_no_category(service, monkeypatch):
    """`_parse_skill_info` swallows a parse failure and returns its defaults,
    which must include the key, or the listing would silently omit it."""
    monkeypatch.setattr(skill_service_module.pkg, "extract_contract",
                        MagicMock(side_effect=RuntimeError("boom")))
    by_name = {s["name"]: s for s in service.list_skills()}
    assert by_name["tagged"]["category"] is None


def test_get_skill_carries_it(service):
    assert service.get_skill("tagged")["category"] == "project-management"
    assert service.get_skill("plain")["category"] is None


def test_the_rest_listing_names_it(service, monkeypatch):
    """The route builds `SkillInfo(...)` field by field: a service key it does
    not name never leaves the backend."""
    from routers import skills as mod
    monkeypatch.setattr(mod, "skill_service", service)
    user = SimpleNamespace(id=1, username="owner", role="user", agent_name=None)

    body = {s.name: s.model_dump() for s in asyncio.run(mod.list_skills(current_user=user))}

    assert body["tagged"]["category"] == "project-management"
    assert body["plain"]["category"] is None
