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
import datetime
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services.compatibility import spec
from services.compatibility import static_checks
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
        # #2137: 101 -> 88. 17 retired (dead-field, legacy-layout, duplicate)
        # + 4 DP checks implemented. Retired ids are never reissued.
        # ent#411: +1 (I-006, Trinity plugin presence) -> 89.
        assert len(spec.CHECKS) == 89, f"expected 89 checks, found {len(spec.CHECKS)}"

    def test_retired_ids_are_never_reissued(self):
        """#2137: persisted `checks_json` rows predate the retirement.

        Reusing a retired id would silently re-interpret an old stored verdict
        as a verdict about a different check.
        """
        retired = {
            "F-008", "F-012", "F-013", "T-012", "T-016", "T-017", "C-009",
            "K-002", "K-005", "G-002", "G-003", "G-004", "G-005", "D-006",
            "I-003", "I-004", "I-005", "DP-005",
        }
        clash = retired & set(spec.ALL_IDS)
        assert not clash, f"retired ids reissued: {sorted(clash)}"

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
        # #2137: was `[A-Z]-\d{3}` — a SINGLE letter. The doc's two-letter
        # `DP-001`..`DP-005` never matched, so five checks sat documented,
        # indexed, and entirely unimplemented while this test reported the two
        # files in sync. The anti-drift guarantee did not hold for any
        # two-letter category.
        doc_ids = set(re.findall(r"^\|\s*([A-Z]{1,2}-\d{3})\s*\|", text, flags=re.MULTILINE))
        spec_ids = set(spec.ALL_IDS)
        assert doc_ids == spec_ids, (
            f"spec.py and docs/agent-validation-spec.md diverge: "
            f"in_doc_only={sorted(doc_ids - spec_ids)} in_spec_only={sorted(spec_ids - doc_ids)}"
        )

    def test_severities_match_doc(self):
        """#2137: the id SET matching is not enough.

        Every severity change has to land in both files, and an id-only test
        passes happily while the doc claims HARD for a check the catalog now
        emits as INFO — which is the column an operator actually reads.
        """
        doc = Path(__file__).resolve().parents[2] / "docs" / "agent-validation-spec.md"
        text = doc.read_text(encoding="utf-8")
        rows = re.findall(
            r"^\|\s*([A-Z]{1,2}-\d{3})\s*\|\s*(HARD|SOFT|INFO)\s*\|",
            text, flags=re.MULTILINE,
        )
        mismatched = {
            cid: (sev.lower(), spec.BY_ID[cid].severity)
            for cid, sev in rows
            if cid in spec.BY_ID and sev.lower() != spec.BY_ID[cid].severity
        }
        assert not mismatched, (
            "doc Severity column disagrees with spec.py (doc, spec): "
            f"{mismatched}"
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

    # -- D block: self-explaining messages + hardening against real YAML (#2110) --

    _SUPPORTED = "metric, status, progress, text, markdown, table, list, link, image, divider, spacer"

    @staticmethod
    def _dash(snap, yaml_text):
        snap["files"]["dashboard.yaml"] = _f(yaml_text)
        return snap

    def test_d002_names_each_unsupported_type_with_count(self):
        # the issue's shape: widgets nested under sections, five chart widgets
        snap = self._dash(good_snapshot(), (
            "title: Ops\n"
            "sections:\n"
            "  - title: A\n"
            "    widgets:\n"
            "      - type: chart\n"
            "      - type: chart\n"
            "      - type: chart\n"
            "      - type: metric\n"
            "        label: x\n"
            "        value: 1\n"
            "  - title: B\n"
            "    widgets:\n"
            "      - type: chart\n"
            "      - type: gauge\n"
            "      - type: chart\n"
        ))
        status, msg, detail = _run_one("D-002", snap)
        assert status == "fail"
        assert msg.startswith("unsupported dashboard widget type(s)")  # prefix byte-identical
        assert "'chart' ×5, 'gauge' ×1" in msg
        assert f"— not rendered; supported: {self._SUPPORTED}" in msg
        assert "there is no chart widget" in msg
        assert detail == {"types": ["chart", "gauge"]}

    def test_d002_passes_when_every_type_is_supported_and_scalars_are_ignored(self):
        # pins `_dashboard`'s dict filter: non-dict list entries never reach a check
        types = "".join(f"  - type: {t}\n" for t in self._SUPPORTED.split(", "))
        snap = self._dash(good_snapshot(), "widgets:\n" + types + "  - plain-string\n  - 42\n")
        assert _run_one("D-002", snap)[0] == "pass"

    def test_d002_falsy_types_are_not_counted(self):
        # missing/empty type is the agent server's "missing 'type'" strip — not D-002's finding
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            "  - type: \"\"\n"
            "  - type: null\n"
            "  - type: 0\n"
            "  - type: false\n"
            "  - type: text\n"
            "    content: hi\n"
        ))
        assert _run_one("D-002", snap)[0] == "pass"

    def test_d002_is_deterministic_and_caps_named_types(self):
        shuffled = ["zeta", "alpha", "gamma", "beta", "eta", "delta", "theta", "epsilon"]
        yaml_text = "widgets:\n" + "".join(f"  - type: {t}\n" for t in shuffled)
        first = _run_one("D-002", self._dash(good_snapshot(), yaml_text))
        second = _run_one("D-002", self._dash(good_snapshot(), yaml_text))
        assert first == second  # identical string every run
        status, msg, detail = first
        assert status == "fail"
        assert msg.count(" ×") == 5  # at most five named …
        assert "'alpha' ×1, 'beta' ×1, 'delta' ×1, 'epsilon' ×1, 'eta' ×1, +3 more" in msg  # … alphabetical
        assert detail["types"] == sorted(shuffled)  # detail keeps all eight

    def test_d002_non_string_types_are_named_not_check_errors(self):
        # at HEAD `sorted({5, "chart"})` raised TypeError → "check could not be evaluated"
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            "  - type: 5\n"
            "  - type: true\n"
            "  - type: {a: 1}\n"
            "  - type: chart\n"
        ))
        status, msg, detail = _run_one("D-002", snap)
        assert status == "fail"
        assert "'5' ×1" in msg
        assert "'True' ×1" in msg
        assert "'chart' ×1" in msg
        assert "check_error" not in (detail or {})
        assert "TypeError" not in msg

    def test_d002_clips_long_agent_authored_type_names(self):
        # 120 printable chars + a newline, an ANSI escape and a bidi override; and a blank name
        hostile = "x" * 50 + "\\n" + "y" * 30 + "\\e[31m" + "z" * 30 + "\\u202E" + "w" * 10
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            f'  - type: "{hostile}"\n'
            '  - type: "   "\n'
        ))
        status, msg, detail = _run_one("D-002", snap)
        assert status == "fail"
        named = re.findall(r"'([^']*)' ×1", msg)
        assert len(named) == 2
        blank, clipped = named  # '(' sorts before 'x'
        assert blank == "(blank)"
        assert len(clipped) <= 40 and clipped.endswith("…")
        assert "\n" not in clipped and "\x1b" not in clipped and "‮" not in clipped
        assert detail["types"] == [blank, clipped]  # same bounded strings in message and detail

    def test_d002_trend_hint_only_when_chart_is_involved(self):
        gauge = _run_one("D-002", self._dash(good_snapshot(), "widgets:\n  - type: gauge\n"))
        chart = _run_one("D-002", self._dash(good_snapshot(), "widgets:\n  - type: chart\n"))
        assert gauge[0] == "fail" and "trend lines" not in gauge[1]
        assert chart[0] == "fail" and "trend lines" in chart[1]

    def test_d003_message_names_type_and_missing_field(self):
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            "  - type: text\n"
            "    value: oops\n"
            "  - type: list\n"
            "    label: things\n"
        ))
        status, msg, _detail = _run_one("D-003", snap)
        assert status == "fail"
        assert msg.startswith("dashboard widgets missing required fields (won't render)")  # prefix byte-identical
        assert "'list' needs items, 'text' needs content" in msg

    def test_d003_non_string_type_is_not_a_hard_check_error(self):
        # at HEAD `req.get({'a': 1})` raised (unhashable) → a spurious HARD "check could not be evaluated"
        snap = self._dash(good_snapshot(), "widgets:\n  - type: {a: 1}\n  - type: text\n    content: hi\n")
        status, _msg, detail = _run_one("D-003", snap)
        assert status == "pass"
        assert "check_error" not in (detail or {})

    def test_d004_detail_is_json_safe_for_date_labels(self):
        # the hardened loader hands back a `datetime.date` for `label: 2026-01-01`;
        # at HEAD that reached `detail` raw and `json.dumps(checks)` in upsert_result raised
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            "  - type: progress\n"
            "    label: 2026-01-01\n"
            "    value: 150\n"
            "  - type: progress\n"
            "    value: -3\n"
        ))
        status, msg, detail = _run_one("D-004", snap)
        assert status == "fail"
        assert msg == "progress widget values outside 0–100"  # unchanged
        json.dumps(detail)
        assert detail["widgets"] == ["2026-01-01", -3]

    def test_d005_colors_are_json_safe_and_mixed_types_do_not_crash(self):
        # at HEAD `sorted({"teal", 5})` raised TypeError → "check could not be evaluated"
        snap = self._dash(good_snapshot(), (
            "widgets:\n"
            "  - {type: status, label: a, value: x, color: teal}\n"
            "  - {type: status, label: b, value: x, color: 5}\n"
            "  - {type: status, label: c, value: x, color: 2026-01-01}\n"
        ))
        status, msg, detail = _run_one("D-005", snap)
        assert status == "fail"
        assert msg == "status widget colors not in the allowed palette"  # unchanged
        json.dumps(detail)
        assert detail["colors"] == ["2026-01-01", "5", "teal"]
        assert "check_error" not in (detail or {})

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

    def test_persisted_d002_message_names_the_type(self, monkeypatch):
        """#2110 surfaces (iii) and (ii): the self-explaining message survives the
        persistence round-trip AND the response model the router returns."""
        import services.compatibility as svc
        from models import CompatibilityCheck

        snap = good_snapshot()
        snap["files"]["dashboard.yaml"] = _f("sections:\n  - widgets:\n      - type: chart\n")

        async def fake_collect(name):
            return {"status": "ok", "snapshot": snap, "runtime": "claude-code"}

        monkeypatch.setattr(svc, "collect", fake_collect)
        asyncio.run(svc.build_report("chart-agent-2110", include_ai=False))

        persisted = svc.db.get_compatibility_result("chart-agent-2110")
        entry = next(c for c in persisted["checks"] if c["check_id"] == "D-002")
        assert entry["status"] == "fail"
        assert "'chart' ×1" in entry["message"]
        assert entry["detail"]["types"] == ["chart"]
        CompatibilityCheck(**entry)  # what `CompatibilityReport(**report)` constructs per check

    def test_upsert_result_tolerates_non_json_natives_in_detail(self):
        """#2110 sink belt: one check's YAML-native `detail` value must not fail
        the persistence of the whole report."""
        import services.compatibility as svc

        svc.db.upsert_compatibility_result(
            "date-detail-2110",
            overall_status="issues",
            checks=[{"check_id": "D-004", "status": "fail", "message": "m",
                     "detail": {"widgets": [datetime.date(2026, 1, 1)]}}],
            hard_count=0, soft_count=1, info_count=0,
            container_running=True, ai_ran_at=None, static_ran_at="2026-01-01T00:00:00Z",
        )
        row = svc.db.get_compatibility_result("date-detail-2110")
        assert row["checks"][0]["detail"]["widgets"] == ["2026-01-01"]

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


