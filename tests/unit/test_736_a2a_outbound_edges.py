"""#736 — edge cases for the outbound A2A path (`/edge-cases`, matrix rows A–H).

Companion to ``test_736_a2a_outbound_properties.py`` (the Hypothesis arm) and the
fourth file on this feature, deliberately separate from the three that shipped
with it:

* ``test_736_a2a_url_validation.py`` — the destination predicate's happy path and
  its named refusals;
* ``test_736_a2a_outbound_transport.py`` — transport properties over a real
  ``httpx.AsyncClient``;
* ``test_736_a2a_outbound_call.py`` — the route, the store and the seam.

Those 147 tests are thorough on the paths the feature was designed around. What
this file adds is the boundary sweep: both sides of every cap rather than the
far side only, the falsy-but-present values (``0``, ``""``, ``[]``, port ``0``)
that the taxonomy's null/optional row exists to separate, the IPv6 transition
prefixes an attacker reaches for once the plain IPv4-mapped form is blocked, and
the cross-namespace collisions in the endpoint store.

The sweep found six defects. One remains strict-``xfail`` (F5b): it
are real, recorded rather than patched, because this file's job is to find them
and fixing product code is a separate decision. ``strict=True`` matters — each
one turns into a FAILURE the moment the defect is fixed, so the fix cannot land
without deleting the marker and the finding cannot quietly rot into folklore.
Each names its matrix row, its finding id and its tracker issue; see
``.plan/edge-cases-736-a2a-outbound.md``.

+---------+--------+--------------------------------------------------+--------+
| finding | issue  | one-line                                         | state  |
+=========+========+==================================================+========+
| F1      | ent#399| ``_same_origin`` compares IPv6 literals textually | FIXED  |
| F2      | ent#398| port ``:0`` normalised three ways in one path    | FIXED  |
| F3      | ent#397| refusal ``reason`` reverse-engineered from prose | FIXED  |
| F4      | ent#395| ``remove_endpoint`` can delete two endpoints     | FIXED  |
| F5b     | ent#396| a whitespace-only credential clears the secret   | xfail  |
+---------+--------+--------------------------------------------------+--------+

**F1, F2, F3 and F4 were fixed while this sweep was open**, which is why their
markers are gone rather than pending. F1 landed in #2181 (``canonical_origin_host``
compares IP literals as addresses); its E6 test now asserts the property directly.
F3 landed in #2182 (``PublicUrlRefusal`` carries the kind from the raise site,
mapped through one table); its D12 test now asserts the reason directly. F2 landed in #2180 (one shared ``effective_port``
consumed by validation, connection pinning and origin comparison); its D7 test
now asserts the three agreeing. F4 landed in #2177 (first-match-wins delete,
shared with ``resolve_endpoint``); that PR's own regression suite — including
the create-time guard which refuses the collision at the source — is kept
verbatim at the end of this file rather than replaced by the xfail this sweep
had written for it.

The tracker entry is not decoration: a strict-``xfail`` cannot rot silently in
*code*, but nothing about it is discoverable from the backlog, so a reader
deciding what to work on would never see these three (ent#393 review, I1).

**F0 is fixed** (ent#393): the CGNAT clause in ``_is_internal_address`` was
gated on ``ip.version == 4``, so ``::ffff:100.64.0.1`` — the IPv4-MAPPED form of
the one range CPython does *not* refuse for us — reached both
``validate_a2a_endpoint_url`` (a credentialed POST) and
``validate_template_registry_url`` as a public destination. Its marker is gone
and section C now asserts the refusal plainly, with ``test_C1b`` holding the
other side so the fix cannot widen into refusing legitimate public addresses.

Section I was added after the Stage 5a coverage pass rather than from the input
enumeration — see its header for why that distinction is worth recording.

Sync throughout with explicit ``asyncio.run``: ``tests/unit/pytest.ini``
overrides ``pyproject.toml``, so ``asyncio_mode = auto`` does not apply here and
a bare ``async def test_*`` would be collected and never awaited (the rule the
transport file states for the same reason).
"""
from __future__ import annotations

import ast
import asyncio
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import a2a_client, a2a_outbound, a2a_protocol  # noqa: E402
from services.a2a_outbound import ResolvedEndpoint  # noqa: E402
from services.a2a_client import A2ACallError  # noqa: E402
from services.a2a_protocol import (  # noqa: E402
    UnsupportedProtocolVersion,
    resolve_dialect,
    text_from_parts,
)
from utils import url_validation as uv  # noqa: E402
from utils.url_validation import (  # noqa: E402
    A2AEndpointUrlError,
    ValidatedPublicUrl,
    canonical_host,
    validate_a2a_endpoint_url,
)

pytestmark = pytest.mark.unit

PEER_URL = "https://peer.example.com/a2a/bot"
PUBLIC_V4 = "93.184.216.34"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def resolves(monkeypatch):
    """Point every hostname at a chosen set of addresses.

    Lifted from ``test_736_a2a_url_validation.py`` rather than imported: that
    file registers stub modules in ``sys.modules`` at import time, and importing
    across the two would couple this file to that setup.
    """

    def _set(*addresses):
        def _getaddrinfo(host, port, *a, **k):
            out = []
            for addr in addresses:
                if ":" in addr:
                    out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
            return out

        monkeypatch.setattr(uv.socket, "getaddrinfo", _getaddrinfo)

    return _set


@pytest.fixture
def dns_fails(monkeypatch):
    def _fail(host, port, *a, **k):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(uv.socket, "getaddrinfo", _fail)


def _validated(url=PEER_URL, host="peer.example.com", port=443, addr=PUBLIC_V4):
    return ValidatedPublicUrl(url=url, hostname=host, port=port, addresses=(addr,))


# =========================================================================== #
# A. resolve_dialect (matrix A1–A7)
# =========================================================================== #
@pytest.mark.parametrize(
    "raw,expected",
    [
        # A2/A3/A7 — absent, blank and every 0.x spelling default to v0.3. The
        # spec's back-compat rule: an absent version MEANS v0.3, so defaulting
        # is the contract, not leniency.
        ("", "0.3"),
        ("   ", "0.3"),
        ("0", "0.3"),
        ("0.3", "0.3"),
        ("0.3.0", "0.3"),
        (" 0.3 ", "0.3"),
        ("0.3.0-beta", "0.3"),
        ("0.3\n", "0.3"),
        ("0\xa0", "0.3"),          # NBSP — str.strip() takes unicode spaces
        ("0 .3", "0.3"),           # split-then-strip, not strip-then-split
    ],
)
def test_A1_every_0x_spelling_resolves_to_v03(raw, expected):
    assert resolve_dialect(raw).version == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1.0", "2.0", "1", "10.0",
        # A5 — the major comparison is `== "0"`, i.e. exact. These are all
        # arguably "0.x" to a human and are refused. That is the SAFE direction
        # (refusing costs a named error; guessing sends a credential using an
        # unexercised vocabulary), so it is pinned as intended, not filed.
        "00", "00.3", "-0.3", ".3", "0x1", "０.3",
    ],
)
def test_A1_a_major_that_is_not_exactly_zero_is_refused(raw):
    with pytest.raises(UnsupportedProtocolVersion):
        resolve_dialect(raw)


@pytest.mark.parametrize("raw", [None, 1.0, 0.3, 1, True, ["0.3"], {"v": "1.0"}, object()])
def test_A2_a_non_string_version_defaults_to_v03_and_never_raises(raw):
    """Matrix A6 — the type gate runs before the value gate.

    Worth pinning explicitly because it means a peer emitting
    ``"protocolVersion": 1.0`` as a JSON **number** never reaches the v1.x
    refusal and silently negotiates v0.3. That is safe by the module's own
    rationale — v0.3 against a v1 peer yields a clean ``method not found``
    rather than a credential sent in an unexercised dialect — so this asserts
    the behaviour rather than reporting it. It is also the reason the refusal
    cannot be relied on as a protocol *gate*.
    """
    assert resolve_dialect(raw).version == "0.3"


