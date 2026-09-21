"""ent#454 — the membership primitive a DESTRUCTIVE system verb reads.

`system_member_names` (#2373) is THE ONE membership predicate, and the teardown
verb is the consumer its own docstring predicted: *"it is also the prerequisite
for the system teardown verb, where the same collision would delete."* This file
pins the structured entry point that verb reads — `system_membership` — against
the class of bug that produced it.

WHY PROPERTIES AND NOT ONLY CASES
---------------------------------
#2373's fallback rule was wrong three times, and each wrong rule PASSED the
case-by-case test written for its predecessor (learnings 2026-08-31):

  1. `"-" in short_name` dropped all eleven `vc-due-diligence-dd-*` agents of
     the bundled flagship manifest — 0 of 11, so a healthy fleet read as
     `404 System not found`;
  2. narrowing on roster evidence dropped `acme-api-worker` because sibling
     member `acme-api` is a name-prefix of it, and returned `[]` for the whole
     system whenever any agent was named exactly `acme`.

Two case tests, each satisfied by a different member-losing rule. So the
fallback is pinned as a PROPERTY — `members ⊇ raw prefix match` whenever the
tags are unreadable — which any future narrowing fails by construction,
whatever shape it narrows on. The named incidents stay as `@example`s and as
discrete cases: they are the fixture, not the ceiling.

The ranking that property encodes is #2373's, and it is worth restating because
teardown is where it gets QUESTIONED rather than inherited: on a fleet-wide
verb, under-capture is worse than over-capture — over-capture acts on one extra
target and logs it, under-capture acts on a SUBSET and reports success. For a
verb that DELETES, over-capture stops being cheap, which is exactly why
`tags_readable` is exposed here instead of the predicate being forked: the
destructive consumer refuses on an unverified read rather than re-deciding
membership. That refusal is the consumer's test, not this file's.

CI DETERMINISM
--------------
The unit gate is a base-vs-head failing-ID diff, so a property that fails on a
rare draw is a head-only red unrelated to the change. The `ci` profile is
derandomized with no example database (the `test_1771b_timestamp_helpers_
properties.py` precedent); `HYPOTHESIS_PROFILE=explore` restores randomized
search. pytest-randomly's three CI seeds randomize test ORDER, not Hypothesis
input generation.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "explore",
    max_examples=5000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "ci"))


# ---------------------------------------------------------------------------
# Strategies — the collision shapes the incidents were made of, not free text.
# ---------------------------------------------------------------------------
# Bases chosen so that one is a strict name-prefix of another (`acme` ⊂
# `acme-extra`, `acme-api` ⊂ `acme-api-worker`) — the whole #2373 class lives in
# that relationship, and a random-string generator would essentially never
# produce it.
_BASES = ["acme", "acme-extra", "acme-api", "vc-due-diligence", "other"]
_SUFFIXES = ["", "-web", "-worker", "-api", "-api-worker", "-dd-lead", "-dd-analyst"]

AGENT_NAME = st.builds(
    lambda b, s: f"{b}{s}", st.sampled_from(_BASES), st.sampled_from(_SUFFIXES)
)
ROSTER = st.lists(AGENT_NAME, min_size=0, max_size=8, unique=True)
SYSTEM_NAME = st.sampled_from(_BASES)
# A tag map is drawn per name so partial tagging — the state `PUT /tags` (a
# full-set replacement), a post-deploy addition, and a mid-loop `configure_tags`
# raise all produce — is in the domain rather than assumed away.
TAG_MAP = st.dictionaries(
    AGENT_NAME, st.lists(st.sampled_from(_BASES), max_size=3, unique=True), max_size=8
)


def _svc():
    from services import system_service

    return system_service


class _TagRead:
    """Stand-in for `db.get_tags_for_agents`, readable or exploding.

    `raising=False` is deliberately NOT used at the patch site: the accessor
    exists, and a test that would still pass if it were renamed is not pinning
    anything.
    """

    def __init__(self, tags, readable=True):
        self.tags = tags
        self.readable = readable
        self.calls = 0

    def __call__(self, names):
        self.calls += 1
        if not self.readable:
            raise RuntimeError("tags table unavailable")
        return {n: self.tags.get(n, []) for n in names}


@pytest.fixture
def tag_read(monkeypatch):
    """Install a tag read and hand the test the knob."""

    def _install(tags, readable=True):
        stub = _TagRead(tags, readable)
        monkeypatch.setattr(_svc().db, "get_tags_for_agents", stub)
        return stub

    return _install


# ===========================================================================
# P1 — the wrapper is a wrapper
# ===========================================================================

@given(system=SYSTEM_NAME, roster=ROSTER, tags=TAG_MAP)
@example(system="acme", roster=["acme-web"], tags={})
@pytest.mark.parametrize("readable", [True, False])
def test_the_name_wrapper_returns_exactly_the_structured_members(
    system, roster, tags, readable
):
    """`system_member_names` must stay a projection of `system_membership`.

    This is the property that makes the split safe: three copies of a wrong rule
    is what made #2373 a bug in three places, and `get_system` /
    `restart_system` / `export_manifest` all still call the wrapper. If the two
    can ever disagree, the refactor has reintroduced the original defect in a
    new shape — a second membership rule reachable from a different entry point.
    """
    svc = _svc()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc.db, "get_tags_for_agents", _TagRead(tags, readable))
        assert svc.system_member_names(system, roster) == svc.system_membership(
            system, roster
        ).members


def test_the_wrapper_body_is_one_line_over_the_structured_entry_point():
    """Source-anchored, because P1 cannot see a COPIED body that happens to
    agree today. The wrapper must delegate, not re-derive."""
    import inspect

    src = inspect.getsource(_svc().system_member_names)
    assert "system_membership(" in src
    assert 'startswith(f"{system_name}-")' not in src
    assert "get_tags_for_agents" not in src


# ===========================================================================
# P2 — the fallback never loses a prefix match (the #2373 invariant)
# ===========================================================================

@given(system=SYSTEM_NAME, roster=ROSTER, tags=TAG_MAP)
@example(
    system="vc-due-diligence",
    roster=[f"vc-due-diligence-dd-{n}" for n in ("lead", "analyst")],
    tags={},
)
@example(system="acme", roster=["acme", "acme-api", "acme-api-worker"], tags={})
def test_an_unreadable_tag_read_never_returns_fewer_than_the_raw_prefix(
    system, roster, tags
):
    """The superset property, through the structured entry point.

    #2373 pins this for `system_member_names` over a fixed 5-roster loop. Here
    it is generated, and asserted on the entry point the destructive verb reads
    — so a future narrowing cannot slip in through the new door while the old
    door's case test stays green.
    """
    svc = _svc()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc.db, "get_tags_for_agents", _TagRead(tags, readable=False))
        result = svc.system_membership(system, roster)

    # Only claimed where the read actually happens. An empty roster (or an
    # empty system name) is answered from the inputs and never reaches the tag
    # read, so it reports `tags_readable: True` BY DESIGN — see
    # `test_the_empty_cases_report_a_readable_read_rather_than_a_failure` for
    # why that matters (a destructive caller refuses on False, and refusing a
    # teardown of a system with no members is a 503 where 404 is the truth).
    # Hypothesis found this as `roster=[]`; the precondition was missing from
    # the property, not from the code.
    if system and roster:
        assert result.tags_readable is False
    raw_prefix = {n for n in roster if n.startswith(f"{system}-")}
    assert raw_prefix <= set(result.members), (
        f"dropped {sorted(raw_prefix - set(result.members))} — a narrowed "
        "fallback loses members of a healthy system (the #2373 class)"
    )


@given(system=SYSTEM_NAME, roster=ROSTER, tags=TAG_MAP)
def test_a_tagged_member_is_always_a_member_when_the_tags_are_readable(
    system, roster, tags
):
    """The other half: a tag is a RECORD of membership, so it must never be
    narrowed away. `if tagged: return tagged` made one tag hide every other
    member; the union must not cost the property the tag half exists for."""
    svc = _svc()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc.db, "get_tags_for_agents", _TagRead(tags, readable=True))
        result = svc.system_membership(system, roster)

    tagged = {n for n in roster if system in tags.get(n, [])}
    assert tagged <= set(result.members), (
        f"dropped tagged member(s) {sorted(tagged - set(result.members))}"
    )


# ===========================================================================
# P3 — evidence cannot disagree with membership
# ===========================================================================

@given(system=SYSTEM_NAME, roster=ROSTER, tags=TAG_MAP)
@pytest.mark.parametrize("readable", [True, False])
def test_evidence_is_keyed_by_exactly_the_members_and_is_true_of_each(
    system, roster, tags, readable
):
    """Evidence is what a destructive caller renders as "matched by name only",
    and an operator unchecks a member on the strength of it. So it has to be
    derived from the same two sets that decided membership, never a second
    opinion computed beside them."""
    svc = _svc()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc.db, "get_tags_for_agents", _TagRead(tags, readable))
        result = svc.system_membership(system, roster)

    assert set(result.evidence) == set(result.members), (
        "every member needs a reason, and a non-member must not have one"
    )
    for name, why in result.evidence.items():
        assert why in {"tag", "prefix", "both"}
        has_prefix = name.startswith(f"{system}-")
        has_tag = readable and system in tags.get(name, [])
        if why == "prefix":
            assert has_prefix and not has_tag
        elif why == "tag":
            assert has_tag and not has_prefix
        else:
            assert has_tag and has_prefix


def test_a_tag_only_member_reads_as_tag_not_prefix(tag_read):
    """The renamed-member case: membership admits a member whose name carries no
    prefix at all, and that is precisely the member a name-based rule cannot
    see. It must not be reported as prefix-matched."""
    svc = _svc()
    tag_read({"helper": ["acme"], "acme-web": ["acme"]})
    result = svc.system_membership("acme", ["acme-web", "helper", "other"])

    assert sorted(result.members) == ["acme-web", "helper"]
    assert result.evidence == {"acme-web": "both", "helper": "tag"}


def test_an_untagged_prefix_member_reads_as_prefix(tag_read):
    """The over-capture signal. `acme-extra-worker` under system `acme` with no
    tag to say otherwise is a member by INFERENCE, and the caller has to be able
    to say so."""
    svc = _svc()
    tag_read({})
    result = svc.system_membership("acme", ["acme-web", "acme-extra-worker"])

    assert result.evidence == {"acme-web": "prefix", "acme-extra-worker": "prefix"}


# ===========================================================================
# The named incidents, discretely — the fixture the properties generalize
# ===========================================================================

def test_the_eleven_dd_members_of_the_flagship_manifest_are_all_members(tag_read):
    """Incident 1: `"-" in short_name` returned 0 of 11 for the bundled
    `vc-due-diligence` manifest, so a correct fleet 404'd and exported empty."""
    svc = _svc()
    roster = [f"vc-due-diligence-dd-{n}" for n in
              ("lead", "analyst", "legal", "finance", "market", "tech",
               "team", "refs", "memo", "risk", "scribe")]
    for readable in (True, False):
        tag_read({}, readable=readable)
        result = svc.system_membership("vc-due-diligence", roster)
        assert result.members == roster, f"readable={readable}"


