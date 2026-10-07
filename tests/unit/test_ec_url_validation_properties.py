"""/edge-cases 2026-10-06 — property tests for `utils/url_validation.py` (slug `ec_url_validation`).

Companion to `test_ec_url_validation_edges.py` (named rows). Where that file pins
boundaries, this one asserts invariants over the input space. No DNS is dialled:
every resolver here is an in-memory double (see the edges file for the
`FaithfulResolver` rationale).

THE ORACLE IS INDEPENDENT OF THE STDLIB
---------------------------------------
`test_P2_ipv4_verdict_matches_an_independent_iana_oracle` recomputes "internal?"
from a hand-written copy of the IANA IPv4 Special-Purpose Address Registry
(Globally Reachable = False rows) plus multicast, NOT from `ipaddress`. A
membership-in-stdlib check would be a tautology: `_is_internal_address` IS six
stdlib predicates, so a CPython change to those tables (the #1891 class — the
image runs 3.13, a dev box may run 3.14) would move both sides together. This
oracle moves on its own, so a range silently dropped by either Trinity or the
interpreter fails here.

PROPERTIES
----------
P1  mapped equivalence — `internal(v4) == internal(::ffff:v4)` for every v4.
P2  the IANA oracle above.
P3  acceptance soundness — an accepted URL's `addresses` are EXACTLY the
    resolver's records, all public; `hostname` is the canonical host; port in
    1..65535; the resolver was asked once, with the canonical host.
P4  metamorphic — adding ONE internal record anywhere in a passing answer flips
    it to `private_address`; record order never changes the verdict.
P5  no-crash-total (fail-closed) — any text either validates or raises
    `ValueError`; a `PublicUrlRefusal` always carries a declared `kind`.
P6  the skills seam — whatever the write-path gate pair accepts, the URL
    `SkillService` clones is https on an allowlisted host, and the platform PAT
    is offered to nothing else.
P7  credential detection — a non-empty userinfo on any schemeful or scheme-less
    URL is refused by `reject_embedded_credentials`, and stripped by
    `strip_url_credentials` (the protocol-relative shape is the edges file's
    strict-xfail D3, deliberately excluded here so this property stays green).
P8  `canonical_host` is idempotent; `effective_port` is the identity on 1..65535.
P9  `canonical_origin_host` folds every spelling of one IP to one key.
"""

import ipaddress
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import utils.url_validation as uv  # noqa: E402

pytestmark = pytest.mark.unit

assert Path(uv.__file__).resolve() == (_BACKEND / "utils" / "url_validation.py").resolve()

_FAST = settings(max_examples=200, deadline=None)


# --------------------------------------------------------------------------- #
# Independent IANA oracle (IPv4 Special-Purpose Address Registry, Global=False)
# --------------------------------------------------------------------------- #
_IANA_NOT_GLOBAL_V4 = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
    "255.255.255.255/32",
]
# Inside 192.0.0.0/24 but registered Globally Reachable (PCP / TURN anycast).
_IANA_GLOBAL_EXCEPTIONS_V4 = ["192.0.0.9/32", "192.0.0.10/32"]
_MULTICAST_V4 = ["224.0.0.0/4"]  # refused by Trinity although IANA calls some global

_NOT_GLOBAL = [ipaddress.ip_network(n) for n in _IANA_NOT_GLOBAL_V4]
_EXCEPT = [ipaddress.ip_network(n) for n in _IANA_GLOBAL_EXCEPTIONS_V4]
_MCAST = [ipaddress.ip_network(n) for n in _MULTICAST_V4]


def _oracle_v4(addr: ipaddress.IPv4Address) -> bool:
    if any(addr in n for n in _MCAST):
        return True
    if any(addr in n for n in _EXCEPT):
        return False
    return any(addr in n for n in _NOT_GLOBAL)


def _edges():
    """Every range's first/last address and their outside neighbours."""
    out = set()
    for n in _NOT_GLOBAL + _EXCEPT + _MCAST:
        lo, hi = int(n.network_address), int(n.broadcast_address)
        for v in (lo - 1, lo, lo + 1, hi - 1, hi, hi + 1):
            if 0 <= v < 2**32:
                out.add(v)
    return sorted(out)


v4_ints = st.one_of(st.integers(0, 2**32 - 1), st.sampled_from(_edges()))


@_FAST
@given(v4_ints)
@example(int(ipaddress.IPv4Address("100.64.0.0")))
@example(int(ipaddress.IPv4Address("169.254.169.254")))
def test_P1_mapped_form_has_the_same_verdict(n):
    v4 = ipaddress.IPv4Address(n)
    mapped = ipaddress.IPv6Address("::ffff:" + str(v4))
    assert uv._is_internal_address(v4) == uv._is_internal_address(mapped)