# ---------------------------------------------------------------------------
# T-018 + the three in-radius corrections (trinity-enterprise#89)
# ---------------------------------------------------------------------------

def _template_snapshot(template_yaml: str, **extra):
    """A snapshot whose only interesting content is `template.yaml`."""
    snap = good_snapshot()
    snap["files"]["template.yaml"] = _f(template_yaml)
    snap.update(extra)
    return snap


_SCHEDULES_OK = """\
name: acme-bot
description: does things
schedules:
  - name: daily-briefing
    cron: "0 9 * * MON"
    message: /triage
    enabled: true
"""

_SCHEDULES_MALFORMED = """\
name: acme-bot
description: does things
schedules:
  - cron: "0 9 * * *"
    message: /triage
"""

# The live c_p006 fail-open shape: a non-iterable `schedules:`.
_SCHEDULES_SCALAR = """\
name: acme-bot
description: does things
schedules: 5
"""

_HOSTILE_TEMPLATES = [
    _SCHEDULES_SCALAR,
    "name: a\ndescription: d\nschedules: yes\n",
    "name: a\ndescription: d\nschedules:\n  - null\n",
    "name: a\ndescription: d\nschedules:\n  a: b\n",
    "name: a\ndescription: d\nschedules:\n  - name: {z: 1}\n",
    "name: a\ndescription: d\nschedules:\n  - name: x\n    cron: 5\n    message: m\n",
    "name: a\ndescription: d\nschedules:\n  - name: x\n    cron: '* * * * *'\n"
    "    message: m\n    timezone: 5\n",
]