def test_an_agent_named_exactly_the_system_does_not_empty_the_system(tag_read):
    """Incident 2b: an agent named literally `acme` is a name-prefix of EVERY
    member, and the roster-evidence rule resolved the whole system to `[]`."""
    svc = _svc()
    roster = ["acme", "acme-web", "acme-worker"]
    for readable in (True, False):
        tag_read({}, readable=readable)
        result = svc.system_membership("acme", roster)
        assert sorted(result.members) == ["acme-web", "acme-worker"], (
            f"readable={readable}"
        )
        # `acme` itself is not a member of `acme`: there is no `acme-` prefix and
        # no tag. It is also the agent a prefix rule would sweep INTO a teardown.
        assert "acme" not in result.members


def test_a_sibling_prefixed_member_survives(tag_read):
    """Incident 2a: `acme-api-worker` was dropped because sibling member
    `acme-api` prefixes it — an ordinary member whose manifest key is
    `api-worker` sitting beside key `api`."""
    svc = _svc()
    tag_read({})
    result = svc.system_membership("acme", ["acme-api", "acme-api-worker", "acme-web"])
    assert sorted(result.members) == ["acme-api", "acme-api-worker", "acme-web"]


def test_the_acme_vs_acme_extra_collision_is_still_resolved_by_tags(tag_read):
    """The AC's floor case. With tags readable, an operation on `acme` must not
    reach `acme-extra`'s tagged members — for teardown that is the difference
    between removing a system and removing someone else's."""
    svc = _svc()
    tag_read({
        "acme-web": ["acme"],
        "acme-extra-web": ["acme-extra"],
        "acme-extra-worker": ["acme-extra"],
    })
    result = svc.system_membership(
        "acme", ["acme-web", "acme-extra-web", "acme-extra-worker"]
    )
    assert result.members == ["acme-web"]
    assert result.tags_readable is True


