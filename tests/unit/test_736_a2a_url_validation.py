"""#736 — `validate_a2a_endpoint_url`, the call-time SSRF gate.

This validator runs on **every** outbound A2A call, on a URL that came out of
the endpoint registry — because "it is in the registry" does not mean "it is
safe to fetch". The registration surface validates a URL with
`startswith("http://") or startswith("https://")` and a length cap, so without
this gate an operator (or a compromised management surface) could register
`http://169.254.169.254/…` and aim a credentialed server-side fetch at cloud
metadata.

DNS is stubbed rather than dialled — the property under test is the predicate,
not the resolver (the `test_ent14_registry_url_ssrf.py` rule).

The file also pins the two things this validator does that its
template-registry sibling deliberately does not: it refuses `http://` at USE
with a named reason, and it RETURNS the resolved addresses so the caller can pin
the connection to one the validator approved.
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
# same way test_ent14_registry_url_ssrf.py does.
_spec = importlib.util.spec_from_file_location(
    "backend_url_validation_736",
    os.path.join(_backend_path, "utils", "url_validation.py"),
)
_mod = importlib.util.module_from_spec(_spec)

# The `sys.modules` entry below is installed at IMPORT time — before any fixture
# runs — so `monkeypatch.setitem` cannot reach it. The sanctioned alternative is
# the named snapshot/restore pair (#762 ratchet; precedent:
# tests/unit/test_telegram_webhook_backfill.py), with one adaptation: that file
# installs its stubs INSIDE each test, so a snapshot taken at fixture setup is
# genuinely pre-stub. Here the entry is already in place by then, so the
# pre-stub value is captured HERE, at module scope — restoring a fixture-time
# snapshot would just write the stub back and leak it for the rest of the
# session (`tests/unit/conftest.py`'s `_POP_PREFIXES` sweep does not match this
# name).
_STUBBED_MODULE_NAMES = ["backend_url_validation_736"]
_PRE_STUB_SYS_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}

sys.modules["backend_url_validation_736"] = _mod
_spec.loader.exec_module(_mod)

validate = _mod.validate_a2a_endpoint_url
A2AEndpointUrlError = _mod.A2AEndpointUrlError

URL = "https://peer.example.com/a2a/bot"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Undo this file's import-time `sys.modules` registration after each test.

    `_mod` is held by a module global, so the tests keep working once the entry
    is gone; what goes away is the cross-file leak.
    """
    try:
        yield
    finally:
        for name, value in _PRE_STUB_SYS_MODULES.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def resolves(monkeypatch):
    """Point every hostname at a chosen set of addresses."""

    def _set(*addresses):
        def _getaddrinfo(host, port, *a, **k):
            out = []
            for addr in addresses:
                if ":" in addr:
                    out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
                else:
                    out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
            return out

        monkeypatch.setattr(_mod.socket, "getaddrinfo", _getaddrinfo)

    return _set


# --------------------------------------------------------------------------- #
# The happy path — and the part the pin depends on
# --------------------------------------------------------------------------- #
def test_public_endpoint_is_accepted_and_returns_its_addresses(resolves):
    """The addresses are not decoration: `a2a_client` connects to one of THEM.

    A validator that resolves a host and throws the answer away forces the HTTP
    client to resolve a second time, which is exactly the TOCTOU window a
    rebinding attack lives in.
    """
    resolves("93.184.216.34")
    result = validate(URL)
    assert result.url == URL
    assert result.hostname == "peer.example.com"
    assert result.port == 443
    assert result.addresses == ("93.184.216.34",)


def test_explicit_port_is_preserved(resolves):
    resolves("93.184.216.34")
    result = validate("https://peer.example.com:8443/a2a")
    assert result.port == 8443


# --------------------------------------------------------------------------- #
# Scheme — refused at USE, with a reason that tells the operator what to do
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://peer.example.com/a2a",
        "file:///etc/passwd",
        "gopher://peer.example.com/a2a",
        "ftp://peer.example.com/a2a",
    ],
)
def test_non_https_is_refused(resolves, url):
    resolves("93.184.216.34")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(url)
    assert exc.value.reason == "endpoint_not_https"


def test_http_is_refused_rather_than_upgraded(resolves):
    """The registry accepts `http://` at registration. The call path must NOT
    quietly rewrite it to https — an operator who typed http has to find out,
    and a client that silently rewrites a scheme is one that will silently
    rewrite it back."""
    resolves("93.184.216.34")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("http://peer.example.com/a2a")
    assert exc.value.reason == "endpoint_not_https"
    assert "https" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Destination predicates
