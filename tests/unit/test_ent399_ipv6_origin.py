"""IP-literal origins compare as addresses, not as text (ent#399).

`_same_origin`'s docstring claimed "IPv6 bracket forms compared after
normalisation" while the code compared `urlsplit().hostname` textually —
`canonical_host` leaves a literal untouched, because `idna.encode` rejects it and
the ASCII fallback returns it verbatim. So a registered IPv6-literal endpoint
whose peer card declared the same address in expanded form was refused
`card_origin_mismatch`: fail-closed, but permanently, over a spelling the two
sides have no reason to agree on. The docstring asserting a property the code did
not have is the second half of the defect — it is what the next reader builds on.

What these pin, in both directions:

  * equivalent spellings of ONE address compare equal (the ent#399 AC);
  * addresses that merely LOOK similar stay distinct — different address,
    different scope id, IPv4 vs its mapped IPv6 form, and the spellings
    `ipaddress` refuses outright rather than folding (leading zeros, integer
    forms), which is what keeps this from becoming a way to make two different
    destinations compare equal;
  * the same-origin property the card check exists for is not weakened: a card
    is a hint, and a cross-origin card is still refused.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
sys.path.insert(0, _backend_path)

# tests/utils shadows src/backend/utils — load the backend module directly, with
# the snapshot/restore of the `sys.modules` entry that
# test_736_a2a_url_validation.py documents.
_spec = importlib.util.spec_from_file_location(
    "backend_url_validation_ent399",
    os.path.join(_backend_path, "utils", "url_validation.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_STUBBED = ["backend_url_validation_ent399"]
_PRE_STUB = {name: sys.modules.get(name) for name in _STUBBED}
sys.modules["backend_url_validation_ent399"] = _mod
_spec.loader.exec_module(_mod)

from services import a2a_client  # noqa: E402

canonical_origin_host = _mod.canonical_origin_host
same_origin = a2a_client._same_origin

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    try:
        yield
    finally:
        for name, value in _PRE_STUB.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# --------------------------------------------------------------------------- #
# The AC property
# --------------------------------------------------------------------------- #
def test_E6_ipv6_literal_origins_compare_equal_across_spellings():
    """The sweep's own case (pinned strict-xfail on the PR #2178 branch)."""
    compressed = "https://[2606:4700:4700::1111]/a2a"
    expanded = "https://[2606:4700:4700:0:0:0:0:1111]/a2a"
    assert same_origin(compressed, compressed) is True     # sanity
    assert same_origin(compressed, expanded) is True


@pytest.mark.parametrize(
    "a,b",
    [
        # compressed vs expanded
        ("https://[2001:db8::1]/a2a", "https://[2001:0db8:0000:0000:0000:0000:0000:0001]/a2a"),
        # uppercase vs lowercase hex
        ("https://[2001:DB8::1]/a2a", "https://[2001:db8::1]/a2a"),
        # leading-zero groups
        ("https://[2001:0db8::0001]/a2a", "https://[2001:db8::1]/a2a"),
        # the all-zeros and loopback spellings
        ("https://[::1]/a2a", "https://[0:0:0:0:0:0:0:1]/a2a"),
        # a non-default port carried alongside the literal
        ("https://[2001:db8::1]:8443/a2a", "https://[2001:0db8::0001]:8443/a2a"),
        # ent#398 interaction: `:0` on one side, default on the other
        ("https://[2001:db8::1]:0/a2a", "https://[2001:0db8::1]:443/a2a"),
    ],
)
def test_equivalent_spellings_of_one_address_are_one_origin(a, b):
    assert same_origin(a, b) is True
    assert same_origin(b, a) is True, "the comparison must be symmetric"


# --------------------------------------------------------------------------- #
# ...and what must stay DIFFERENT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "a,b,why",
    [
        ("https://[2001:db8::1]/a2a", "https://[2001:db8::2]/a2a", "different address"),
        ("https://[2001:db8::1]/a2a", "https://[2001:db8::1]:8443/a2a", "different port"),
        ("https://[2001:db8::1]/a2a", "http://[2001:db8::1]/a2a", "different scheme"),
        ("https://[fe80::1%eth0]/a2a", "https://[fe80::1%eth1]/a2a", "different scope id"),
        ("https://[fe80::1%eth0]/a2a", "https://[fe80::1]/a2a", "scoped vs unscoped"),
        ("https://93.184.216.34/a2a", "https://[::ffff:93.184.216.34]/a2a",
         "an IPv4 literal and its mapped IPv6 form are different destinations to the pin"),
        ("https://[2001:db8::1]/a2a", "https://peer.example.com/a2a", "literal vs name"),
    ],
)
def test_addresses_that_are_not_the_same_stay_different(a, b, why):
    assert same_origin(a, b) is False, why
    assert same_origin(b, a) is False, why


def test_ambiguous_ipv4_spellings_are_not_folded():
    """`ipaddress` REFUSES leading-zero and integer forms rather than folding
    them, so they fall through to the textual path. That is the property that
    keeps this normalisation from equating two hosts a resolver would not."""
    assert canonical_origin_host("93.184.216.034") == "93.184.216.034"   # untouched
    assert canonical_origin_host("1560022562") == "1560022562"
    assert same_origin("https://93.184.216.034/a2a", "https://93.184.216.34/a2a") is False


# --------------------------------------------------------------------------- #
# The canonicaliser itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("2001:DB8::1", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),          # bracketed input handled
        ("  2001:db8::1  ", "2001:db8::1"),        # whitespace
        # `ipaddress` renders a v4-mapped address in the dotted form, and folds
        # the hex spelling of the same address onto it.
        ("::ffff:93.184.216.34", "::ffff:93.184.216.34"),
        ("::FFFF:5DB8:D822", "::ffff:93.184.216.34"),
        ("93.184.216.34", "93.184.216.34"),
        ("fe80::1%eth0", "fe80::1%eth0"),          # scope id preserved
        ("PEER.example.com", "peer.example.com"),  # names still go through canonical_host
        ("peer.example.com.", "peer.example.com"),  # trailing dot still stripped
        ("", None),
    ],
)
def test_canonical_origin_host(raw, expected):
    assert canonical_origin_host(raw) == expected


def test_an_idn_name_still_canonicalises_to_its_a_label():
    """The clause ent#399 must not disturb: the IDN fix (SV-7) that made a card
    from an ordinary IDN peer compare equal to its registration."""
    assert canonical_origin_host("bücher.example.com") == "xn--bcher-kva.example.com"
    assert same_origin("https://bücher.example.com/a2a",
                       "https://xn--bcher-kva.example.com/a2a") is True


def test_an_uncanonicalisable_host_is_still_comparable_to_itself():
    """The textual fallback stays: a host neither path can canonicalise must not
    become un-callable, only un-normalised."""
    assert same_origin("https://my_registry.example.com/a2a",
                       "https://MY_registry.example.com/a2a") is True


# --------------------------------------------------------------------------- #
# The card check still refuses what it exists to refuse
# --------------------------------------------------------------------------- #
def test_a_cross_origin_card_is_still_refused_for_literal_endpoints():
    endpoint = "https://[2001:db8::1]/a2a"
    for card in (
        "https://[2001:db8::2]/a2a",        # another address
        "https://evil.example.com/a2a",     # a name
        "http://[2001:db8::1]/a2a",         # scheme downgrade
        "https://[2001:db8::1]:8443/a2a",   # another port
    ):
        assert same_origin(endpoint, card) is False, card