class TestT018SchedulesWellFormed:
    def test_passes_on_a_well_formed_block(self):
        st, _msg, _detail = STATIC_CHECKS["T-018"](_template_snapshot(_SCHEDULES_OK))
        assert st == "pass"

    def test_passes_when_no_block_is_declared(self):
        st, _msg, _detail = STATIC_CHECKS["T-018"](good_snapshot())
        assert st == "pass"

    def test_fails_on_a_missing_required_key(self):
        st, _msg, detail = STATIC_CHECKS["T-018"](
            _template_snapshot(_SCHEDULES_MALFORMED))
        assert st == "fail"
        assert any("name" in e for e in detail["errors"])

    def test_fails_on_a_non_list_block(self):
        st, _msg, _detail = STATIC_CHECKS["T-018"](
            _template_snapshot(_SCHEDULES_SCALAR))
        assert st == "fail"

    def test_does_not_report_cron_syntax(self):
        """A-002 owns cron. Two checks contradicting each other on the same
        field is worse than either alone — and A-002 already ships."""
        bad_cron = ("name: a\ndescription: d\nschedules:\n"
                    "  - name: x\n    cron: '99 99 * * *'\n    message: /m\n")
        st, _msg, detail = STATIC_CHECKS["T-018"](_template_snapshot(bad_cron))
        assert st == "fail"
        # It fires (the entry IS dropped at materialization), but the message is
        # about the entry, and A-002 is the check that names cron validity.
        a002, _m, _d = STATIC_CHECKS["A-002"](_template_snapshot(bad_cron))
        assert a002 == "fail"

    def test_fails_closed_when_the_reader_raises(self, monkeypatch):
        """R4 — `run_static` turns a raise into `skipped`, and the report counts
        only `fail`, so a raising SOFT check flips overall_status from `issues`
        to `compatible` exactly when its finding was the only failure."""
        import services.template_schedules as tsched

        def _boom(_block):
            raise RuntimeError("reader exploded")

        monkeypatch.setattr(tsched, "schedule_shape_errors", _boom)
        st, _msg, detail = STATIC_CHECKS["T-018"](
            _template_snapshot(_SCHEDULES_MALFORMED))
        assert st == "fail", "T-018 must not rely on the fail-open outer net"
        assert detail == {"error_type": "RuntimeError"}

    def test_failure_detail_never_echoes_the_exception_message(self, monkeypatch):
        """`detail` is persisted to agent_compatibility_results.checks_json and
        rendered in the UI; `str(e)` can carry untrusted template content."""
        import services.template_schedules as tsched

        def _boom(_block):
            raise RuntimeError("SECRETTEMPLATECONTENT")

        monkeypatch.setattr(tsched, "schedule_shape_errors", _boom)
        _st, msg, detail = STATIC_CHECKS["T-018"](
            _template_snapshot(_SCHEDULES_MALFORMED))
        assert "SECRETTEMPLATECONTENT" not in repr(detail)
        assert "SECRETTEMPLATECONTENT" not in msg

    def test_run_static_does_not_convert_the_raise_to_skipped(self, monkeypatch):
        import services.template_schedules as tsched

        monkeypatch.setattr(
            tsched, "schedule_shape_errors",
            lambda _b: (_ for _ in ()).throw(RuntimeError("boom")))
        out = run_static(_template_snapshot(_SCHEDULES_MALFORMED), ["T-018"])
        assert out["T-018"][0] == "fail"


