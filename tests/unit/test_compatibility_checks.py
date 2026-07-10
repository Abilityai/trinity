"""
Agent compatibility validation — fixture-driven unit tests (#668).

These exercise the PURE layers (no Docker, no network):
  * spec catalog consistency + spec↔docs id sync,
  * STATIC check functions over fixture snapshots,
  * gitignore auto-fix transforms (edge cases),
  * AI batching (no-key skip, omitted-check → skipped),
  * build_report orchestration (collect monkeypatched, real tmp DB persistence).

Related flow: docs/agent-validation-spec.md, services/compatibility/.

Relies on tests/unit/conftest.py for src/backend on sys.path and the dummy
REDIS_URL / tmp TRINITY_DB_PATH so backend imports succeed without a stack.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from services.compatibility import spec
from services.compatibility.static_checks import run_static, STATIC_CHECKS
from services.compatibility import fixes
from services.compatibility import ai_checks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _f(content=None, *, exists=True, binary=False, truncated=False, mode_exec=False, is_file=True):
    d = {"exists": exists, "is_file": is_file}
    if not exists:
        return {"exists": False}
    d["size"] = len(content or "")
    d["mode_exec"] = mode_exec
    if content is not None:
        d["binary"] = binary
        d["truncated"] = truncated
        d["content"] = None if binary else content
    return d


_GOOD_TEMPLATE = """\
name: acme-bot
display_name: Acme Bot
description: |
  Acme Bot helps the sales team triage inbound leads.
  It enriches each lead and routes hot ones to an owner.
version: 1.0.0
author: Acme Inc
resources:
  cpu: "2"
  memory: 2g
use_cases:
  - "Triage today's inbound leads and flag the hot ones"
  - "Summarize the pipeline for this week"
  - "Draft a follow-up for lead X"
capabilities:
  - lead triage
git:
  push_enabled: true
