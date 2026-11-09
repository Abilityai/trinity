"""The owner stands in for an unassigned primary — one shared rule for the
addressing surfaces, and never for the skill gate
(abilityai/trinity-enterprise#1134, ruling 1 of 2026-11-07).

`role_addressing.resolve` keeps the ent#606 rule: a provider that answers
"nobody fills primary" resolves to the operators, because the same function
decides who may APPROVE a gated skill (ent#904: an address is not a grant —
with the owner standing in there, an owner requesting a gated skill with no
primary seated would self-approve, `skill_gate_service.enforce`).

`role_addressing.resolve_addressee` is the one helper the ADDRESSING surfaces
share — asks (`ask_service._address`), messages and voice
(`resolve_single_recipient`), and the enterprise proposals' `_primaries` — and
it stands the agent's owner in when the provider answered nobody. Before this,
three local copies of that fallback sat around one resolver that said
"operators", and the ask sink had none: an ask to `primary` on a fresh
enterprise install went to the Operating Room and nobody in the chat saw it.

Pinned here:

* `resolve_addressee`: provider people → them; provider nobody → the owner,
  `stood_in` True, `resolved` True; no provider → the owner (ent#606
  unchanged); owner with no email → the operators, `resolved` False;
  the stand-in is for `primary` only; an unknown role refuses as before;
* `resolve` is unchanged for every arm (the gate's rule);
* the ask sink: a provider answering nobody now addresses the owner — the
  row's `resolved_to` and `addressed_to_email`, the receipt's `resolved` — and
  an owner without an email still falls to the operators;
* the message surface gives the same answer through the helper.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent606_role_addressing import AGENT, ALICE, BOB, OWNER, _msg, _provide, ra  # noqa: E402,F401

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def test_a_provider_saying_nobody_is_primary_stands_the_owner_in(ra):
    _provide({"primary": []})
    r = ra.resolve_addressee(AGENT, "primary")
    assert (r.people, r.single, r.resolved, r.stood_in) == ([OWNER], OWNER, True, True)


def test_the_providers_answer_still_wins(ra):
    _provide({"primary": [ALICE]})
    r = ra.resolve_addressee(AGENT, "primary")
    assert (r.people, r.single, r.resolved, r.stood_in) == ([ALICE], ALICE, True, False)
    _provide({"primary": [ALICE, BOB]})
    r = ra.resolve_addressee(AGENT, "primary")
    assert (r.people, r.single, r.stood_in) == ([ALICE, BOB], None, False)


def test_with_no_provider_the_owner_is_the_core_default_not_a_stand_in(ra):
    r = ra.resolve_addressee(AGENT, "primary")
    assert (r.people, r.single, r.resolved, r.stood_in) == ([OWNER], OWNER, True, False)


def test_an_owner_with_no_email_still_falls_to_the_operators(ra, monkeypatch):
    _provide({"primary": []})
    monkeypatch.setattr(ra, "owner_email", lambda agent: None)
    r = ra.resolve_addressee(AGENT, "primary")
    assert (r.people, r.single, r.resolved, r.stood_in) == ([], None, False, False)


def test_a_callers_owner_lookup_is_the_one_read(ra):
    _provide({"primary": []})
    r = ra.resolve_addressee(AGENT, "primary", owner_lookup=lambda agent: "lookup@example.com")
    assert (r.single, r.stood_in) == ("lookup@example.com", True)


@pytest.mark.parametrize("role", ["approver", "viewer"])
def test_the_stand_in_is_for_primary_only(ra, role):
    _provide({role: []})
    with pytest.raises(ra.RoleRefused) as exc:
        ra.resolve_addressee(AGENT, role)
    assert (exc.value.code, exc.value.role) == ("role_unassigned", role)


def test_operator_and_an_unknown_role_are_unchanged(ra):
    r = ra.resolve_addressee(AGENT, "operator")
    assert (r.people, r.resolved, r.stood_in) == ([], True, False)
    with pytest.raises(ra.RoleRefused) as exc:
        ra.resolve_addressee(AGENT, "boss")
    assert exc.value.code == "invalid_to"


# ---------------------------------------------------------------------------
# The grant rule is untouched
# ---------------------------------------------------------------------------

def test_resolve_still_says_nobody_fills_primary(ra):
    """The gate's rule (ent#904): `resolve` never stands the owner in."""
    _provide({"primary": []})
    r = ra.resolve(AGENT, "primary")
    assert (r.people, r.single, r.resolved) == ([], None, False)
    assert not getattr(r, "stood_in", False)


def test_a_gated_skill_approved_by_primary_still_refuses(ra):
    from services import skill_gate_service

    _provide({"primary": []})
    with pytest.raises(skill_gate_service.SkillGateRefused) as exc:
        skill_gate_service._approvers(AGENT, "primary")
    assert exc.value.code == "role_unassigned"


# ---------------------------------------------------------------------------
# The surfaces share it
# ---------------------------------------------------------------------------

def test_the_ask_sink_stands_the_owner_in(ra):
    from services import ask_service

    _provide({"primary": []})
    assert ask_service._address(AGENT, "primary") == ([OWNER], OWNER, True)


def test_the_ask_sinks_own_owner_lookup_is_the_one_read(ra, monkeypatch):
    """The sink keeps `_owner_email` as its patch target (every ask test
    stubs it); the helper must read THAT, not the module function."""
    from services import ask_service

    _provide({"primary": []})
    monkeypatch.setattr(ask_service, "_owner_email", lambda agent: "sink@example.com")
    assert ask_service._address(AGENT, "primary") == (["sink@example.com"], "sink@example.com", True)
    monkeypatch.setattr(ask_service, "_owner_email", lambda agent: None)
    assert ask_service._address(AGENT, "primary") == ([], None, False)


def test_the_message_surface_gives_the_same_answer(ra):
    from routers import messages

    _provide({"primary": []})
    assert messages._recipient(AGENT, _msg(to="primary")) == OWNER
    _provide({"primary": [ALICE]})
    assert messages._recipient(AGENT, _msg(to="primary")) == ALICE


def test_the_message_surface_has_no_local_owner_branch_left(ra, monkeypatch):
    """One rule: the send surface reads the helper, so a patch on the helper
    governs it and nothing in `resolve_single_recipient` re-reads the owner."""
    from routers import messages

    _provide({"primary": []})
    calls = []
    real = ra.resolve_addressee

    def spy(agent, role, **kw):
        calls.append(role)
        return real(agent, role, **kw)

    monkeypatch.setattr(ra, "resolve_addressee", spy)
    monkeypatch.setattr(ra, "owner_email", lambda agent: (calls.append("owner_email") or OWNER))
    assert messages._recipient(AGENT, _msg(to="primary")) == OWNER
    assert calls == ["primary", "owner_email"]