class TestP006ScheduleGuard:
    def test_non_list_schedules_no_longer_raises(self):
        """The live instance of the fail-open class: `c_p006` iterated
        `data.get("schedules") or []` with no list guard, unlike all four of its
        siblings — so `schedules: 5` made a HARD check silently vanish from
        hard_count."""
        out = run_static(_template_snapshot(_SCHEDULES_SCALAR), ["P-006"])
        st, _msg, detail = out["P-006"]
        assert st != "skipped"
        assert (detail or {}).get("skip_reason") != "check_error"


class TestA002CronAuthority:
    def test_accepts_a_named_day(self):
        """`0 9 * * MON` is valid — the scheduler translates Unix day numbers to
        APScheduler names and passes named days through. The old per-field
        regex rejected it."""
        tpl = ("name: a\ndescription: d\nschedules:\n"
               "  - name: x\n    cron: '0 9 * * MON'\n    message: /m\n")
        st, _msg, _detail = STATIC_CHECKS["A-002"](_template_snapshot(tpl))
        assert st == "pass"

    def test_rejects_an_out_of_range_field(self):
        """The old regex accepted `99 99 * * *` — so the report said "cron
        expressions are valid" for an expression creation silently drops."""
        tpl = ("name: a\ndescription: d\nschedules:\n"
               "  - name: x\n    cron: '99 99 * * *'\n    message: /m\n")
        st, _msg, _detail = STATIC_CHECKS["A-002"](_template_snapshot(tpl))
        assert st == "fail"

    def test_rejects_macros_and_six_field_crons(self):
        for cron in ("@daily", "0 9 * * * *"):
            tpl = ("name: a\ndescription: d\nschedules:\n"
                   f"  - name: x\n    cron: '{cron}'\n    message: /m\n")
            st, _msg, _detail = STATIC_CHECKS["A-002"](_template_snapshot(tpl))
            assert st == "fail", cron

    def test_agrees_with_the_materializer(self):
        """One cron authority: what A-002 blesses is what the reader keeps."""
        from services.template_schedules import normalize_declared_schedules

        for cron, expect_ok in [("0 9 * * MON", True), ("*/15 * * * 1-5", True),
                                ("99 99 * * *", False), ("@daily", False)]:
            tpl = ("name: a\ndescription: d\nschedules:\n"
                   f"  - name: x\n    cron: '{cron}'\n    message: /m\n")
            report_ok = STATIC_CHECKS["A-002"](_template_snapshot(tpl))[0] == "pass"
            kept = bool(normalize_declared_schedules(
                [{"name": "x", "cron": cron, "message": "/m"}]))
            assert report_ok is expect_ok and kept is expect_ok, cron


class TestNoCheckFailsOpenOnAHostileTemplate:
    """R4.4 — asserted across the WHOLE static catalog, not just T-018.

    A T-018-only assertion would never have caught `c_p006`, which had been
    failing open on `schedules: 5` for months.
    """

    @pytest.mark.parametrize("template_yaml", _HOSTILE_TEMPLATES)
    def test_no_static_check_lands_at_check_error(self, template_yaml):
        out = run_static(_template_snapshot(template_yaml), list(spec.STATIC_IDS))
        offenders = {
            cid: res for cid, res in out.items()
            if (res[2] or {}).get("skip_reason") == "check_error"
        }
        assert not offenders, offenders


