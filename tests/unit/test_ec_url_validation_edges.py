"""/edge-cases 2026-10-06 — `utils/url_validation.py`, the SSRF gates (slug `ec_url_validation`).

Companion to the suites that already pin this module's headline behaviour:
`test_ssrf_skills_library.py` (SEC-179), `test_ent14_registry_url_ssrf.py`
(CGNAT + mapped CGNAT), `test_736_a2a_url_validation.py` / `_edges.py` (the
shared public-HTTPS gate, IPv6 transition forms, length cap, CRLF, ports),
`test_ent397_refusal_reasons.py`, `test_ent398_port_normalisation.py`,
`test_ent399_ipv6_origin.py` and `test_2052_scrubber_authority_parity.py`. This
file works the rows those suites leave open (full matrix:
the 2026-10-06 /edge-cases matrix):

* **A — `validate_skills_library_url`**: IP-literal spellings (decimal / octal /
  hex / short form) and userinfo `@` tricks refused on the NAME before any
  resolution; metadata / ULA / scoped link-local resolution refused; and the
  seam the validator itself cannot see — its `owner/repo` shorthand branch
  returns ANY slash-bearing string unvalidated, so the only thing standing
  between `127.0.0.1/x` and a clone of 127.0.0.1 is the caller,
  `SkillService._normalized_url`. "The validator says X" is pinned TOGETHER with
  "the caller then fetches Y" here.
* **B — `_is_internal_address`**: exact range BOUNDARIES (the shipped tests probe
  one address per range, which passes equally against a `/8` written as `/16`),
  the stdlib's `192.0.0.9`/`.10` global exceptions, the IPv6 metadata address
  `fd00:ec2::254`, and v6 multicast / unspecified.
* **C — `_validate_public_https_url`**: encoded IPv4 hosts are vetted on what the
  resolver RETURNS (the validator never parses the literal itself, and must not
  need to); bracketed v6 literals; zone ids; `@` inside fragment/query is NOT
  userinfo; the resolver is handed the CANONICAL host, never the raw text.
* **D — `reject_embedded_credentials`**: the protocol-relative `//tok@host` shape.

DNS is never dialled. The resolver doubles here model CPython's
`socket.getaddrinfo` faithfully where it matters: an IPv4 *text* goes through
`inet_aton` (which is what accepts `2130706433`, `0x7f.1`, `0177.0.0.1`), and a
name goes through the `idna` codec first (which is where CPython raises
`UnicodeError` on an empty or over-long label, BEFORE any network call).

REAL BUGS (strict xfail; each reason names its GitHub issue):
* D3 — FIXED (#3323): `reject_embedded_credentials("//tok@github.com/o/r")`
  did not raise, and `validate_skills_library_url` returns it verbatim, so the
  token was persisted to `skill_sources.url` and to the audit row. Now a plain
  regression test.
* C-xfail — a host the `idna` codec refuses (`a..b`, a 64-char label) escapes
  `_validate_public_https_url` as a bare `UnicodeError`, not a
  `PublicUrlRefusal` carrying a `kind`.
"""

import ipaddress
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import utils.url_validation as uv  # noqa: E402

pytestmark = pytest.mark.unit

assert Path(uv.__file__).resolve() == (_BACKEND / "utils" / "url_validation.py").resolve(), (
    "tests/utils shadowed src/backend/utils — this file would test the wrong module"
)

PUBLIC_V4 = "93.184.216.34"
GITHUB_V4 = "140.82.121.4"


# --------------------------------------------------------------------------- #
# Resolver doubles
# --------------------------------------------------------------------------- #
def _record(addr, port=0):
    if ":" in addr:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, port, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))