# --------------------------------------------------------------------------- #
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
        ("100.64.0.1", "CGNAT / RFC 6598"),
        ("::1", "IPv6 loopback"),
        ("fc00::1", "IPv6 unique-local"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("::ffff:10.0.0.1", "IPv4-mapped private"),
        # ent#393: CGNAT is the one range the stdlib does NOT refuse for us, so
        # it is the one whose mapped form had to be handled in
        # `_is_internal_address` itself. The rows above pass because CPython
        # delegates `is_private`/`is_loopback` through `ipv4_mapped`; these pass
        # only because Trinity's own clause now resolves the same v4 view.
        ("::ffff:100.64.0.1", "IPv4-mapped CGNAT / RFC 6598"),
        ("::ffff:100.127.255.254", "IPv4-mapped CGNAT, upper half"),
    ],
)
def test_internal_destinations_are_refused(resolves, address, what):
    resolves(address)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(URL)
    assert exc.value.reason == "endpoint_private_address"


def test_one_internal_record_among_public_ones_refuses_the_whole_endpoint(resolves):
    """Which record a resolver hands the socket is not our choice, so a host
    resolving to both a public and an internal address is a DNS-level smuggle,
    not a host with one usable address."""
    resolves("93.184.216.34", "169.254.169.254")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(URL)
    assert exc.value.reason == "endpoint_private_address"


def test_refusal_message_never_echoes_the_resolved_address(resolves):
    """An operator-visible refusal that names the internal IP it found is a
    topology oracle. Fixed strings only."""
    resolves("10.11.12.13")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(URL)
    assert "10.11.12.13" not in str(exc.value)


def test_dns_failure_is_fatal(monkeypatch):
    """Unlike `validate_skills_library_url` (whose target is a later git clone
    that fails loudly on its own), an unresolvable A2A endpoint is refused: the
    next step would be a credentialed request."""

    def _boom(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(_mod.socket, "getaddrinfo", _boom)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(URL)
    assert exc.value.reason == "endpoint_dns_failure"


def test_empty_resolution_is_refused(monkeypatch):
    """An empty resolver result would sail through the per-address loop and be
    indistinguishable from "nothing to check"."""
    monkeypatch.setattr(_mod.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(URL)
    assert exc.value.reason == "endpoint_dns_failure"


# --------------------------------------------------------------------------- #
# Userinfo and shape
# --------------------------------------------------------------------------- #
def test_embedded_credentials_are_refused_not_stripped(resolves):
    resolves("93.184.216.34")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://user:s3cret@peer.example.com/a2a")
    assert exc.value.reason == "endpoint_invalid"
    assert "s3cret" not in str(exc.value)


def test_over_length_is_refused(resolves):
    resolves("93.184.216.34")
    with pytest.raises(A2AEndpointUrlError):
        validate("https://peer.example.com/" + "a" * 3000)


@pytest.mark.parametrize("url", ["", "   ", None])
def test_empty_is_refused(url):
    with pytest.raises(A2AEndpointUrlError):
        validate(url)


# --------------------------------------------------------------------------- #
# IDNA / homoglyph parity (SV-7)
# --------------------------------------------------------------------------- #
def test_unicode_host_is_canonicalised_to_its_a_label(resolves):
    """The parser and the resolver must agree on WHICH host was approved. UTS-46
    nontransitional (the `setup_url_display` rule) — the stdlib `encode("idna")`
    codec is IDNA2003 and folds `faß.de` to `fass.de`, a different registrable
    domain than the one a browser visits."""
    resolves("93.184.216.34")
    result = validate("https://faß.de/a2a")
    assert result.hostname == "xn--fa-hia.de"


def test_percent_encoded_authority_is_refused(resolves):
    """`urlparse` leaves `%D0%B0pple.com` undecoded while a browser decodes it
    and resolves a different host."""
    resolves("93.184.216.34")
    with pytest.raises(A2AEndpointUrlError):
        validate("https://%D0%B0pple.com/a2a")


def test_ascii_host_the_idna_codec_rejects_still_passes(resolves):
    """An underscore is illegal in IDNA but legal in a real hostname an operator
    may genuinely run. Canonicalising an ASCII host is a no-op by definition —
    there is no confusable to fold — so a codec refusal on ASCII must not
    refuse the URL. (A codec refusal on a NON-ASCII host is fatal; that is
    where the homograph lives.)"""
    resolves("93.184.216.34")
    result = validate("https://my_peer.example.com/a2a")
    assert result.hostname == "my_peer.example.com"


def test_trailing_dot_is_normalised(resolves):
    """`host.` and `host` resolve identically, and the dot survives idna.encode."""
    resolves("93.184.216.34")
    assert validate("https://peer.example.com./a2a").hostname == "peer.example.com"


# --------------------------------------------------------------------------- #
# The sibling validator must keep behaving exactly as before the #736 refactor
# --------------------------------------------------------------------------- #
def test_template_registry_validator_is_unchanged_by_the_shared_helper(resolves):
    """#736 extracted `_validate_public_https_url` and repointed the registry
    validator at it (F11 — a third ~90% clone inside the module whose job is
    centralising this policy would be the Invariant #5 failure happening in the
    file that exists to prevent it). Its operator-facing messages are part of
    its contract, so they are pinned here as well as in the ent#14 suite."""
    resolves("169.254.169.254")
    with pytest.raises(ValueError, match="internal address"):
        _mod.validate_template_registry_url("https://registry.example.com/r.yaml")