class TestReportDirectionOnAFailingValidator:
    """Pins the DIRECTION, not just the absence of a raise: a broken validator
    must not report a healthy agent."""

    def test_build_report_stays_issues_when_the_reader_raises(self, monkeypatch):
        import services.compatibility as svc
        import services.template_schedules as tsched

        snap = _template_snapshot(_SCHEDULES_MALFORMED)

        async def fake_collect(_name):
            return {"status": "ok", "snapshot": snap, "runtime": "claude-code"}

        monkeypatch.setattr(svc, "collect", fake_collect)
        monkeypatch.setattr(
            tsched, "schedule_shape_errors",
            lambda _b: (_ for _ in ()).throw(RuntimeError("boom")))

        report = asyncio.run(svc.build_report("t018-agent", include_ai=False))
        t018 = next(c for c in report["checks"] if c["check_id"] == "T-018")
        assert t018["status"] == "fail"
        assert report["soft_count"] >= 1
        assert report["overall_status"] == "issues"

    def test_persisted_check_error_does_not_replay_as_clean(self):
        """`_report_from_persisted` recomputes counts from `checks_json`, so a
        swallowed raise persisted once used to be replayed as a clean bill of
        health on every stopped-agent read. ent#128 closed that at the sink:
        `_did_not_pass` counts a `skipped` row carrying `check_error` as a
        finding, so a row persisted by an OLDER build — before the swallow
        started returning `fail` — still cannot replay as clean."""
        import services.compatibility as svc

        swallowed = {"checks": [{
            "check_id": "T-018", "status": "skipped", "severity": "soft",
            "skip_reason": "check_error",
        }]}
        failed = {"checks": [{
            "check_id": "T-018", "status": "fail", "severity": "soft",
            "skip_reason": None,
        }]}

        assert svc._counts(swallowed["checks"])["soft_count"] == 1
        assert svc._counts(failed["checks"])["soft_count"] == 1

        degraded = svc._report_from_persisted(
            "a", failed, False, "stopped", "msg", "claude-code")
        assert degraded["soft_count"] == 1