class FaithfulResolver:
    """`getaddrinfo` as CPython + libc behave, minus the network.

    * literal IPv6 / IPv4 text → itself (IPv4 via `inet_aton`, the libc parser
      that accepts the decimal / octal / hex / short spellings);
    * otherwise the host is encoded with the `idna` codec first — CPython's
      socket module does exactly this, and it raises `UnicodeError` on an empty
      or over-63-char label before any lookup;
    * then a fixed name table; anything else is NXDOMAIN (`gaierror`).
    """

    def __init__(self, names=None):
        self.names = dict(names or {})
        self.calls = []

    def __call__(self, host, port, *a, **k):
        self.calls.append(host)
        try:
            return [_record(str(ipaddress.ip_address(host)), port)]
        except ValueError:
            pass
        try:
            return [_record(socket.inet_ntoa(socket.inet_aton(host)), port)]
        except OSError:
            pass
        host.encode("idna")  # CPython's own step; raises UnicodeError
        if host in self.names:
            return [_record(a, port) for a in self.names[host]]
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _shared(url, resolver):
    return uv._validate_public_https_url(
        url,
        label="L",
        host_label="H",
        credential_advice="CRED",
        internal_advice="INTERNAL",
        resolver=resolver,
    )


@pytest.fixture
def gh_dns(monkeypatch):
    """`validate_skills_library_url` resolves through the module-global socket."""
    res = FaithfulResolver({"github.com": [GITHUB_V4], "www.github.com": [GITHUB_V4]})
    monkeypatch.setattr(uv.socket, "getaddrinfo", res)
    return res


# =========================================================================== #
# A. validate_skills_library_url
# =========================================================================== #
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://2130706433/o/r", id="A1-decimal"),
        pytest.param("https://0x7f000001/o/r", id="A1-hex"),
        pytest.param("https://0177.0.0.1/o/r", id="A1-octal"),
        pytest.param("https://0x7f.1/o/r", id="A1-mixed-short"),
        pytest.param("https://127.1/o/r", id="A1-short"),
        pytest.param("https://[::ffff:7f00:1]/o/r", id="A1-mapped-hex"),
        pytest.param("https://[fd00:ec2::254]/latest", id="A1-aws-v6-metadata"),
        pytest.param("https://github.com@evil.com/o/r", id="A2-userinfo-host"),
        pytest.param("https://github.com:443@evil.com/o/r", id="A2-userinfo-port-lookalike"),
        pytest.param("https://github.com\\@evil.com/o/r", id="A2-backslash-at"),
        pytest.param("https://a@b@evil.com/o/r", id="A2-double-at"),
        pytest.param("https://evil.com#@github.com/o/r", id="A2-fragment-at"),
        pytest.param("https://evil.com?@github.com/o/r", id="A2-query-at"),
        pytest.param("https://github.com%40evil.com/o/r", id="A2-pct-at"),
        pytest.param("https://github.com./o/r", id="A4-trailing-dot"),
        pytest.param("https://ｇｉｔｈｕｂ.com/o/r", id="A5-fullwidth"),
        pytest.param("https://xn--gthub-cta.com/o/r", id="A5-punycode-lookalike"),
        pytest.param("https://gіthub.com/o/r", id="A5-cyrillic-i"),
        pytest.param("https:///o/r", id="A11-empty-host"),
        pytest.param("https://:443/o/r", id="A11-port-only"),
    ],
)
def test_A_non_github_hosts_are_refused_on_the_name_before_resolution(gh_dns, url):
    """The skills validator is an ALLOWLIST: a host it does not recognise is
    refused before `getaddrinfo` is consulted at all, so no IP spelling — and no
    rebinding answer — can be the reason it passes."""
    with pytest.raises(ValueError):
        uv.validate_skills_library_url(url)
    assert gh_dns.calls == [], "the allowlist must refuse before any resolution"


@pytest.mark.parametrize(
    "url,host",
    [
        pytest.param("https://evil.com@github.com/o/r", "github.com", id="A3-userinfo-then-github"),
        pytest.param("https://github.com#@evil.com", "github.com", id="A3-fragment-at-is-data"),
        pytest.param("https://GitHub.COM/o/r", "github.com", id="A3-host-case"),
        pytest.param("https://www.github.com/o/r", "www.github.com", id="A3-www"),
    ],
)
def test_A3_accepted_urls_resolve_exactly_the_allowlisted_host(gh_dns, url, host):
    """Accepted ⇒ the host it vetted is the host `urlparse` reads; the `@`
    before it is userinfo (refused separately by `reject_embedded_credentials`)
    and the `@` after `#` is fragment data."""
    assert uv.validate_skills_library_url(url) == url
    assert gh_dns.calls == [host]