@settings(max_examples=2000, deadline=None)
@given(v4_ints)
@example(int(ipaddress.IPv4Address("192.0.0.9")))
@example(int(ipaddress.IPv4Address("192.0.0.11")))
@example(int(ipaddress.IPv4Address("100.127.255.255")))
@example(int(ipaddress.IPv4Address("172.32.0.0")))
def test_P2_ipv4_verdict_matches_an_independent_iana_oracle(n):
    v4 = ipaddress.IPv4Address(n)
    assert uv._is_internal_address(v4) == _oracle_v4(v4), str(v4)


# --------------------------------------------------------------------------- #
# P3/P4/P5 — the shared public-HTTPS gate
# --------------------------------------------------------------------------- #
def _rec(addr):
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    sa = (addr, 0, 0, 0) if ":" in addr else (addr, 0)
    return (fam, socket.SOCK_STREAM, 6, "", sa)


def _shared(url, resolver):
    return uv._validate_public_https_url(
        url, label="L", host_label="H", credential_advice="C",
        internal_advice="I", resolver=resolver,
    )


public_v4 = st.integers(0, 2**32 - 1).map(ipaddress.IPv4Address).filter(
    lambda a: not _oracle_v4(a)).map(str)
public_v6 = st.sampled_from(
    ["2606:4700:4700::1111", "2001:4860:4860::8888", "2a00:1450:4001::200e"])
internal_addr = st.one_of(
    st.integers(0, 2**32 - 1).map(ipaddress.IPv4Address).filter(_oracle_v4).map(str),
    st.sampled_from([
        "::1", "::", "fd00:ec2::254", "fe80::1", "ff02::1", "::ffff:169.254.169.254",
        "::ffff:100.64.0.1", "64:ff9b::a9fe:a9fe", "2002:7f00:1::",
    ]),
)
label = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12).filter(
    lambda s: not s.startswith("-") and not s.endswith("-"))
hostnames = st.lists(label, min_size=2, max_size=4).map(".".join)
ports = st.one_of(st.none(), st.integers(0, 65535))
paths = st.sampled_from(["", "/", "/a2a", "/x/y?q=1", "/r.yaml#frag"])


def _url(host, port, path):
    return f"https://{host}{'' if port is None else f':{port}'}{path}"


@_FAST
@given(hostnames, ports, paths, st.lists(st.one_of(public_v4, public_v6), min_size=1, max_size=5))
def test_P3_acceptance_is_sound(host, port, path, records):
    calls = []

    def resolver(h, p, *a, **k):
        calls.append((h, p))
        return [_rec(r) for r in records]

    out = _shared(_url(host, port, path), resolver)
    assert out.addresses == tuple(str(ipaddress.ip_address(r)) for r in records)
    assert not any(uv._is_internal_address(ipaddress.ip_address(a)) for a in out.addresses)
    assert out.hostname == uv.canonical_host(host)
    assert 1 <= out.port <= 65535
    assert out.port == (port or 443)
    assert calls == [(out.hostname, out.port)]


@_FAST
@given(hostnames, st.lists(st.one_of(public_v4, public_v6), max_size=4), internal_addr,
       st.integers(0, 4), st.randoms(use_true_random=False))
@example("peer.example.com", ["93.184.216.34"], "169.254.169.254", 1, None)
def test_P4_one_internal_record_anywhere_refuses_and_order_is_irrelevant(
        host, public, bad, pos, rnd):
    records = list(public)
    records.insert(min(pos, len(records)), bad)
    if rnd is not None:
        rnd.shuffle(records)
    with pytest.raises(uv.PublicUrlRefusal) as exc:
        _shared(f"https://{host}/", lambda *a, **k: [_rec(r) for r in records])
    assert exc.value.kind == "private_address"
    assert bad not in str(exc.value)


url_soup = st.lists(
    st.sampled_from(
        list("aZ09.-_:/@#?%[]\\ \t\r\n\x00。．") +
        ["https://", "HTTPS://", "//", "http://", "peer.example.com", "[::1]", "[fe80::1%25x]",
         "127.0.0.1", "0x7f.1", "2130706433", ":0", ":65536", "xn--", "ｐ", "а"]),
    max_size=14,
).map("".join)


@_FAST
@given(st.one_of(url_soup, st.text(max_size=60)))
@example("https://a..example.com/")
@example("https://" + "a" * 64 + ".example.com/")
def test_P5_total_and_fail_closed(url):
    """Every input either validates or raises a ValueError. NOT asserted:
    "always a PublicUrlRefusal" — that is the edges file's strict-xfail C12."""

    def faithful(h, p, *a, **k):
        try:
            return [_rec(str(ipaddress.ip_address(h)))]
        except ValueError:
            pass
        try:
            return [_rec(socket.inet_ntoa(socket.inet_aton(h)))]
        except (OSError, ValueError):
            pass
        h.encode("idna")
        if h == "peer.example.com":
            return [_rec("93.184.216.34")]
        raise socket.gaierror(socket.EAI_NONAME, "nx")

    try:
        out = _shared(url, faithful)
    except uv.PublicUrlRefusal as exc:
        assert exc.kind in uv.PUBLIC_URL_REFUSAL_KINDS
        return
    except ValueError:
        return
    assert out.addresses and not any(
        uv._is_internal_address(ipaddress.ip_address(a)) for a in out.addresses)
    assert urlparse(out.url).scheme == "https"