class TestRunStaticLogsItsSwallow:
    def test_a_raising_check_is_logged(self, monkeypatch, caplog):
        """Before ent#89 this swallow left no trace anywhere, for all ~100
        checks — a broken validator reported "healthy" and the logs were
        silent. Both halves are now closed: ent#128 makes the result a FAIL so
        `_counts` sees it, and ent#89 logs it so it is diagnosable."""
        import logging

        monkeypatch.setitem(
            STATIC_CHECKS, "T-001",
            lambda _s: (_ for _ in ()).throw(RuntimeError("kaboom")))
        with caplog.at_level(logging.ERROR):
            out = run_static(good_snapshot(), ["T-001"])

        status, _msg, detail = out["T-001"]
        # FAIL, not a skip: a check that could not evaluate is not a check that
        # passed, and only a counted status reaches `hard_count`/`soft_count`.
        assert status == "fail"
        assert (detail or {}).get("check_error") == "kaboom"
        assert any("T-001" in r.message or "T-001" in str(r.args)
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# #2137 — catalog alignment with what the platform implements and the
# `create-agent` marketplace wizards actually generate.
# ---------------------------------------------------------------------------

_WIZARD_SKILL = """\
---
name: weekly-report
description: Produce the weekly pipeline report. Use when the user asks for a weekly summary.
---

# Weekly Report

1. Pull the pipeline.
2. Summarize movement.
3. Write the report.
"""

# The layout `plugins/create-agent/skills/custom/SKILL.md` documents verbatim:
# skills under `.claude/skills/<name>/SKILL.md`, never `.claude/commands/`.
_WIZARD_TEMPLATE = """\
name: acme-scout
display_name: Acme Scout
description: |
  Acme Scout tracks competitor movement for the Acme product team.
  It sweeps public sources weekly and reports material changes.
resources:
  cpu: "2"
  memory: 2g
schedules:
  - name: Weekly competitor sweep
    cron: "0 9 * * 1"
    message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."
"""

_WIZARD_GITIGNORE = "\n".join([
    ".env", ".env.*", ".mcp.json", ".claude/projects/", ".trinity/",
    ".claude/statsig/", ".claude/todos/", ".claude/debug/", ".claude/sessions/",
    ".claude/shell-snapshots/", "content/", "*.pem", "*.key", "credentials.json",
]) + "\n"


def wizard_snapshot():
    """An agent exactly as a `create-agent` wizard scaffolds it.

    Skills live at `.claude/skills/<name>/SKILL.md` with NO `.claude/commands/`,
    and schedule messages are prose. Before #2137 this shape drew a fistful of
    findings the author could not act on.
    """
    return {
        "schema": 1,
        "root": "/home/developer",
        "files": {
            "template.yaml": _f(_WIZARD_TEMPLATE),
            "CLAUDE.md": _f("# Acme Scout\n\nYou track competitors.\n\n## Workflow\n1. Sweep\n2. Report\n"),
            ".gitignore": _f(_WIZARD_GITIGNORE),
            ".env.example": _f("# Acme API key\nACME_API_KEY=your-key-here\n"),
            ".mcp.json.template": _f('{"mcpServers": {}}'),
            "README.md": _f("# Acme Scout\n"),
            "ARCHITECTURE.md": _f("# Architecture\n"),
            "dashboard.yaml": _f("widgets:\n  - type: text\n    content: ok\n"),
        },
        "dirs": {
            ".claude/commands": None,          # the wizards never create it
            ".claude/skills": ["weekly-report"],
            ".claude/agents": None,
            "schemas": None,
        },
        "skills": {".claude/skills/weekly-report/SKILL.md": _f(_WIZARD_SKILL)},
        "hit_total_cap": False,
    }


class TestWizardAgentIsClean:
    """The headline regression: a freshly-scaffolded agent has nothing to fix."""

    def _static_results(self, snap):
        static_ids = [c.id for c in spec.CHECKS if c.type == "static"]
        return run_static(snap, static_ids)

    def test_no_hard_findings(self):
        res = self._static_results(wizard_snapshot())
        hard = [
            cid for cid, r in res.items()
            if r[0] == "fail" and spec.BY_ID[cid].severity == "hard"
        ]
        assert hard == [], f"wizard-scaffolded agent has HARD findings: {hard}"

    def test_no_soft_findings(self):
        """SOFT means 'the author should act'. A wizard agent gives them nothing."""
        res = self._static_results(wizard_snapshot())
        soft = [
            (cid, res[cid][1]) for cid in res
            if res[cid][0] == "fail" and spec.BY_ID[cid].severity == "soft"
        ]
        assert soft == [], f"wizard-scaffolded agent has unactionable SOFT findings: {soft}"

    def test_no_check_errors(self):
        """`run_static` captures a raise as a FAIL carrying `check_error`."""
        res = self._static_results(wizard_snapshot())
        errored = [cid for cid, r in res.items() if (r[2] or {}).get("check_error")]
        assert errored == [], f"checks raised on the wizard fixture: {errored}"


class TestSlashCommandAnywhere:
    """#2137: `_slash_command` anchored at position 0, so the marketplace's own
    `"Run /skill"` schedules resolved to nothing and P-006 was inert."""

    def test_matches_mid_message(self):
        assert static_checks._slash_command("Run /pipeline-tick") == "pipeline-tick"
        assert static_checks._slash_command("Run /weekly-report and post it") == "weekly-report"
        assert static_checks._slash_command("  /leading") == "leading"

    def test_still_matches_at_position_zero(self):
        assert static_checks._slash_command("/report now") == "report"

    def test_prose_has_no_command(self):
        assert static_checks._slash_command("Summarize yesterday's activity.") is None

    def test_paths_and_urls_are_not_commands(self):
        """X-007 turns a resolved name into a SOFT finding, so a path read as a
        command manufactures the exact unactionable failure #2137 removes."""
        assert static_checks._slash_command("Read reports/daily and summarize") is None
        assert static_checks._slash_command("Fetch https://example.com/status") is None
        # Absolute paths: a command is ONE segment; these are not.
        assert static_checks._slash_command("Open /etc/passwd") is None
        assert static_checks._slash_command("Check /var/log/app.log for errors") is None

    def test_a_command_after_a_path_is_still_found(self):
        assert static_checks._slash_command(
            "Read /var/log/app.log then run /weekly-report"
        ) == "weekly-report"

    def test_p006_now_fires_on_a_run_slash_schedule(self):
        """The HARD guard that had never fired."""
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE.replace(
            'message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."',
            'message: "Run /weekly-report and post the summary"',
        ))
        snap["skills"][".claude/skills/weekly-report/SKILL.md"] = _f(
            _WIZARD_SKILL.replace("2. Summarize movement.", "2. Ask the user which region to cover.")
        )
        r = _run_one("P-006", snap)
        assert r[0] == "fail", r
        assert "approval gate" in r[1]

    def test_x007_resolves_a_skill_directory(self):
        """Before #2137 `_command_names` globbed only `.claude/commands/`."""
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE.replace(
            'message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."',
            'message: "Run /weekly-report and post the summary"',
        ))
        assert _run_one("X-007", snap)[0] == "pass"

    def test_x007_still_fails_on_a_genuinely_missing_target(self):
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE.replace(
            'message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."',
            'message: "Run /does-not-exist"',
        ))
        r = _run_one("X-007", snap)
        assert r[0] == "fail"
        assert r[2]["missing"] == ["does-not-exist"]

    def test_command_names_resolves_both_layouts(self):
        snap = wizard_snapshot()
        snap["skills"][".claude/commands/legacy.md"] = _f("# Legacy\n")
        snap["dirs"][".claude/commands"] = ["legacy.md"]
        assert set(static_checks._command_names(snap)) >= {"weekly-report", "legacy"}