@pytest.mark.parametrize(
    "address",
    [
        pytest.param("169.254.169.254", id="A6-metadata-v4"),
        pytest.param("fd00:ec2::254", id="A6-metadata-v6-ula"),
        pytest.param("::ffff:169.254.169.254", id="A6-mapped-metadata"),
        pytest.param("::ffff:a9fe:a9fe", id="A6-mapped-metadata-hex"),
        pytest.param("fe80::1%eth0", id="A6-scoped-link-local"),
        pytest.param("10.0.0.1", id="A6-rfc1918"),
        pytest.param("0.0.0.0", id="A6-unspecified"),
        pytest.param("::1", id="A6-v6-loopback"),
    ],
)
def test_A6_github_resolving_internal_is_refused(monkeypatch, address):
    monkeypatch.setattr(
        uv.socket, "getaddrinfo", FaithfulResolver({"github.com": [address]})
    )
    with pytest.raises(ValueError, match="internal address"):
        uv.validate_skills_library_url("https://github.com/o/r")


def test_A6_one_internal_record_among_public_ones_refuses(monkeypatch):
    monkeypatch.setattr(
        uv.socket, "getaddrinfo",
        FaithfulResolver({"github.com": [GITHUB_V4, "169.254.169.254"]}),
    )
    with pytest.raises(ValueError, match="internal address"):
        uv.validate_skills_library_url("https://github.com/o/r")


def test_A7_dns_failure_is_deliberately_not_fatal_here(monkeypatch):
    """Documented divergence from the public gate: the target is a later
    `git clone` that fails loudly on its own."""
    monkeypatch.setattr(uv.socket, "getaddrinfo", FaithfulResolver({}))
    assert uv.validate_skills_library_url("https://github.com/o/r") == "https://github.com/o/r"


# --- A8/A9: the shorthand seam ------------------------------------------------
# `validate_skills_library_url` returns ANY scheme-less string containing `/`
# (not starting with `.`/`localhost`) WITHOUT validating it. That is safe only
# because the sole network consumer, `SkillService._normalized_url`, re-parses it
# and lands anything that is not an allowlisted host UNDER github.com. These rows
# pin the validator's pass AND the composed destination together, so a change to
# either half that opens the hole fails here.
_SHORTHAND_SEAM = [
    pytest.param("127.0.0.1/o/r", id="A8-loopback-literal"),
    pytest.param("169.254.169.254/latest/meta-data", id="A8-metadata-literal"),
    pytest.param("2130706433/x", id="A8-decimal-literal"),
    pytest.param("[::1]/x", id="A8-v6-literal"),
    pytest.param("//evil.com/o/r", id="A8-protocol-relative"),
    pytest.param("evil.com/o/r", id="A8-foreign-host"),
    pytest.param("LOCALHOST/x", id="A8-uppercase-localhost"),
    pytest.param("github.com@evil.com/x", id="A8-userinfo-lookalike"),
    pytest.param("github.com.evil.com/x", id="A8-suffix-lookalike"),
    pytest.param("file:/etc/passwd", id="A8-file-single-slash"),
    pytest.param("https:/evil.com/x", id="A8-https-single-slash"),
    pytest.param("https:evil.com/x", id="A8-https-no-slash"),
    pytest.param("evil.com:443/x", id="A8-host-port"),
    pytest.param("abilityai/skills", id="A8-honest-owner-repo"),
    pytest.param("github.com/o/r", id="A8-github-shorthand"),
    pytest.param("www.github.com/o/r", id="A8-www-shorthand"),
]


@pytest.mark.parametrize("raw", _SHORTHAND_SEAM)
def test_A8_shorthand_passes_the_validator_but_the_caller_only_ever_fetches_github(gh_dns, raw):
    import services.skill_service as ss

    validated = uv.validate_skills_library_url(raw)
    clone_url = ss.SkillService._normalized_url(validated)
    parsed = urlparse(clone_url)
    assert parsed.scheme == "https", clone_url
    assert (parsed.hostname or "").lower() in uv.ALLOWED_SKILLS_LIBRARY_HOSTS, clone_url


@pytest.mark.parametrize("raw", ["127.0.0.1/o/r", "evil.com/o/r", "abilityai/skills"])
def test_A9_the_bare_shorthand_branch_never_resolves(gh_dns, raw):
    """It returns before the DNS block — fine ONLY because of A8."""
    uv.validate_skills_library_url(raw)
    assert gh_dns.calls == []