def test_the_collision_residual_on_the_unreadable_path_is_visible_not_hidden(tag_read):
    """And when the tags cannot be read, the same roster over-captures — the
    documented pre-#2373 behaviour. What ent#454 adds is that the caller can SEE
    it: `tags_readable: False` plus `prefix` evidence on every member. A
    destructive verb refuses on this; `restart` proceeds. Same data, different
    ranking, one predicate."""
    svc = _svc()
    tag_read({}, readable=False)
    result = svc.system_membership(
        "acme", ["acme-web", "acme-extra-web", "acme-extra-worker"]
    )
    assert result.tags_readable is False
    assert set(result.members) == {"acme-web", "acme-extra-web", "acme-extra-worker"}
    assert set(result.evidence.values()) == {"prefix"}


def test_the_empty_cases_report_a_readable_read_rather_than_a_failure(tag_read):
    """`tags_readable` must mean "the read succeeded", not "there were tags".
    A destructive caller refuses when it is False, so a no-op input that
    reported False would refuse a teardown of a system that simply has no
    members — a 503 where a 404 is the truth."""
    svc = _svc()
    stub = tag_read({})
    for system, roster in (("acme", []), ("", ["acme-web"])):
        result = svc.system_membership(system, roster)
        assert result.members == []
        assert result.evidence == {}
        assert result.tags_readable is True
    # Short-circuited before the read: no point paying for a tag query to
    # answer a question the inputs already answered.
    assert stub.calls == 0