class TestP006AutomationOptOut:
    """A skill that gates BY DESIGN is a decision, not a defect."""

    def _snap_with(self, frontmatter_extra, body_line):
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE.replace(
            'message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."',
            'message: "Run /weekly-report"',
        ))
        skill = _WIZARD_SKILL.replace(
            "description: Produce the weekly pipeline report. Use when the user asks for a weekly summary.",
            "description: Produce the weekly pipeline report. Use when the user asks for a weekly summary."
            + frontmatter_extra,
        ).replace("2. Summarize movement.", body_line)
        snap["skills"][".claude/skills/weekly-report/SKILL.md"] = _f(skill)
        return snap

    def test_gated_skill_is_not_a_hard_failure(self):
        snap = self._snap_with("\nautomation: gated", "2. Ask the user which region to cover.")
        r = _run_one("P-006", snap)
        assert r[0] == "pass", r
        assert r[2]["declared_gated"][0]["mode"] == "gated"

    def test_manual_skill_is_not_a_hard_failure(self):
        snap = self._snap_with("\nautomation: manual", "2. Wait for the user to confirm.")
        assert _run_one("P-006", snap)[0] == "pass"

    def test_explicit_autonomous_is_still_held_to_the_rule(self):
        snap = self._snap_with("\nautomation: autonomous", "2. Ask the user which region to cover.")
        assert _run_one("P-006", snap)[0] == "fail"

    def test_absent_frontmatter_key_is_still_held_to_the_rule(self):
        snap = self._snap_with("", "2. Ask the user which region to cover.")
        assert _run_one("P-006", snap)[0] == "fail"

    def test_the_word_in_prose_does_not_flip_the_check_off(self):
        """Parsed from frontmatter, never grepped from the body."""
        snap = self._snap_with("", "2. Ask the user which region to cover (automation: gated).")
        assert _run_one("P-006", snap)[0] == "fail"


class TestP004ScopedToSkillMd:
    def test_companion_reference_file_is_not_flagged(self):
        """P-009 tells authors to create these; P-004 used to flag them."""
        snap = wizard_snapshot()
        snap["skills"][".claude/skills/weekly-report/reference.md"] = _f("x\n" * 900)
        assert _run_one("P-004", snap)[0] == "pass"

    def test_oversized_skill_md_still_fails(self):
        snap = wizard_snapshot()
        snap["skills"][".claude/skills/weekly-report/SKILL.md"] = _f("x\n" * 900)
        r = _run_one("P-004", snap)
        assert r[0] == "fail"
        assert r[2]["files"] == [".claude/skills/weekly-report/SKILL.md"]


class TestA001AcceptsSlashAnywhere:
    def test_run_slash_message_passes(self):
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE.replace(
            'message: "Sweep tracked competitors for changes — pricing, product, messaging — and report anything material."',
            'message: "Run /weekly-report and post the summary"',
        ))
        assert _run_one("A-001", snap)[0] == "pass"

    def test_prose_message_is_info_not_soft(self):
        assert spec.BY_ID["A-001"].severity == "info"
        assert _run_one("A-001", wizard_snapshot())[0] == "fail"


class TestF004Conditional:
    def test_no_env_example_is_fine_without_credentials(self):
        snap = wizard_snapshot()
        del snap["files"][".env.example"]
        assert _run_one("F-004", snap)[0] == "pass"

    def test_missing_env_example_still_fails_when_credentials_declared(self):
        snap = wizard_snapshot()
        del snap["files"][".env.example"]
        snap["files"][".mcp.json.template"] = _f(
            '{"mcpServers": {"acme": {"env": {"ACME_API_KEY": "${ACME_API_KEY}"}}}}'
        )
        assert _run_one("F-004", snap)[0] == "fail"