# =========================================================================== #
# B. text_from_parts (matrix B1–B4)
# =========================================================================== #
@pytest.mark.parametrize(
    "container,expected",
    [
        (None, ""),
        ("not a dict", ""),
        (123, ""),
        ([], ""),
        ({}, ""),
        ({"parts": None}, ""),
        ({"parts": "text"}, ""),                       # str is iterable — must not be walked
        ({"parts": {}}, ""),
        ({"parts": []}, ""),
        ({"parts": [None, 1, "x", []]}, ""),           # non-dict members skipped
        ({"parts": [{"kind": "file"}]}, ""),           # wrong kind skipped
        ({"parts": [{"kind": "text"}]}, ""),           # missing text skipped
        ({"parts": [{"kind": "text", "text": 5}]}, ""),  # non-str text skipped
        ({"parts": [{"kind": "text", "text": ""}]}, ""),
        ({"parts": [{"kind": "text", "text": ""}, {"kind": "text", "text": ""}]}, ""),
        ({"parts": [{"kind": "text", "text": "  a  "}]}, "a"),
        # B4 — the strip is applied to the JOINED string, so interior padding
        # survives and only the outer edges are trimmed.
        (
            {"parts": [{"kind": "text", "text": "  a  "}, {"kind": "text", "text": "  b  "}]},
            "a  \n  b",
        ),
    ],
)
def test_B1_text_from_parts_is_tolerant_and_never_raises(container, expected):
    assert text_from_parts(container) == expected


# =========================================================================== #
# C. The destination predicate — IPv6 transition prefixes (matrix C3–C7)
# =========================================================================== #
@pytest.mark.parametrize(
    "address,what",
    [
        # C3 — the mapped form of the address that matters most. The shipped
        # suite covers ::ffff:127.0.0.1 and ::ffff:10.0.0.1 but not this one.
        ("::ffff:169.254.169.254", "IPv4-mapped cloud metadata"),
        ("::ffff:0.0.0.0", "IPv4-mapped unspecified"),
        # (::ffff:100.64.0.1 — IPv4-mapped CGNAT — is NOT here: it was finding F0,
        #  the one mapped form that was accepted. Fixed by ent#393; pinned in
        #  its own test below, because it is the one row here that Trinity's own
        #  code refuses rather than the interpreter.)
        # C3b — the two OTHER v4-in-v6 embeddings, both refused wholesale via
        # `::/8`. They are what makes `ipv4_mapped` a COMPLETE fix for ent#393
        # rather than one shape in a queue of them: `ipv4_mapped` is the only
        # property CPython short-circuits `is_private`/`is_reserved` through, so
        # `::ffff:0:0/96` is the only embedding that can inherit a v4 verdict —
        # every other one falls through to the IPv6 prefix tables. If `::/8`
        # ever leaves those tables, these two rows are the tripwire.
        ("::169.254.169.254", "IPv4-compatible (RFC 4291 ::/96) -> metadata"),
        ("::ffff:0:169.254.169.254", "IPv4-translated (RFC 2765 SIIT) -> metadata"),
        ("::100.64.0.1", "IPv4-compatible -> CGNAT"),
        ("::ffff:0:100.64.0.1", "IPv4-translated -> CGNAT"),
        # C4 — NAT64 well-known prefix (RFC 6052). Refused via is_reserved.
        ("64:ff9b::7f00:1", "NAT64 well-known -> 127.0.0.1"),
        ("64:ff9b::a00:1", "NAT64 well-known -> 10.0.0.1"),
        ("64:ff9b::a9fe:a9fe", "NAT64 well-known -> 169.254.169.254"),
        ("64:ff9b::6440:1", "NAT64 well-known -> 100.64.0.1 (CGNAT)"),
        # C5 — NAT64 local-use prefix (RFC 8215).
        ("64:ff9b:1::7f00:1", "NAT64 local-use -> 127.0.0.1"),
        ("64:ff9b:1::6440:1", "NAT64 local-use -> 100.64.0.1 (CGNAT)"),
        # C6 — 6to4 (RFC 3056): the embedded v4 sits in the second/third groups.
        ("2002:7f00:1::", "6to4 -> 127.0.0.1"),
        ("2002:a9fe:a9fe::", "6to4 -> 169.254.169.254"),
        ("2002:a00:1::", "6to4 -> 10.0.0.1"),
        ("2002:6440:1::", "6to4 -> 100.64.0.1 (CGNAT)"),
        # C7 — Teredo (RFC 4380).
        ("2001:0:5ef5:79fd:0:0:ac10:1", "Teredo"),
        ("2001:db8::1", "documentation range"),
    ],
)
def test_C1_ipv6_transition_forms_of_internal_addresses_are_refused(resolves, address, what):
    """These are the forms an attacker reaches for once the plain IPv4-mapped
    shape is blocked, and every one of them is currently refused — but only
    because of the **interpreter**, not because of anything in Trinity.

    ``_is_internal_address`` asks ``ipaddress`` six questions and adds one CGNAT
    clause. It is Python 3.13's ``ipaddress`` that delegates ``is_private`` /
    ``is_loopback`` through ``ipv4_mapped`` and that carries ``64:ff9b::/96``,
    ``2002::/16``, ``2001::/23`` in its reserved/private tables. On an
    interpreter without those, every row here resolves to a public verdict and a
    credentialed request goes to the metadata service — silently, with the whole
    existing suite still green, because the shipped tests stop at
    ``::ffff:10.0.0.1``.

    That is precisely the class #1891 exists to prevent (CI must run the
    interpreter the image ships), and the guard it added is a *version parity*
    check, which cannot see a behavioural regression inside the stdlib. This
    test can. It belongs to Trinity even though the logic is CPython's, because
    the security property is Trinity's.
    """
    assert uv._is_internal_address(ipaddress.ip_address(address)) is True, what
    resolves(address)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url(PEER_URL)
    assert exc.value.reason == "endpoint_private_address"


# --------------------------------------------------------------------------- #
# C — FINDING F0 (FIXED, ent#393): the CGNAT clause was version-gated, so its
# mapped form escaped. `_is_internal_address` now resolves the v4 VIEW of the
# address (`ipv4_mapped` when v6) and tests `100.64.0.0/10` against that.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address",
    [
        "::ffff:100.64.0.1",
        "::ffff:100.127.255.254",
        "::ffff:100.64.5.5",
        # The edges of the /10 in mapped form. `::ffff:6440:0` is the same
        # address as `::ffff:100.64.0.0` written in hex — `ipaddress`
        # normalises both to one object, so an evasion cannot hide in the
        # notation.
        "::ffff:100.64.0.0",
        "::ffff:6440:0",
        "::ffff:100.127.255.255",
    ],
)
def test_C1_the_mapped_form_of_cgnat_is_refused_like_every_other_mapped_form(
    resolves, address
):
    """The suite already treats IPv4-mapped addresses as a real evasion — it
    parametrizes `::ffff:127.0.0.1` and `::ffff:10.0.0.1`. This is the same
    evasion applied to the one range the stdlib does not cover for us.

    Reachability is the ordinary SSRF-validator threat model the docstring of
    `validate_a2a_endpoint_url` already names: control of DNS for a registered
    endpoint's domain, or a compromised management surface. An `AAAA` record of
    `::ffff:100.64.0.1` is a legal record, and on Linux a dual-stack socket to a
    v4-mapped address routes to the v4 host — so the credential goes to
    100.64.0.1, which several cloud providers use for internal endpoints.
    """
    assert uv._is_internal_address(ipaddress.ip_address(address)) is True
    resolves(address)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url(PEER_URL)
    assert exc.value.reason == "endpoint_private_address"


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:100.63.255.255",  # last address below the /10
        "::ffff:100.128.0.1",     # first address above it
        "::ffff:99.64.0.1",
        "::ffff:101.64.0.1",
        "::ffff:93.184.216.34",
    ],
)
def test_C1b_mapped_addresses_either_side_of_the_cgnat_block_stay_public(
    resolves, address
):
    """The other half of C1, and the reason the fix reads `100.64.0.0/10` rather
    than `100.0.0.0/8`: widening it would blackhole a large slice of the real
    public internet, and the mapped path must not become the place that happens.

    Without this, C1 would pass against a predicate that refused every mapped
    address — which is a strictly worse bug than the one ent#393 fixed, because
    it breaks working endpoints instead of leaking to unreachable ones.
    """
    assert uv._is_internal_address(ipaddress.ip_address(address)) is False
    resolves(address)
    assert validate_a2a_endpoint_url(PEER_URL).addresses == (address,)


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:4700:4700::1111", "1.1.1.1"])
def test_C2_genuinely_public_addresses_still_pass(resolves, address):
    """The other half of C1: a predicate that refuses everything is not a
    predicate. Without this the rows above would pass against `return True`."""
    resolves(address)
    assert validate_a2a_endpoint_url(PEER_URL).addresses == (address,)


