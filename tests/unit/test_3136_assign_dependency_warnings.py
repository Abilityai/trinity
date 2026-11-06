"""#3136 — assigning a skill says what the injection found missing.

Assign delivers (#2703) and returns a `delivery` report, but the report reduced
each skill to `{status, error?}`: the per-skill warnings the injection computed
were dropped. So an assign that landed a skill whose declared binary or env key
the agent lacks answered "delivered — available now" on the Library card, the
Skills tab and MCP, and the warning surfaced only after a manual Sync.

What these tests pin, each by EXECUTING the path (none reads source text):

  * `_deliver_assigned` carries the injection's warnings for every outcome that
    put the package on the agent (`injected` / `fallback` / `unchanged`),
    projected through the start endpoint's public-code rule (#2991): codes only,
    never a path or a source label;
  * the deprecation stamp (ent#672) MERGES with them — one lifecycle code,
    first — instead of overwriting them;
  * the whole chain, through the REAL injection and its dependency probe;
  * the POST route body, on the running-agent path;
  * regression guards, kept on purpose and named as such: a skill with no
    warnings keeps a byte-identical report; outcomes that wrote nothing carry no
    injection codes; the start endpoint's projection is unchanged.

No real Redis, no real Docker, no real agent.
Related flow: docs/memory/feature-flows/skill-assignment.md
"""
from __future__ import annotations

import asyncio
import io
import json
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.skill_service as skill_service_module
from services.skill_service import SkillService
from services.skill_source_clone import SkillSourceClone

pytestmark = pytest.mark.unit

AGENT = "acme-bot"
_SOURCE_ID = "src_aaaaaaaa"


@pytest.fixture
def svc(monkeypatch):
    s = SkillService()
    monkeypatch.setattr(s, "_audit_delivery", AsyncMock())
    # No library read: the deprecation notice is covered on its own below.
    monkeypatch.setattr(s, "list_skills", lambda: [])
    return s


def _state(monkeypatch, value):
    from services import docker_utils
    monkeypatch.setattr(docker_utils, "agent_container_state_async", AsyncMock(return_value=value))


def _result(status, warnings=(), **extra):
    return {"success": status != "failed", "status": status, "files_written": 1,
            "warnings": list(warnings), **extra}


def _injection(**per_skill):
    return {"success": True, "skills_injected": len(per_skill), "skills_unchanged": 0,
            "skills_failed": 0, "results": per_skill}


def _deliver(svc, monkeypatch, names, injection):
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(return_value=injection))
    return asyncio.run(svc.deliver_assigned(AGENT, names))


# ---- the report carries the injection's codes --------------------------------

@pytest.mark.parametrize("outcome", ["injected", "fallback", "unchanged"])
def test_every_outcome_that_landed_the_package_carries_its_dependency_codes(svc, monkeypatch, outcome):
    codes = ["missing_binary:pdftotext", "missing_env:PDF_API_KEY", "packages_not_checked"]
    report = _deliver(svc, monkeypatch, ["pdf-report"],
                      _injection(**{"pdf-report": _result(outcome, codes)}))
    assert report["status"] == "injected"
    assert report["skills"]["pdf-report"] == {"status": "injected", "warnings": codes}


def test_codes_pass_and_free_text_details_do_not(svc, monkeypatch):
    """The start endpoint's projection rule (#2991 / ent#334): the assign body
    reaches REST and MCP callers, so a path or a source label never leaves."""
    report = _deliver(svc, monkeypatch, ["pdf-report"], _injection(**{"pdf-report": _result("injected", [
        "missing_binary:pdftotext",
        "stale_delete_failed:/home/developer/.claude/skills/pdf-report/old.md",
        "shadowed_source:Acme Skills",
        "multi_file_dropped_old_image",
    ])}))
    assert report["skills"]["pdf-report"]["warnings"] == [
        "missing_binary:pdftotext", "multi_file_dropped_old_image"]


def test_the_deprecation_notice_merges_with_the_dependency_codes(svc, monkeypatch):
    """ent#672 stamped `warnings = [code]`, which would now throw the injection's
    codes away. One lifecycle code, first, from the library entry; no duplicate
    of the one the injection carried itself."""
    monkeypatch.setattr(svc, "list_skills", lambda: [
        {"name": "old", "deprecated": True, "superseded_by": "backlog"}])
    report = _deliver(svc, monkeypatch, ["old"], _injection(old=_result(
        "injected", ["deprecated:backlog", "missing_binary:jq"])))
    assert report["skills"]["old"]["warnings"] == ["deprecated:backlog", "missing_binary:jq"]


