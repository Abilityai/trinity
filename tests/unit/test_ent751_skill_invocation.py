"""
Gated skills — which gated skills does a request invoke? (trinity-enterprise#751)

Target: ``src/backend/utils/skill_invocation.py::find_gated_invocations``.

The gate matches the agent's set of gated skill names against the requester's
own text, instead of tokenising the text and looking each token up: one read of
the gate set per dispatch, and no tokeniser to disagree with the runtime about
where a name ends (``/pay-invoice.`` at the end of a sentence is still
``pay-invoice``). A false positive raises an approval nobody needed; a false
negative runs a gated skill without one — so every ambiguous row below errs
toward matching. Not a boundary against a hostile requester who names a skill
only in prose: that lane is trinity-enterprise#752.
"""
import pytest

from utils.skill_invocation import find_gated_invocations

GATED = {"pay-invoice", "deploy.prod"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/pay-invoice 100 EUR to ACME", ["pay-invoice"]),
        ("   \n/pay-invoice", ["pay-invoice"]),
        ("Run /pay-invoice for March", ["pay-invoice"]),
        ("please run `/pay-invoice` now", ["pay-invoice"]),
        ('call "/pay-invoice"', ["pay-invoice"]),
        ("run:/pay-invoice", ["pay-invoice"]),
        ("(/pay-invoice)", ["pay-invoice"]),
        ("/PAY-INVOICE", ["pay-invoice"]),
        ("Then /pay-invoice.", ["pay-invoice"]),
        ("/pay-invoice, then report", ["pay-invoice"]),
        ("／pay-invoice", ["pay-invoice"]),            # fullwidth solidus (NFKC)
        ("/\u200bpay-invoice", ["pay-invoice"]),           # zero-width space after the slash
        ("/pay\u200d-invoice", ["pay-invoice"]),           # zero-width joiner inside the name
        ("\u202e/pay-invoice", ["pay-invoice"]),           # bidi override before the slash
        ("/deploy.prod now", ["deploy.prod"]),
        ("/deploy.prod.", ["deploy.prod"]),
    ],
)
def test_invocations_that_name_a_gated_skill_are_found(text, expected):
    assert find_gated_invocations(text, GATED) == expected


@pytest.mark.parametrize(
    "text",
    [
        "/pay-invoices",            # a different skill
        "/pay-invoice-v2",          # a different skill
        "/pay-invoice.v2",          # a different skill (a dot followed by a name char)
        "/pay_invoice",             # a different skill
        "/deploy.production",       # a different skill
        "docs/pay-invoice",         # a path, not an invocation
        "https://example.com/pay-invoice",
        "//pay-invoice",
        "pay-invoice",              # no slash: prose is #752's lane
        "please pay the invoice",
        "",
    ],
)
def test_text_that_names_no_gated_skill_is_not_matched(text):
    assert find_gated_invocations(text, GATED) == []


def test_several_gated_skills_are_reported_once_in_order_of_first_use():
    gated = {"a-skill", "b-skill"}
    text = "/b-skill first, then /a-skill, then /b-skill again"
    assert find_gated_invocations(text, gated) == ["b-skill", "a-skill"]


def test_the_name_is_returned_as_the_gate_set_spells_it():
    assert find_gated_invocations("/pay-invoice", {"Pay-Invoice"}) == ["Pay-Invoice"]


def test_an_empty_gate_set_matches_nothing():
    assert find_gated_invocations("/pay-invoice", set()) == []
    assert find_gated_invocations("/pay-invoice", {}) == []


def test_none_text_matches_nothing():
    assert find_gated_invocations(None, GATED) == []
