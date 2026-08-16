"""SSRF gate on the template-registry URL (trinity-enterprise#14).

The registry URL is admin-and-human-set, but it is still the one place an
operator hands Trinity an arbitrary host to connect to, so the destination is
resolved and refused before it is ever stored. This file pins that gate — it had
been verified only by hand, which is how S3 below survived review.

**S3 — RFC 6598 shared address space (CGNAT).** `100.64.0.0/10` is reported by
Python's `ipaddress` as neither `is_private` nor `is_reserved`, so the standard
predicate stack admitted it. Harmless in Trinity's own topology (both Docker
networks are `172.28/16` and `172.29/16`, which ARE `is_private`) and a real hole
on the cloud providers that address internal endpoints out of that range.

**ent#393 — the mapped form of S3.** Because CGNAT is the one range refused by
Trinity's own clause rather than by the interpreter, it is also the one whose
IPv4-MAPPED form had to be handled explicitly: the clause was written
`ip.version == 4 and ip in _SHARED_ADDRESS_SPACE`, and `::ffff:100.64.0.1`
reports version 6. Every neighbouring range below survives its mapped form for
free, because CPython delegates `is_private`/`is_reserved` through `ipv4_mapped`
before consulting its IPv6 tables — which is exactly why S3 was the one that
drifted. The fix resolves the v4 view once and tests membership against that.

DNS is stubbed rather than dialled: a test that resolves real names is a test
that fails on an aeroplane, and the property under test is the predicate, not
the resolver.
"""

import importlib.util
import os
import socket
import sys

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
sys.path.insert(0, _backend_path)

# tests/utils shadows src/backend/utils — load the backend module directly, the
# same way `test_ssrf_skills_library.py` does.
_spec = importlib.util.spec_from_file_location(
    "backend_url_validation_ent14",
    os.path.join(_backend_path, "utils", "url_validation.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
validate = _mod.validate_template_registry_url

URL = "https://registry.example.com/registry.yaml"


@pytest.fixture
def resolves(monkeypatch):
    """Point every hostname at a chosen set of addresses."""

    def _set(*addresses):
        def _getaddrinfo(host, port, *a, **k):
            out = []
            for addr in addresses:
                if ":" in addr:
                    out.append(
                        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0))
                    )
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
            return out

        monkeypatch.setattr(_mod.socket, "getaddrinfo", _getaddrinfo)

    return _set


# ---------------------------------------------------------------------------
# S3: RFC 6598 shared address space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "100.64.0.0",  # first address in the range
        "100.64.0.1",
        "100.100.100.100",
        "100.127.255.255",  # last address in the range
    ],
)
def test_cgnat_addresses_are_refused(resolves, address):
    resolves(address)
    with pytest.raises(ValueError, match="internal address"):
        validate(URL)


@pytest.mark.parametrize(
    "address",
    [
        "::ffff:100.64.0.0",  # first address in the range
        "::ffff:100.64.0.1",
        "::ffff:6440:1",  # the same address written in hex, not dotted-quad
        "::ffff:100.100.100.100",
        "::ffff:100.127.255.255",  # last address in the range
    ],
)
def test_mapped_cgnat_addresses_are_refused(resolves, address):
    """ent#393. The registry validator shares `_is_internal_address` with the
    A2A outbound path, so the version-gated CGNAT clause left BOTH open — an
    `AAAA` record of `::ffff:100.64.0.1` made this fetch a probe of whatever the
    provider hosts on RFC 6598 space.

    This is the one range where the mapped form had to be handled by Trinity:
    the rows in `test_internal_destinations_are_refused` below pass because
    CPython delegates `is_private`/`is_loopback` through `ipv4_mapped` before it
    consults its IPv6 tables, and CGNAT is deliberately absent from both.
    """
    resolves(address)
    with pytest.raises(ValueError, match="internal address"):
        validate(URL)


@pytest.mark.parametrize(
    "address",
    [
        "100.63.255.255",
        "100.128.0.1",
        "99.64.0.1",
        "101.64.0.1",
        # ent#393: the same boundaries through the mapped path, which is now a
        # second way into the CGNAT clause and so a second way to widen it by
        # accident.
        "::ffff:100.63.255.255",
        "::ffff:100.128.0.1",
        "::ffff:99.64.0.1",
        "::ffff:101.64.0.1",
    ],
)
def test_the_boundaries_either_side_stay_public(resolves, address):
    """/10, not /8. Refusing `100.0.0.0/8` would blackhole a large slice of the
    real public internet — the failure would be an operator who cannot configure
    a legitimate registry, with a message telling them their host is internal."""
    resolves(address)
    assert validate(URL) == URL


