"""trinity-enterprise#500 (B2) — staff identities must not reach outside audiences.

``compose_system_prompt`` is the UNIFIED prompt path: an anonymous public-link
chat, an x402 paid turn and a Workspace client turn all go through it, exactly
as an authenticated staff chat does. The assignment record names a real person,
so it would be the FIRST third-party identity in the auto-filled context block —
`collaborators` are agent names and `source_user_email` is the caller's own
address, so this is a new disclosure class rather than an inherited one. The
visitor asks "who is your primary human?" and the model answers.

Two mechanisms keep that shut, and both are pinned here:

  * the seam forwards ``triggered_by`` verbatim, so a provider can suppress on
    the audience — the OSS side carries the label, the provider decides;
  * the rendered identity field is a DISPLAY NAME. The answer contract has no
    email-shaped key at all, so an address cannot ride into the block even by a
    provider's mistake.

And one fact the suppression depends on: a Workspace/portal turn is labelled
``public``, not some third label. That is asserted over the source rather than
assumed, because a future portal turn introduced under its own label would slip
past an allow-list written today and disclose silently.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services import assignment_provider as ap
from services import platform_prompt_service as pps
from services.platform_prompt_service import ExecutionContext, compose_system_prompt

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# The trigger labels the OUTSIDE-FACING ROUTERS emit. `public` covers BOTH the
# anonymous public link and every Workspace/portal turn — see
# `test_outside_facing_surfaces_use_only_suppressed_trigger_labels`, which pins
# that over the sources.
OUTSIDE_AUDIENCES = ("public", "paid")

# Labels a provider must ALSO suppress, and the reason each one is easy to get
# wrong: none of them is emitted by an outside-facing router, so the source scan
# above cannot see them. The reader is outside the organisation anyway.
TRANSITIVE_AUDIENCES = {
    # An MCP turn is `mcp` whether the key is a user-scoped OPERATOR key or a
    # CONNECTOR key — an end-user consumption credential bound to one agent
    # (ent#46). The seam receives the LABEL, not the principal, so it cannot
    # tell them apart.
    "mcp": "a connector key is an external end-user credential",
    # Agent-to-agent is transitive: a public-facing agent delegating to a
    # companion puts that companion's primary human one model-hop from a
    # visitor.
    "agent": "transitive — the calling agent may be serving an outside caller",
    # ent#363 exists precisely because a room may contain a person outside the
    # fleet, and its whole job is to tell the agent so.
    "room": "a room may contain someone outside the fleet",
}


@pytest.fixture(autouse=True)
def _clean_provider():
    ap.clear_provider()
    yield
    ap.clear_provider()


@pytest.fixture
def quiet_db():
    fake = MagicMock()
    fake.get_setting_value = MagicMock(return_value=None)
    fake.get_permitted_agents = MagicMock(return_value=[])
    with patch.object(pps, "db", fake):
        yield fake


class _SuppressingProvider:
    """Mirrors the audience rule an entitled provider is required to apply: an
    ALLOW-list, so a trigger label invented tomorrow is suppressed by default
    rather than disclosed by default."""

    ALLOWED = frozenset(
        {"chat", "user", "manual", "self_task", "schedule", "retry", "webhook",
         "event", "reminder", "loop", "fan_out", "system", "validation"}
    )

    def __init__(self):
        self.seen: list[str | None] = []

    def assignment_for(self, agent_name, triggered_by):
        self.seen.append(triggered_by)
        if triggered_by not in self.ALLOWED:
            return None
        return {"primary_user_display": "A. Smith", "role_id": "head-of-ops"}


# ---------------------------------------------------------------------------
# The seam carries the audience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("triggered_by", OUTSIDE_AUDIENCES)
def test_an_outside_turn_renders_no_assignment_lines(triggered_by, quiet_db):
    ap.register_provider(_SuppressingProvider())
    out = compose_system_prompt(
        ExecutionContext(agent_name="ops-companion", triggered_by=triggered_by)
    )
    assert "## Execution Context" in out  # the block itself still renders
    assert "Primary human" not in out
    assert "Stakeholders" not in out
    assert "A. Smith" not in out


@pytest.mark.parametrize("triggered_by", ["chat", "user", "manual", "schedule"])
def test_an_inside_turn_still_renders_them(triggered_by, quiet_db):
    """The suppression must be audience-scoped, not a blanket kill — otherwise
    the feature delivers nothing to the one audience it exists for."""
    ap.register_provider(_SuppressingProvider())
    out = compose_system_prompt(
        ExecutionContext(agent_name="ops-companion", triggered_by=triggered_by)
    )
    assert "- **Primary human**: A. Smith (role: head-of-ops)" in out


@pytest.mark.parametrize("triggered_by", sorted(TRANSITIVE_AUDIENCES))
def test_a_transitive_or_ambiguous_audience_renders_nothing(triggered_by, quiet_db):
    """The three the source scan cannot catch.

    None of these is emitted by an outside-facing router, so
    `test_outside_facing_surfaces_use_only_suppressed_trigger_labels` says
    nothing about them — yet each has a reader who may be outside the
    organisation. Pinned here so the public contract states the requirement even
    though the rule itself is enforced by the provider.
    """
    ap.register_provider(_SuppressingProvider())
    out = compose_system_prompt(
        ExecutionContext(agent_name="ops", triggered_by=triggered_by)
    )
    assert "Primary human" not in out, TRANSITIVE_AUDIENCES[triggered_by]
    assert "A. Smith" not in out


def test_an_unrecognised_trigger_label_is_suppressed_by_default(quiet_db):
    """Fail-closed: a surface added later must not disclose until someone
    deliberately admits it."""
    ap.register_provider(_SuppressingProvider())
    out = compose_system_prompt(
        ExecutionContext(agent_name="ops", triggered_by="some_future_surface")
    )
    assert "Primary human" not in out


def test_the_seam_forwards_the_trigger_label_verbatim(quiet_db):
    provider = _SuppressingProvider()
    ap.register_provider(provider)
    for label in ("chat", "public", "paid", None):  # verbatim, including None
        compose_system_prompt(
            ExecutionContext(agent_name="ops", triggered_by=label)
        )
    assert provider.seen == ["chat", "public", "paid", None]


def test_a_missing_trigger_label_is_suppressed(quiet_db):
    """`None` is 'audience unknown', which is not 'audience trusted'."""
    ap.register_provider(_SuppressingProvider())
    out = compose_system_prompt(ExecutionContext(agent_name="ops"))
    assert "Primary human" not in out


# ---------------------------------------------------------------------------
# The rendered field cannot be an address
# ---------------------------------------------------------------------------


def test_the_answer_contract_has_no_email_key():
    """Structural, not stylistic: there is no key an address could arrive on,
    so no provider revision can add one without changing this contract."""
    assert "primary_user_display" in ap._ANSWER_KEYS
    for key in ap._ANSWER_KEYS:
        assert "email" not in key
        assert "address" not in key


def test_execution_context_carries_no_assignment_email_field():
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ExecutionContext)}
    assert {"primary_user_display", "role_id", "stakeholders", "proactive_consent"} <= fields
    # `source_user_email` is the CALLER's own address and predates this work;
    # nothing named for the assignment may be an address.
    assert "primary_user_email" not in fields
    assert "stakeholder_emails" not in fields


# ---------------------------------------------------------------------------
# The fact the suppression rests on
# ---------------------------------------------------------------------------


def test_outside_facing_surfaces_use_only_suppressed_trigger_labels():
    """A Workspace/portal turn is labelled `public`, so `{public, paid}` really
    does cover every outside audience — there is no third label to remember.

    If a new outside-facing turn ships under its own label, this fails and
    forces the audience decision to be made deliberately, rather than letting
    the new surface inherit disclosure by default.
    """
    literal = re.compile(r'triggered_by\s*=\s*"([a-z_]+)"')

    def _no_outside_label_beyond_the_suppressed_set(rel, found):
        unexpected = found - set(OUTSIDE_AUDIENCES)
        assert not unexpected, (
            f"{rel} labels a turn {sorted(unexpected)}, which is outside the "
            f"suppressed set {sorted(OUTSIDE_AUDIENCES)}. An outside-facing turn "
            "under a new label would disclose staff identities to that audience."
        )

    # The files that OWN an outside-facing label. Each must carry one, so a
    # dispatch moving house trips the "did the file move?" branch rather than
    # passing vacuously.
    #
    # #1028: `routers/public.py` used to be in this set. That router was made
    # thin (Invariant #1) and its public-link / x402 dispatch moved to
    # `services/public_chat_service.py`; the router now only COMPARES
    # `triggered_by`, so a scan for an assignment finds nothing there. The
    # audience decision this test exists to force is made in the service now.
    for rel in (
        "services/public_chat_service.py",
        "routers/paid.py",
        "client_portal/service.py",
    ):
        found = set(literal.findall((_BACKEND / rel).read_text()))
        assert found, f"no triggered_by literal found in {rel} — did the file move?"
        _no_outside_label_beyond_the_suppressed_set(rel, found)

    # ...and the files that no longer own one but still SIT on an outside-facing
    # path. They may legitimately carry zero literals, but if one reappears it
    # is an outside-facing label and must still be in the suppressed set —
    # otherwise the #1028 split would have quietly narrowed this guard, which is
    # the one thing a refactor must never do to a disclosure check.
    for rel in ("routers/public.py",):
        _no_outside_label_beyond_the_suppressed_set(
            rel, set(literal.findall((_BACKEND / rel).read_text()))
        )