# =========================================================================== #
# D. _validate_public_https_url boundaries (matrix D1–D12)
# =========================================================================== #
def test_D1_the_length_cap_is_checked_on_both_sides(resolves):
    """max is accepted, max+1 is refused — the shipped suite asserts only the
    far side, which passes equally against an off-by-one cap."""
    resolves(PUBLIC_V4)
    max_len = uv._REGISTRY_URL_MAX_LEN
    prefix = "https://peer.example.com/"
    at_cap = prefix + "a" * (max_len - len(prefix))
    assert len(at_cap) == max_len
    assert validate_a2a_endpoint_url(at_cap).url == at_cap

    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url(at_cap + "a")
    assert exc.value.reason == "endpoint_invalid"


def test_D1_the_cap_is_applied_after_stripping(resolves):
    """Surrounding whitespace must not consume the operator's budget."""
    resolves(PUBLIC_V4)
    max_len = uv._REGISTRY_URL_MAX_LEN
    prefix = "https://peer.example.com/"
    at_cap = prefix + "a" * (max_len - len(prefix))
    assert validate_a2a_endpoint_url("   " + at_cap + "   ").url == at_cap


@pytest.mark.parametrize("url", ["HTTPS://peer.example.com/a2a", "HtTpS://peer.example.com/a2a"])
def test_D2_an_uppercase_scheme_is_accepted(resolves, url):
    """`urlparse` lowercases the scheme, so the `!= "https"` test is
    case-insensitive by construction. Pinned because it reads as case-sensitive
    and a future hand-rolled `url.startswith("https://")` would break it — which
    is exactly the shape `validate_skills_library_url` still uses."""
    resolves(PUBLIC_V4)
    assert validate_a2a_endpoint_url(url).hostname == "peer.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "https://[oops/a2a",             # unterminated IPv6 — urlparse RAISES here
        "https://[::1/a2a",
        "https:///a2a",                  # no authority at all
        "https://",
    ],
)
def test_D3_a_malformed_authority_is_a_named_refusal_never_a_traceback(resolves, url):
    """`urlparse` raises `ValueError: Invalid IPv6 URL` on an unbalanced
    bracket. This path is reached on every call with an operator-registered URL,
    so an escaping ValueError would be a 500 rather than a refusal."""
    resolves(PUBLIC_V4)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url(url)
    assert exc.value.reason == "endpoint_invalid"


@pytest.mark.parametrize(
    "url,accepted",
    [
        ("https://peer.example.com:8443/a2a", True),
        ("https://peer.example.com:65535/a2a", True),
        ("https://peer.example.com:/a2a", True),       # empty port -> default
        ("https://peer.example.com:99999/a2a", False),  # .port raises
        ("https://peer.example.com:-1/a2a", False),
        ("https://peer.example.com:abc/a2a", False),
    ],
)
def test_D4_port_parsing_refuses_by_name_rather_than_raising(resolves, url, accepted):
    resolves(PUBLIC_V4)
    if accepted:
        assert validate_a2a_endpoint_url(url).url == url
    else:
        with pytest.raises(A2AEndpointUrlError) as exc:
            validate_a2a_endpoint_url(url)
        assert exc.value.reason == "endpoint_invalid"


@pytest.mark.parametrize(
    "url",
    [
        "https://peer.example.com\r\nX-Injected: 1/a2a",
        "https://peer.example.com\n/a2a",
        "https://peer\t.example.com/a2a",
    ],
)
def test_D5_crlf_in_the_authority_cannot_reach_a_header(resolves, url):
    """Matrix D11 / finding F7's safe half.

    ``canonical_host`` called directly WILL hand back a host containing a
    newline (its ASCII fallback only checks encodability). The reason that is
    not a header-injection primitive is one layer up: ``urlparse`` strips
    ``\\t\\r\\n`` from the URL before parsing (CPython's
    ``_UNSAFE_URL_BYTES_TO_REMOVE``), so no such host can arrive through the
    only path that produces one in production.

    Pinned because the safety is entirely CPython's and invisible at this call
    site — the validator reads as though it accepted whatever the authority
    contained. Either outcome below is fail-closed; what must never happen is a
    validated endpoint whose ``hostname`` still carries a control character.
    """
    resolves(PUBLIC_V4)
    try:
        result = validate_a2a_endpoint_url(url)
    except A2AEndpointUrlError:
        return  # refused outright — also fine
    assert not re.search(r"[\r\n\t]", result.hostname), (
        f"a control character survived into the validated hostname: {result.hostname!r}"
    )


# --------------------------------------------------------------------------- #
# D7 / E7 — finding F2 (FIXED, ent#398): port `:0` meant three different
# things in one call path — the validator coalesced it to 443, `_pinned_url`
# dropped it, and `_same_origin` compared it literally as port 0, so a `:0`
# endpoint validated, would have connected, and was then permanently refused
# `card_origin_mismatch`. Fixed in #2180 by one shared `effective_port`
# consumed by all three sites; this pins the three of them agreeing.
# --------------------------------------------------------------------------- #
def test_D7_a_zero_port_is_normalised_consistently_across_the_call_path(resolves):
    resolves(PUBLIC_V4)
    validated = validate_a2a_endpoint_url("https://peer.example.com:0/a2a")

    # The validator already decided `:0` means the scheme default.
    assert validated.port == 443
    # ...and the pinned URL agrees — it drops the port entirely, i.e. 443.
    assert a2a_client._pinned_url(validated.url, PUBLIC_V4) == f"https://{PUBLIC_V4}/a2a"
    # So the origin comparison must agree too. It does not.
    assert a2a_client._same_origin(validated.url, "https://peer.example.com/a2a") is True


# =========================================================================== #
# E. a2a_client pure helpers (matrix E1–E17)
# =========================================================================== #
@pytest.mark.parametrize(
    "url,address,expected",
    [
        # E3 — fragment dropped, query kept, empty path becomes "/".
        ("https://h.example/a2a#frag", PUBLIC_V4, f"https://{PUBLIC_V4}/a2a"),
        ("https://h.example/a2a?x=1&y=2", PUBLIC_V4, f"https://{PUBLIC_V4}/a2a?x=1&y=2"),
        ("https://h.example", PUBLIC_V4, f"https://{PUBLIC_V4}/"),
        ("https://h.example:8443/a2a", PUBLIC_V4, f"https://{PUBLIC_V4}:8443/a2a"),
        ("https://u:p@h.example/a2a", PUBLIC_V4, f"https://{PUBLIC_V4}/a2a"),
        ("https://h.example/a2a", "2606:4700::1111", "https://[2606:4700::1111]/a2a"),
        ("https://h.example:8443/a2a", "2606:4700::1111", "https://[2606:4700::1111]:8443/a2a"),
    ],
)
def test_E1_pinned_url_preserves_the_request_and_nothing_else(url, address, expected):
    assert a2a_client._pinned_url(url, address) == expected


def test_E2_sanitize_bounds_on_both_sides_of_the_cap():
    """Matrix E9 — cap-1 / cap / cap+1 / 2·cap / far past 2·cap.

    The shipped suite asserts an oversized response is truncated with a marker;
    it does not assert that an exactly-at-cap response is left alone, so an
    off-by-one that appended the marker to every full-length answer would pass.
    """
    cap = a2a_client.A2A_MAX_RESPONSE_CHARS
    marker = "\n…[truncated by Trinity]"

    for name, text, want_trunc in [
        ("None", None, False),
        ("empty", "", False),
        ("cap-1", "a" * (cap - 1), False),
        ("cap", "a" * cap, False),
        ("cap+1", "a" * (cap + 1), True),
        ("2cap", "a" * (2 * cap), True),
        ("10cap", "a" * (10 * cap), True),
    ]:
        out, truncated = a2a_client.sanitize_outbound_text(text, "SEKRET")
        assert truncated is want_trunc, name
        if text in (None, ""):
            assert out == text, name
            continue
        assert len(out) <= cap + len(marker), name
        assert out.endswith(marker) is want_trunc, name


