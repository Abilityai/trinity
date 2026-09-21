"""#2914 — a library skill never overwrites an agent-authored skill of the same name.

The library and an agent's repo share one flat namespace on the agent side
(`.claude/skills/<name>/`), so a name match is not proof of the same skill. The
inject path used to write the library package over the agent's directory and
bury an `unmanaged_dir_overwritten` warning in the response. These tests pin
the replacement contract:

  * `_inject_skills_locked` — a directory that EXISTS but carries no
    `.trinity-skill.json` is refused before a single byte is staged: no
    archive, no restore, no finalize (`.gitignore` / untrack), no CLAUDE.md
    entry; status is `conflict`, neither `injected` nor `failed`, and `force`
    (manual Sync) does not override it. A platform-managed dir (meta present)
    keeps upgrading in place; an absent dir is injected.
  * the verdict is recorded on the assignment row (`delivery_status`) and
    cleared on the next injection where the name lands.
  * `deliver_assigned` names it — per-skill `conflict`, top-level `conflict`
    when every requested name collides, `partial` + `conflicts` on a mix.
  * `db.set_skill_delivery_status` and the bulk-replace carry-over, against a
    real SQLite file through the SQLAlchemy Core layer.

Every test EXECUTES the path with the agent/Docker boundary stubbed; none reads
source text. No real Redis, no real Docker, no real agent.
"""
from __future__ import annotations

import asyncio
import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.skill_service as skill_service_module
from services.skill_service import SkillService
from services.skill_source_clone import SkillSourceClone

AGENT = "acme-bot"
_SOURCE_ID = "src_aaaaaaaa"


# ---- harness ----------------------------------------------------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    """One library source in a tmp dir; every exec/transport seam stubbed."""
    svc = SkillService()
    svc.library_root = tmp_path
    svc.library_path = tmp_path / _SOURCE_ID
    (svc.library_path / ".claude" / "skills").mkdir(parents=True)
    clone = SkillSourceClone(
        _SOURCE_ID, "https://github.com/owner/repo", "main", "branch", tmp_path
    )
    svc._clones = lambda enabled_only=True: [clone]
    svc._source_names = lambda: {_SOURCE_ID: "Test library"}
    svc.test_clone = clone

    clone.current_commit = MagicMock(return_value="commit123")
    clone.archive_skill = MagicMock(side_effect=lambda name: _archive(svc, name))
    svc._post_restore = AsyncMock(side_effect=_restore_ok)
    svc._delete_agent_file = AsyncMock(return_value=True)
    svc._probe_dependencies = AsyncMock(return_value=None)
    svc._finalize_injected_dirs = AsyncMock(
        return_value={"chmod": 0, "gitignore": True, "untracked": True, "errors": []}
    )
    svc._update_claude_md_skills_section = AsyncMock()
    svc._acquire_inject_lock = MagicMock(return_value=None)
    svc._release_inject_lock = MagicMock()
    svc.recorded = []
    monkeypatch.setattr(
        skill_service_module.db, "set_skill_delivery_status",
        lambda agent, conflicted, resolved: svc.recorded.append((agent, list(conflicted), list(resolved))),
    )
    monkeypatch.setattr(skill_service_module, "get_agent_client", lambda name: SimpleNamespace())
    return svc


def _write_skill(svc, name, body="# skill\n"):
    d = svc.library_path / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)


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


def _wire_metas(svc):
    svc.test_clone.tree_shas = MagicMock(return_value={
        p.name: f"tree-{p.name}"
        for p in (svc.library_path / ".claude" / "skills").iterdir() if p.is_dir()
    })


def _inject(svc, names, *, metas, force):
    _wire_metas(svc)
    svc._read_agent_skill_metas = AsyncMock(return_value=metas)
    return asyncio.run(svc.inject_skills(AGENT, names, force=force))


UNMANAGED = {"exists": True, "meta": None}          # the agent's own playbook
MANAGED = {"exists": True, "meta": {"version": "old", "manifest": []}}
ABSENT = {"exists": False, "meta": None}


# ---- the refusal -------------------------------------------------------------

@pytest.mark.parametrize("force", [True, False], ids=["manual-sync", "start-path"])
def test_agent_authored_dir_survives_byte_for_byte(service, force):
    """AC#1: nothing is written into the agent's directory — no archive is even
    built, no restore call, no finalize (`.gitignore` line / untrack)."""
    _write_skill(service, "backlog")
    result = _inject(service, ["backlog"], metas={"backlog": UNMANAGED}, force=force)

    verdict = result["results"]["backlog"]
    assert verdict["status"] == "conflict"
    assert verdict["success"] is False
    assert verdict["files_written"] == 0
    assert "name_conflict" in verdict["error"]
    service.test_clone.archive_skill.assert_not_called()
    service._post_restore.assert_not_awaited()
    service._finalize_injected_dirs.assert_not_awaited()
    service._delete_agent_file.assert_not_awaited()