@pytest.mark.parametrize("raw", ["localhost/x", "localhost:8000/x", "./x", "../x", "o", "github.com"])
def test_A10_the_few_shorthands_it_does_refuse(gh_dns, raw):
    with pytest.raises(ValueError):
        uv.validate_skills_library_url(raw)


@pytest.mark.parametrize("url", ["https://[::1/o/r", "https://[github.com/o/r", "https://github.com]/o/r"])
def test_A13_an_unbalanced_bracket_is_a_named_refusal(gh_dns, url):
    """`urlparse` raises on these; the validator maps it to its own message
    (`url_validation.py:297-300`) rather than letting the parser's text out."""
    with pytest.raises(ValueError, match="Invalid URL format"):
        uv.validate_skills_library_url(url)
    assert gh_dns.calls == []


def test_A12_the_platform_pat_is_only_offered_to_github_for_any_shorthand(gh_dns):
    """The credential half of the seam: `_clone_target` decides whether the
    platform PAT travels, by parsing the COMPOSED url."""
    import services.skill_service as ss

    svc = ss.SkillService.__new__(ss.SkillService)
    for p in _SHORTHAND_SEAM:
        raw = p.values[0]
        clone_url, pat = svc._clone_target(uv.validate_skills_library_url(raw), "ghp_TEST")
        host = (urlparse(clone_url).hostname or "").lower()
        if pat:
            assert host in uv.ALLOWED_SKILLS_LIBRARY_HOSTS, (raw, clone_url)


# =========================================================================== #
# B. _is_internal_address — range boundaries
# =========================================================================== #
_BOUNDARIES = [
    # (address, internal?)  — both sides of every edge
    ("9.255.255.255", False), ("10.0.0.0", True), ("10.255.255.255", True), ("11.0.0.0", False),
    ("126.255.255.255", False), ("127.0.0.0", True), ("127.255.255.255", True), ("128.0.0.0", False),
    ("169.253.255.255", False), ("169.254.0.0", True), ("169.254.255.255", True), ("169.255.0.0", False),
    ("172.15.255.255", False), ("172.16.0.0", True), ("172.31.255.255", True), ("172.32.0.0", False),
    ("192.167.255.255", False), ("192.168.0.0", True), ("192.168.255.255", True), ("192.169.0.0", False),
    ("100.63.255.255", False), ("100.64.0.0", True), ("100.127.255.255", True), ("100.128.0.0", False),
    ("198.17.255.255", False), ("198.18.0.0", True), ("198.19.255.255", True), ("198.20.0.0", False),
    ("223.255.255.255", False), ("224.0.0.0", True), ("239.255.255.255", True), ("240.0.0.0", True),
    ("255.255.255.255", True), ("0.0.0.0", True), ("0.255.255.255", True), ("1.0.0.0", False),
    # IANA marks these two inside 192.0.0.0/24 as GLOBAL (PCP / TURN anycast);
    # refusing them would be over-blocking, admitting their neighbours a hole.
    ("192.0.0.8", True), ("192.0.0.9", False), ("192.0.0.10", False), ("192.0.0.11", True),
    ("192.0.0.192", True),            # OCI metadata
    ("100.100.100.200", True),        # Alibaba metadata (CGNAT)
    ("fd00:ec2::254", True),          # AWS IPv6 metadata (ULA)
    ("fc00::", True), ("fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", True), ("fe00::", True),
    ("fe80::", True), ("febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", True),
    ("ff02::1", True), ("ff0e::1", True),  # v6 multicast, incl. global scope
    ("::", True), ("::1", True), ("::2", True),
    ("2606:4700:4700::1111", False), ("2001:4860:4860::8888", False),
]


@pytest.mark.parametrize("address,internal", _BOUNDARIES, ids=[a for a, _ in _BOUNDARIES])
def test_B_range_boundaries(address, internal):
    assert uv._is_internal_address(ipaddress.ip_address(address)) is internal


@pytest.mark.parametrize(
    "address,internal",
    [(a, i) for a, i in _BOUNDARIES if ":" not in a],
    ids=[f"mapped-{a}" for a, _ in _BOUNDARIES if ":" not in a],
)
def test_B_mapped_form_has_the_same_verdict_at_every_boundary(address, internal):
    """ent#393 generalised: every boundary, not only CGNAT's, through `::ffff:`."""
    assert uv._is_internal_address(ipaddress.ip_address("::ffff:" + address)) is internal