@pytest.mark.parametrize(
    "payload,expected",
    [
        (None, ("unknown", None, None, None)),
        ("a string", ("unknown", None, None, None)),
        (0, ("unknown", None, None, None)),
        ([], ("unknown", None, None, None)),
        ({}, ("unknown", None, None, None)),
        ({"status": "not a dict"}, ("unknown", None, None, None)),
        ({"status": {"state": 123}}, ("unknown", None, None, None)),   # non-str state
        ({"status": {"state": ""}}, ("unknown", None, None, None)),    # "" or "unknown"
        ({"artifacts": "not a list"}, ("unknown", None, None, None)),
        ({"artifacts": [None, 1, {}]}, ("unknown", None, None, None)),
        ({"id": 123, "contextId": None}, ("unknown", None, None, None)),  # non-str ids dropped
        (
            {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]},
            ("completed", "hi", None, None),
        ),
        (
            {"status": {"state": "working"}, "id": "t1", "contextId": "c1"},
            ("working", None, "t1", "c1"),
        ),
    ],
)
def test_E3_parse_task_is_total_over_hostile_payloads(payload, expected):
    """Every field is peer-controlled, so a shape we do not recognise must yield
    `unknown` rather than an exception — a malformed success becoming a 500 on
    our side is the failure this tolerance exists to prevent."""
    assert a2a_client._parse_task(payload) == expected


@pytest.mark.parametrize(
    "body,outcome",
    [
        # E14 — falsy-but-PRESENT results are results. `result is None` is the
        # right test and `if not result` would be the bug; pinned so it stays.
        ({"result": 0}, ("ok", 0)),
        ({"result": []}, ("ok", [])),
        ({"result": False}, ("ok", False)),
        ({"result": ""}, ("ok", "")),
        ({"error": None, "result": {}}, ("ok", {})),
        # missing vs explicitly-null result are conflated, and both are refused.
        ({}, ("raise", "rpc_invalid")),
        ({"result": None}, ("raise", "rpc_invalid")),
        # E16 — a non-int code still surfaces the error, with remote_code None.
        ({"error": {"code": "x", "message": "boom"}}, ("raise", "remote_error")),
        ({"error": {}}, ("raise", "remote_error")),
        ({"error": {"code": -32001, "message": "boom"}}, ("raise", "remote_error")),
    ],
)
def test_E4_rpc_error_discrimination(body, outcome):
    kind, expected = outcome
    if kind == "ok":
        assert a2a_client._raise_for_rpc_error(body, "SEK") == expected
    else:
        with pytest.raises(A2ACallError) as exc:
            a2a_client._raise_for_rpc_error(body, "SEK")
        assert exc.value.reason == expected


def test_E4_a_non_dict_error_is_reported_as_no_error_at_all():
    """Matrix E15 / finding F5 — CHARACTERISATION, not an endorsement.

    A peer answering ``{"error": "boom"}`` — a string rather than the spec's
    error object — is reported as *"returned neither a result nor an error"*.
    The refusal itself is right (fail-closed, never a fabricated success), but
    the message is factually false and the remote's own error text is dropped on
    the floor, which is the one thing the operator debugging a federation would
    want. Recorded as informational rather than xfail because nothing unsafe
    happens; pinned so a future fix is a deliberate change and not a surprise.
    """
    with pytest.raises(A2ACallError) as exc:
        a2a_client._raise_for_rpc_error({"error": "boom"}, "SEK")
    assert exc.value.reason == "rpc_invalid"
    assert "neither a result nor an error" in exc.value.detail
    assert "boom" not in exc.value.detail


def test_E5_the_sub_budgets_still_fit_inside_the_total_deadline():
    """Matrix E17 / finding F6.

    ``_with_deadline``'s docstring rests on an arithmetic — "10 s card + 30 s
    RPC + slack = 45 s" — that nothing enforced. The slack is exactly the DNS
    budget, so the real margin is **zero**: bump ``A2A_RPC_TIMEOUT`` to 35 and
    the total deadline starts firing before the RPC timeout does, changing which
    detail message the calling agent gets for an ordinary slow peer.

    Asserting ``<=`` rather than ``<`` pins today's equality without inventing a
    margin the design never claimed.
    """
    budgeted = (
        a2a_client.A2A_DNS_TIMEOUT
        + a2a_client.A2A_CARD_FETCH_TIMEOUT
        + a2a_client.A2A_RPC_TIMEOUT
    )
    assert budgeted <= a2a_client.A2A_TOTAL_DEADLINE, (
        f"sub-budgets total {budgeted}s but the wall-clock deadline is "
        f"{a2a_client.A2A_TOTAL_DEADLINE}s — a normal slow call would now be cut "
        "short by the deadline instead of by its own timeout"
    )
    assert a2a_client.A2A_RPC_TIMEOUT <= a2a_client.A2A_TOTAL_DEADLINE


# --------------------------------------------------------------------------- #
# E6 — FINDING F1: _same_origin does not normalise IPv6 literals
# --------------------------------------------------------------------------- #
def test_E6_ipv6_literal_origins_compare_equal_across_spellings():
    compressed = "https://[2606:4700:4700::1111]/a2a"
    expanded = "https://[2606:4700:4700:0:0:0:0:1111]/a2a"
    assert a2a_client._same_origin(compressed, compressed) is True  # sanity
    assert a2a_client._same_origin(compressed, expanded) is True


# =========================================================================== #
# F. _read_capped boundaries (matrix F1–F4)
# =========================================================================== #
def _streaming(status, body: bytes, headers=None):
    """A response httpx will actually STREAM.

    ``httpx.Response(content=…)`` buffers in the constructor and marks the
    stream consumed, so ``aiter_raw()`` — the call production makes — raises
    ``StreamConsumed`` against it. Same helper and same reason as the transport
    file's ``_as_streaming``.
    """

    async def _gen():
        for i in range(0, max(len(body), 1), 4096):
            yield body[i:i + 4096]

    return httpx.Response(status, headers=headers or {}, stream=httpx.AsyncByteStream() if False else _AsyncIter(_gen()))


class _AsyncIter(httpx.AsyncByteStream):
    def __init__(self, gen):
        self._gen = gen

    async def __aiter__(self):
        async for chunk in self._gen:
            yield chunk


def _read(body: bytes, max_bytes: int, headers=None, status=200):
    transport = httpx.MockTransport(lambda request: _streaming(status, body, headers))

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await a2a_client._read_capped(
                client, "GET", "https://1.2.3.4/x",
                sni="h.example", host_header="h.example",
                max_bytes=max_bytes, headers={}, error_prefix="card",
            )

    return asyncio.run(_run())


def test_F1_the_byte_ceiling_is_inclusive_at_the_cap_and_refuses_one_over():
    """Matrix F1 — the shipped suite asserts only that an oversized body is
    aborted, which passes equally against an off-by-one that also rejected a
    body of exactly `max_bytes`."""
    assert _read(b"a" * 1000, 1000) == b"a" * 1000

    with pytest.raises(A2ACallError) as exc:
        _read(b"a" * 1001, 1000)
    assert exc.value.reason == "card_too_large"


@pytest.mark.parametrize(
    "declared,refused_early",
    [
        ("2000", True),      # honest and over -> refused before any body read
        ("1000", False),     # honest and at cap -> allowed
        ("-5", False),       # not isdigit -> ignored, stream cap governs
        (" 2000 ", False),   # not isdigit (spaces) -> ignored
        ("2e3", False),      # not isdigit -> ignored
        ("", False),         # falsy -> ignored
    ],
)
def test_F2_a_content_length_is_a_hint_and_never_the_only_check(declared, refused_early):
    """A declared length over the cap short-circuits; anything unparseable is
    ignored and the wire-byte counter is what actually bounds the read."""
    if refused_early:
        with pytest.raises(A2ACallError) as exc:
            _read(b"a" * 10, 1000, headers={"content-length": declared})
        assert exc.value.reason == "card_too_large"
    else:
        assert _read(b"a" * 10, 1000, headers={"content-length": declared}) == b"a" * 10


@pytest.mark.parametrize(
    "encoding,refused",
    [
        ("gzip", True),
        ("GZIP", True),
        ("br", True),
        ("identity, gzip", True),   # a list containing identity is still not identity
        ("identity", False),
        ("IDENTITY", False),
        ("  identity  ", False),
        ("", False),
    ],
)
def test_F2_content_encoding_is_refused_unless_it_is_exactly_identity(encoding, refused):
    if refused:
        with pytest.raises(A2ACallError) as exc:
            _read(b"body", 1000, headers={"content-encoding": encoding})
        assert exc.value.reason == "card_encoding"
    else:
        assert _read(b"body", 1000, headers={"content-encoding": encoding}) == b"body"