# --------------------------------------------------------------------------- #
# P6 — validate_skills_library_url → SkillService (validator says X, caller fetches Y)
# --------------------------------------------------------------------------- #
skills_soup = st.lists(
    st.sampled_from(
        list("ab/:.@#?\\ %") +
        ["github.com", "www.github.com", "GitHub.com", "https://", "http://", "//", "evil.com",
         "127.0.0.1", "localhost", "o/r", "file:", "[::1]", "ｇ", "。", "%40", ":443"]),
    min_size=1, max_size=10,
).map("".join)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(skills_soup)
@example("127.0.0.1/o/r")
@example("//evil.com/o/r")
@example("github.com@evil.com/x")
@example("evil.com\\@github.com/o/r")
@example("https:/evil.com/x")
def test_P6_the_skills_clone_target_is_always_github(monkeypatch, raw):
    import services.skill_service as ss

    monkeypatch.setattr(
        uv.socket, "getaddrinfo",
        lambda *a, **k: [_rec("140.82.121.4")],
    )
    # The gate pair every write path runs, in the routers' order
    # (`routers/skills.py:941-945`, `skill_service.py:825-826`). A bare
    # ValueError out of `reject_embedded_credentials` (a stray `[`) is the edges
    # file's strict-xfail D4: it 500s the route, so such a row is never stored —
    # which is why it counts as "refused" here rather than as a P6 counterexample
    # (`_normalized_url` would raise on it in the sync loop).
    try:
        uv.reject_embedded_credentials(raw)
        validated = uv.validate_skills_library_url(raw)
    except ValueError:
        return
    svc = ss.SkillService.__new__(ss.SkillService)
    clone_url, pat = svc._clone_target(validated, "ghp_TEST")
    parsed = urlparse(clone_url)
    host = (parsed.hostname or "").lower()
    assert parsed.scheme == "https", (raw, clone_url)
    assert host in uv.ALLOWED_SKILLS_LIBRARY_HOSTS, (raw, clone_url)
    if pat:
        assert "@" not in parsed.netloc, (raw, clone_url)


# --------------------------------------------------------------------------- #
# P7 — credential detection
# --------------------------------------------------------------------------- #
userinfo = st.text(alphabet="abcXYZ019_-.~:!$&'()*+,;=", min_size=1, max_size=20).filter(
    lambda s: s.strip(":") != "")


@_FAST
@given(st.sampled_from(["https://", "http://", "HTTPS://", "git+ssh://", ""]), userinfo,
       st.sampled_from(["github.com", "github.com:8443", "example.org"]),
       st.sampled_from(["/o/r", "/o/r?x=a@b", ""]))
@example("", "tok", "github.com", "/o/r")
def test_P7_non_empty_userinfo_is_refused_and_stripped(prefix, info, host, tail):
    url = f"{prefix}{info}@{host}{tail}"
    with pytest.raises(uv.EmbeddedCredentialError):
        uv.reject_embedded_credentials(url)
    stripped = uv.strip_url_credentials(url)
    parsed = urlparse(stripped if "://" in stripped else "https://" + stripped)
    # The netloc, not the whole string: `info@` may legitimately recur in the
    # query (`?x=a@b`), which is data, not userinfo.
    assert "@" not in parsed.netloc, stripped
    assert parsed.hostname == host.split(":")[0]


# --------------------------------------------------------------------------- #
# P8/P9 — normalisers
# --------------------------------------------------------------------------- #
@_FAST
@given(st.one_of(st.text(max_size=40), hostnames, hostnames.map(str.upper),
                 hostnames.map(lambda h: h + "."), st.sampled_from(["ｐｅｅｒ.com", "faß.de", "a..b"])))
def test_P8_canonical_host_is_idempotent(h):
    once = uv.canonical_host(h)
    if once is not None:
        assert uv.canonical_host(once) == once


@_FAST
@given(st.integers(1, 65535), st.sampled_from(["https", "HTTPS", "http", "wss", ""]))
def test_P8_effective_port_is_identity_on_real_ports(port, scheme):
    assert uv.effective_port(port, scheme) == port


@_FAST
@given(st.one_of(
    st.integers(0, 2**32 - 1).map(ipaddress.IPv4Address),
    st.integers(0, 2**128 - 1).map(ipaddress.IPv6Address),
))
def test_P9_every_spelling_of_one_ip_is_one_origin_key(ip):
    spellings = {str(ip), str(ip).upper()}
    if ip.version == 6:
        spellings |= {ip.exploded, ip.exploded.upper(), f"[{ip.compressed}]"}
    keys = {uv.canonical_origin_host(s) for s in spellings}
    assert keys == {str(ip)}
