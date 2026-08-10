"""
Regression for #1821 — a mistyped label body must not silently clear the label.

`AgentLabelUpdate` ignored unknown fields and defaulted `label` to None, and
None *means* "clear". So a body the server did not recognise was
indistinguishable from a deliberate clear:

    PUT {"display_label": "Typo Name"}  ->  200, label wiped

That mistake is easy to make: the DB column is `display_label`, the response
field is `display_name`, and only `label` is accepted.

Clearing stays supported — it just has to be said explicitly.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _model():
    from models import AgentLabelUpdate

    return AgentLabelUpdate


def test_unknown_field_is_rejected_not_treated_as_a_clear():
    """The exact bug: the field name from the DB column must not wipe the label."""
    with pytest.raises(ValidationError):
        _model()(display_label="Typo Name")


def test_empty_body_is_rejected():
    """`{}` was also a silent clear — ambiguous with a malformed request."""
    with pytest.raises(ValidationError):
        _model()()


def test_explicit_null_still_clears():
    """Clearing is legitimate — it just has to be explicit."""
    assert _model()(label=None).label is None


def test_blank_string_still_clears():
    """Documented clear-by-blank behaviour is unchanged (normalizer maps to None)."""
    assert _model()(label="   ").label is None


def test_setting_a_label_still_works():
    assert _model()(label="Alpha (Research Lead)").label == "Alpha (Research Lead)"


def test_label_is_still_normalized():
    """The ent#181 normalizer must still run — strictness must not bypass it.

    It trims and NFC-normalizes; it does NOT collapse internal whitespace.
    """
    assert _model()(label="  Alpha Lead  ").label == "Alpha Lead"


def test_control_characters_are_still_rejected():
    """The normalizer's own guard must survive the stricter model config."""
    with pytest.raises(ValidationError):
        _model()(label="bad\nlabel")