def test_F3_the_refusal_order_is_redirect_then_encoding_then_length_then_status():
    """Matrix F4 — CHARACTERISATION of the current precedence.

    It matters that this is pinned rather than assumed: a 500 carrying a gzip
    body reports `card_encoding`, not `card_http_error`, because the encoding
    guard runs first. That is defensible (the body is refused either way and
    never decoded) but it is not obvious, and an operator reading `card_encoding`
    for what is really an outage would be misled if the order silently changed.
    """
    with pytest.raises(A2ACallError) as exc:
        _read(b"x", 1000, headers={"content-encoding": "gzip"}, status=500)
    assert exc.value.reason == "card_encoding"

    with pytest.raises(A2ACallError) as exc:
        _read(b"x", 1000, headers={"content-encoding": "gzip"}, status=302)
    assert exc.value.reason == "card_redirect"

    with pytest.raises(A2ACallError) as exc:
        _read(b"x", 1000, status=500)
    assert exc.value.reason == "card_http_error"


# =========================================================================== #
# G. The OSS endpoint store (matrix G1–G8)
# =========================================================================== #
@pytest.fixture
def store(monkeypatch):
    """An in-memory `system_settings` + the REAL AES-256-GCM round trip.

    The crypto is deliberately not stubbed, for the reason the shipped
    `oss_store` fixture records: patching `CredentialEncryptionService` by string
    target passes in isolation and fails inside the full suite once
    `tests/unit/conftest.py` restores `sys.modules`.
    """
    import secrets

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))

    import database
    from services import a2a_outbound

    backing = {}
    monkeypatch.setattr(database.db, "get_setting_value",
                        lambda key, default=None: backing.get(key, default), raising=False)
    monkeypatch.setattr(database.db, "set_setting",
                        lambda key, value: backing.__setitem__(key, value), raising=False)
    monkeypatch.setattr("utils.url_validation.validate_a2a_endpoint_url",
                        lambda url: _validated(url=url))
    a2a_outbound.clear_provider()
    return a2a_outbound


@pytest.mark.parametrize(
    "ref,found",
    [
        ("partner", True),          # exact name
        ("PARTNER", True),          # name match is case-insensitive
        ("  partner  ", True),      # ref is stripped
        ("partne", False),          # no prefix matching
        ("", False),
        ("   ", False),
    ],
)
def test_G1_reference_resolution_is_exact_after_strip(store, ref, found):
    store.upsert_endpoint("partner", PEER_URL, "sek")
    assert (store.resolve_endpoint("bot", ref) is not None) is found


def test_G1_id_lookup_is_case_sensitive_while_name_lookup_is_not(store):
    """An asymmetry worth pinning: ids are opaque server-minted tokens compared
    with `==`, names are operator-typed and compared lowercased. Neither is
    wrong; a future "normalise the ref once" refactor would quietly change one."""
    record = store.upsert_endpoint("partner", PEER_URL, "sek")
    assert store.resolve_endpoint("bot", record["id"]) is not None
    assert store.resolve_endpoint("bot", record["id"].upper()) is None


def test_G2_a_record_with_an_empty_url_shadows_a_later_id_match(store):
    """Matrix G3 — CHARACTERISATION of an early `return None` inside the loop.

    `resolve_endpoint` returns (rather than continues) when a matching record has
    no URL, so a later record matching the same ref by *id* is never reached.
    `upsert_endpoint` cannot create a URL-less record, so this is only reachable
    through a legacy or hand-written settings row — which is exactly the case the
    module's fail-closed contract is written for. Refusing is the safe answer;
    pinned so the shadowing is a known property rather than a surprise.
    """
    store._store_endpoint_records([
        {"id": "id1", "name": "dup", "url": ""},
        {"id": "dup", "name": "other", "url": PEER_URL},
    ])
    assert store.resolve_endpoint("bot", "dup") is None


@pytest.mark.parametrize("credential", [None, ""])
def test_G4_a_blank_credential_never_overwrites_a_stored_one(store, credential):
    """Matrix G5 — `None` and `""` both mean "leave it alone".

    That is the documented update semantics (`clear_credential` is the only
    removal path). Pinned because an operator PUTting an empty string to "clear"
    the secret gets a silent no-op — the request model normalises blank to None,
    so the HTTP surface is honest, but `upsert_endpoint` is a public module
    function and a future caller could reasonably expect `""` to clear.
    """
    store.upsert_endpoint("partner", PEER_URL, "real-secret")
    store.upsert_endpoint("partner", PEER_URL, credential)
    assert store.resolve_endpoint("bot", "partner").credential == "real-secret"

    store.upsert_endpoint("partner", PEER_URL, clear_credential=True)
    assert store.resolve_endpoint("bot", "partner").credential is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG (finding F5b): a WHITESPACE-ONLY credential is the one blank "
        "spelling that destroys the stored secret. `upsert_endpoint` skips the "
        "header-safety check on it (`if credential.strip() and …`) but then takes "
        "the `elif credential:` branch, because '   ' is truthy, and writes "
        "`''`. So `None` and `''` preserve, `'   '` silently clears — a fourth "
        "path through a model the docstring describes as three (omit / set / "
        "clear_credential). Not reachable over HTTP (the request model normalises "
        "blank to None), so this is a public-module-function defect, not an API "
        "one. Tracked as ent#396. See /edge-cases report 2026-08-13."
    ),
)
def test_G4_a_whitespace_only_credential_does_not_silently_clear_the_stored_one(store):
    store.upsert_endpoint("partner", PEER_URL, "real-secret")
    store.upsert_endpoint("partner", PEER_URL, "   ")
    assert store.resolve_endpoint("bot", "partner").credential == "real-secret"


def test_G3_the_cap_bounds_creation_but_never_an_update(store):
    """Matrix G4 — updates must stay possible at the cap, or an operator who
    fills the registry can no longer repoint or re-credential anything."""
    for i in range(store.MAX_ENDPOINTS):
        store.upsert_endpoint(f"ep{i}", PEER_URL)
    assert len(store.list_oss_endpoints()) == store.MAX_ENDPOINTS

    with pytest.raises(store.EndpointValidationError):
        store.upsert_endpoint("one-too-many", PEER_URL)

    store.upsert_endpoint("ep0", "https://moved.example/a2a", "new-secret")
    assert len(store.list_oss_endpoints()) == store.MAX_ENDPOINTS
    assert store.resolve_endpoint("bot", "ep0").url == "https://moved.example/a2a"