"""

_GOOD_GITIGNORE = "\n".join([
    ".env", ".env.*", ".mcp.json", ".claude/projects/", ".trinity/",
    ".claude/statsig/", ".claude/todos/", ".claude/debug/", ".claude/sessions/",
    ".claude/shell-snapshots/", "content/", "*.pem", "*.key", "credentials.json",
]) + "\n"


def good_snapshot():
    return {
        "schema": 1,
        "root": "/home/developer",
        "files": {
            "template.yaml": _f(_GOOD_TEMPLATE),
            "CLAUDE.md": _f("# Acme Bot\n\nYou triage sales leads.\n## Workflow\n1. Fetch leads\n"),
            ".gitignore": _f(_GOOD_GITIGNORE),
            ".env.example": _f("# Your Acme API key\nACME_API_KEY=your-key-here\n"),
            ".mcp.json.template": _f('{"mcpServers": {}}'),
            "README.md": _f("# Acme Bot\n"),
            "dashboard.yaml": _f("widgets:\n  - type: text\n    content: hi\n"),
        },
        "dirs": {".claude/commands": ["triage.md"], ".claude/skills": [], ".claude/agents": [], "schemas": None},
        "skills": {".claude/commands/triage.md": _f("# Triage\nDo the triage.\n")},
        "hit_total_cap": False,
    }


def empty_snapshot():
    """An agent missing everything important."""
    return {"schema": 1, "root": "/home/developer", "files": {}, "dirs": {}, "skills": {}, "hit_total_cap": False}


# ---------------------------------------------------------------------------
# Spec catalog consistency
# ---------------------------------------------------------------------------

class TestSpecConsistency:
    def test_ids_unique(self):
        ids = [c.id for c in spec.CHECKS]
        assert len(ids) == len(set(ids)), "duplicate check ids in spec.CHECKS"

    def test_catalog_size(self):
        assert len(spec.CHECKS) == 100, f"expected 100 checks, found {len(spec.CHECKS)}"

    def test_severity_and_type_valid(self):
        for c in spec.CHECKS:
            assert c.severity in spec.SEVERITIES, c.id
            assert c.type in spec.TYPES, c.id
            assert c.category in spec.CATEGORIES, c.id

    def test_ai_checks_have_prompts(self):
        for c in spec.CHECKS:
            if c.type == "ai":
                assert c.prompt, f"AI check {c.id} has no prompt"

    def test_static_registry_matches_spec(self):
        assert set(STATIC_CHECKS.keys()) == set(spec.STATIC_IDS), (
            "static_checks.STATIC_CHECKS must match spec.STATIC_IDS exactly: "
            f"missing={set(spec.STATIC_IDS) - set(STATIC_CHECKS)} "
            f"extra={set(STATIC_CHECKS) - set(spec.STATIC_IDS)}"
        )

    def test_auto_fixable_are_static(self):
        for cid in spec.AUTO_FIXABLE_IDS:
            assert spec.BY_ID[cid].type == "static", f"{cid} is auto_fixable but not static"

    def test_every_auto_fixable_has_a_fix(self):
        for cid in spec.AUTO_FIXABLE_IDS:
            # _compute_new_gitignore must not raise FixError for an auto-fixable id.
            fixes._compute_new_gitignore(cid, "")  # no exception == has a fix

    def test_ai_severity_capped_at_soft(self):
        for c in spec.CHECKS:
            if c.type == "ai":
                assert spec.effective_severity(c) in ("soft", "info"), c.id


class TestSpecDocSync:
    def test_ids_match_doc(self):
        doc = Path(__file__).resolve().parents[2] / "docs" / "agent-validation-spec.md"
        text = doc.read_text(encoding="utf-8")
        doc_ids = set(re.findall(r"^\|\s*([A-Z]-\d{3})\s*\|", text, flags=re.MULTILINE))
        spec_ids = set(spec.ALL_IDS)
        assert doc_ids == spec_ids, (
            f"spec.py and docs/agent-validation-spec.md diverge: "
            f"in_doc_only={sorted(doc_ids - spec_ids)} in_spec_only={sorted(spec_ids - doc_ids)}"
        )


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

def _run_one(cid, snap):
    return run_static(snap, [cid])[cid]


class TestStaticChecks:
    def test_good_agent_passes_hard_checks(self):
        snap = good_snapshot()
        res = run_static(snap, list(spec.STATIC_IDS))
        hard_fails = [
            cid for cid, (status, _m, _d) in res.items()
            if status == "fail" and spec.BY_ID[cid].severity == "hard"
        ]
        assert hard_fails == [], f"good agent unexpectedly fails HARD checks: {hard_fails}"

    def test_missing_template_and_claude_fail(self):
        snap = empty_snapshot()
        assert _run_one("F-001", snap)[0] == "fail"
        assert _run_one("F-002", snap)[0] == "fail"

    def test_template_yaml_fields_skip_when_missing(self):
        # T-002 should SKIP (not FAIL) when template.yaml is absent — F-001 owns it.
        assert _run_one("T-002", empty_snapshot())[0] == "skipped"

    def test_invalid_template_yaml_fails_t001(self):
        snap = good_snapshot()
        snap["files"]["template.yaml"] = _f("name: [unclosed\n")
        assert _run_one("T-001", snap)[0] == "fail"

    def test_gitignore_secret_exclusions(self):
        snap = good_snapshot()
        snap["files"][".gitignore"] = _f("# nothing useful\n")
        assert _run_one("S-001", snap)[0] == "fail"   # .env not ignored
        assert _run_one("S-002", snap)[0] == "fail"   # .mcp.json not ignored
        assert _run_one("S-005", snap)[0] == "fail"   # .trinity/ not ignored

    def test_s005_accepts_star_form_trinity_ignore(self):
        # Brain-Orb templates ship `.trinity/*` + `!.trinity/brain-orb/` so the
        # committed hooks stay tracked (trinity-enterprise#76). The star form
        # satisfies the same "runtime state isn't committed" intent — S-005
        # must not flag it (its auto-fix would re-append the dir form).
        snap = good_snapshot()
        snap["files"][".gitignore"] = _f(
            ".env\n.mcp.json\n.trinity/*\n!.trinity/brain-orb/\n"
        )
        assert _run_one("S-005", snap)[0] == "pass"

    def test_blanket_claude_exclusion_g001(self):
        snap = good_snapshot()
        snap["files"][".gitignore"] = _f(".claude/\n")
        assert _run_one("G-001", snap)[0] == "fail"
        # but a specific subdir exclusion must NOT trip it
        snap["files"][".gitignore"] = _f(".claude/projects/\n")
        assert _run_one("G-001", snap)[0] == "pass"

    def test_hardcoded_secret_s003(self):
        snap = good_snapshot()
        snap["files"]["CLAUDE.md"] = _f("Use this key: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n")
        status, msg, detail = _run_one("S-003", snap)
        assert status == "fail"
        # the secret value itself must never appear in the result
        blob = (msg + str(detail)).lower()
        assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in blob

    def test_mcp_var_documented_k001(self):
        snap = good_snapshot()
        snap["files"][".mcp.json.template"] = _f('{"mcpServers": {"x": {"env": {"K": "${ACME_TOKEN}"}}}}')
        # ACME_TOKEN not in .env.example → fail
        assert _run_one("K-001", snap)[0] == "fail"
        snap["files"][".env.example"] = _f("ACME_TOKEN=your-token\n")
        assert _run_one("K-001", snap)[0] == "pass"

    def test_dashboard_required_field_d003(self):
        snap = good_snapshot()
        snap["files"]["dashboard.yaml"] = _f("widgets:\n  - type: text\n    value: oops\n")  # text needs 'content'
        assert _run_one("D-003", snap)[0] == "fail"

    def test_p006_approval_gate_in_scheduled_skill(self):
        snap = good_snapshot()
        snap["files"]["template.yaml"] = _f(
            _GOOD_TEMPLATE + "schedules:\n  - name: daily\n    message: /triage\n    cron: '0 9 * * *'\n"
        )
        snap["skills"][".claude/commands/triage.md"] = _f("# Triage\nFirst, ask the user to confirm.\n")
        assert _run_one("P-006", snap)[0] == "fail"
        # remove the approval gate → pass
        snap["skills"][".claude/commands/triage.md"] = _f("# Triage\nProcess all leads automatically.\n")
        assert _run_one("P-006", snap)[0] == "pass"

    def test_check_that_raises_is_skipped_not_fatal(self):
        # A garbage snapshot (no 'files' key) must not raise out of run_static.
        res = run_static({}, ["F-001"])
        assert res["F-001"][0] in ("fail", "skipped")


# ---------------------------------------------------------------------------
# Gitignore auto-fix transforms
# ---------------------------------------------------------------------------

class TestGitignoreFixes:
    def test_append_env(self):
        out = fixes._compute_new_gitignore("S-001", "*.log\n")
        assert ".env" in out.splitlines()
        assert "*.log" in out.splitlines()

    def test_idempotent_noop(self):
        current = ".env\n.env.*\n"
        assert fixes._compute_new_gitignore("S-001", current) == current

    def test_crlf_no_duplicate(self):
        # An existing CRLF line must be recognised so we don't append a duplicate.
        current = ".env\r\n"
        out = fixes._compute_new_gitignore("S-001", current)
        # .env should appear once (the .env.* may be added)
        assert out.count(".env\n") <= 1 or ".env" in out
        assert sum(1 for l in out.splitlines() if l.strip().rstrip("\r") == ".env") == 1

    def test_comment_line_not_matched(self):
        out = fixes._compute_new_gitignore("S-007", "# content/ is special\n")
        assert "content/" in [l.strip() for l in out.splitlines()]

    def test_g001_removes_blanket_keeps_specific(self):
        current = ".claude/\n.claude/projects/\nsomething\n"
        out = fixes._compute_new_gitignore("G-001", current)
        lines = [l.strip() for l in out.splitlines()]
        assert ".claude/" not in lines          # blanket removed
        assert ".claude/projects/" in lines      # specific survives
        assert "something" in lines

    def test_unknown_check_raises(self):
        with pytest.raises(fixes.FixError):
            fixes._compute_new_gitignore("Z-999", "")


# ---------------------------------------------------------------------------
# AI checks
# ---------------------------------------------------------------------------

class TestAiChecks:
    def test_no_key_skips_all(self, monkeypatch):
        monkeypatch.setattr(ai_checks, "get_anthropic_api_key", lambda: "")
        ids = list(spec.AI_IDS)[:5]
        out = asyncio.run(ai_checks.run_ai(good_snapshot(), ids))
        assert set(out.keys()) == set(ids)
        for cid in ids:
            assert out[cid]["status"] == "skipped"
            assert out[cid]["skip_reason"] == "no_api_key"

    def test_omitted_check_becomes_skipped(self, monkeypatch):
        monkeypatch.setattr(ai_checks, "get_anthropic_api_key", lambda: "fake-key")
        ids = ["C-002", "C-003", "C-004"]

        async def fake_call(client, api_key, checks, bundle):
            # Only answer the FIRST check; omit the rest.
            first = checks[0]
            return {first.id: {"status": "pass", "explanation": "ok", "confidence": 0.9}}

        monkeypatch.setattr(ai_checks, "_call_category", fake_call)
        out = asyncio.run(ai_checks.run_ai(good_snapshot(), ids))
        assert set(out.keys()) == set(ids)  # iterate-expected: none vanish
        answered = [cid for cid in ids if out[cid]["status"] != "skipped"]
        skipped = [cid for cid in ids if out[cid]["status"] == "skipped"]
        assert len(answered) == 1
        assert len(skipped) == 2
        for cid in skipped:
            assert out[cid]["skip_reason"] == "ai_no_result"

    def test_redaction_strips_secrets(self):
        bundle = ai_checks._redact("token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345\n")
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in bundle
        assert "[REDACTED]" in bundle


# ---------------------------------------------------------------------------
# build_report orchestration (collect monkeypatched; real tmp DB persistence)
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_assembles_and_persists(self, monkeypatch):
        import services.compatibility as svc

        async def fake_collect(name):
            return {"status": "ok", "snapshot": good_snapshot(), "runtime": "claude-code"}

        async def fake_run_ai(snapshot, ai_ids):
            return {cid: {"status": "pass", "explanation": None, "confidence": 0.8} for cid in ai_ids}

        monkeypatch.setattr(svc, "collect", fake_collect)
        monkeypatch.setattr(svc, "run_ai", fake_run_ai)

        report = asyncio.run(svc.build_report("acme-bot", include_ai=True))
        assert report["agent_name"] == "acme-bot"
        assert report["container_running"] is True
        assert report["overall_status"] in ("compatible", "issues")
        assert report["ai_ran_at"] is not None
        # every emitted AI check must be capped at soft/info severity
        for c in report["checks"]:
            if c["type"] == "ai":
                assert c["severity"] in ("soft", "info")
        # persisted — a follow-up read returns the row
        persisted = svc.db.get_compatibility_result("acme-bot")
        assert persisted is not None
        assert persisted["agent_name"] == "acme-bot"

    def test_codex_runtime_omits_claude_only_checks(self, monkeypatch):
        import services.compatibility as svc

        async def fake_collect(name):
            return {"status": "ok", "snapshot": good_snapshot(), "runtime": "codex"}

        async def fake_run_ai(snapshot, ai_ids):
            return {cid: {"status": "pass", "explanation": None, "confidence": 0.8} for cid in ai_ids}

        monkeypatch.setattr(svc, "collect", fake_collect)
        monkeypatch.setattr(svc, "run_ai", fake_run_ai)

        report = asyncio.run(svc.build_report("codex-agent", include_ai=False))
        ids = {c["check_id"] for c in report["checks"]}
        # CLAUDE.md / .claude-skill checks are claude_only → omitted for codex
        assert "C-001" not in ids
        assert "F-002" not in ids
        assert "P-006" not in ids
        # runtime-agnostic checks still present
        assert "F-001" in ids
        assert "S-001" in ids

    def test_stopped_container_is_unavailable(self, monkeypatch):
        import services.compatibility as svc

        async def fake_collect(name):
            return {"status": "not_running", "snapshot": None, "runtime": "claude-code"}

        monkeypatch.setattr(svc, "collect", fake_collect)
        report = asyncio.run(svc.build_report("stopped-agent", include_ai=False))
        assert report["container_running"] is False
        assert report["overall_status"] == "unavailable"
        assert "stopped" in (report["message"] or "").lower()


# ---------------------------------------------------------------------------
# Collector degraded path (no Docker)
# ---------------------------------------------------------------------------

class TestCollectorDegraded:
    def test_not_running_when_no_container(self, monkeypatch):
        from services.compatibility import collector

        monkeypatch.setattr(collector, "get_agent_container", lambda name: None)
        monkeypatch.setattr(collector, "get_agent_runtime", lambda name: "claude-code")
        out = asyncio.run(collector.collect("ghost"))
        assert out["status"] == "not_running"
        assert out["snapshot"] is None


class TestCollectorScript:
    """The collector's in-container script is pure stdlib Python; run the
    generated script against a temp ROOT in a subprocess (no Docker) and assert
    it emits a single valid JSON snapshot with the right per-file handling."""

    def _run(self, root: Path):
        import json as _json
        import subprocess
        import sys
        from services.compatibility import collector

        script = collector._build_script(str(root))
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        return _json.loads(proc.stdout)

    def test_snapshot_shape(self, tmp_path):
        (tmp_path / "template.yaml").write_text("name: x\n")
        (tmp_path / "CLAUDE.md").write_text("# hi\n")
        # secret-bearing file: content must NOT be captured
        (tmp_path / ".env").write_text("SECRET=ghp_realtokenvalue1234567890\n")
        # binary file
        (tmp_path / ".gitignore").write_bytes(b"\x00\x01binary")
        skills = tmp_path / ".claude" / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")

        snap = self._run(tmp_path)
        assert snap["files"]["template.yaml"]["content"] == "name: x\n"
        assert snap["files"]["CLAUDE.md"]["exists"] is True
        # .env existence captured, content NOT (read_content False → no 'content' key path)
        assert snap["files"][".env"]["exists"] is True
        assert "content" not in snap["files"][".env"]
        # binary flagged, content None
        assert snap["files"][".gitignore"]["binary"] is True
        assert snap["files"][".gitignore"]["content"] is None
        # missing file → exists False
        assert snap["files"]["dashboard.yaml"]["exists"] is False
        # skill walked
        assert any(rel.endswith("SKILL.md") for rel in snap["skills"])

    def test_huge_file_truncated(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("x" * (300 * 1024))
        snap = self._run(tmp_path)
        assert snap["files"]["CLAUDE.md"]["truncated"] is True
        assert len(snap["files"]["CLAUDE.md"]["content"]) <= 256 * 1024
