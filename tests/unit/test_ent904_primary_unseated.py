"""trinity-enterprise#904 — `to: primary` on an agent with no primary seat.

With the assignments module entitled, an agent with no rows answers "nobody
fills primary" (`people_for → []`), which overrides the owner fallback. That is
the default state of a fresh enterprise install, and it left `send_message`
with no sanctioned way to reach a person.

Ruled 2026-10-15: the owner stands in on the MESSAGE surface only. Pinned here,
beyond the stand-in itself (`test_ent606_role_addressing`):

* the resolver is unchanged — asks still fall back to the operators;
* a gated skill whose approver is `primary` still refuses — the stand-in must
  never make the owner an approver nobody assigned (an authorization);
* an owner with no email is refused with the remedy named, not the old advice
  to raise an alert (the Operating Room reaches no person);
* the MCP tool text no longer names an alert as the way to reach a person.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent606_role_addressing import AGENT, OWNER, _msg, _provide, ra  # noqa: F401


def test_the_resolver_still_says_nobody_fills_primary(ra):
    _provide({"primary": []})
    r = ra.resolve(AGENT, "primary")
    assert (r.people, r.single, r.resolved) == ([], None, False)


def test_a_gated_skill_approved_by_primary_still_refuses(ra):
    from services import skill_gate_service

    _provide({"primary": []})
    with pytest.raises(skill_gate_service.SkillGateRefused) as exc:
        skill_gate_service._approvers(AGENT, "primary")
    assert exc.value.code == "role_unassigned"
    assert skill_gate_service.approver_people(AGENT, "primary") == []


def test_an_owner_with_no_email_is_refused_with_the_remedy(ra, monkeypatch):
    from routers import messages

    _provide({"primary": []})
    monkeypatch.setattr(ra, "owner_email", lambda agent: None)
    with pytest.raises(HTTPException) as exc:
        messages._recipient(AGENT, _msg(to="primary"))
    detail = exc.value.detail
    assert exc.value.status_code == 422 and detail["code"] == "role_unassigned"
    assert "Access" in detail["message"]          # where a primary is assigned
    assert "alert" not in detail["message"]


def test_the_stand_in_is_only_for_primary(ra):
    from routers import messages

    _provide({"primary": [], "approver": []})
    with pytest.raises(HTTPException) as exc:
        messages._recipient(AGENT, _msg(to="approver"))
    assert exc.value.detail["code"] == "role_unassigned"


def test_a_seated_primary_still_wins_over_the_owner(ra):
    from routers import messages

    _provide({"primary": ["dana@example.com"]})
    assert messages._recipient(AGENT, _msg(to="primary")) == "dana@example.com"


def test_the_tool_text_no_longer_sends_a_person_to_an_alert():
    src = (Path(__file__).resolve().parents[2] / "src" / "mcp-server" / "src" / "tools"
           / "messages.ts").read_text()
    start = src.index('name: "send_message"')
    description = src[start:src.index("INTENT_KEY_TEACHING", start)]
    assert "raise an ask of type alert" not in description
    assert "owner" in description