# =========================================================================== #
# H. reason -> HTTP status parity (matrix H1–H2)
# =========================================================================== #
def _raised_reasons() -> set:
    """Every literal `A2ACallError` reason raised in `a2a_client.py`, by AST.

    Static rather than behavioural on purpose: the point is to catch a reason
    that is renamed at the raise site while its entry in the router's map keeps
    the old spelling, which no runtime test of the paths that still work can
    see. Same shape and same motivation as `test_1880_canary_alert_parity.py`.
    """
    tree = ast.parse((_BACKEND / "services" / "a2a_client.py").read_text())
    reasons = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "A2ACallError" or not node.exc.args:
            continue
        arg = node.exc.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            reasons.add(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            # f"{error_prefix}_redirect" — both prefixes are passed literally by
            # `fetch_card` ("card") and `_rpc` ("rpc").
            suffix = "".join(v.value for v in arg.values if isinstance(v, ast.Constant))
            reasons.update({"card" + suffix, "rpc" + suffix})
    return reasons


def _endpoint_url_reasons() -> set:
    """Every reason `validate_a2a_endpoint_url` can actually produce.

    ent#397 moved most of these out of literal raise sites and into the
    `_A2A_REASON_BY_KIND` table, so a literals-only scan under-reports: it
    stopped seeing `endpoint_private_address` and `endpoint_dns_failure` even
    though both are still produced, which made H1 report a live reason as a
    dead map entry. Both production sites are read — the remaining literals
    AND the table's values — so this stays a scan of what the module emits
    rather than of how it happens to spell it this month.
    """
    src = (_BACKEND / "utils" / "url_validation.py").read_text()
    literal = set(re.findall(r'A2AEndpointUrlError\(\s*"([a-z_]+)"', src))
    block = re.search(r"_A2A_REASON_BY_KIND\s*=\s*\{(.*?)\}", src, re.S)
    assert block, "the kind->reason table moved or was renamed"
    mapped = set(re.findall(r':\s*"([a-z_]+)"', block.group(1)))
    return literal | mapped


def _status_map() -> dict:
    src = (_BACKEND / "routers" / "a2a.py").read_text()
    block = re.search(r"_A2A_CLIENT_ERROR_STATUS\s*=\s*\{(.*?)\}", src, re.S)
    assert block, "the reason->status map moved or was renamed"
    return {k: int(v) for k, v in re.findall(r'"([a-z_]+)":\s*(\d+)', block.group(1))}


def test_H1_the_status_map_has_no_dead_entries():
    """A mapped reason nobody raises is a rename that already happened: the new
    spelling falls through to the 502 default, so the endpoint that used to
    answer 400 now answers 502 and every test of the working paths stays green."""
    produced = _raised_reasons() | _endpoint_url_reasons()
    dead = set(_status_map()) - produced
    assert not dead, (
        f"{sorted(dead)} are mapped to a status but never raised — if a reason was "
        "renamed, its new spelling is now silently defaulting to 502"
    )


def test_H1_every_endpoint_refusal_is_a_4xx_and_never_the_502_default():
    """`A2AEndpointUrlError` reasons are the caller's/operator's fault by
    construction — a bad registry row, not a misbehaving peer — so each must be
    mapped explicitly. A new one added to `A2A_URL_REASONS` without a map entry
    would be reported as a peer failure."""
    status = _status_map()
    for reason in _endpoint_url_reasons():
        assert reason in status, f"{reason!r} has no explicit status and would default to 502"
        assert 400 <= status[reason] < 500, f"{reason!r} maps to {status[reason]}, not a 4xx"


def test_H1_the_declared_reason_vocabulary_matches_what_is_raised():
    """`A2A_URL_REASONS` is the module's published vocabulary; the raises are the
    truth. Drift means the tool branches on a code the backend never sends."""
    assert set(uv.A2A_URL_REASONS) == _endpoint_url_reasons()
    assert all(k == v for k, v in uv.A2A_URL_REASONS.items()), (
        "A2A_URL_REASONS is a self-map; a key/value divergence would make the "
        "constant and the literal disagree"
    )


# --------------------------------------------------------------------------- #
# D10/D12 — FINDING F3: reason codes are derived by substring-matching prose
# --------------------------------------------------------------------------- #
def test_D12_a_dns_failure_is_never_misreported_as_a_private_address(dns_fails):
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url("https://an internal address.example/a2a")
    assert exc.value.reason == "endpoint_dns_failure"


# =========================================================================== #
# I. Branch gaps surfaced by the Stage 5a coverage pass
#
# Each of these is a real matrix row that the first generation pass missed: the
# coverage report is what turned "I enumerated the inputs" into "I enumerated
# the paths". They are grouped rather than filed under A–H because what they
# have in common is how they were found.
# =========================================================================== #
@pytest.mark.parametrize("bad", ["not-an-ip", "", "999.999.999.999", "::gg", "127.0.0.1 "])
def test_I1_an_unparseable_resolver_record_refuses_rather_than_skipping(monkeypatch, bad):
    """`url_validation.py` — the `except ValueError` inside the address loop.

    "An address the stdlib itself cannot parse is not one we are going to vet —
    refuse rather than skip (skipping is how a bad record passes through
    unexamined)." That is the whole argument for the branch, and nothing
    exercised it: a `continue` there would have passed every shipped test while
    letting a malformed record be dropped from the very set the caller then pins
    the connection to.
    """
    def _getaddrinfo(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (bad, 0))]

    monkeypatch.setattr(uv.socket, "getaddrinfo", _getaddrinfo)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url(PEER_URL)
    assert exc.value.reason == "endpoint_private_address"


def test_I1_a_bad_record_alongside_good_ones_still_refuses(monkeypatch):
    """The mixed case, for the same reason P9 exists: one unusable record must
    not be silently dropped from a set that otherwise looks fine."""
    def _getaddrinfo(host, port, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_V4, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("garbage", 0)),
        ]

    monkeypatch.setattr(uv.socket, "getaddrinfo", _getaddrinfo)
    with pytest.raises(A2AEndpointUrlError):
        validate_a2a_endpoint_url(PEER_URL)


def test_I2_the_dialect_cache_expires_on_its_ttl_boundary(monkeypatch):
    """`_cached_target` — the TTL arm and the eviction it performs.

    The taxonomy's TTL row: a cache read exactly AT the boundary must still be a
    hit (`>` not `>=`), and a read past it must both miss and evict, or the
    entry lives forever behind a permanently-stale timestamp.
    """
    a2a_client.clear_dialect_cache()
    key = "https://peer.example.com/a2a"
    clock = {"t": 1000.0}
    monkeypatch.setattr(a2a_client.time, "monotonic", lambda: clock["t"])

    a2a_client._cache_target(key, a2a_protocol.DIALECT_V03, key)
    assert a2a_client._cached_target(key) == (a2a_protocol.DIALECT_V03, key)

    clock["t"] = 1000.0 + a2a_client.A2A_DIALECT_CACHE_TTL          # exactly at TTL
    assert a2a_client._cached_target(key) is not None, "the boundary itself is still a hit"

    clock["t"] = 1000.0 + a2a_client.A2A_DIALECT_CACHE_TTL + 0.001  # one tick past
    assert a2a_client._cached_target(key) is None
    assert key not in a2a_client._dialect_cache, "an expired entry must be evicted, not just missed"


def test_I2_a_cached_dialect_we_no_longer_claim_is_a_miss_not_a_guess():
    """The `version != "0.3"` arm. Unreachable today (only v0.3 is ever cached)
    and deliberately so — it is the guard that stops a future v1.0 rollout from
    silently reviving a stale entry and sending a credential in a dialect the
    running code no longer claims."""
    a2a_client.clear_dialect_cache()
    key = "https://peer.example.com/a2a"
    a2a_client._cache_target(key, a2a_protocol.DIALECT_V10, key)
    assert a2a_client._cached_target(key) is None
    a2a_client.clear_dialect_cache()


@pytest.mark.parametrize(
    "registered,card,expected",
    [
        # A card with no opinion + a registered path: honour the operator.
        ("https://peer.example.com/a2a/bot", {}, "https://peer.example.com/a2a/bot"),
        ("https://peer.example.com/a2a/bot", {"url": None}, "https://peer.example.com/a2a/bot"),
        ("https://peer.example.com/a2a/bot", {"url": "   "}, "https://peer.example.com/a2a/bot"),
        ("https://peer.example.com/a2a/bot", {"url": 42}, "https://peer.example.com/a2a/bot"),
        # No card opinion and no registered path: fall back to the origin root.
        ("https://peer.example.com", {}, "https://peer.example.com/"),
        ("https://peer.example.com/", {}, "https://peer.example.com/"),
    ],
)
def test_I3_an_opinionless_card_falls_back_to_the_operators_registration(registered, card, expected):
    """`resolve_rpc_target`'s two no-declared-url arms.

    The card is a hint, so its ABSENCE has to be handled as deliberately as its
    presence — and the two arms differ: with a registered path the operator named
    a specific endpoint, without one there is nothing to honour but the origin
    root. A single fallback would POST to `/` for a path-registered endpoint,
    which is a different endpoint, silently.
    """
    validated = _validated(url=registered)
    assert a2a_client.resolve_rpc_target(validated, card) == expected


def test_I4_a_card_that_is_not_json_or_not_an_object_is_named_not_raised():
    """`fetch_card`'s two decode arms — a peer-controlled body must never turn a
    `json.JSONDecodeError` loose inside the orchestrator."""
    for body, label in [(b"<html>nope</html>", "html"), (b"", "empty"), (b"[1,2,3]", "array"),
                        (b'"a string"', "string"), (b"\xff\xfe", "invalid utf-8")]:
        transport = httpx.MockTransport(lambda request: _streaming(200, body))

        async def _run():
            async with httpx.AsyncClient(transport=transport) as client:
                return await a2a_client.fetch_card(client, _validated())

        with pytest.raises(A2ACallError) as exc:
            asyncio.run(_run())
        assert exc.value.reason == "card_invalid", label


