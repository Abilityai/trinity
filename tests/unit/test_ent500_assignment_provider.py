"""trinity-enterprise#500 — the OSS assignment seam.

The seam's whole job is to be safe when the thing behind it is not. Three
degrade paths are pinned here, and the third is the one a `try`/`except` cannot
give you:

  * **no provider** — a core build has none, so the answer is ``None`` and the
    prompt renders exactly as it did before this feature existed;
  * **a raising provider** — the seam owns the handler, not the provider.
    ``compose_system_prompt`` has no exception handler of its own, and all three
    of its callers lose the execution-context block if this escapes (one loses
    the whole platform prompt), so a provider bug must cost one line, not every
    prompt on the platform;
  * **a malformed non-raising answer** — a ``str`` where a list was promised
    iterates into single characters and renders the WRONG prompt without
    raising. That is strictly worse than a missing line, because nothing looks
    broken. Same degrade path, checked explicitly.

``clear_provider`` is exercised as part of the contract rather than as test
housekeeping: the suite runs under ``pytest-randomly``, so a provider left
registered by one test leaks into whichever test happens to run next.
"""
from __future__ import annotations

import logging

import pytest

from services import assignment_provider as ap


@pytest.fixture(autouse=True)
def _no_provider():
    """Every test starts and ends on the core (no-provider) path."""
    ap.clear_provider()
    yield
    ap.clear_provider()


class _Provider:
    def __init__(self, answer=None, raises: BaseException | None = None):
        self.answer = answer
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    def assignment_for(self, agent_name, triggered_by):
        self.calls.append((agent_name, triggered_by))
        if self.raises is not None:
            raise self.raises
        return self.answer


# ---------------------------------------------------------------------------
# The core path
# ---------------------------------------------------------------------------


def test_no_provider_resolves_to_none():
    assert ap.get_provider() is None
    assert ap.resolve_assignment("ops-companion", "chat") is None


def test_no_agent_name_never_calls_the_provider():
    provider = _Provider(answer={"role_id": "head-of-ops"})
    ap.register_provider(provider)
    assert ap.resolve_assignment(None, "chat") is None
    assert ap.resolve_assignment("", "chat") is None
    assert provider.calls == []


def test_clear_provider_restores_the_core_path():
    ap.register_provider(_Provider(answer={"role_id": "head-of-ops"}))
    assert ap.resolve_assignment("ops", "chat") == {"role_id": "head-of-ops"}
    ap.clear_provider()
    assert ap.get_provider() is None
    assert ap.resolve_assignment("ops", "chat") is None


def test_register_provider_is_last_wins():
    first = _Provider(answer={"role_id": "one"})
    second = _Provider(answer={"role_id": "two"})
    ap.register_provider(first)
    ap.register_provider(second)
    assert ap.resolve_assignment("ops", "chat") == {"role_id": "two"}
    assert first.calls == []


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_registered_provider_is_called_with_agent_and_trigger():
    provider = _Provider(
        answer={
            "primary_user_display": "A. Smith",
            "role_id": "head-of-ops",
            "stakeholders": ["approver: B. Jones"],
            "proactive_consent": False,
        }
    )
    ap.register_provider(provider)
    out = ap.resolve_assignment("ops-companion", "schedule")
    assert provider.calls == [("ops-companion", "schedule")]
    assert out == {
        "primary_user_display": "A. Smith",
        "role_id": "head-of-ops",
        "stakeholders": ["approver: B. Jones"],
        "proactive_consent": False,
    }


def test_a_provider_answering_none_resolves_to_none():
    ap.register_provider(_Provider(answer=None))
    assert ap.resolve_assignment("ops", "chat") is None


def test_unknown_keys_are_dropped():
    """A provider cannot smuggle an unrendered field onto the prompt path."""
    ap.register_provider(
        _Provider(answer={"role_id": "head-of-ops", "user_email": "a@example.com"})
    )
    assert ap.resolve_assignment("ops", "chat") == {"role_id": "head-of-ops"}


