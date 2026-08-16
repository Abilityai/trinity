"""One port normalisation, consumed by validation, pinning and origin comparison (ent#398).

Port `:0` used to mean three different things along one outbound A2A call path:

    _validate_public_https_url   `parsed.port or 443`  -> 443   (0 is falsy)
    _pinned_url                  `if parts.port`       -> dropped, i.e. 443
    _same_origin._key            `p.port`              -> literally 0

So a `:0` endpoint validated, would have connected correctly, and was then
permanently refused `card_origin_mismatch` against any card declaring the
ordinary form. Fail-closed, hence P3 — the damage is a permanently broken
endpoint with a reason code that points at the wrong thing.

The hazard worth pinning is not `:0` itself but the three independent
normalisations: today they disagree harmlessly, and an edit to any one of them is
what turns that into something else. These tests hold all three to one answer,
and pin the default-port equivalence #736 depends on (Trinity's own card emits no
port, so `https://h` and `https://h:443` must compare equal or Trinity becomes
unreachable by its own rule).

DNS is stubbed, never dialled — the property under test is the predicate, not the
resolver (the `test_ent14_registry_url_ssrf.py` rule).
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
sys.path.insert(0, _backend_path)

# tests/utils shadows src/backend/utils — load the backend module directly, with
# the same snapshot/restore of the `sys.modules` entry that
# test_736_a2a_url_validation.py documents (the entry is installed at import
# time, so the pre-stub value has to be captured at module scope).
_spec = importlib.util.spec_from_file_location(
    "backend_url_validation_ent398",
    os.path.join(_backend_path, "utils", "url_validation.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_STUBBED_MODULE_NAMES = ["backend_url_validation_ent398"]
_PRE_STUB_SYS_MODULES = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
sys.modules["backend_url_validation_ent398"] = _mod
_spec.loader.exec_module(_mod)

from services import a2a_client  # noqa: E402  (after the sys.path insert above)

validate = _mod.validate_a2a_endpoint_url
effective_port = _mod.effective_port

PUBLIC_V4 = "93.184.216.34"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
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
# The normalisation itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "port,scheme,expected",
    [
        (8443, "https", 8443),      # explicit, non-default
        (443, "https", 443),        # explicit, default
        (None, "https", 443),       # absent -> scheme default
        (0, "https", 443),          # THE bug: `:0` is "no port given", not port 0
        (0, "HTTPS", 443),          # scheme case-insensitive
        (None, "http", 80),
        (0, "http", 80),
        (None, "ftp", None),        # no default to give
        (0, "", None),
        (8443, "ftp", 8443),        # an explicit port needs no default
    ],
)
def test_effective_port_is_one_answer_for_every_spelling(port, scheme, expected):
    assert effective_port(port, scheme) == expected


# --------------------------------------------------------------------------- #
# The three consumers agree — the ent#398 acceptance criterion
# --------------------------------------------------------------------------- #
def test_a_zero_port_is_normalised_consistently_across_the_call_path(resolves):
    """Mirrors `test_736_a2a_outbound_edges.py::test_D7…`, which is pinned as a
    strict-xfail on the branch that carries it (PR #2178) and must be un-xfailed
    when that lands. Held here too so `dev` cannot regress in the meantime."""
    resolves(PUBLIC_V4)
    validated = validate("https://peer.example.com:0/a2a")

    # The validator decided `:0` means the scheme default...
    assert validated.port == 443
    # ...the pinned URL agrees — it omits the port, i.e. 443...
    assert a2a_client._pinned_url(validated.url, PUBLIC_V4) == f"https://{PUBLIC_V4}/a2a"
    # ...and so does the origin comparison, which used to read it as port 0.
    assert a2a_client._same_origin(validated.url, "https://peer.example.com/a2a") is True


def test_a_zero_port_endpoint_matches_a_card_that_spells_the_default(resolves):
    """The end-to-end symptom: `card_origin_mismatch` forever, for an endpoint
    that validates and would connect."""
    resolves(PUBLIC_V4)
    validated = validate("https://peer.example.com:0/a2a")
    for card_url in (
        "https://peer.example.com/a2a",
        "https://peer.example.com:443/a2a",
        "https://peer.example.com:0/a2a",
    ):
        assert a2a_client._same_origin(validated.url, card_url) is True, card_url


def test_the_host_header_of_a_zero_port_endpoint_carries_no_port(resolves):
    """The fourth reader of this field. It answers a deliberately different
    question — "what did the operator type?", since an added `:443` changes what
    a peer sees — but `:0` must still resolve to the bare host, or the request
    carries an authority nothing else in the path agrees with."""
    resolves(PUBLIC_V4)
    validated = validate("https://peer.example.com:0/a2a")
    assert a2a_client._host_header(validated) == "peer.example.com"


# --------------------------------------------------------------------------- #
# The properties the fix must not break
# --------------------------------------------------------------------------- #
def test_default_port_equivalence_is_preserved(resolves):
    """#736 depends on this: Trinity's own card emits no explicit port, so
    treating `https://h` and `https://h:443` as different origins would make
    Trinity unreachable by its own rule and break #738 federation."""
    assert a2a_client._same_origin("https://h.example/a2a", "https://h.example:443/a2a") is True
    assert a2a_client._same_origin("https://h.example:443/a2a", "https://h.example/a2a") is True


def test_a_real_non_default_port_still_distinguishes_origins():
    assert a2a_client._same_origin("https://h.example:8443/a2a", "https://h.example/a2a") is False
    assert a2a_client._same_origin("https://h.example:8443/a2a", "https://h.example:8443/a2a") is True


def test_a_non_default_port_survives_pinning():
    assert a2a_client._pinned_url("https://h.example:8443/a2a", PUBLIC_V4) == \
        f"https://{PUBLIC_V4}:8443/a2a"
    assert a2a_client._pinned_url("https://h.example:8443/a2a", "2606:4700::1111") == \
        "https://[2606:4700::1111]:8443/a2a"


def test_an_explicit_default_port_is_pinned_as_the_bare_host():
    """A behaviour change, deliberate and equivalent: `:443` is now omitted from
    the pinned URL rather than spelled out. Same destination, and it keeps the
    pinned form consistent with the default-port equivalence rule above. The
    `Host` header, which is where an explicit `:443` is actually observable to a
    peer, still preserves what the operator typed."""
    assert a2a_client._pinned_url("https://h.example:443/a2a", PUBLIC_V4) == \
        f"https://{PUBLIC_V4}/a2a"


def test_pinning_preserves_the_rest_of_the_request():
    """Regression net around the netloc change: path, query and IPv6 bracketing."""
    assert a2a_client._pinned_url("https://h.example/a2a?x=1&y=2", PUBLIC_V4) == \
        f"https://{PUBLIC_V4}/a2a?x=1&y=2"
    assert a2a_client._pinned_url("https://h.example/a2a#frag", PUBLIC_V4) == \
        f"https://{PUBLIC_V4}/a2a"
    assert a2a_client._pinned_url("https://h.example", PUBLIC_V4) == f"https://{PUBLIC_V4}/"
    assert a2a_client._pinned_url("https://h.example/a2a", "2606:4700::1111") == \
        "https://[2606:4700::1111]/a2a"


def test_an_ordinary_endpoint_still_validates_to_its_own_port(resolves):
    resolves(PUBLIC_V4)
    assert validate("https://peer.example.com/a2a").port == 443
    assert validate("https://peer.example.com:8443/a2a").port == 8443


def test_a_junk_port_is_still_a_named_refusal_not_a_traceback(resolves):
    """`urlparse` raises only when `.port` is read, so the normalisation must not
    move that read out from under its except clause."""
    resolves(PUBLIC_V4)
    with pytest.raises(_mod.A2AEndpointUrlError) as exc:
        validate("https://peer.example.com:notaport/a2a")
    assert exc.value.reason == "endpoint_invalid"

    with pytest.raises(_mod.A2AEndpointUrlError):
        validate("https://peer.example.com:99999/a2a")


def test_an_unknown_scheme_compares_to_itself_and_to_nothing_else():
    """`-1` for "no default port" keeps two such URLs comparable without ever
    colliding with a real port."""
    assert a2a_client._same_origin("ftp://h.example/a2a", "ftp://h.example/a2a") is True
    assert a2a_client._same_origin("ftp://h.example/a2a", "https://h.example/a2a") is False