def test_one_cgnat_record_among_public_ones_is_enough_to_refuse(resolves):
    """Every returned record is checked, not just the first: a host that
    resolves to both a public address and an internal one is a DNS-level
    smuggle, and picking a record is the resolver's choice, not ours."""
    resolves("93.184.216.34", "100.64.0.1")
    with pytest.raises(ValueError, match="internal address"):
        validate(URL)


# ---------------------------------------------------------------------------
# The neighbouring ranges the CGNAT clause sits beside — pinned so a future
# refactor of the predicate stack cannot quietly drop one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,what",
    [
        ("127.0.0.1", "loopback"),
        ("10.0.0.1", "private"),
        ("172.28.0.5", "the agent network"),
        ("172.29.0.5", "the platform network"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "cloud metadata"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("::1", "IPv6 loopback"),
        ("fc00::1", "IPv6 unique-local"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:10.0.0.1", "IPv4-mapped private"),
    ],
)
def test_internal_destinations_are_refused(resolves, address, what):
    resolves(address)
    with pytest.raises(ValueError, match="internal address"):
        validate(URL)


def test_a_public_destination_is_accepted(resolves):
    resolves("93.184.216.34")
    assert validate(URL) == URL


# ---------------------------------------------------------------------------
# The non-DNS half of the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.example.com/r.yaml",
        "file:///etc/passwd",
        "gopher://registry.example.com/r.yaml",
        "ftp://registry.example.com/r.yaml",
    ],
)
def test_non_https_schemes_are_refused(resolves, url):
    resolves("93.184.216.34")
    with pytest.raises(ValueError):
        validate(url)


def test_embedded_credentials_are_refused_not_stripped(resolves):
    """Rejected outright so a token can never be persisted into a
    `system_settings` row or echoed back through the status payload. Silently
    stripping would be worse — the operator would believe it was in use."""
    resolves("93.184.216.34")
    with pytest.raises(ValueError, match="credentials"):
        validate("https://user:ghp_secret@registry.example.com/r.yaml")


def test_an_unresolvable_host_is_refused(monkeypatch):
    """Not a pass-through: a registry host that cannot resolve is a URL that
    cannot work, and storing it yields a setting that silently never fetches."""

    def _boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(_mod.socket, "getaddrinfo", _boom)
    with pytest.raises(ValueError, match="could not be resolved"):
        validate(URL)


# ---------------------------------------------------------------------------
# Why the sibling validator does NOT get the same clause.
#
# `validate_skills_library_url` has no CGNAT check and must not grow one as a
# "consistency" fix. The two gates defend different things: this one lets the
# operator name ANY host, so the destination has to be vetted; that one pins the
# host to an allowlist, so the operator never chooses a destination at all and
# the address it resolves to is not an operator-controlled input.
#
# Pinned because the reasoning is invisible at the call site — the next reader
# sees one validator with a `100.64.0.0/10` clause and one without, and the
# obvious "fix" is to copy it over. Copying it is harmless; deleting THIS one as
# redundant, having concluded the two should match, is not. What actually
# carries the argument is the allowlist below: if it were ever relaxed, the
# skills-library gate WOULD need the full predicate stack, and this test is
# where that turns from a judgement call into a failure.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "evil.com",
        "github.com.evil.com",  # suffix lookalike
        "notgithub.com",
        "raw.githubusercontent.com",  # a real GitHub host, still not allowlisted
    ],
)
def test_skills_library_is_allowlisted_by_host_not_by_address(monkeypatch, host):
    """The reason `validate_skills_library_url` needs no CGNAT clause: a host it
    does not recognise is refused on its NAME, before any address is consulted.
    Each of these resolves to a perfectly public address here — and is still
    refused, so the refusal cannot be coming from the address predicates."""

    def _public(*a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(_mod.socket, "getaddrinfo", _public)
    with pytest.raises(ValueError):
        _mod.validate_skills_library_url(f"https://{host}/owner/repo")


def test_skills_library_accepts_its_allowlisted_host(monkeypatch):
    """The other half — so the test above is proving an allowlist and not merely
    that the function refuses everything."""

    def _public(*a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.121.4", 0))]

    monkeypatch.setattr(_mod.socket, "getaddrinfo", _public)
    url = "https://github.com/owner/repo"
    assert _mod.validate_skills_library_url(url) == url
