"""Unit tests for the A2A exposed-skills filter (ent#180).

An exposed agent's card advertised EVERY `template.yaml capabilities[]` tag, and
the well-known card is unauthenticated — so that list was world-readable. This
adds a per-agent filter through the existing open-core seam.

What these tests pin, in order of what matters:

1. **OSS is unchanged.** No provider registered → the card is byte-identical to
   before ent#180. The whole design rests on that.
2. **The default is unchanged.** Provider present but with no opinion (`None`)
   → advertise everything. Exposure is already opt-in; upgrading must not
   silently empty an exposed agent's card.
3. **`[]` is not `None`.** An explicit "advertise nothing" is a real operator
   choice and must not collapse into the default.
4. **Fail-open.** A provider error advertises unfiltered rather than emptying
   the card. Honest only because this is a disclosure control, not a boundary
   (§32.4 FR-1/FR-5) — a security gate failing open would be a bug.
5. **Both surfaces agree** (§32.4 FR-3) — the public and authenticated cards go
   through one helper.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
while _backend in sys.path:
    sys.path.remove(_backend)
sys.path.insert(0, _backend)


def _load_gate():
    """Fresh module per test — the providers are module-level globals."""
    spec = importlib.util.spec_from_file_location(
        "_a2a_gate_180", f"{_backend}/services/a2a_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _skills(*ids):
    return [{"id": i, "name": i, "description": i, "tags": [i], "examples": []} for i in ids]


class _Provider:
    def __init__(self, result):
        self._result = result

    def exposed_skills(self, agent_name):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class TestOssIsUnchanged:
    def test_no_provider_returns_the_same_list_object(self):
        gate = _load_gate()
        skills = _skills("research", "publish")
        # Identity, not just equality: OSS must not even rebuild the list.
        assert gate.filter_exposed_skills("a", skills) is skills

    def test_no_provider_with_empty_skills(self):
        gate = _load_gate()
        assert gate.filter_exposed_skills("a", []) == []

    def test_provider_is_not_consulted_when_there_are_no_skills(self):
        """A template with no capabilities can't disclose anything — don't pay
        for a provider/DB round-trip on every card render to prove it."""
        gate = _load_gate()

        class _Boom:
            def exposed_skills(self, agent_name):
                raise AssertionError("provider must not be called for an empty card")

        gate.register_skills_provider(_Boom())
        assert gate.filter_exposed_skills("a", []) == []


class TestDefaultIsUnchanged:
    def test_none_means_no_opinion_advertise_all(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider(None))
        skills = _skills("research", "publish")
        assert gate.filter_exposed_skills("a", skills) is skills

    def test_empty_list_is_an_explicit_advertise_nothing(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider([]))
        assert gate.filter_exposed_skills("a", _skills("research")) == []


class TestFiltering:
    def test_keeps_only_allowed_ids_in_card_order(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider(["publish", "research"]))
        out = gate.filter_exposed_skills("a", _skills("research", "internal", "publish"))
        # Card order preserved, not the provider's order.
        assert [s["id"] for s in out] == ["research", "publish"]

    def test_unknown_stored_ids_are_inert(self):
        """§32.4 FR-4: the selection only subtracts. A tag the template no
        longer declares must not conjure a skill onto the card."""
        gate = _load_gate()
        gate.register_skills_provider(_Provider(["research", "removed-from-template"]))
        out = gate.filter_exposed_skills("a", _skills("research", "publish"))
        assert [s["id"] for s in out] == ["research"]

    def test_selection_naming_nothing_that_exists_advertises_nothing(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider(["gone"]))
        assert gate.filter_exposed_skills("a", _skills("research")) == []

    def test_non_string_ids_do_not_crash_the_card(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider([1, "research"]))
        out = gate.filter_exposed_skills("a", [{"id": 1}, {"id": "research"}])
        assert [s["id"] for s in out] == [1, "research"]


class TestFailOpen:
    def test_provider_error_advertises_unfiltered(self, caplog):
        gate = _load_gate()
        gate.register_skills_provider(_Provider(RuntimeError("policy store down")))
        skills = _skills("research", "publish")
        with caplog.at_level("WARNING"):
            out = gate.filter_exposed_skills("a", skills)
        assert out is skills
        assert "skills provider error" in caplog.text

    def test_a_malformed_return_fails_open_like_an_error(self, caplog):
        """A provider returning a str (not list/None) is a defect, and must take
        the same fail-open path as a raised error.

        Found by writing this test: a str is iterable, so it would have
        iterated into single characters, matched no skill id, and SILENTLY
        EMPTIED the card — fail-closed, the opposite of the documented
        contract, and invisible (an empty card reads as "no capabilities", not
        as "the provider is broken")."""
        gate = _load_gate()

        class _Nonsense:
            def exposed_skills(self, agent_name):
                return "not-a-list-or-none"

        gate.register_skills_provider(_Nonsense())
        skills = _skills("research", "publish")
        with caplog.at_level("WARNING"):
            out = gate.filter_exposed_skills("a", skills)
        assert out is skills
        assert "expected list/None" in caplog.text

    def test_a_tuple_or_set_return_is_accepted(self):
        gate = _load_gate()
        gate.register_skills_provider(_Provider(("research",)))
        assert [s["id"] for s in gate.filter_exposed_skills("a", _skills("research", "x"))] == ["research"]


class TestProviderRegistration:
    def test_register_get_clear_roundtrip(self):
        gate = _load_gate()
        p = _Provider(None)
        gate.register_skills_provider(p)
        assert gate.get_skills_provider() is p
        gate.clear_skills_provider()
        assert gate.get_skills_provider() is None

    def test_skills_and_allowlist_providers_are_independent(self):
        """Two seams, one module — registering one must not disturb the other."""
        gate = _load_gate()

        class _Allow:
            def is_inbound_allowed(self, agent_name, caller_identity):
                return False

        gate.register_skills_provider(_Provider([]))
        assert gate.check_inbound_allowed("a", "someone") is True  # no allow-list provider

        gate.clear_skills_provider()
        gate.register_provider(_Allow())
        assert gate.check_inbound_allowed("a", "someone") is False
        assert gate.filter_exposed_skills("a", _skills("x")) == _skills("x")  # no skills provider


class TestBothCardSurfacesFilter:
    """§32.4 FR-3: the public well-known card and the authenticated per-agent
    card must answer "what does this agent do?" identically. They go through one
    helper (`_card_with_exposed_skills`) precisely so a future third surface
    can't quietly skip the filter."""

    def _router(self):
        import importlib
        import services.a2a_gate as gate
        mod = importlib.import_module("routers.a2a")
        return mod, gate

    def test_one_helper_serves_both_routes(self):
        """Structural: if someone reintroduces a bare generate_a2a_card call in
        a route, the filter is silently skipped on that surface."""
        src = Path(f"{_backend}/routers/a2a.py").read_text()
        # The pure builder is invoked exactly once in this module — inside the
        # helper. A second call site would be a route building an unfiltered
        # card.
        assert src.count("generate_a2a_card(") == 1
        # ...and the helper is defined once and used by both routes.
        assert src.count("_card_with_exposed_skills(") == 3

    def test_helper_applies_the_filter(self):
        mod, gate = self._router()
        try:
            gate.register_skills_provider(_Provider(["research"]))
            card = mod._card_with_exposed_skills(
                agent_name="a",
                template_data={
                    "name": "a",
                    "description": "d",
                    "capabilities": ["research", "internal-ops"],
                    "use_cases": [],
                },
                base_url="http://x",
            )
            assert [s["id"] for s in card["skills"]] == ["research"]
        finally:
            gate.clear_skills_provider()

    def test_helper_is_identity_without_a_provider(self):
        """The OSS path through the real router helper — card unchanged."""
        mod, gate = self._router()
        gate.clear_skills_provider()
        template = {
            "name": "a",
            "description": "d",
            "capabilities": ["research", "internal-ops"],
            "use_cases": ["do a thing"],
        }
        card = mod._card_with_exposed_skills(
            agent_name="a", template_data=template, base_url="http://x"
        )
        assert [s["id"] for s in card["skills"]] == ["research", "internal-ops"]