def test_an_injection_only_deprecation_code_survives_a_failed_library_read(svc, monkeypatch):
    def _boom():
        raise RuntimeError("git timed out")
    monkeypatch.setattr(svc, "list_skills", _boom)
    report = _deliver(svc, monkeypatch, ["old"], _injection(old=_result(
        "injected", ["deprecated:backlog", "missing_env:TOKEN"])))
    assert report["skills"]["old"]["warnings"] == ["deprecated:backlog", "missing_env:TOKEN"]


def test_a_mixed_assign_keeps_each_skill_its_own_codes(svc, monkeypatch):
    report = _deliver(svc, monkeypatch, ["a", "b"], _injection(
        a=_result("injected", ["missing_binary:jq"]), b=_result("unchanged")))
    assert report["skills"] == {"a": {"status": "injected", "warnings": ["missing_binary:jq"]},
                                "b": {"status": "injected"}}


# ---- regression guards (green before #3136 too — kept on purpose) -------------

def test_a_skill_with_no_warnings_keeps_a_byte_identical_report(svc, monkeypatch):
    report = _deliver(svc, monkeypatch, ["live"], _injection(live=_result("injected")))
    assert json.dumps(report, sort_keys=True) == json.dumps(
        {"status": "injected", "skills": {"live": {"status": "injected"}}}, sort_keys=True)


@pytest.mark.parametrize("outcome, expected", [
    ("conflict", {"status": "conflict", "error": "name_conflict"}),
    ("failed", {"status": "failed", "error": "boom"}),
    ("unassigned_meanwhile", {"status": "unassigned_meanwhile"}),
])
def test_an_outcome_that_wrote_nothing_carries_no_injection_codes(svc, monkeypatch, outcome, expected):
    extra = {"error": "boom"} if outcome == "failed" else {}
    report = _deliver(svc, monkeypatch, ["x"], _injection(x=_result(
        outcome, ["missing_binary:jq"], **extra)))
    assert report["skills"]["x"] == expected


def test_a_stopped_agent_has_no_probe_to_report(svc, monkeypatch):
    _state(monkeypatch, "stopped")
    report = asyncio.run(svc.deliver_assigned(AGENT, ["pdf-report"]))
    assert report["skills"] == {"pdf-report": {"status": "pending_start"}}


def test_the_start_projection_and_the_assign_report_agree():
    """One rule for both bodies: the start endpoint keeps exactly the codes the
    assign report now keeps (and its own behaviour is unchanged)."""
    from services.agent_service.lifecycle import public_skills_result
    from services import skill_packaging as pkg
    codes = ["missing_binary:pdftotext", "missing_env:K", "packages_not_checked",
             "deprecated:backlog", "deprecated:not a name — prose",
             "stale_delete_failed:/x/y", "shadowed_source:Acme Skills"]
    raw = {"status": "success", "results": {"s": {"status": "injected", "warnings": codes}}}
    kept = ["missing_binary:pdftotext", "missing_env:K", "packages_not_checked", "deprecated:backlog"]
    assert public_skills_result(raw)["skills"]["s"]["warnings"] == kept
    assert pkg.public_warnings(codes) == kept
    assert pkg.public_warnings(None) == [] and pkg.public_warnings(["", 7, None]) == []


# ---- the whole chain: the REAL injection and its dependency probe -------------