# =========================================================================== #
# C. _validate_public_https_url (the shared gate)
# =========================================================================== #
@pytest.mark.parametrize(
    "url,resolver_saw",
    [
        pytest.param("https://2130706433/", "2130706433", id="C1-decimal"),
        pytest.param("https://0x7f000001/", "0x7f000001", id="C1-hex"),
        pytest.param("https://0177.0.0.1/", "0177.0.0.1", id="C1-octal"),
        pytest.param("https://0x7f.1/", "0x7f.1", id="C1-mixed"),
        pytest.param("https://127.1/", "127.1", id="C1-short"),
        pytest.param("https://0/", "0", id="C1-zero"),
        pytest.param("https://2852039166/", "2852039166", id="C1-decimal-metadata"),
        pytest.param("https://0xa9fea9fe/", "0xa9fea9fe", id="C1-hex-metadata"),
        pytest.param("https://[::1]/", "::1", id="C2-v6-loopback"),
        pytest.param("https://[::]/", "::", id="C2-v6-unspecified"),
        pytest.param("https://[fd00:ec2::254]/", "fd00:ec2::254", id="C2-aws-v6-metadata"),
        pytest.param("https://[::ffff:169.254.169.254]/", "::ffff:169.254.169.254", id="C2-mapped-metadata"),
        pytest.param("https://[::ffff:a9fe:a9fe]/", "::ffff:a9fe:a9fe", id="C2-mapped-metadata-hex"),
        pytest.param("https://[::FFFF:7F00:1]:8443/", "::ffff:7f00:1", id="C2-upper-hex-with-port"),
    ],
)
def test_C1_C2_encoded_literals_are_vetted_on_what_the_resolver_returns(url, resolver_saw):
    """The validator never parses an IP literal itself — it hands the host to the
    resolver and vets every record it gets back. That is why `2130706433` needs
    no special case: libc turns it into 127.0.0.1 and the predicate refuses that.
    """
    res = FaithfulResolver()
    with pytest.raises(uv.PublicUrlRefusal) as exc:
        _shared(url, res)
    assert exc.value.kind == "private_address"
    assert res.calls == [resolver_saw]
    assert "127.0.0.1" not in str(exc.value) and "169.254" not in str(exc.value)


@pytest.mark.parametrize(
    "url,kind",
    [
        pytest.param("https://[fe80::1%25eth0]/", "invalid", id="C3-zone-id"),
        pytest.param("https://[1.2.3.4]/", "invalid", id="C3-bracketed-v4"),
        pytest.param("https://[peer.example.com]/", "invalid", id="C3-bracketed-name"),
        pytest.param("https://peer.example.com@127.0.0.1/", "credentials", id="C4-userinfo"),
        pytest.param("https://@peer.example.com/", "credentials", id="C4-empty-userinfo"),
        pytest.param("https://:@peer.example.com/", "credentials", id="C4-colon-userinfo"),
        pytest.param("https://127.0.0.1\\@peer.example.com/", "credentials", id="C4-backslash-at"),
        pytest.param("https://peer.example.com%2e/", "invalid", id="C11-pct-dot"),
        pytest.param("https://peer.example.com:65536/", "invalid", id="C6-port-65536"),
        pytest.param("https://peer.example.com:443:443/", "invalid", id="C6-double-port"),
        pytest.param("https://peer.example.com:+443/", "invalid", id="C6-signed-port"),
        pytest.param("https:///x", "invalid", id="C7-no-authority"),
        pytest.param("https://:443/", "invalid", id="C7-port-only"),
        pytest.param("https://./", "invalid", id="C7-dot-only"),
        pytest.param("https:peer.example.com/", "invalid", id="C7-opaque"),
        pytest.param("https:/peer.example.com/", "invalid", id="C7-single-slash"),
        pytest.param("//peer.example.com/", "not_https", id="C14-protocol-relative"),
        pytest.param("http://peer.example.com/", "not_https", id="C14-http"),
        pytest.param("file:///etc/passwd", "not_https", id="C14-file"),
        pytest.param("gopher://peer.example.com:6379/_FLUSHALL", "not_https", id="C14-gopher"),
    ],
)
def test_C3_to_C14_refusals_happen_before_any_resolution(url, kind):
    res = FaithfulResolver({"peer.example.com": [PUBLIC_V4]})
    with pytest.raises(uv.PublicUrlRefusal) as exc:
        _shared(url, res)
    assert exc.value.kind == kind
    assert res.calls == []


