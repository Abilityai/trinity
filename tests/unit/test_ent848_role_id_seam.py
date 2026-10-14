"""Gate approvers by canon role — the OSS seam (trinity-enterprise#848, C1).

A skill gate's `approver` may name a canon role, stored as `role:<id>` beside
the kinds `primary` | `approver`. This file pins the three OSS pieces the
enterprise provider plugs into:

* `role_addressing.parse_role_id` / `role_label` — the one parser (prefix +
  `ROLE_ID_RE`, matched with `fullmatch`, lower-cased for storage) and the
  humanised label a role id shows as until canon titles exist (D1);
* `assignment_provider.resolve_role_id_people` — the people who fill a role id
  on an agent, read through the optional `people_for_role_id` seam method:
  `None` for every degraded case, `[]` when the provider says nobody;
* `assignment_provider.resolve_approver_roles` — the role ids known on an
  agent's roster through the optional `approver_roles_for` seam method:
  shape-checked, regex-filtered, de-duplicated, capped;
* `role_addressing.resolve_role_id` — the gate's resolution rule for a role
  id: a person or a loud `role_unassigned`. `ROLES` / `resolve` are untouched.
Related flow: docs/memory/feature-flows/skill-gate.md
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import assignment_provider, role_addressing  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "fin-848"


class FakeProvider:
    """An enterprise assignments provider answering the two ent#848 methods
    beside `people_for`. `role_people` maps a role id to its answer; `roles`
    is the `approver_roles_for` answer, passed through verbatim."""

    def __init__(self, role_people=None, roles=None, people=None):
        self.role_people = role_people or {}
        self.roles = roles
        self.people = people or {}
        self.calls = []

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        return {"emails": list(self.people.get(role, []))}

    def people_for_role_id(self, agent_name, role_id):
        self.calls.append(("people_for_role_id", agent_name, role_id))
        answer = self.role_people.get(role_id, "missing")
        return None if answer == "missing" else answer

    def approver_roles_for(self, agent_name):
        self.calls.append(("approver_roles_for", agent_name))
        return self.roles


@pytest.fixture
def provider():
    assignment_provider.clear_provider()
    yield lambda **kw: assignment_provider.register_provider(FakeProvider(**kw))
    assignment_provider.clear_provider()


# ---------------------------------------------------------------------------
# parse_role_id — the one parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("role:finance", "finance"),
    ("role:Finance", "finance"),                  # lower-cased for storage (T1)
    ("role:head-of-sales", "head-of-sales"),
    ("role:cfo.eu_2", "cfo.eu_2"),
    ("role:a", "a"),
    ("role:" + "x" * 64, "x" * 64),               # 64 chars after the prefix: the cap
])
def test_a_well_formed_role_id_parses_lower_cased(value, expected):
    assert role_addressing.parse_role_id(value) == expected


@pytest.mark.parametrize("value", [
    "primary", "approver", "viewer", "operator",   # kinds are not role ids
    "finance",                                     # a bare id is not one either
    "role:", "role: finance", "role:-finance", "role:.x", "role:fin ance",
    "role:finance\n",                              # E1: `re.match` would accept this
    "role:fin/ance", "role:fin:ance", "Role:finance", "ROLE:finance",
    "role:" + "x" * 65,                            # one over the cap
    None, 7, ["role:finance"],
])
def test_anything_else_is_not_a_role_id(value):
    assert role_addressing.parse_role_id(value) is None


def test_the_regex_is_the_enterprise_pattern_without_its_anchors():
    """The provider validates `role_id` at write with `ROLE_ID_PATTERN`; the
    OSS parser must accept exactly that set (anchored by `fullmatch`), or a
    role the enterprise stores could never be named on a gate."""
    ent = _BACKEND / "enterprise" / "backend" / "assignments" / "models.py"
    expected = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    if ent.exists():
        m = re.search(r'ROLE_ID_PATTERN = r"\^(.*)\$"', ent.read_text())
        assert m and m.group(1) == expected
    assert role_addressing.ROLE_ID_RE.pattern == expected
    assert role_addressing.ROLE_ID_RE is assignment_provider.ROLE_ID_RE


def test_hypothesis_round_trip_matches_the_enterprise_pattern():
    hyp = pytest.importorskip("hypothesis")
    from hypothesis import strategies as st

    ent_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    @hyp.given(st.text(min_size=0, max_size=70))
    @hyp.settings(max_examples=300, deadline=None)
    def check(raw):
        parsed = role_addressing.parse_role_id("role:" + raw)
        if ent_pattern.fullmatch(raw) and "\n" not in raw:
            assert parsed == raw.lower()
        else:
            assert parsed is None

    check()


# ---------------------------------------------------------------------------
# role_label — what a role id shows as (D1: canon titles later)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, label", [
    ("role:head-of-sales", "Head of sales"),
    ("role:cfo", "Cfo"),
    ("role:Finance", "Finance"),
    ("role:ops.eu_west-2", "Ops eu west 2"),
    ("primary", "primary"),                         # kinds keep their own name
    ("approver", "approver"),
    ("role:", "role:"),                             # not a role id: shown as given
])
def test_role_label(value, label):
    assert role_addressing.role_label(value) == label


# ---------------------------------------------------------------------------
# resolve_role_id_people — the seam read
# ---------------------------------------------------------------------------


def test_no_provider_is_no_answer(provider):
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") is None


def test_a_provider_without_the_method_is_no_answer(provider):
    class Old:
        def assignment_for(self, agent_name, triggered_by):
            return None
    assignment_provider.register_provider(Old())
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") is None


def test_a_provider_answering_none_is_no_answer(provider):
    provider(role_people={"finance": None})
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") is None


def test_nobody_is_a_real_answer(provider):
    provider(role_people={"finance": {"emails": []}})
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") == []


def test_people_are_lower_cased_and_de_duplicated_in_order(provider):
    provider(role_people={"finance": {"emails": [" Dana@Example.com ", "x@example.com",
                                                 "dana@example.com", "not-an-email", 7, ""]}})
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") == [
        "dana@example.com", "x@example.com"]


def test_the_provider_is_asked_for_this_agent_and_role(provider):
    p = FakeProvider(role_people={"finance": {"emails": ["d@example.com"]}})
    assignment_provider.register_provider(p)
    assignment_provider.resolve_role_id_people(AGENT, "finance")
    assert p.calls == [("people_for_role_id", AGENT, "finance")]


@pytest.mark.parametrize("answer", ["dana@example.com", ["dana@example.com"], {"emails": "dana@example.com"},
                                    {"people": ["dana@example.com"]}, 42])
def test_a_malformed_answer_is_no_answer_with_a_warning(provider, answer, caplog):
    provider(role_people={"finance": answer})
    with caplog.at_level("WARNING"):
        assert assignment_provider.resolve_role_id_people(AGENT, "finance") is None
    assert "people_for_role_id" in caplog.text


def test_a_raising_provider_is_no_answer_with_a_warning(provider, caplog):
    class Boom(FakeProvider):
        def people_for_role_id(self, agent_name, role_id):
            raise RuntimeError("db down")
    assignment_provider.register_provider(Boom())
    with caplog.at_level("WARNING"):
        assert assignment_provider.resolve_role_id_people(AGENT, "finance") is None
    assert "people_for_role_id" in caplog.text


def test_the_kind_read_is_unchanged_by_the_new_one(provider):
    provider(people={"approver": ["A@example.com"]}, role_people={"finance": {"emails": ["f@example.com"]}})
    assert assignment_provider.resolve_role_people(AGENT, "approver") == ["a@example.com"]
    assert assignment_provider.resolve_role_id_people(AGENT, "finance") == ["f@example.com"]


# ---------------------------------------------------------------------------
# resolve_approver_roles — the role ids an agent's roster names
# ---------------------------------------------------------------------------


def test_no_provider_lists_no_roles(provider):
    assert assignment_provider.resolve_approver_roles(AGENT) is None


def test_a_provider_without_the_method_lists_no_roles(provider):
    class Old:
        def assignment_for(self, agent_name, triggered_by):
            return None
    assignment_provider.register_provider(Old())
    assert assignment_provider.resolve_approver_roles(AGENT) is None


def test_a_provider_answering_none_lists_no_roles(provider):
    provider(roles=None)
    assert assignment_provider.resolve_approver_roles(AGENT) is None


def test_an_empty_roster_is_a_real_answer(provider):
    provider(roles={"roles": []})
    assert assignment_provider.resolve_approver_roles(AGENT) == []


def test_roles_are_lower_cased_de_duplicated_and_carry_an_optional_title(provider):
    provider(roles={"roles": [{"id": "Finance", "title": "Head of Finance"}, {"id": "finance"},
                              {"id": "ops", "title": None}, {"id": "ops", "title": "Ops"}]})
    assert assignment_provider.resolve_approver_roles(AGENT) == [
        {"id": "finance", "title": "Head of Finance"}, {"id": "ops", "title": "Ops"}]   # a later row may add the title


def test_an_id_failing_the_regex_is_dropped_with_a_warning(provider, caplog):
    provider(roles={"roles": [{"id": "fin ance"}, {"id": "-x"}, {"id": "ok"}, {"id": "x" * 65}]})
    with caplog.at_level("WARNING"):
        assert assignment_provider.resolve_approver_roles(AGENT) == [{"id": "ok", "title": None}]
    assert "approver_roles_for" in caplog.text


@pytest.mark.parametrize("answer", ["finance", ["finance"], {"roles": "finance"}, {"roles": ["finance"]},
                                    {"roles": [{"title": "x"}]}, {"roles": [{"id": 7}]},
                                    {"roles": [{"id": "ok", "title": 7}]}, {"ids": ["finance"]}])
def test_a_malformed_roster_is_no_answer_with_a_warning(provider, answer, caplog):
    provider(roles=answer)
    with caplog.at_level("WARNING"):
        assert assignment_provider.resolve_approver_roles(AGENT) is None
    assert "approver_roles_for" in caplog.text


def test_a_raising_provider_lists_no_roles(provider, caplog):
    class Boom(FakeProvider):
        def approver_roles_for(self, agent_name):
            raise RuntimeError("db down")
    assignment_provider.register_provider(Boom())
    with caplog.at_level("WARNING"):
        assert assignment_provider.resolve_approver_roles(AGENT) is None


def test_the_roster_is_capped(provider):
    provider(roles={"roles": [{"id": f"r{i}"} for i in range(150)]})
    out = assignment_provider.resolve_approver_roles(AGENT)
    assert len(out) == assignment_provider.MAX_APPROVER_ROLES == 100
    assert out[0]["id"] == "r0" and out[-1]["id"] == "r99"


# ---------------------------------------------------------------------------
# resolve_role_id — the gate's rule: a person, or a loud refusal
# ---------------------------------------------------------------------------


def test_a_filled_role_resolves_to_its_people(provider):
    provider(role_people={"finance": {"emails": ["Dana@example.com", "x@example.com"]}})
    r = role_addressing.resolve_role_id(AGENT, "finance")
    assert (r.role, r.people, r.single, r.resolved) == (
        "role:finance", ["dana@example.com", "x@example.com"], None, True)


def test_one_person_is_the_single_addressee(provider):
    provider(role_people={"finance": {"emails": ["dana@example.com"]}})
    assert role_addressing.resolve_role_id(AGENT, "finance").single == "dana@example.com"


@pytest.mark.parametrize("setup", ["no_provider", "nobody", "no_answer"])
def test_an_unfilled_role_is_refused_by_name_never_defaulted(provider, setup):
    """Unlike `primary`, a role id has no owner fallback: no provider, no
    answer and "nobody" all refuse `role_unassigned` (fail closed)."""
    if setup == "nobody":
        provider(role_people={"finance": {"emails": []}})
    elif setup == "no_answer":
        provider(role_people={})
    with pytest.raises(role_addressing.RoleRefused) as info:
        role_addressing.resolve_role_id(AGENT, "finance")
    assert (info.value.code, info.value.role) == ("role_unassigned", "role:finance")
    assert "Finance" in info.value.message


def test_the_kind_rule_still_refuses_a_role_id_as_an_unknown_role(provider):
    """`ROLES` / `resolve` are untouched: an ask addressed `to: role:x` by an
    agent stays `invalid_to` (role ids on asks are out of scope)."""
    with pytest.raises(role_addressing.RoleRefused) as info:
        role_addressing.resolve(AGENT, "role:finance")
    assert info.value.code == "invalid_to"