class TestRuntimeDataPathChecks:
    """DP-001..DP-004 — documented since #1169, implemented in #2137."""

    def _snap(self, data_paths_yaml):
        snap = wizard_snapshot()
        snap["files"]["template.yaml"] = _f(_WIZARD_TEMPLATE + data_paths_yaml)
        return snap

    def test_absent_data_paths_passes_every_dp_check(self):
        snap = wizard_snapshot()
        for cid in ("DP-001", "DP-002", "DP-003", "DP-004"):
            assert _run_one(cid, snap)[0] == "pass", cid

    def test_dp001_accepts_entries_under_the_data_root(self):
        snap = self._snap('data_paths:\n  - "data/*.sqlite"\n  - "data/exports/*.csv"\n')
        assert _run_one("DP-001", snap)[0] == "pass"

    def test_dp001_rejects_a_path_outside_the_data_root(self):
        """`data/export` archives `/home/developer/data` and nothing else, so a
        plain relative entry is as unsnapshotted as `../escape`."""
        snap = self._snap('data_paths:\n  - "outputs/*.csv"\n')
        r = _run_one("DP-001", snap)
        assert r[0] == "fail"
        assert r[2]["entries"][0]["reason"] == "outside_data_root"

    def test_dp001_accepts_the_bare_data_root(self):
        snap = self._snap('data_paths:\n  - "data"\n')
        assert _run_one("DP-001", snap)[0] == "pass"

    def test_dp001_rejects_absolute_and_traversal(self):
        for bad in ("/etc/passwd", "../escape", "~/secrets"):
            snap = self._snap(f'data_paths:\n  - "{bad}"\n')
            r = _run_one("DP-001", snap)
            assert r[0] == "fail", bad
            assert r[2]["entries"][0]["path"] == bad

    def test_dp001_rejects_what_the_materializer_would_drop(self):
        """Shares `git_service._is_safe_data_path` with the executor."""
        snap = self._snap('data_paths:\n  - "data/$(whoami).db"\n')
        r = _run_one("DP-001", snap)
        assert r[0] == "fail"
        assert r[2]["entries"][0]["reason"] == "shell_metacharacters"

    def test_dp001_reports_a_non_list_block(self):
        snap = self._snap("data_paths: 5\n")
        r = _run_one("DP-001", snap)
        assert r[0] == "fail"
        assert r[2]["found_type"] == "int"

    def test_dp002_requires_the_data_root_ignored(self):
        snap = self._snap('data_paths:\n  - "data/*.sqlite"\n')
        assert _run_one("DP-002", snap)[0] == "fail"
        snap["files"][".gitignore"] = _f(_WIZARD_GITIGNORE + "data/\n")
        assert _run_one("DP-002", snap)[0] == "pass"

    def test_dp003_flags_overlap_with_managed_paths(self):
        snap = self._snap('data_paths:\n  - ".trinity/state.json"\n')
        assert _run_one("DP-003", snap)[0] == "fail"

    def test_dp003_flags_overlap_with_persistent_state(self):
        snap = self._snap('persistent_state:\n  - "data/keep.db"\ndata_paths:\n  - "data/keep.db"\n')
        r = _run_one("DP-003", snap)
        assert r[0] == "fail"
        assert r[2]["entries"][0]["conflicts_with"] == "persistent_state"

    def test_dp004_is_info_and_reports_a_property(self):
        assert spec.BY_ID["DP-004"].severity == "info"
        snap = self._snap('data_paths:\n  - "data/*.sqlite"\n')
        assert _run_one("DP-004", snap)[0] == "fail"


class TestCompatFixLock:
    """#1920: compat_fix adopts the shared ownership-checked SingleFlightLock.
    A busy holder -> FixBusy, and release is a compare-and-delete against the
    minted token -- never the pre-#1920 constant-'1' + unconditional delete
    that could remove a *successor's* lock."""

    @staticmethod
    def _store_backed_redis():
        client = MagicMock()
        store = {}

        def _set(key, val, nx=None, ex=None):
            if nx and key in store:
                return None
            store[key] = val
            return True

        client.set.side_effect = _set
        client.get.side_effect = lambda key: store.get(key)
        client.delete.side_effect = lambda key: store.pop(key, None)
        client.store = store
        return client

    def test_lock_win_busy_then_ownership_checked_release(self, monkeypatch):
        client = self._store_backed_redis()
        monkeypatch.setattr(
            fixes, "_redis_lib", MagicMock(from_url=lambda *a, **k: client)
        )
        monkeypatch.setattr(fixes, "_REDIS_URL", "redis://x")

        lock = fixes._lock("agent-x")
        assert lock is not None and lock.held
        # a real uuid token was stored, not the constant '1'
        assert client.store["compat_fix:agent-x"] == lock.token

        # A concurrent fix finds the key held -> FixBusy.
        with pytest.raises(fixes.FixBusy):
            fixes._lock("agent-x")

        # A successor took the key after our TTL lapsed -> release must NOT
        # delete it (the #1920 successor-safety property).
        client.store["compat_fix:agent-x"] = "successor-token"
        fixes._unlock(lock, "agent-x")
        assert client.store["compat_fix:agent-x"] == "successor-token"

    def test_lock_fails_open_when_redis_unconfigured(self, monkeypatch):
        monkeypatch.setattr(fixes, "_redis_lib", None)
        assert fixes._lock("agent-x") is None  # fail-open
        fixes._unlock(None, "agent-x")  # no-op, no raise

    def test_lock_fails_open_when_client_connect_raises(self, monkeypatch, caplog):
        """Redis is CONFIGURED (URL present) but the client build throws — the
        distinct fail-open branch from the ``None`` case above: it logs a
        warning and returns ``None`` so the fix still runs best-effort."""

        def _boom(*a, **k):
            raise RuntimeError("redis unreachable")

        monkeypatch.setattr(fixes, "_redis_lib", MagicMock(from_url=_boom))
        monkeypatch.setattr(fixes, "_REDIS_URL", "redis://x")
        with caplog.at_level("WARNING"):
            assert fixes._lock("agent-x") is None  # fail-open, not FixBusy
        assert any("fix lock unavailable" in r.getMessage() for r in caplog.records)