@pytest.mark.parametrize(
    "url,host,port",
    [
        pytest.param("https://peer.example.com#@127.0.0.1", "peer.example.com", 443, id="C4-fragment-at-is-data"),
        pytest.param("https://peer.example.com?@127.0.0.1", "peer.example.com", 443, id="C4-query-at-is-data"),
        pytest.param("https://peer.example.com/@127.0.0.1", "peer.example.com", 443, id="C4-path-at-is-data"),
        pytest.param("https://peer.example.com./", "peer.example.com", 443, id="C5-trailing-dot"),
        pytest.param("https://PEER.Example.COM/", "peer.example.com", 443, id="C5-upper-host"),
        pytest.param("HTTPS://peer.example.com/", "peer.example.com", 443, id="C5-upper-scheme"),
        pytest.param("https://ｐｅｅｒ.example.com/", "peer.example.com", 443, id="C10-fullwidth"),
        pytest.param("https://peer。example。com/", "peer.example.com", 443, id="C10-ideographic-dot"),
        pytest.param("https://peer.example.com:0/", "peer.example.com", 443, id="C6-port-0"),
        pytest.param("https://peer.example.com:1/", "peer.example.com", 1, id="C6-port-1"),
        pytest.param("https://peer.example.com:65535/", "peer.example.com", 65535, id="C6-port-max"),
        pytest.param("https://peer.example.com:0443/", "peer.example.com", 443, id="C6-port-leading-zero"),
        pytest.param("https://peer.example.com:/", "peer.example.com", 443, id="C6-port-empty"),
    ],
)
def test_C4_C5_C6_C10_the_resolver_is_handed_the_canonical_host(url, host, port):
    """The parser and the resolver must agree on WHICH host was approved: the
    resolver sees the canonical A-label form, and the returned `hostname` (what
    the A2A client puts in `Host` + SNI) is that same string."""
    res = FaithfulResolver({"peer.example.com": [PUBLIC_V4]})
    out = _shared(url, res)
    assert res.calls == [host]
    assert out.hostname == host
    assert out.port == port
    assert out.addresses == (PUBLIC_V4,)


def test_C9_whitespace_inside_the_host_is_a_dns_failure_not_a_pass():
    res = FaithfulResolver({"peer.example.com": [PUBLIC_V4]})
    with pytest.raises(uv.PublicUrlRefusal) as exc:
        _shared("https://peer .example.com/", res)
    assert exc.value.kind == "dns_failure"


