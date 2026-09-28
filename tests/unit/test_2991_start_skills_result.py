"""#2991 — a start's skill-delivery result reaches REST (and so MCP) callers.

`start_agent_internal` computed `skills_injection` / `skills_result`, and the
start endpoint rebuilt its response from a whitelist that left both out, so a
caller could not tell a clean start from one whose assigned skill hit a
`conflict` or failed to land.

What these tests pin (every one EXECUTES the path; none reads source text):

1. the endpoint returns both fields with the values the lifecycle produced —
   clean, conflict, failed, no skills, already running, and absent;
2. a conflict is distinguishable from a clean start in the one response (AC3):
   `inject_assigned_skills` names it even though #2914 keeps `success` true;
3. nothing but names, statuses and codes leaves — a per-skill error string that
   carries an exception, URL or path is reduced to a code (AC5, ent#334).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

AGENT = "acme-bot"
LEAKY = "HTTPConnectionPool(host='agent-acme-bot', port=8000): Max retries exceeded with url: /api/files/restore?token=s3cret"


def _lifecycle_result(skills_injection, skills_result):
    return {
        "message": f"Agent {AGENT} started",
        "credentials_injection": "success",
        "credentials_result": {"status": "success"},
        "skills_injection": skills_injection,
        "skills_result": skills_result,
        "recreated": False,
        "recreate_reason": None,
        "recreate_deferred": None,
    }


@pytest.fixture
def start(monkeypatch):
    """Drive the REAL `start_agent_endpoint` with the lifecycle stubbed."""
    from routers import agents

    audit = AsyncMock()
    monkeypatch.setattr(agents.platform_audit_service, "log", audit)
    monkeypatch.setattr(agents, "enforce_agent_spawn_scope", lambda *a, **k: None)
    monkeypatch.setattr(agents, "invalidate_context_stats_cache", lambda: None)
    monkeypatch.setattr(agents, "manager", None, raising=False)
    monkeypatch.setattr(agents, "filtered_manager", None, raising=False)

    def run(lifecycle_result):
        monkeypatch.setattr(agents, "start_agent_internal", AsyncMock(return_value=lifecycle_result))
        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                                  url=SimpleNamespace(path=f"/api/agents/{AGENT}/start"))
        user = SimpleNamespace(id=1, username="alice", role="admin")
        return asyncio.run(agents.start_agent_endpoint(AGENT, request, user))

    return run


# ---- 1. the fields reach the response -----------------------------------------

def test_a_clean_start_reports_its_skills(start):
    body = start(_lifecycle_result("success", {
        "status": "success", "skills_injected": 2, "skills_unchanged": 1, "skills_warnings": 0,
        "conflicts": [],
        "results": {"research": {"success": True, "status": "injected", "files_written": 3, "warnings": []},
                    "groom": {"success": True, "status": "unchanged", "warnings": []}},
        "reconcile": {"status": "clean", "removed": 0},
    }))
    assert body["skills_injection"] == "success"
    r = body["skills_result"]
    assert r["status"] == "success" and r["conflicts"] == []
    assert (r["skills_injected"], r["skills_unchanged"]) == (2, 1)
    assert r["skills"] == {"research": {"status": "injected"}, "groom": {"status": "unchanged"}}
    assert r["reconcile"] == {"status": "clean", "removed": 0}
    # The existing whitelist is untouched.
    assert body["credentials_injection"] == "success" and body["message"] == f"Agent {AGENT} started"


def test_a_conflict_is_distinguishable_from_a_clean_start(start):
    """AC3 — #2914 keeps `success` true for a conflict, so the list is the signal."""
    body = start(_lifecycle_result("success", {
        "status": "success", "skills_injected": 1, "skills_unchanged": 0, "skills_warnings": 0,
        "conflicts": ["backlog"],
        "results": {"research": {"success": True, "status": "injected", "warnings": []},
                    "backlog": {"success": False, "status": "conflict",
                                "error": "name_conflict: agent-authored .claude/skills/backlog/ kept", "warnings": []}},
    }))
    r = body["skills_result"]
    assert r["conflicts"] == ["backlog"]
    assert r["skills"]["backlog"] == {"status": "conflict", "code": "name_conflict"}