def test_an_all_none_answer_resolves_to_none():
    """Nothing to render is the same as nothing to say."""
    ap.register_provider(
        _Provider(answer={"primary_user_display": None, "role_id": None})
    )
    assert ap.resolve_assignment("ops", "chat") is None


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_a_raising_provider_degrades_to_none(caplog):
    ap.register_provider(_Provider(raises=RuntimeError("provider is on fire")))
    with caplog.at_level(logging.WARNING):
        assert ap.resolve_assignment("ops", "chat") is None
    assert any("assignment_provider" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad", ["a string", 42, ["a", "list"], object()])
def test_a_non_mapping_answer_degrades_to_none(bad, caplog):
    ap.register_provider(_Provider(answer=bad))
    with caplog.at_level(logging.WARNING):
        assert ap.resolve_assignment("ops", "chat") is None
    assert any("expected mapping" in r.message for r in caplog.records)


def test_a_string_stakeholders_field_is_dropped_not_iterated(caplog):
    """The B13 defect in its exact shape.

    Without the isinstance check a ``str`` iterates into single characters, so
    the prompt would read `Stakeholders: A, .,  , S, m, i, t, h` — wrong, not
    absent, and therefore invisible.
    """
    ap.register_provider(
        _Provider(answer={"role_id": "head-of-ops", "stakeholders": "A. Smith"})
    )
    with caplog.at_level(logging.WARNING):
        out = ap.resolve_assignment("ops", "chat")
    assert out == {"role_id": "head-of-ops"}
    assert "stakeholders" not in out
    assert any("stakeholders" in r.message for r in caplog.records)


@pytest.mark.parametrize("field", ["primary_user_display", "role_id"])
def test_a_non_string_scalar_field_is_dropped(field, caplog):
    ap.register_provider(_Provider(answer={field: ["not", "a", "string"]}))
    with caplog.at_level(logging.WARNING):
        assert ap.resolve_assignment("ops", "chat") is None


def test_stakeholder_entries_are_coerced_to_str():
    ap.register_provider(_Provider(answer={"stakeholders": ("A. Smith", 7)}))
    assert ap.resolve_assignment("ops", "chat") == {
        "stakeholders": ["A. Smith", "7"]
    }


def test_proactive_consent_is_coerced_to_bool():
    ap.register_provider(_Provider(answer={"proactive_consent": 1}))
    assert ap.resolve_assignment("ops", "chat") == {"proactive_consent": True}


def test_a_partial_answer_keeps_the_good_fields():
    """One malformed field must not throw away the rest of the answer."""
    ap.register_provider(
        _Provider(
            answer={
                "primary_user_display": "A. Smith",
                "role_id": object(),
                "stakeholders": ["approver: B. Jones"],
            }
        )
    )
    out = ap.resolve_assignment("ops", "chat")
    assert out == {
        "primary_user_display": "A. Smith",
        "stakeholders": ["approver: B. Jones"],
    }


# ---------------------------------------------------------------------------
# The guard on the guard (B12)
# ---------------------------------------------------------------------------


def test_the_seam_file_is_covered_by_the_enterprise_docs_guard():
    """The guard greps a HARDCODED file list, so an unlisted seam file leaks
    silently and forever. #1461 is that lesson, and the guard's own header
    cites it — this asserts the lesson was applied to this file too."""
    from pathlib import Path

    guard = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "enterprise-docs-guard.yml"
    )
    text = guard.read_text()
    seam_line = next(
        line for line in text.splitlines() if line.strip().startswith("SEAM_FILES=")
    )
    assert "src/backend/services/assignment_provider.py" in seam_line
    # A file in SEAM_FILES but absent from the `paths:` filters is only grepped
    # when some OTHER path in the filter changed — i.e. not when it is edited.
    assert text.count("- 'src/backend/services/assignment_provider.py'") == 2