@pytest.mark.parametrize(
    "context_id,task_id,expect_context,expect_task",
    [
        ("c1", "t1", True, True),
        (None, None, False, False),
        # Falsy-but-present: "" is dropped exactly like None. Correct for this
        # protocol (an empty contextId is not a context) but it is the §1
        # None-vs-empty conflation, so it is pinned rather than assumed.
        ("", "", False, False),
    ],
)
def test_I5_optional_message_ids_are_omitted_when_falsy(context_id, task_id, expect_context, expect_task):
    message = a2a_protocol.text_message("hello", "mid1", context_id=context_id, task_id=task_id)
    assert message["role"] == "user"
    assert message["parts"] == [{"kind": "text", "text": "hello"}]
    assert message["messageId"] == "mid1"
    assert ("contextId" in message) is expect_context
    assert ("taskId" in message) is expect_task
    # The VALUES, not just the keys. Asserting presence alone let a mutant that
    # wrote `message["contextId"] = None` survive the Stage 5b gate — and that
    # mutation is the exact silent failure that matters here: `contextId` is what
    # threads a multi-turn A2A conversation, so a None would break continuity
    # while every "the key is there" assertion stayed green.
    if expect_context:
        assert message["contextId"] == context_id
    if expect_task:
        assert message["taskId"] == task_id


def test_I5_the_jsonrpc_envelope_is_well_formed():
    """`build_request` — the envelope both directions agree on.

    Untested until the mutation gate reported nine live mutants here with no
    covering test: it is exercised only incidentally through `call_endpoint`, so
    every field could be renamed or blanked without a red test. The field names
    are wire protocol, not style — `jsonrpc` must be exactly `"2.0"` and the id
    must round-trip, or a compliant peer rejects the request.
    """
    envelope = a2a_protocol.build_request("rpc-1", "message/send", {"message": {"a": 1}})
    assert envelope == {
        "jsonrpc": "2.0",
        "id": "rpc-1",
        "method": "message/send",
        "params": {"message": {"a": 1}},
    }
    # Empty params is a legal call, not an omission.
    assert a2a_protocol.build_request("x", "tasks/get", {})["params"] == {}


def test_I6_same_origin_returns_false_rather_than_raising_on_junk():
    """`_same_origin._key`'s four guard arms.

    Both sides of this comparison can be peer-controlled — the card's declared
    `url` is whatever the remote wrote — and the function's answer gates a
    CREDENTIALED POST. So the requirement is not merely "does not raise" but
    "an unparseable input is never *equal* to anything": a `_key` that returned
    a shared sentinel on failure would make two junk URLs compare same-origin
    and wave a hostile card straight through.
    """
    junk = [
        "https://[unterminated",     # urlsplit raises
        "https://h:99999/",          # .port raises
        "https://h:-1/",
        "no-scheme.example/a2a",     # no scheme
        "https:///a2a",              # no host
        "",
        "   ",
        "::::",
    ]
    for a in junk:
        assert a2a_client._same_origin(a, a) is False, f"{a!r} must not be same-origin with itself"
        assert a2a_client._same_origin(a, PEER_URL) is False
        assert a2a_client._same_origin(PEER_URL, a) is False
    for a in junk:
        for b in junk:
            assert a2a_client._same_origin(a, b) is False


def test_I7_validate_endpoint_maps_every_failure_to_a_named_call_error(monkeypatch):
    """`validate_endpoint` — the off-the-event-loop wrapper.

    It exists so a stalling resolver cannot freeze a worker, and its error
    mapping is what keeps that decision invisible to the caller: everything it
    can raise must arrive as an `A2ACallError` with a stable reason, or the
    router's error map never sees it.
    """
    # 1. The underlying validator's own named refusal is forwarded verbatim.
    def _refuse(url):
        raise A2AEndpointUrlError("endpoint_private_address", "nope")

    monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url", _refuse)
    with pytest.raises(A2ACallError) as exc:
        asyncio.run(a2a_client.validate_endpoint(PEER_URL))
    assert exc.value.reason == "endpoint_private_address"

    # 2. A bare ValueError (a shape the validator can still raise) becomes
    #    endpoint_invalid rather than escaping as a 500.
    def _boom(url):
        raise ValueError("something else")

    monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url", _boom)
    with pytest.raises(A2ACallError) as exc:
        asyncio.run(a2a_client.validate_endpoint(PEER_URL))
    assert exc.value.reason == "endpoint_invalid"

    # 3. A resolver that hangs past the DNS budget is a refusal, not a hang.
    import time as _time

    def _stall(url):
        _time.sleep(a2a_client.A2A_DNS_TIMEOUT + 0.5)
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(a2a_client, "A2A_DNS_TIMEOUT", 0.05)
    monkeypatch.setattr(a2a_client, "validate_a2a_endpoint_url", lambda url: _time.sleep(0.5))
    with pytest.raises(A2ACallError) as exc:
        asyncio.run(a2a_client.validate_endpoint(PEER_URL))
    assert exc.value.reason == "endpoint_dns_failure"


def test_I8_a_transport_timeout_is_the_timeout_reason_not_unreachable():
    """`_read_capped`'s `httpx.TimeoutException` arm, which must be caught
    BEFORE the generic `httpx.HTTPError` arm — `TimeoutException` is a subclass,
    so reordering them silently reclassifies every slow peer as `*_unreachable`
    and the router stops answering 504."""
    def _timeout(request):
        raise httpx.ReadTimeout("too slow", request=request)

    transport = httpx.MockTransport(_timeout)

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await a2a_client._read_capped(
                client, "GET", "https://1.2.3.4/x", sni="h.example",
                host_header="h.example", max_bytes=1000, headers={},
                error_prefix="card",
            )

    with pytest.raises(A2ACallError) as exc:
        asyncio.run(_run())
    assert exc.value.reason == "timeout"


@pytest.mark.parametrize(
    "body,expected",
    [
        (b"not json", "rpc_invalid"),
        (b"", "rpc_invalid"),
        (b"[1,2]", "rpc_invalid"),      # valid JSON, not an object
        (b'"str"', "rpc_invalid"),
        (b"null", "rpc_invalid"),
    ],
)
def test_I9_a_non_object_rpc_body_is_named_not_raised(body, expected):
    """`_rpc`'s two decode arms — the mirror of `fetch_card`'s, on the hop that
    carries the credential."""
    transport = httpx.MockTransport(lambda request: _streaming(200, body))

    async def _run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await a2a_client._rpc(
                client, _validated(), PEER_URL, "sekret", "message/send", {}
            )

    with pytest.raises(A2ACallError) as exc:
        asyncio.run(_run())
    assert exc.value.reason == expected


def test_I10_a_status_message_is_read_when_there_are_no_artifacts():
    """`_parse_task`'s `status.message` arm — how a peer answers a task that
    produced prose rather than an artifact. Ordering matters: artifacts first,
    then the status message, so a task with both does not lose its artifact."""
    only_status = {
        "status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "done"}]}},
    }
    assert a2a_client._parse_task(only_status) == ("completed", "done", None, None)

    both = {
        "status": {"state": "completed", "message": {"parts": [{"kind": "text", "text": "note"}]}},
        "artifacts": [{"parts": [{"kind": "text", "text": "payload"}]}],
    }
    state, text, _, _ = a2a_client._parse_task(both)
    assert text == "payload\nnote", "artifacts must come first and neither may be dropped"


# =========================================================================== #
# G7 + delete semantics — the id/name namespace collision (#2174, ent#395)
#
# Landed on `dev` in #2177 while this sweep was open, which is why F4 above is
# gone rather than xfailed. Kept verbatim (own `oss_store` fixture and
# constants) so the shipped fix keeps its own regression tests — including the
# create-time guard that now refuses this collision at the source.
# =========================================================================== #
DELETE_PEER_URL = "https://peer.example.com/a2a"
DELETE_PEER = ResolvedEndpoint(
    id="peer", name="peer", url=DELETE_PEER_URL, credential=None
)