def test_C13_resolution_happens_exactly_once_and_the_answer_is_what_is_returned():
    """Rebinding seam, validator half: one lookup, and the approved set IS the
    returned set — a second (rebinding) answer is never consulted here. The
    connect half (pinning to `addresses[0]`) is pinned by
    `test_736_a2a_outbound_transport.py`."""
    answers = iter([[_record(PUBLIC_V4)], [_record("127.0.0.1")]])
    calls = []

    def flipping(host, port, *a, **k):
        calls.append(host)
        return next(answers)

    out = _shared("https://rebind.example.com/", flipping)
    assert calls == ["rebind.example.com"]
    assert out.addresses == (PUBLIC_V4,)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://a..example.com/", id="C12-empty-label"),
        pytest.param("https://" + "a" * 64 + ".example.com/", id="C12-label-64"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=UnicodeError,
    reason=(
        "BUG: a host the idna codec refuses escapes _validate_public_https_url as a "
        "bare UnicodeError, not a PublicUrlRefusal with a kind — see #3322"
    ),
)
def test_C12_an_unencodable_ascii_host_is_a_named_refusal(url):
    """`canonical_host`'s ASCII fallback re-admits a host `idna` rightly refused
    (empty label / >63 chars), and CPython's `getaddrinfo` then raises
    `UnicodeError` from its own `idna` encode — not `gaierror`, so the
    `except socket.gaierror` arm misses it. Fail-closed (it is still a
    ValueError), but kind-less: the template-registry route echoes the codec's
    text as its 400 detail, and the A2A wrapper can only guess `endpoint_invalid`.
    """
    with pytest.raises(uv.PublicUrlRefusal):
        _shared(url, FaithfulResolver({"peer.example.com": [PUBLIC_V4]}))


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://a..example.com/", id="C12-empty-label"),
        pytest.param("https://" + "a" * 64 + ".example.com/", id="C12-label-64"),
    ],
)
def test_C12_the_same_hosts_are_still_refused_fail_closed(url):
    """The safe half of C12, so the xfail above can never hide an ACCEPT — plus
    its precondition: CPython's own `idna` encode (the first thing
    `socket.getaddrinfo` does with a str host) refuses this host, and
    `canonical_host` nonetheless hands it on via the ASCII fallback."""
    host = url.split("/")[2]
    with pytest.raises(UnicodeError):
        host.encode("idna")
    assert uv.canonical_host(host) == host
    with pytest.raises(ValueError):
        _shared(url, FaithfulResolver({"peer.example.com": [PUBLIC_V4]}))


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("☃.com", id="C16-symbol"),
        pytest.param("exa\u200dmple.com", id="C16-zero-width-joiner"),
        pytest.param("\u0661\u0031.com", id="C16-bidi-mixed-digits"),
    ],
)
def test_C16_a_non_ascii_host_idna_refuses_is_fatal_not_passed_through(host):
    """The other half of the ASCII asymmetry: a codec failure on a NON-ASCII host
    is where the homograph lives, so it must never reach the resolver raw
    (mutation M11: returning the host here survived every pre-existing test)."""
    assert uv.canonical_host(host) is None
    res = FaithfulResolver({"peer.example.com": [PUBLIC_V4]})
    with pytest.raises(uv.PublicUrlRefusal) as exc:
        _shared(f"https://{host}/", res)
    assert exc.value.kind == "invalid"
    assert res.calls == []


def test_C15_an_underscore_host_still_reaches_the_resolver():
    """The documented ASCII asymmetry: `idna` rejects `_`, the codec CPython uses
    does not, so the fallback is load-bearing for real operator hostnames."""
    res = FaithfulResolver({"my_registry.example.com": [PUBLIC_V4]})
    assert _shared("https://my_registry.example.com/", res).hostname == "my_registry.example.com"