PDF_MD = ("---\ndescription: pdf\nrequires:\n  binaries: [pdftotext]\n"
          "  env: [PDF_API_KEY, PDF_REGION]\n---\n# pdf-report\n")


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A one-skill library on disk, the ent#672 harness: real parse, real
    contract extraction, real `_apply_dep_warnings`; the agent is a stub."""
    s = SkillService()
    s.library_root = tmp_path
    s.library_path = tmp_path / _SOURCE_ID
    skill_dir = s.library_path / ".claude" / "skills" / "pdf-report"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(PDF_MD)
    clone = SkillSourceClone(_SOURCE_ID, "https://github.com/owner/repo", "main", "branch", tmp_path)
    s._clones = lambda enabled_only=True: [clone]
    s._source_names = lambda: {_SOURCE_ID: "Test library"}
    clone.current_commit = MagicMock(return_value=None)
    clone.tree_shas = MagicMock(return_value={"pdf-report": "tree-pdf"})
    clone.archive_skill = MagicMock(side_effect=lambda name: _archive(skill_dir, s.library_path))
    s._post_restore = AsyncMock(side_effect=_restore_ok)
    s._delete_agent_file = AsyncMock(return_value=True)
    s._finalize_injected_dirs = AsyncMock(
        return_value={"chmod": 0, "gitignore": True, "untracked": True, "errors": []})
    s._acquire_inject_lock = MagicMock(return_value=None)
    s._release_inject_lock = MagicMock()
    s._audit_delivery = AsyncMock()
    s._read_agent_skill_metas = AsyncMock(return_value={"pdf-report": {"exists": False, "meta": None}})
    client = MagicMock()
    client.read_file = AsyncMock(return_value={"success": True, "content": "# Agent\n"})
    client.write_file = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(skill_service_module, "get_agent_client", lambda name: client)
    monkeypatch.setattr(skill_service_module.db, "get_agent_skill_names", lambda a: ["pdf-report"])
    monkeypatch.setattr(skill_service_module.db, "set_skill_delivery_status", lambda *a, **k: None)
    monkeypatch.setattr(skill_service_module, "_via_sets_or_empty", lambda a: {})
    return s


def _archive(skill_dir, root) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                tf.add(str(f), arcname=f.relative_to(root).as_posix())
    return buf.getvalue()


def _restore_ok(agent_name, skill_name, tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        names = tf.getnames()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"restored": names, "skipped_outside_allowlist": []}
    resp.text = ""
    return resp


def test_an_assign_through_the_real_injection_names_what_the_probe_found_missing(library, monkeypatch):
    _state(monkeypatch, "running")
    library._probe_dependencies = AsyncMock(return_value={
        "binaries": {"pdftotext": False}, "env": {"PDF_API_KEY": False, "PDF_REGION": True}})

    report = asyncio.run(library.deliver_assigned(AGENT, ["pdf-report"]))

    library._probe_dependencies.assert_awaited_once()
    assert report["status"] == "injected"
    assert report["skills"]["pdf-report"] == {
        "status": "injected", "warnings": ["missing_binary:pdftotext", "missing_env:PDF_API_KEY"]}


def test_an_assign_whose_probe_finds_everything_keeps_the_clean_report(library, monkeypatch):
    _state(monkeypatch, "running")
    library._probe_dependencies = AsyncMock(return_value={
        "binaries": {"pdftotext": True}, "env": {"PDF_API_KEY": True, "PDF_REGION": True}})
    report = asyncio.run(library.deliver_assigned(AGENT, ["pdf-report"]))
    assert report == {"status": "injected", "skills": {"pdf-report": {"status": "injected"}}}


# ---- the route body, on the running-agent path --------------------------------

def test_the_assign_route_returns_the_codes_in_its_delivery_block(svc, monkeypatch):
    """POST /api/agents/{name}/skills/{skill} hands the service's report back
    unchanged — the Library card and MCP `assign_skill_to_agent` read this."""
    from routers import skills as mod
    from services import skill_assignment_service as sas
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(return_value=_injection(
        **{"pdf-report": _result("injected", ["missing_binary:pdftotext"])})))
    monkeypatch.setattr(sas, "skill_service", svc)          # the real assign → deliver hand-off
    monkeypatch.setattr(mod, "skill_service", svc)
    monkeypatch.setattr(svc, "get_skill", lambda n: {"name": n})
    monkeypatch.setattr(sas.db, "assign_skill",
                        lambda *a, **k: {"skill_name": "pdf-report"}, raising=False)
    monkeypatch.setattr(sas, "sync_gates", AsyncMock(return_value=None))
    monkeypatch.setattr(sas, "broadcast_skills_changed", AsyncMock())

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(mod.router)
    human = SimpleNamespace(id=1, username="alice", email="a@example.com", role="admin",
                            mcp_scope=None, agent_name=None, connector_agent=None, mcp_key_id=None)
    app.dependency_overrides[mod.get_current_user] = lambda: human
    app.dependency_overrides[mod.get_skill_managed_agent_by_name] = lambda agent_name: agent_name

    r = TestClient(app).post(f"/api/agents/{AGENT}/skills/pdf-report")

    assert r.status_code == 200, r.text
    assert r.json()["delivery"]["skills"]["pdf-report"] == {
        "status": "injected", "warnings": ["missing_binary:pdftotext"]}