def test_conflict_is_neither_injected_nor_failed(service):
    """AC#2: an honest status of its own. The run is not a failure (a fleet
    re-inject must not alarm on it every auto-sync) and it is not delivered."""
    _write_skill(service, "backlog")
    result = _inject(service, ["backlog"], metas={"backlog": UNMANAGED}, force=True)

    assert result["success"] is True
    assert result["skills_injected"] == 0
    assert result["skills_failed"] == 0
    assert result["skills_conflict"] == 1
    assert result["conflicts"] == ["backlog"]
    assert "unmanaged_dir_overwritten" not in str(result)


def test_conflicted_name_is_left_out_of_the_claude_md_section(service):
    """The agent's copy is what runs; listing it under Platform Skills would
    claim the library's version is on the agent."""
    _write_skill(service, "backlog")
    _write_skill(service, "research")
    _inject(service, ["backlog", "research"],
            metas={"backlog": UNMANAGED, "research": ABSENT}, force=True)

    service._update_claude_md_skills_section.assert_awaited_once()
    present = service._update_claude_md_skills_section.await_args.args[1]
    assert present == ["research"]


def test_platform_managed_dir_still_upgrades_in_place(service):
    """AC#4 / AC#6: a directory carrying the platform's own marker is ours to
    rewrite — including one a pre-#2914 assignment overwrote."""
    _write_skill(service, "backlog")
    result = _inject(service, ["backlog"], metas={"backlog": MANAGED}, force=True)
    assert result["results"]["backlog"]["status"] == "injected"
    service._post_restore.assert_awaited_once()


def test_absent_dir_is_injected(service):
    _write_skill(service, "backlog")
    result = _inject(service, ["backlog"], metas={"backlog": ABSENT}, force=False)
    assert result["results"]["backlog"]["status"] == "injected"


def test_unreadable_probe_keeps_the_fail_open_direction(service):
    """`_read_agent_skill_metas` answers `{}` on any exec fault. That is "could
    not look", and the start path has always injected through it rather than
    refusing the whole fleet on a transient fault; #2914 does not change that.
    (The removal path's `deferred` is the recoverable direction there; here it
    would strand every skill on every agent whose probe hiccups.)"""
    _write_skill(service, "backlog")
    result = _inject(service, ["backlog"], metas={}, force=False)
    assert result["results"]["backlog"]["status"] == "injected"


# ---- the recorded verdict ----------------------------------------------------

def test_conflict_is_recorded_on_the_row_and_landed_names_are_cleared(service):
    """AC#2 + AC#5: one row write per injection — conflicts stamped, names that
    landed (injected / unchanged / fallback) cleared, so an operator who
    unassigns or renames the agent's copy sees the state clear on the next
    sync without doing anything else."""
    _write_skill(service, "backlog")
    _write_skill(service, "research")
    _write_skill(service, "writing")
    _inject(service, ["backlog", "research", "writing"],
            metas={"backlog": UNMANAGED, "research": ABSENT,
                   "writing": {"exists": True, "meta": {"version": "tree-writing", "manifest": []}}},
            force=False)

    assert service.recorded == [(AGENT, ["backlog"], ["research", "writing"])]


def test_failed_restore_neither_stamps_nor_clears(service):
    """A failed sync is not a resolution — the row keeps what it had."""
    _write_skill(service, "backlog")
    service._post_restore = AsyncMock(return_value=None)   # transport failure
    result = _inject(service, ["backlog"], metas={"backlog": ABSENT}, force=True)
    assert result["results"]["backlog"]["status"] == "failed"
    assert service.recorded == []


def test_a_row_write_failure_never_fails_the_injection(service, monkeypatch):
    _write_skill(service, "backlog")

    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(skill_service_module.db, "set_skill_delivery_status", _boom)
    result = _inject(service, ["backlog"], metas={"backlog": UNMANAGED}, force=True)
    assert result["results"]["backlog"]["status"] == "conflict"


# ---- deliver_assigned --------------------------------------------------------

def _state(monkeypatch, value):
    from services import docker_utils
    monkeypatch.setattr(docker_utils, "agent_container_state_async", AsyncMock(return_value=value))