def test_no_assigned_skills_is_a_stated_no_op_not_an_absent_field(start):
    """AC4 — absence must never spell 'nothing to do'."""
    body = start(_lifecycle_result("skipped", {"status": "skipped", "reason": "no_skills",
                                               "reconcile": {"status": "clean", "removed": 0}}))
    assert body["skills_injection"] == "skipped"
    assert body["skills_result"]["status"] == "skipped" and body["skills_result"]["reason"] == "no_skills"


def test_an_already_running_start_says_why_it_skipped(start):
    body = start(_lifecycle_result("skipped", {"status": "skipped", "reason": "container_already_running"}))
    assert body["skills_result"] == {"status": "skipped", "reason": "container_already_running", "conflicts": []}


def test_a_missing_result_reads_unknown_never_absent(start):
    result = _lifecycle_result("success", None)
    del result["skills_injection"], result["skills_result"]
    body = start(result)
    assert body["skills_injection"] == "unknown" and body["skills_result"] == {"status": "unknown"}


# ---- 3. names, statuses and codes only -----------------------------------------

def test_a_failed_package_is_reported_by_code_and_no_error_text_leaks(start):
    body = start(_lifecycle_result("partial", {
        "status": "partial", "skills_injected": 1, "skills_unchanged": 0, "skills_failed": 3, "skills_warnings": 2,
        "conflicts": [],
        "results": {
            "research": {"success": True, "status": "injected", "warnings": ["missing_env:GITHUB_TOKEN"]},
            "big": {"success": False, "status": "failed",
                    "error": "skill_too_large: 11000000 bytes exceeds cap 10485760", "warnings": []},
            "gone": {"success": False, "status": "failed", "error": "Skill not found in library", "warnings": []},
            "flaky": {"success": False, "status": "failed", "error": LEAKY,
                      "warnings": ["see /home/developer/.claude/skills/flaky for details"]},
        },
        "reconcile": {"status": "skipped", "reason": "injection_already_running"},
    }))
    r = body["skills_result"]
    assert r["skills"]["big"] == {"status": "failed", "code": "skill_too_large"}
    assert r["skills"]["gone"] == {"status": "failed", "code": "not_in_library"}
    assert r["skills"]["flaky"] == {"status": "failed", "code": "error"}          # free text → a code
    assert r["skills"]["research"] == {"status": "injected", "warnings": ["missing_env:GITHUB_TOKEN"]}
    assert r["skills_failed"] == 3 and r["reconcile"] == {"status": "skipped", "reason": "injection_already_running"}
    dumped = json.dumps(body)
    for leaked in ("s3cret", "HTTPConnectionPool", "/home/developer", "10485760"):
        assert leaked not in dumped, leaked


def test_the_projection_drops_unaddressable_names_and_junk():
    from services.agent_service import public_skills_result
    got = public_skills_result({
        "status": "success",
        "results": {"../etc": {"status": "injected"}, "ok": "not a dict", "fine": {"status": "injected"}},
        "conflicts": ["fine", "../x", 7],
    })
    assert got["skills"] == {"fine": {"status": "injected"}} and got["conflicts"] == ["fine"]
    assert public_skills_result("nonsense") == {"status": "unknown"}


# ---- 2. the lifecycle names conflicts -----------------------------------------

def test_inject_assigned_skills_names_the_conflicts(monkeypatch):
    from services.agent_service import lifecycle
    from database import db
    monkeypatch.setattr(db, "get_agent_skill_names", lambda a: ["research", "backlog"])
    monkeypatch.setattr(lifecycle.skill_service, "inject_skills", AsyncMock(return_value={
        "success": True, "skills_injected": 1, "skills_unchanged": 0,
        "results": {"research": {"success": True, "status": "injected", "warnings": []},
                    "backlog": {"success": False, "status": "conflict", "error": "name_conflict", "warnings": []}},
    }))
    monkeypatch.setattr(lifecycle.skill_service, "reconcile_agent_skills",
                        AsyncMock(return_value={"status": "clean", "removed": 0}))
    out = asyncio.run(lifecycle.inject_assigned_skills(AGENT))
    assert out["status"] == "success" and out["conflicts"] == ["backlog"]


def test_a_malformed_result_never_turns_a_start_into_a_500(start):
    """Review finding 1: the projection runs inside the endpoint's try."""
    body = start(_lifecycle_result("success", {"status": "success", "results": ["x"], "conflicts": 7}))
    assert body["skills_result"] == {"status": "success", "conflicts": []}
    body = start(_lifecycle_result("success", {"status": "success", "conflicts": "backlog"}))
    assert body["skills_result"]["conflicts"] == []                 # a string is never split into names

