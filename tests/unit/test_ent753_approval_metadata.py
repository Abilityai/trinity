"""
`approval: recommended` in a library skill's frontmatter (trinity-enterprise#753,
AC 5).

Target: ``skill_packaging.extract_contract`` (the one contract parser the
library listing and validation share) and ``skill_service._parse_skill_info``,
which carries it into every ``list_skills()`` entry the gate reconcile reads.

YAML 1.1 hands a check ``yes`` as a bool and an unquoted date as a ``date``: a
non-string value is named ``frontmatter_invalid:approval`` and read as no
recommendation — never a raise, never a guess.
"""
import datetime
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("frontmatter,approval,warned", [
    ({"approval": "recommended"}, "recommended", False),
    ({"approval": " Recommended "}, "recommended", False),
    ({"trinity": {"approval": "recommended"}, "approval": "nope"}, "recommended", False),
    ({}, None, False),
    ({"approval": None}, None, False),
    ({"approval": "required"}, None, True),
    ({"approval": True}, None, True),
    ({"approval": 1}, None, True),
    ({"approval": datetime.date(2026, 1, 1)}, None, True),
    ({"approval": {"by": "approver"}}, None, True),
    ({"approval": ["recommended"]}, None, True),
])
def test_the_contract_reads_approval(frontmatter, approval, warned):
    from services.skill_packaging import extract_contract
    contract, warnings = extract_contract(frontmatter)
    assert contract["approval"] == approval
    assert ("frontmatter_invalid:approval" in warnings) is warned


def test_a_library_entry_carries_it(tmp_path):
    from services.skill_service import skill_service
    d = tmp_path / "deploy"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: deploy\napproval: recommended\n---\n# Deploy\n\nShip it.\n")
    info = skill_service._parse_skill_info(None, "deploy", d / "SKILL.md")
    assert info["approval"] == "recommended"
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "SKILL.md").write_text("---\nname: notes\n---\nNotes.\n")
    assert skill_service._parse_skill_info(None, "notes", plain / "SKILL.md")["approval"] is None


def test_the_api_model_exposes_it():
    from db_models import SkillInfo
    assert SkillInfo(name="deploy", path="p", approval="recommended").approval == "recommended"
    assert SkillInfo(name="notes", path="p").approval is None