def _inject_result(**per_skill):
    results = {}
    for name, status in per_skill.items():
        entry = {"success": status in ("injected", "unchanged"), "status": status,
                 "files_written": 0, "warnings": []}
        if status == "conflict":
            entry["error"] = "name_conflict: the agent already has its own .claude/skills/x/"
        if status == "failed":
            entry["error"] = "boom"
        results[name] = entry
    return {"success": True, "skills_injected": 0, "skills_unchanged": 0,
            "skills_failed": 0, "results": results}


def test_delivery_names_the_conflict_explicitly(monkeypatch):
    """AC#2: the assign response says `conflict`, never `injected`, never a
    post-hoc warning, and never `failed` (the next action is different)."""
    svc = SkillService()
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(return_value=_inject_result(backlog="conflict")))
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())

    report = asyncio.run(svc.deliver_assigned(AGENT, ["backlog"]))

    assert report["status"] == "conflict"
    assert report["conflicts"] == ["backlog"]
    assert report["skills"]["backlog"]["status"] == "conflict"
    assert "name_conflict" in report["skills"]["backlog"]["error"]
    assert "reason" not in report


def test_delivery_with_a_mix_is_partial_and_lists_the_conflicts(monkeypatch):
    svc = SkillService()
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(
        return_value=_inject_result(backlog="conflict", research="injected")))
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())

    report = asyncio.run(svc.deliver_assigned(AGENT, ["backlog", "research"]))

    assert report["status"] == "partial"
    assert report["conflicts"] == ["backlog"]
    assert report["skills"]["research"] == {"status": "injected"}


def test_delivery_conflict_plus_failure_is_not_delivered_but_still_names_the_conflict(monkeypatch):
    svc = SkillService()
    _state(monkeypatch, "running")
    monkeypatch.setattr(svc, "inject_skills", AsyncMock(
        return_value=_inject_result(backlog="conflict", research="failed")))
    monkeypatch.setattr(svc, "_audit_delivery", AsyncMock())

    report = asyncio.run(svc.deliver_assigned(AGENT, ["backlog", "research"]))

    assert report["status"] == "not_delivered"
    assert report["reason"] == "injection_error"
    assert report["conflicts"] == ["backlog"]


# ---- the DB layer, for real --------------------------------------------------

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """A fresh SQLite file through the SQLAlchemy Core layer the skills ops use."""
    from sqlalchemy import create_engine
    import db.engine as engine_module
    import db.tables as tables
    from db.skills import SkillsOperations

    engine = create_engine(f"sqlite:///{tmp_path / 'skills.db'}")
    tables.agent_skills.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    import db.skills as skills_module
    monkeypatch.setattr(skills_module, "get_engine", lambda: engine)
    return SkillsOperations()


def test_set_skill_delivery_status_stamps_and_clears_only_named_rows(real_db):
    real_db.set_agent_skills(AGENT, ["backlog", "research", "writing"], "alice")

    real_db.set_skill_delivery_status(AGENT, ["backlog", "writing"], ["research"])
    rows = {s.skill_name: s.delivery_status for s in real_db.get_agent_skills(AGENT)}
    assert rows == {"backlog": "conflict", "research": None, "writing": "conflict"}

    # The agent's copy went away and the next sync landed `backlog`; `writing`
    # was not in that run and keeps its verdict.
    real_db.set_skill_delivery_status(AGENT, [], ["backlog"])
    rows = {s.skill_name: s.delivery_status for s in real_db.get_agent_skills(AGENT)}
    assert rows == {"backlog": None, "research": None, "writing": "conflict"}

    # Another agent's rows are never touched.
    real_db.set_agent_skills("other-bot", ["backlog"], "alice")
    real_db.set_skill_delivery_status(AGENT, ["backlog"], [])
    assert real_db.get_agent_skills("other-bot")[0].delivery_status is None


def test_bulk_replace_carries_the_verdict_for_retained_names(real_db):
    """The PUT is delete-all + reinsert and only re-injects ADDED names, so a
    retained conflict must survive the replace or the tab goes quiet until the
    next start."""
    real_db.set_agent_skills(AGENT, ["backlog", "research"], "alice")
    real_db.set_skill_delivery_status(AGENT, ["backlog"], [])

    real_db.set_agent_skills(AGENT, ["backlog", "writing"], "alice")
    rows = {s.skill_name: s.delivery_status for s in real_db.get_agent_skills(AGENT)}
    assert rows == {"backlog": "conflict", "writing": None}

    # Unassigning it is a resolution: the row (and its verdict) is gone.
    real_db.set_agent_skills(AGENT, ["writing"], "alice")
    real_db.set_agent_skills(AGENT, ["backlog", "writing"], "alice")
    rows = {s.skill_name: s.delivery_status for s in real_db.get_agent_skills(AGENT)}
    assert rows == {"backlog": None, "writing": None}
