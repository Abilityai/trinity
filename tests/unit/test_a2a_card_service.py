"""
Unit tests for the A2A v1.0 Agent Card generator (Issue #737 Phase 1).

`a2a_card_service.generate_a2a_card` is pure-function — takes a
`template_data` dict + agent name + base URL and returns a card
dict. No I/O, no DB, no Docker. Tests cover:

- The "happy path" — full template.yaml shape with capabilities and
  use_cases, exercising the skills mapping
- Label-fallback shape — what the router passes when /info is
  unreachable
- Field edge cases — missing version, missing description,
  empty/whitespace capabilities, no base_url
- Capabilities defensive: non-string values, whitespace-only entries
  must not produce malformed skills entries
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


def _load_card_service():
    """Load a2a_card_service standalone — bypasses the agent_service
    package init (which would drag pydantic/fastapi into a pure-data
    test). Mirrors the loader pattern used by other backend-internal
    tests (#816, #834)."""
    path = _BACKEND / "services" / "a2a_card_service.py"
    spec = importlib.util.spec_from_file_location("a2a_card_service_t", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def card_service():
    return _load_card_service()


# -----------------------------------------------------------------------------
# Happy path — full template.yaml shape
# -----------------------------------------------------------------------------


def test_card_full_template_shape(card_service):
    """A template with capabilities + use_cases produces a card with
    populated skills[], correct top-level fields, and a working URL."""
    template = {
        "name": "dd-compliance",
        "display_name": "DD Compliance Agent",
        "description": "Regulatory landscape analysis and compliance assessment",
        "tagline": "Navigating the rules",
        "version": "1.2.0",
        "capabilities": [
            "regulatory-research",
            "compliance-assessment",
            "geographic-expansion",
        ],
        "use_cases": [
            "Assess industry regulation level",
            "Evaluate compliance requirements",
        ],
    }

    card = card_service.generate_a2a_card(
        agent_name="dd-compliance",
        template_data=template,
        base_url="https://trinity.example.com",
    )

    # Top-level fields
    assert card["protocolVersion"] == "1.0"
    assert card["name"] == "DD Compliance Agent"
    assert card["description"] == template["description"]
    assert card["version"] == "1.2.0"

    # URL points where A2A clients would call (placeholder until
    # the dedicated JSON-RPC endpoint ships in a follow-up)
    assert card["url"] == "https://trinity.example.com/api/agents/dd-compliance/chat"

    # Streaming flag — Trinity always supports SSE
    assert card["capabilities"]["streaming"] is True
    assert card["capabilities"]["pushNotifications"] is False
    assert card["capabilities"]["stateTransitionHistory"] is False

    # Provider
    assert card["provider"]["organization"] == "Trinity"
    assert card["provider"]["url"] == "https://trinity.example.com"

    # Skills — one per capability
    skills = card["skills"]
    assert len(skills) == 3
    skill_ids = {s["id"] for s in skills}
    assert skill_ids == {"regulatory-research", "compliance-assessment", "geographic-expansion"}

    # Each skill has the full A2A shape
    for s in skills:
        assert s["id"] == s["name"]  # id == name (capability string)
        assert s["description"] == template["description"]
        assert s["tags"] == [s["id"]]
        assert s["examples"] == template["use_cases"]

    # Auth declared
    assert card["securitySchemes"]["bearerAuth"]["type"] == "http"
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert card["security"] == [{"bearerAuth": []}]

    # Input/output modes — text-only for Phase 1
    assert card["defaultInputModes"] == ["text"]
    assert card["defaultOutputModes"] == ["text"]


# -----------------------------------------------------------------------------
# Label-fallback shape — what the router passes when /info unreachable
# -----------------------------------------------------------------------------


def test_card_label_fallback_shape(card_service):
    """The router's label fallback hands the generator a minimal dict.
    The card should still be well-formed (no missing fields, no nulls
    that would break A2A clients) but skills will be empty."""
    label_fallback = {
        "name": "my-agent",
        "display_name": "Some Template Name",  # what trinity.template label has
        "description": "",
        "version": "1.0.0",
        "capabilities": [],
        "use_cases": [],
    }

    card = card_service.generate_a2a_card(
        agent_name="my-agent",
        template_data=label_fallback,
        base_url="http://localhost:8000",
    )

    assert card["protocolVersion"] == "1.0"
    assert card["name"] == "Some Template Name"
    # Description must never be empty in a valid A2A card — falls back to
    # synthetic "Trinity agent: …"
    assert card["description"] == "Trinity agent: Some Template Name"
    assert card["skills"] == []
    # All other required fields still present
    assert "capabilities" in card
    assert "securitySchemes" in card


# -----------------------------------------------------------------------------
# Field defaults / edge cases
# -----------------------------------------------------------------------------


def test_card_omits_url_when_no_base_url(card_service):
    """If the router can't compute a base URL, the card must NOT
    contain a broken `url` field — orchestrators fall back to the
    discovery URL they fetched the card from."""
    card = card_service.generate_a2a_card(
        agent_name="x",
        template_data={"name": "x", "description": "test"},
        base_url=None,
    )
    assert "url" not in card
    assert card["provider"]["url"] == ""


def test_card_uses_agent_name_when_display_name_missing(card_service):
    card = card_service.generate_a2a_card(
        agent_name="research-bot",
        template_data={"description": "research things"},
        base_url=None,
    )
    assert card["name"] == "research-bot"


def test_card_uses_tagline_when_description_missing(card_service):
    card = card_service.generate_a2a_card(
        agent_name="x",
        template_data={"name": "x", "tagline": "punchy one-liner"},
        base_url=None,
    )
    assert card["description"] == "punchy one-liner"


def test_card_synthesizes_description_when_both_missing(card_service):
    """No description, no tagline → synthesized from agent name so
    clients always get something to display."""
    card = card_service.generate_a2a_card(
        agent_name="x",
        template_data={"display_name": "X Agent"},
        base_url=None,
    )
    assert card["description"] == "Trinity agent: X Agent"


def test_card_version_coerces_non_string_to_string(card_service):
    """YAML loaders sometimes return floats/ints for version. A2A
    expects a string; we coerce defensively."""
    card = card_service.generate_a2a_card(
        agent_name="x",
        template_data={"name": "x", "version": 1.0},
        base_url=None,
    )
    assert card["version"] == "1.0"
    assert isinstance(card["version"], str)


# -----------------------------------------------------------------------------
# Capabilities defensive shapes
# -----------------------------------------------------------------------------


def test_skills_skip_non_string_capabilities(card_service):
    """A capability that's not a string (operator typo: nested dict,
    bare null) must not produce a malformed skill entry that breaks
    JSON-schema validators on the consumer side."""
    template = {
        "name": "x",
        "description": "test",
        "capabilities": [
            "valid-one",
            None,
            42,
            {"nested": "dict"},
            "valid-two",
        ],
    }
    card = card_service.generate_a2a_card(agent_name="x", template_data=template, base_url=None)
    skill_ids = [s["id"] for s in card["skills"]]
    assert skill_ids == ["valid-one", "valid-two"]


def test_skills_strip_whitespace_and_skip_empty(card_service):
    template = {
        "name": "x",
        "description": "test",
        "capabilities": ["  spaced-cap  ", "", "   "],
    }
    card = card_service.generate_a2a_card(agent_name="x", template_data=template, base_url=None)
    skill_ids = [s["id"] for s in card["skills"]]
    assert skill_ids == ["spaced-cap"]


def test_skills_examples_handles_missing_use_cases(card_service):
    """No use_cases on the template — every skill's examples is
    still a list (empty), not None."""
    template = {
        "name": "x",
        "description": "test",
        "capabilities": ["one"],
    }
    card = card_service.generate_a2a_card(agent_name="x", template_data=template, base_url=None)
    assert card["skills"][0]["examples"] == []


# -----------------------------------------------------------------------------
# JSON-serializable contract (defensive)
# -----------------------------------------------------------------------------


def test_card_is_json_serializable(card_service):
    """The card returned by the generator must be json.dumps-able as-
    is — the endpoint returns it directly. Catches any future
    addition of non-serializable types (datetime, set, …)."""
    import json

    template = {
        "name": "x",
        "description": "test",
        "version": 2,  # int — should coerce
        "capabilities": ["a", "b"],
        "use_cases": ["one", "two"],
    }
    card = card_service.generate_a2a_card(
        agent_name="x",
        template_data=template,
        base_url="https://trinity.example.com",
    )
    # Must not raise
    serialized = json.dumps(card)
    roundtrip = json.loads(serialized)
    assert roundtrip["name"] == "x"