# =========================================================================== #
# D. reject_embedded_credentials
# =========================================================================== #
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://tok@github.com/o/r", id="D1-scheme"),
        pytest.param("https://:pw@github.com/o/r", id="D1-password-only"),
        pytest.param("tok@github.com/o/r", id="D1-scheme-less"),
        pytest.param("git+ssh://tok@github.com/o/r", id="D1-alt-scheme"),
        pytest.param("https://a@b@github.com/o/r", id="D1-double-at"),
        pytest.param("HTTPS://tok@github.com/o/r", id="D1-upper-scheme"),
        pytest.param("https://tok\\@github.com/o/r", id="D1-backslash"),
    ],
)
def test_D1_userinfo_is_refused(url):
    with pytest.raises(uv.EmbeddedCredentialError):
        uv.reject_embedded_credentials(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://github.com/o/r?ref=a@b", id="D2-query-at"),
        pytest.param("https://github.com/o/r#a@b", id="D2-fragment-at"),
        pytest.param("https://github.com/o/user@x", id="D2-path-at"),
        pytest.param("owner/repo", id="D2-shorthand"),
    ],
)
def test_D2_an_at_outside_the_authority_is_not_a_credential(url):
    uv.reject_embedded_credentials(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("//tok@github.com/o/r", id="D3-protocol-relative"),
        pytest.param("  //tok@github.com/o/r", id="D3-protocol-relative-leading-space"),
        pytest.param("//:pw@github.com/o/r", id="D3-protocol-relative-password"),
        pytest.param("//\ttok@github.com/o/r", id="D3-protocol-relative-tab-in-userinfo"),
        pytest.param("//TOK@GITHUB.COM/o/r", id="D3-protocol-relative-uppercase"),
        pytest.param("//a@b@github.com/o/r", id="D3-protocol-relative-double-at"),
        pytest.param("//tok@[oops/x", id="D3-protocol-relative-unparseable-fallback"),
    ],
)
def test_D3_protocol_relative_userinfo_is_refused(url):
    """`reject_embedded_credentials` prefixes `https://` whenever `://` is absent,
    turning `//tok@host` into `https:////tok@host` — whose netloc is EMPTY, so
    `.username` is None. `strip_url_credentials` documents and handles exactly
    this shape ("A leading `//` is protocol-relative … must NOT get the assumed
    scheme prepended"); its rejection twin does not.

    Reachable end to end: `routers/skills.py:941` then `:945` (create) and
    `:984`/`:988` (update), and `services/skill_service.py:825-826` (legacy
    adoption) run exactly this pair; the shorthand branch of
    `validate_skills_library_url` returns the string unchanged, and it is written
    to `skill_sources.url` and into the `skill_source_create` audit details.
    """
    with pytest.raises(uv.EmbeddedCredentialError):
        uv.reject_embedded_credentials(url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("//github.com/o/r", id="D3-protocol-relative-no-userinfo"),
        pytest.param("//[oops/x", id="D3-protocol-relative-unparseable-no-userinfo"),
        pytest.param("//@github.com/o/r", id="D3-protocol-relative-empty-userinfo"),
        pytest.param("///tok@github.com/o/r", id="D3-triple-slash-scope-boundary"),
    ],
)
def test_D3_protocol_relative_without_a_credential_is_not_refused(url):
    """The #3323 fix must not over-reach. A `//host` with no userinfo passes; an
    unparseable `//[oops` keeps passing rather than becoming a NEW bare
    ValueError (today's route would 500 on it — the #3322 class, which stays
    owned by D4 for every non-`//` input); empty userinfo carries no secret and
    is not refused, matching the `https://@host` form. `///tok@…` has an empty
    authority — `strip_url_credentials` agrees it carries none — and is the
    deliberate scope boundary of this fix."""
    uv.reject_embedded_credentials(url)


@pytest.mark.parametrize(
    "url",
    ["//tok@github.com/o/r", "  //tok@github.com/o/r", "//:pw@github.com/o/r"],
)
def test_D3_preconditions_the_shape_is_live_and_carries_userinfo(gh_dns, url):
    """What makes D3 a bug rather than an unreachable input: the companion
    validator returns the string verbatim (shorthand branch, no resolution), and
    the module's own display scrubber agrees it carries userinfo."""
    assert uv.validate_skills_library_url(url) == url.strip()
    assert gh_dns.calls == []
    assert "@" not in uv.strip_url_credentials(url.strip())


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://[::1/x", id="D4-unbalanced-bracket"),
        pytest.param("x[/y", id="D4-shorthand-open-bracket"),
        pytest.param("o]/r", id="D4-shorthand-close-bracket"),
    ],
)
@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason=(
        "BUG: reject_embedded_credentials leaks urlparse's bare ValueError('Invalid "
        "IPv6 URL') on a bracket in the authority; the skills-source routes catch only "
        "EmbeddedCredentialError, so a typo is a 500 — #3322"
    ),
)
def test_D4_a_malformed_authority_is_not_a_bare_value_error(url):
    """`routers/skills.py:940-943` (create) and `:983-986` (update) wrap this call
    in `except EmbeddedCredentialError` ONLY, and run it BEFORE
    `validate_skills_library_url` — whose own `except Exception -> "Invalid URL
    format"` would have produced the 400. No global ValueError handler is
    registered in `main.py`, so the request 500s. `strip_url_credentials` in the
    same module documents this exact `urlparse` raise and guards it.

    Expected: either `EmbeddedCredentialError` or no raise — never a plain
    ValueError the route cannot map."""
    try:
        uv.reject_embedded_credentials(url)
    except uv.EmbeddedCredentialError:
        pass


@pytest.mark.parametrize("url", ["https://[::1/x", "x[/y", "o]/r"])
def test_D4_preconditions_the_raise_is_a_plain_value_error_from_urlparse(url):
    with pytest.raises(ValueError) as exc:
        uv.reject_embedded_credentials(url)
    assert not isinstance(exc.value, uv.EmbeddedCredentialError)
    assert "IPv6" in str(exc.value)
