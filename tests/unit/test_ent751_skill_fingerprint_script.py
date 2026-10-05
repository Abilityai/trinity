"""
Gated skills — the in-container fingerprint script, executed for real
(trinity-enterprise#751).

Target: ``services/skill_gate_service._FINGERPRINT_SCRIPT`` — the script the
gate runs as root inside the executor with ``python3 -I -S``. It decides what
"the skill that was approved" means at approval time, so it is run here, under
the same flags, against a real skills tree (the request's ``home`` field points
it at a temp dir; the backend always sends the default).
"""
import json
import os
import subprocess
import sys

import pytest

from services.skill_gate_service import _FINGERPRINT_SCRIPT

pytestmark = pytest.mark.unit


def _run(home, names):
    out = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _FINGERPRINT_SCRIPT,
         json.dumps({"names": names, "home": str(home)})],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _skill(home, dirname, body="# do it\n", fm_name=None, files=None):
    d = home / "skills" / dirname
    d.mkdir(parents=True, exist_ok=True)
    head = f"---\nname: {fm_name}\n---\n" if fm_name else ""
    (d / "SKILL.md").write_text(head + body)
    for rel, content in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def test_a_skill_is_found_by_directory_and_fingerprinted(tmp_path):
    _skill(tmp_path, "pay-invoice", files={"scripts/pay.py": "print(1)\n"})
    out = _run(tmp_path, ["pay-invoice"])
    assert out["pay-invoice"]["kind"] == "own"
    assert len(out["pay-invoice"]["fingerprint"]) == 64


def test_any_content_change_changes_the_fingerprint(tmp_path):
    d = _skill(tmp_path, "pay-invoice", files={"scripts/pay.py": "print(1)\n"})
    before = _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"]
    (d / "scripts" / "pay.py").write_text("print(2)\n")
    assert _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"] != before


def test_an_added_file_or_an_exec_bit_changes_the_fingerprint(tmp_path):
    d = _skill(tmp_path, "pay-invoice", files={"run.sh": "echo hi\n"})
    first = _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"]
    os.chmod(d / "run.sh", 0o755)
    second = _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"]
    (d / "extra.md").write_text("more\n")
    third = _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"]
    assert len({first, second, third}) == 3


def test_the_platform_meta_file_is_ignored_and_marks_a_library_skill(tmp_path):
    d = _skill(tmp_path, "pay-invoice")
    before = _run(tmp_path, ["pay-invoice"])["pay-invoice"]["fingerprint"]
    (d / ".trinity-skill.json").write_text('{"version": "x"}')
    after = _run(tmp_path, ["pay-invoice"])["pay-invoice"]
    assert after["fingerprint"] == before
    assert after["kind"] == "library"


def test_a_name_resolves_by_frontmatter_when_no_directory_matches(tmp_path):
    _skill(tmp_path, "payments-v2", fm_name="pay-invoice")
    assert "fingerprint" in _run(tmp_path, ["pay-invoice"])["pay-invoice"]


def test_lookup_ignores_case(tmp_path):
    _skill(tmp_path, "Pay-Invoice")
    assert "fingerprint" in _run(tmp_path, ["pay-invoice"])["pay-invoice"]


def test_two_candidates_are_ambiguous(tmp_path):
    _skill(tmp_path, "pay-invoice")
    _skill(tmp_path, "other", fm_name="pay-invoice")
    assert _run(tmp_path, ["pay-invoice"])["pay-invoice"] == {"error": "ambiguous"}


def test_a_skill_and_a_legacy_command_of_the_same_name_are_ambiguous(tmp_path):
    _skill(tmp_path, "pay-invoice")
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "pay-invoice.md").write_text("do it")
    assert _run(tmp_path, ["pay-invoice"])["pay-invoice"] == {"error": "ambiguous"}


def test_a_legacy_command_alone_is_fingerprinted(tmp_path):
    (tmp_path / "commands").mkdir()
    (tmp_path / "commands" / "pay-invoice.md").write_text("do it")
    out = _run(tmp_path, ["pay-invoice"])["pay-invoice"]
    assert out["kind"] == "command" and len(out["fingerprint"]) == 64


def test_a_symlink_inside_the_skill_is_refused(tmp_path):
    d = _skill(tmp_path, "pay-invoice")
    os.symlink("/etc/hostname", d / "link")
    assert _run(tmp_path, ["pay-invoice"])["pay-invoice"]["error"] == "symlink"


def test_a_missing_skill_is_not_found(tmp_path):
    assert _run(tmp_path, ["pay-invoice"]) == {"pay-invoice": {"error": "not_found"}}