@pytest.fixture
def oss_store(monkeypatch):
    """A real envelope round-trip over an in-memory settings row."""
    import secrets

    import database

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    store = {}
    monkeypatch.setattr(database.db, "get_setting_value",
                        lambda key, default=None: store.get(key, default), raising=False)
    monkeypatch.setattr(database.db, "set_setting",
                        lambda key, value: store.__setitem__(key, value), raising=False)
    monkeypatch.setattr("utils.url_validation.validate_a2a_endpoint_url", lambda url: DELETE_PEER)
    a2a_outbound.clear_provider()
    return store


def _names(records=None) -> list:
    return [r["name"] for r in (records if records is not None else a2a_outbound.list_oss_endpoints())]


# ---------------------------------------------------------------------------
# G7 — the reported defect
# ---------------------------------------------------------------------------

def test_G7_removing_one_endpoint_removes_exactly_one_endpoint(oss_store):
    """The repro from #2174, verbatim: three endpoints, one delete, two gone.

    `alpha`'s id is a legal name for a third endpoint (the admin GET shows ids,
    and `upsert_endpoint` only enforced uniqueness on name), so a delete by that
    id matched two records and destroyed both — including a credential.
    """
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL, "alpha-secret")
    a2a_outbound.upsert_endpoint("beta", DELETE_PEER_URL, "beta-secret")
    # Pre-existing collision: stored before the #2174 create-time guard, or by any
    # writer that is not `upsert_endpoint`. The delete must be safe regardless.
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": DELETE_PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    assert len(a2a_outbound.list_oss_endpoints()) == 3

    assert a2a_outbound.remove_endpoint(alpha["id"]) is True

    remaining = a2a_outbound.list_oss_endpoints()
    assert len(remaining) == 2, "a single-target delete removed more than one endpoint"
    assert "beta" in _names(remaining), "an unrelated endpoint was destroyed"
    # First-match-wins: alpha was registered first, so the id-matched record goes
    # and the name-matched one survives — with its credential.
    assert alpha["id"] in _names(remaining)
    survivor = a2a_outbound.resolve_endpoint("bot", "a2aep_collider01")
    assert survivor is not None and survivor.credential == "gamma-secret"


def test_delete_agrees_with_resolve_about_which_record_a_ref_means(oss_store):
    """The invariant behind the fix: whatever a ref RESOLVES to is what it DELETES.

    Checked against the store's own order rather than an assumed one, so it holds
    whichever way a collision was created."""
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": DELETE_PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    ref = alpha["id"]
    resolved = a2a_outbound.resolve_endpoint("bot", ref)
    assert resolved is not None
    doomed_id = resolved.id

    assert a2a_outbound.remove_endpoint(ref) is True
    surviving_ids = {r["id"] for r in a2a_outbound.list_oss_endpoints()}
    assert doomed_id not in surviving_ids, "delete removed a different record than resolve returned"
    assert len(surviving_ids) == 1


def test_a_second_delete_of_the_same_ref_removes_the_survivor(oss_store):
    """Bounded per call, not per ref: the operator can still clear a collision —
    it just takes one deliberate delete each, which is the point."""
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": DELETE_PEER_URL})
    a2a_outbound._store_endpoint_records(records)

    assert a2a_outbound.remove_endpoint(alpha["id"]) is True
    assert a2a_outbound.remove_endpoint(alpha["id"]) is True
    assert a2a_outbound.list_oss_endpoints() == []
    assert a2a_outbound.remove_endpoint(alpha["id"]) is False


# ---------------------------------------------------------------------------
# The ordinary paths the fix must not disturb
# ---------------------------------------------------------------------------

def test_removing_by_name_still_works(oss_store):
    a2a_outbound.upsert_endpoint("partner", DELETE_PEER_URL, "the-secret")
    assert a2a_outbound.remove_endpoint("partner") is True
    assert a2a_outbound.resolve_endpoint("bot", "partner") is None


def test_removing_by_name_is_case_insensitive(oss_store):
    a2a_outbound.upsert_endpoint("Partner", DELETE_PEER_URL)
    assert a2a_outbound.remove_endpoint("pArTnEr") is True
    assert a2a_outbound.list_oss_endpoints() == []


def test_removing_by_id_still_works(oss_store):
    record = a2a_outbound.upsert_endpoint("partner", DELETE_PEER_URL)
    assert a2a_outbound.remove_endpoint(record["id"]) is True
    assert a2a_outbound.list_oss_endpoints() == []


def test_removing_leaves_every_other_endpoint_alone(oss_store):
    a2a_outbound.upsert_endpoint("one", DELETE_PEER_URL, "s1")
    a2a_outbound.upsert_endpoint("two", DELETE_PEER_URL, "s2")
    a2a_outbound.upsert_endpoint("three", DELETE_PEER_URL, "s3")
    assert a2a_outbound.remove_endpoint("two") is True
    assert _names() == ["one", "three"]
    assert a2a_outbound.resolve_endpoint("bot", "one").credential == "s1"
    assert a2a_outbound.resolve_endpoint("bot", "three").credential == "s3"


@pytest.mark.parametrize("ref", ["", "   ", "\t\n", "nope", "a2aep_doesnotexist"])
def test_a_miss_or_an_empty_ref_returns_false_and_changes_nothing(oss_store, ref):
    a2a_outbound.upsert_endpoint("partner", DELETE_PEER_URL, "keep-me")
    before = oss_store.get(a2a_outbound.A2A_ENDPOINTS_SETTING)

    assert a2a_outbound.remove_endpoint(ref) is False

    assert oss_store.get(a2a_outbound.A2A_ENDPOINTS_SETTING) == before, \
        "a no-op delete rewrote the stored envelope"
    assert a2a_outbound.resolve_endpoint("bot", "partner").credential == "keep-me"


def test_removing_from_an_empty_store_is_false_not_an_error(oss_store):
    assert a2a_outbound.remove_endpoint("anything") is False


# ---------------------------------------------------------------------------
# The create-time guard: stop new collisions at the source (#2174, additive)
# ---------------------------------------------------------------------------

def test_a_new_endpoint_may_not_be_named_after_an_existing_id(oss_store):
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL, "alpha-secret")
    with pytest.raises(a2a_outbound.EndpointValidationError) as e:
        a2a_outbound.upsert_endpoint(alpha["id"], DELETE_PEER_URL, "gamma-secret")
    assert "id of another registered endpoint" in str(e.value)
    assert len(a2a_outbound.list_oss_endpoints()) == 1


def test_the_guard_is_case_insensitive(oss_store):
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL)
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint(alpha["id"].upper(), DELETE_PEER_URL)


def test_an_already_stored_collision_stays_editable(oss_store):
    """The guard must not strand an operator in the state it exists to prevent:
    a collision written before it shipped is still updatable and removable.

    Note the deliberate asymmetry this pins. `upsert_endpoint` is **update-by-name**
    (its shipped contract, matching the enterprise registration path), so passing
    the colliding string edits the record NAMED that. `resolve_endpoint` /
    `remove_endpoint` are **by id or name**, so the same string reaches the
    id-owning record first. Both are documented and neither is destructive; the
    create-time guard above is what stops the state arising in the first place.
    """
    alpha = a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL, "alpha-secret")
    records = a2a_outbound._load_endpoint_records()
    records.append({"id": "a2aep_collider01", "name": alpha["id"], "url": DELETE_PEER_URL,
                    "credential": "gamma-secret"})
    a2a_outbound._store_endpoint_records(records)

    updated = a2a_outbound.upsert_endpoint(alpha["id"], "https://peer.example.com/a2a/moved")

    assert updated["id"] == "a2aep_collider01", "update-by-name edited the record named that"
    assert len(a2a_outbound.list_oss_endpoints()) == 2, "an update minted a new record"
    # alpha is untouched: same url, credential intact.
    still_alpha = a2a_outbound.resolve_endpoint("bot", "alpha")
    assert still_alpha.url == DELETE_PEER_URL and still_alpha.credential == "alpha-secret"
    # ...and the collision is removable, one deliberate delete at a time.
    assert a2a_outbound.remove_endpoint("a2aep_collider01") is True
    assert _names() == ["alpha"]


def test_an_ordinary_name_is_unaffected_by_the_guard(oss_store):
    a2a_outbound.upsert_endpoint("alpha", DELETE_PEER_URL)
    a2a_outbound.upsert_endpoint("beta", DELETE_PEER_URL)
    a2a_outbound.upsert_endpoint("a2aep-not-an-id", DELETE_PEER_URL)
    assert _names() == ["alpha", "beta", "a2aep-not-an-id"]