def test_one_membership_resolution_reads_the_tags_once(tag_read):
    """The tag read is the expensive half and the one that can fail. Teardown
    resolves membership ONCE and shares it between preview and execute (#2373
    pass-4 I1); this pins that the primitive itself does not multiply it."""
    svc = _svc()
    stub = tag_read({"acme-web": ["acme"]})
    svc.system_membership("acme", ["acme-web", "other"])
    assert stub.calls == 1


def test_the_auto_view_description_is_one_constant_shared_with_the_writer(monkeypatch):
    """Nothing persists a system↔view link, so this description IS the
    identifier for the view a deploy auto-created. Asserted THROUGH the writer
    rather than by restating the literal — a test that hardcodes the string is a
    second vocabulary, which is the drift it is meant to prevent."""
    svc = _svc()
    captured = {}

    def _capture(owner_id, data):
        captured["data"] = data
        return None

    monkeypatch.setattr(svc.db, "create_system_view", _capture)
    svc.create_system_view(
        "acme",
        svc.SystemViewConfig(name="Acme", icon=None, color=None, shared=False),
        None,
        "u1",
    )
    assert captured["data"].description == svc.SYSTEM_VIEW_AUTO_DESCRIPTION.format(
        system_name="acme"
    )
    # And the filter carries the system tag LOWER-CASED, which any finder must
    # match on rather than comparing the raw system name.
    assert "acme" in captured["data"].filter_tags
