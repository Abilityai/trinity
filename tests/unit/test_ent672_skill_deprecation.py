"""trinity-enterprise#672 — a library skill marked `deprecated: true` says so.

The library's contributor guide documents two lifecycle keys, `deprecated` and
`superseded-by`. The platform parser dropped both, so a retired skill was
listed, assignable and injected exactly like a live one. These tests pin the
replacement contract, each by EXECUTING the path (none reads source text):

  * `extract_contract` — both keys survive, in both spellings, with a
    `trinity:` block winning; garbage is a named `frontmatter_invalid:*`
    warning, never an error; the successor is the author's text (one printable
    line, capped) and is `None` unless the skill is deprecated.
  * `deprecation_warning` — the ONE producer of the machine code: the
    successor rides it only when it is a valid skill name.
  * `list_skills` / `get_skill` and the REST route — the fields reach the
    listing entry and survive the router's explicit `SkillInfo(...)`.
  * `_inject_skills_locked` — the code rides the per-skill warnings on every
    outcome (injected, unchanged, conflict).
  * `deliver_assigned` — the assign response carries it for a deprecated name
    on EVERY delivery outcome (a stopped agent never runs an injection), and a
    live skill's report is byte-identical to before.
  * `public_skills_result` — the start endpoint's projection keeps the code.
  * CLAUDE.md — deliberately NOT annotated: a holding agent's instructions are
    unchanged (the fourth acceptance criterion).

No real Redis, no real Docker, no real agent.
Related flow: docs/memory/feature-flows/skill-assignment.md
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.skill_service as skill_service_module
from services import skill_packaging as pkg
from services.skill_service import SkillService
from services.skill_source_clone import SkillSourceClone

AGENT = "acme-bot"
_SOURCE_ID = "src_aaaaaaaa"

# Synthetic stand-ins for the two shapes a library ships: a bare skill name and
# a sentence saying the successor lives in another catalog.
PROSE = "other-skill — another catalog (not reachable from this one)"
OLD_MD = "---\ndescription: old\ndeprecated: true\nsuperseded-by: backlog\n---\n# old\n"
PROSE_MD = f'---\ndescription: prose\ndeprecated: true\nsuperseded-by: "{PROSE}"\n---\n# prose\n'
LIVE_MD = "---\ndescription: live\n---\n# live\n"


def _contract(frontmatter_body: str):
    fm, warning = pkg.parse_frontmatter(f"---\n{frontmatter_body}\n---\n# body\n")
    assert warning is None
    return pkg.extract_contract(fm)


# ---- extract_contract: `deprecated` ------------------------------------------

@pytest.mark.parametrize("body, expected, warnings", [
    ("deprecated: true", True, []),
    ("deprecated: yes", True, []),                      # a YAML 1.1 boolean
    ("deprecated: false", False, []),
    ("name: x", False, []),                             # absent
    ("deprecated:", False, []),                         # null
    ('deprecated: "true"', False, ["frontmatter_invalid:deprecated"]),
    ("deprecated: 1", False, ["frontmatter_invalid:deprecated"]),
    ("deprecated: [true]", False, ["frontmatter_invalid:deprecated"]),
])
def test_deprecated_is_a_real_boolean_or_a_named_warning(body, expected, warnings):
    contract, got = _contract(body)
    assert contract["deprecated"] is expected
    assert got == warnings


# ---- extract_contract: `superseded-by` ---------------------------------------

@pytest.mark.parametrize("body, expected", [
    ("deprecated: true\nsuperseded-by: backlog", "backlog"),
    ("deprecated: true\nsuperseded_by: backlog", "backlog"),             # underscore spelling
    ("deprecated: true\nsuperseded-by: hyphen\nsuperseded_by: snake", "hyphen"),
    ("deprecated: true\nsuperseded-by:\nsuperseded_by: snake", "snake"),  # a null key masks nothing
    ("deprecated: true", None),
    ('deprecated: true\nsuperseded-by: "   "', None),                     # whitespace only
    (f'deprecated: true\nsuperseded-by: "{PROSE}"', PROSE),               # the author's text, kept
])
def test_successor_is_read_in_both_spellings_and_kept_as_text(body, expected):
    contract, warnings = _contract(body)
    assert contract["superseded_by"] == expected
    assert warnings == []


def test_a_trinity_block_wins_whichever_spelling_it_uses():
    contract, _ = _contract(
        "deprecated: true\nsuperseded-by: flat\ntrinity:\n  superseded_by: namespaced"
    )
    assert contract["superseded_by"] == "namespaced"


def test_a_null_key_in_a_trinity_block_does_not_un_deprecate():
    """The same rule as the successor: present-but-null masks nothing."""
    contract, warnings = _contract("deprecated: true\nsuperseded-by: backlog\ntrinity:\n  deprecated:")
    assert contract["deprecated"] is True
    assert contract["superseded_by"] == "backlog"
    assert warnings == []


def test_a_trinity_block_can_un_deprecate_a_flat_key():
    contract, _ = _contract("deprecated: true\nsuperseded-by: backlog\ntrinity:\n  deprecated: false")
    assert contract["deprecated"] is False
    assert contract["superseded_by"] is None


def test_successor_is_none_unless_the_skill_is_deprecated():
    contract, warnings = _contract("superseded-by: backlog")
    assert contract["deprecated"] is False
    assert contract["superseded_by"] is None
    assert warnings == []


@pytest.mark.parametrize("value", ["[a, b]", "{a: b}", "123", "on"])
def test_a_non_string_successor_is_a_named_warning(value):
    contract, warnings = _contract(f"deprecated: true\nsuperseded-by: {value}")
    assert contract["superseded_by"] is None
    assert warnings == ["frontmatter_invalid:superseded-by"]


def test_a_non_string_successor_is_named_even_on_a_live_skill():
    """The library's CI runs this parser: a malformed value must fail there
    whether or not the skill is deprecated yet."""
    contract, warnings = _contract("superseded-by: [a, b]")
    assert contract["superseded_by"] is None
    assert warnings == ["frontmatter_invalid:superseded-by"]


def test_successor_text_is_one_printable_bounded_line():
    fm = {"deprecated": True,
          "superseded-by": "  new\n\tskill \u202eevil\u200b \x1b[31m" + "x" * 500}
    contract, warnings = pkg.extract_contract(fm)
    text = contract["superseded_by"]
    assert warnings == []
    assert text.startswith("new skill evil [31m")
    assert len(text) == 200
    assert all(ch.isprintable() for ch in text)
    assert "  " not in text


def test_a_cut_that_lands_on_a_space_leaves_no_trailing_space():
    fm = {"deprecated": True, "superseded-by": "a" * 199 + " " + "b" * 50}
    contract, _ = pkg.extract_contract(fm)
    assert contract["superseded_by"] == "a" * 199


def test_the_documented_shapes_raise_no_frontmatter_invalid():
    """Parser parity: whatever this parser calls invalid fails the library's
    own validation, so the two shapes a library ships must both be clean."""
    for md in (OLD_MD, PROSE_MD):
        fm, warning = pkg.parse_frontmatter(md)
        contract, warnings = pkg.extract_contract(fm)
        assert warning is None and warnings == []
        assert contract["deprecated"] is True and contract["superseded_by"]


# ---- deprecation_warning: the one grammar producer ---------------------------

@pytest.mark.parametrize("info, expected", [
    ({"deprecated": True, "superseded_by": "backlog"}, "deprecated:backlog"),
    ({"deprecated": True, "superseded_by": PROSE}, "deprecated"),       # prose never enters a code
    ({"deprecated": True, "superseded_by": "a:b"}, "deprecated"),
    ({"deprecated": True, "superseded_by": None}, "deprecated"),
    ({"deprecated": False, "superseded_by": "backlog"}, None),
    ({"deprecated": "yes", "superseded_by": "backlog"}, None),          # only a real True counts
    ({}, None),
])
def test_the_code_names_a_successor_only_when_it_is_a_skill_name(info, expected):
    assert pkg.deprecation_warning(info) == expected


# ---- harness: one library source on disk, transport stubbed -------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    svc = SkillService()
    svc.library_root = tmp_path
    svc.library_path = tmp_path / _SOURCE_ID
    (svc.library_path / ".claude" / "skills").mkdir(parents=True)
    clone = SkillSourceClone(
        _SOURCE_ID, "https://github.com/owner/repo", "main", "branch", tmp_path
    )
    svc._clones = lambda enabled_only=True: [clone]
    svc._source_names = lambda: {_SOURCE_ID: "Test library"}

    clone.current_commit = MagicMock(return_value=None)
    clone.archive_skill = MagicMock(side_effect=lambda name: _archive(svc, name))
    svc._post_restore = AsyncMock(side_effect=_restore_ok)
    svc._delete_agent_file = AsyncMock(return_value=True)
    svc._probe_dependencies = AsyncMock(return_value=None)
    svc._finalize_injected_dirs = AsyncMock(
        return_value={"chmod": 0, "gitignore": True, "untracked": True, "errors": []}
    )
    svc._acquire_inject_lock = MagicMock(return_value=None)
    svc._release_inject_lock = MagicMock()
    svc._audit_delivery = AsyncMock()
    monkeypatch.setattr(skill_service_module.db, "set_skill_delivery_status", lambda *a, **k: None)

    for name, md in (("old", OLD_MD), ("prose", PROSE_MD), ("live", LIVE_MD)):
        d = svc.library_path / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(md)
    clone.tree_shas = MagicMock(return_value={n: f"tree-{n}" for n in ("old", "prose", "live")})
    return svc


def _archive(svc, name) -> bytes:
    skill_dir = svc.library_path / ".claude" / "skills" / name
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                tf.add(str(f), arcname=f.relative_to(svc.library_path).as_posix())
    return buf.getvalue()


def _restore_ok(agent_name, skill_name, tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        names = tf.getnames()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"restored": names, "skipped_outside_allowlist": []}
    resp.text = ""
    return resp


def _client():
    client = MagicMock()
    client.read_file = AsyncMock(return_value={"success": True, "content": "# Agent\n"})
    client.write_file = AsyncMock(return_value={"success": True})
    return client


def _state(monkeypatch, value):
    from services import docker_utils
    monkeypatch.setattr(docker_utils, "agent_container_state_async", AsyncMock(return_value=value))


def _inject_result(**per_skill):
    results = {n: {"success": True, "status": s, "files_written": 1, "warnings": []}
               for n, s in per_skill.items()}
    return {"success": True, "skills_injected": len(results), "skills_unchanged": 0,
            "skills_failed": 0, "results": results}


# ---- the listing, get, and the REST route ------------------------------------

def test_list_skills_carries_both_fields(service):
    by_name = {s["name"]: s for s in service.list_skills()}
    assert by_name["old"]["deprecated"] is True
    assert by_name["old"]["superseded_by"] == "backlog"
    assert by_name["prose"]["superseded_by"] == PROSE
    assert by_name["live"]["deprecated"] is False
    assert by_name["live"]["superseded_by"] is None


def test_a_skill_whose_parse_blows_up_reads_not_deprecated(service, monkeypatch):
    """`_parse_skill_info` swallows a parse failure and returns its defaults —
    which must include both fields, or the listing route raises on the key."""
    monkeypatch.setattr(skill_service_module.pkg, "extract_contract",
                        MagicMock(side_effect=RuntimeError("boom")))
    by_name = {s["name"]: s for s in service.list_skills()}
    assert by_name["old"]["deprecated"] is False
    assert by_name["old"]["superseded_by"] is None


def test_get_skill_carries_both_fields(service):
    skill = service.get_skill("old")
    assert skill["deprecated"] is True
    assert skill["superseded_by"] == "backlog"
    assert service.get_skill("live")["deprecated"] is False


def test_the_rest_listing_names_both_fields(service, monkeypatch):
    """The route builds `SkillInfo(...)` field by field — a service key it does
    not name never leaves the backend."""
    from routers import skills as mod
    monkeypatch.setattr(mod, "skill_service", service)
    user = SimpleNamespace(id=1, username="owner", role="user", agent_name=None)

    body = {s.name: s.model_dump() for s in asyncio.run(mod.list_skills(current_user=user))}

    assert body["old"]["deprecated"] is True
    assert body["old"]["superseded_by"] == "backlog"
    assert body["prose"]["superseded_by"] == PROSE
    assert body["live"]["deprecated"] is False
    assert body["live"]["superseded_by"] is None


# ---- injection: the code rides every outcome ---------------------------------

ABSENT = {"exists": False, "meta": None}
UNMANAGED = {"exists": True, "meta": None}


def _inject(service, monkeypatch, names, *, metas, force):
    client = _client()
    monkeypatch.setattr(skill_service_module, "get_agent_client", lambda name: client)
    service._read_agent_skill_metas = AsyncMock(return_value=metas)
    result = asyncio.run(service.inject_skills(AGENT, names, force=force))
    return result, client


def test_an_injected_deprecated_skill_carries_the_code(service, monkeypatch):
    result, _ = _inject(service, monkeypatch, ["old", "prose", "live"],
                        metas={n: ABSENT for n in ("old", "prose", "live")}, force=True)
    r = result["results"]
    assert r["old"]["status"] == "injected"
    assert "deprecated:backlog" in r["old"]["warnings"]
    assert "deprecated" in r["prose"]["warnings"]              # prose successor → bare code
    assert not [w for w in r["live"]["warnings"] if w.startswith("deprecated")]
    assert result["success"] is True                            # informational, never a failure


def test_an_unchanged_holder_still_carries_the_code(service, monkeypatch):
    """The start path of an agent that ALREADY holds the skill: nothing is
    written, and the deprecation is still reported."""
    metas = {"old": {"exists": True, "meta": {"version": "tree-old", "manifest": []}}}
    result, _ = _inject(service, monkeypatch, ["old"], metas=metas, force=False)
    assert result["results"]["old"]["status"] == "unchanged"
    assert result["results"]["old"]["warnings"] == ["deprecated:backlog"]


def test_a_name_conflict_still_carries_the_code(service, monkeypatch):
    result, _ = _inject(service, monkeypatch, ["old"], metas={"old": UNMANAGED}, force=True)
    assert result["results"]["old"]["status"] == "conflict"
    assert "deprecated:backlog" in result["results"]["old"]["warnings"]


def test_the_agents_claude_md_line_is_not_annotated(service, monkeypatch):
    """A holding agent's instructions are unchanged: the Platform Skills line
    for a deprecated skill reads exactly like any other."""
    _, client = _inject(service, monkeypatch, ["old", "live"],
                        metas={"old": ABSENT, "live": ABSENT}, force=True)
    path, content = client.write_file.await_args_list[-1].args
    assert path == "CLAUDE.md"
    assert "- `/old` - Use with /old command\n" in content
    assert "deprecated" not in content and "backlog" not in content


# ---- deliver_assigned: every outcome says it ---------------------------------

def test_a_running_assign_names_the_deprecation_and_leaves_live_skills_alone(service, monkeypatch):
    _state(monkeypatch, "running")
    monkeypatch.setattr(service, "inject_skills", AsyncMock(
        return_value=_inject_result(old="injected", prose="injected", live="injected")))

    report = asyncio.run(service.deliver_assigned(AGENT, ["old", "prose", "live"]))

    assert report["status"] == "injected"
    assert report["skills"] == {
        "old": {"status": "injected", "warnings": ["deprecated:backlog"]},
        "prose": {"status": "injected", "warnings": ["deprecated"]},
        "live": {"status": "injected"},                 # byte-identical to before
    }


def test_a_stopped_agent_is_told_too(service, monkeypatch):
    """`pending_start` never runs an injection — the notice comes from the
    library entry, not from injection results."""
    _state(monkeypatch, "stopped")
    inject = AsyncMock()
    monkeypatch.setattr(service, "inject_skills", inject)

    report = asyncio.run(service.deliver_assigned(AGENT, ["old", "live"]))

    inject.assert_not_awaited()
    assert report["skills"] == {
        "old": {"status": "pending_start", "warnings": ["deprecated:backlog"]},
        "live": {"status": "pending_start"},
    }


def test_an_unreadable_docker_state_is_told_too(service, monkeypatch):
    _state(monkeypatch, None)
    report = asyncio.run(service.deliver_assigned(AGENT, ["old"]))
    assert report["status"] == "not_delivered" and report["reason"] == "docker_unavailable"
    assert report["skills"]["old"]["warnings"] == ["deprecated:backlog"]


def test_a_delivery_that_outlives_the_budget_is_told_too(service, monkeypatch):
    _state(monkeypatch, "running")
    monkeypatch.setattr(skill_service_module, "SKILL_DELIVERY_BUDGET_SECONDS", 0.05)
    finished = asyncio.Event()

    async def _slow(agent, names, force, **kw):
        await asyncio.sleep(0.2)
        finished.set()
        return _inject_result(old="injected")

    monkeypatch.setattr(service, "inject_skills", _slow)

    async def _run():
        report = await service.deliver_assigned(AGENT, ["old"])
        assert not finished.is_set()                     # answered before the work landed
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0.05)
        return report

    report = asyncio.run(_run())
    assert report["status"] == "in_progress"
    assert report["skills"]["old"] == {"status": "in_progress", "warnings": ["deprecated:backlog"]}


def test_a_slow_library_read_does_not_hold_delivery_up(service, monkeypatch):
    """The read runs beside delivery: an assign is not made to wait on git I/O
    it does not need, and the notice still lands on the report."""
    import threading
    import time
    _state(monkeypatch, "running")
    read_finished = threading.Event()
    real_notes = service._deprecation_notes

    def _slow_notes(names):
        time.sleep(0.2)
        notes = real_notes(names)
        read_finished.set()
        return notes

    seen = {}

    async def _inject(agent, names, force, **kw):
        seen["read_finished_when_delivery_started"] = read_finished.is_set()
        return _inject_result(old="injected")

    monkeypatch.setattr(service, "_deprecation_notes", _slow_notes)
    monkeypatch.setattr(service, "inject_skills", _inject)

    report = asyncio.run(service.deliver_assigned(AGENT, ["old"]))

    assert seen == {"read_finished_when_delivery_started": False}
    assert report["skills"]["old"]["warnings"] == ["deprecated:backlog"]


def test_a_failed_library_read_never_fails_the_committed_assign(service, monkeypatch):
    _state(monkeypatch, "stopped")
    monkeypatch.setattr(service, "list_skills", MagicMock(side_effect=RuntimeError("git timed out")))
    report = asyncio.run(service.deliver_assigned(AGENT, ["old"]))
    assert report == {"status": "pending_start", "skills": {"old": {"status": "pending_start"}}}


# ---- the start endpoint's public projection ----------------------------------

def test_the_start_projection_keeps_the_code_and_nothing_prose_shaped():
    from services.agent_service.lifecycle import public_skills_result
    raw = {"status": "success", "results": {
        "old": {"status": "unchanged", "warnings": ["deprecated:backlog"]},
        "prose": {"status": "unchanged", "warnings": ["deprecated"]},
        "bad": {"status": "unchanged", "warnings": ["deprecated:not a name — prose"]},
    }}
    skills = public_skills_result(raw)["skills"]
    assert skills["old"]["warnings"] == ["deprecated:backlog"]
    assert skills["prose"]["warnings"] == ["deprecated"]
    assert "warnings" not in skills["bad"]
