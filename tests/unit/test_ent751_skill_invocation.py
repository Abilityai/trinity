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
        # trinity#3274: `-`, `_` and a digit before the slash no longer make a path,
        # and a trailing `_`/`-` (markdown emphasis, a dash) ends the name.
        ("-/pay-invoice", ["pay-invoice"]),
        ("_/pay-invoice_", ["pay-invoice"]),
        ("1/pay-invoice", ["pay-invoice"]),
        ("then /pay-invoice- done", ["pay-invoice"]),
        # trinity#3274: every Default_Ignorable code point is invisible to a model.
        ("/\u00adpay-invoice", ["pay-invoice"]),           # soft hyphen after the slash
        ("/\u180epay-invoice", ["pay-invoice"]),           # Mongolian vowel separator
        ("/\u034fpay-invoice", ["pay-invoice"]),           # combining grapheme joiner
        ("/\ufe0fpay-invoice", ["pay-invoice"]),           # variation selector 16
        ("/\U000e0100pay-invoice", ["pay-invoice"]),       # variation selector 17
        ("/\U000e0020pay-invoice", ["pay-invoice"]),       # tag space
        ("/pay\u00ad-invoice", ["pay-invoice"]),           # soft hyphen inside the name
        # ... and stripping one must never join a letter to the slash: a model
        # reads `run\u00ad/x` as `run /x` just as easily as `run/x`.
        ("run\u00ad/pay-invoice", ["pay-invoice"]),
        ("a\ufe0f/pay-invoice", ["pay-invoice"]),
        ("x\u2065/pay-invoice", ["pay-invoice"]),
        ("run\u200b/pay-invoice", ["pay-invoice"]),        # missed before #3274
        ("/pay-invoice\u00adx", ["pay-invoice"]),          # the name ends at the invisible
        ("run\u00ad/pay\u200d-invoice", ["pay-invoice"]),  # one before the slash, one in the name
        # A run with no letter or digit right after a dot still ends the name, as
        # in #751 (only `.x`, or `_`/`-` runs into a letter, continue it).
        ("/pay-invoice...now", ["pay-invoice"]),
        ("/pay-invoice\u2026now", ["pay-invoice"]),       # an ellipsis (NFKC: `...`)
        ("run /pay-invoice..then", ["pay-invoice"]),
        ("/pay-invoice._x", ["pay-invoice"]),
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
        "/pay-invoice_v2",          # a different skill (an underscore run continues the name)
        "/pay-invoice--v2",         # a different skill (a hyphen run continues the name)
        "/deploy.prod-staging",     # a different skill
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


def test_a_long_run_of_invisibles_is_matched_in_linear_time():
    """#3274 review: the before-slash rule rescanned a run of invisibles from
    every offset (50k took ~10 s, on the event loop, from a public-link
    message). A run is now matched only from its start."""
    import time

    find_gated_invocations("\u200b" * 2_000 + "x", GATED)       # warm the cache
    start = time.perf_counter()
    find_gated_invocations("\u200b" * 50_000 + "x", GATED)
    assert time.perf_counter() - start < 1.0


# The #751 matcher, kept as the reference: #3274 may only WIDEN what matches
# (a false negative runs a gated skill unapproved).
import re as _re
import unicodedata as _ud

_INVISIBLE_751 = _re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def _matched_by_751(text, name):
    norm = lambda t: _INVISIBLE_751.sub("", _ud.normalize("NFKC", t)).casefold()  # noqa: E731
    return bool(_re.search(r"(?<![a-z0-9._/-])/" + _re.escape(norm(name))
                           + r"(?![a-z0-9_-])(?!\.[a-z0-9])", norm(text)))


from hypothesis import given, settings, strategies as _st  # noqa: E402


@settings(max_examples=3000, derandomize=True, deadline=None)
@given(_st.text(alphabet="ab1._-/ \u200b\u00ad", max_size=6))
def test_everything_the_751_matcher_caught_is_still_caught(noise):
    for text in (noise + "/ab", "/ab" + noise, noise + "/ab" + noise):
        if _matched_by_751(text, "ab"):
            assert find_gated_invocations(text, {"ab"}) == ["ab"], repr(text)
