"""trinity-enterprise#838 — the admin's outbound A2A endpoint allowlist.

Pins, in three layers (the inbound source + keyless door this file once pinned
were dropped before 1.0 by trinity-enterprise#885 — see
`test_ent885_keyless_refused.py`; the list is outbound-only now):

* **The admin's list** (`services/a2a_trusted_networks.normalize_entries`):
  a tailnet CIDR, an exact host and a `*.domain` are accepted; the platform's
  own Docker networks, loopback, link-local (cloud metadata), anything broader
  than /8 and a one-label wildcard are refused by name.
* **Outbound** (`validate_a2a_endpoint_url`): an untrusted endpoint is refused
  exactly as before; a trusted one may be private/CGNAT and `http://`; a trusted
  NAME that resolves outside the declared CIDRs, or onto metadata, is refused.
* **The gateway guard** (`set_entries`): a CIDR containing the gateway of a
  network this backend is attached to is refused on write.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import a2a_trusted_networks as tn  # noqa: E402
from utils.url_validation import A2AEndpointUrlError, validate_a2a_endpoint_url  # noqa: E402

pytestmark = pytest.mark.unit

TAILNET = "100.64.0.0/10"


# --------------------------------------------------------------------------- #
# The admin's list
# --------------------------------------------------------------------------- #
def test_the_three_entry_shapes_are_accepted_and_canonicalised():
    assert tn.normalize_entries([" 100.64.0.0/10 ", "*.Tail1234.TS.net", "box.lan.example",
                                 "100.64.0.0/10"]) == [
        "100.64.0.0/10", "*.tail1234.ts.net", "box.lan.example"]


@pytest.mark.parametrize("entry,why", [
    ("172.28.0.0/16", "platform"),
    ("172.29.5.0/24", "platform"),
    ("172.16.0.0/12", "platform"),       # contains both bridges
    ("127.0.0.0/8", "loopback"),
    ("169.254.169.254/32", "loopback"),  # cloud metadata
    ("0.0.0.0/0", "too broad"),
    ("10.0.0.0/7", "too broad"),
    ("::1/128", "loopback"),
    ("*.com", "two labels"),
    ("not a host!", "not a CIDR"),
])
def test_dangerous_or_malformed_entries_are_refused_by_name(entry, why):
    with pytest.raises(tn.TrustedNetworkError) as exc:
        tn.normalize_entries([entry])
    assert why in str(exc.value)


def test_an_empty_list_turns_it_off():
    assert tn.normalize_entries([]) == []


# trinity-enterprise#873: prod/hosted add a third managed bridge, the ingress
# network the bundled nginx and cloudflared sit on. Its prefix is configurable,
# so the refusal must follow the ACTIVE prefix, not a literal.
@pytest.mark.parametrize("prefix,entry,neighbour", [
    ("172.30.0", "172.30.0.0/24", "172.30.1.0/24"),
    ("172.31.7", "172.31.7.128/25", "172.31.8.0/24"),
])
def test_the_active_ingress_network_is_refused_like_the_other_bridges(monkeypatch, prefix, entry, neighbour):
    monkeypatch.setenv("TRINITY_BUNDLED_PROXY_TRUST", "1")
    monkeypatch.setenv("TRINITY_PROXY_PREFIX", prefix)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    with pytest.raises(tn.TrustedNetworkError) as exc:
        tn.normalize_entries([TAILNET, entry])
    assert "platform" in str(exc.value) and entry in str(exc.value)
    # An unrelated neighbouring range is still accepted.
    assert tn.normalize_entries([TAILNET, neighbour]) == [TAILNET, neighbour]


def test_a_trusted_name_never_resolves_into_the_active_ingress_network(monkeypatch):
    monkeypatch.setenv("TRINITY_BUNDLED_PROXY_TRUST", "1")
    monkeypatch.setenv("TRINITY_PROXY_PREFIX", "172.31.7")
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    with pytest.raises(A2AEndpointUrlError):
        validate_a2a_endpoint_url("http://peer.tail1234.ts.net/a2a/x",
                                  trusted_entries=["*.tail1234.ts.net"],
                                  resolver=_resolver("172.31.7.3"))


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #
def _resolver(*addresses):
    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]
    return resolve


def test_an_untrusted_internal_endpoint_is_refused_exactly_as_before():
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url("http://peer.tail1234.ts.net/a2a/x", trusted_entries=[],
                                  resolver=_resolver("100.70.1.2"))
    assert exc.value.reason == "endpoint_not_https"
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate_a2a_endpoint_url("https://peer.tail1234.ts.net/a2a/x", trusted_entries=[],
                                  resolver=_resolver("100.70.1.2"))
    assert exc.value.reason == "endpoint_private_address"


@pytest.mark.parametrize("entries", [
    ["*.tail1234.ts.net"],
    [TAILNET],
    ["*.tail1234.ts.net", TAILNET],
])
def test_a_trusted_endpoint_may_be_cgnat_and_http(entries):
    v = validate_a2a_endpoint_url("http://peer.tail1234.ts.net/a2a/x", trusted_entries=entries,
                                  resolver=_resolver("100.70.1.2"))
    assert v.addresses == ("100.70.1.2",) and v.port == 80


def test_a_trusted_name_that_resolves_outside_the_declared_cidrs_is_refused():
    with pytest.raises(A2AEndpointUrlError):
        validate_a2a_endpoint_url("http://peer.tail1234.ts.net/a2a/x",
                                  trusted_entries=["*.tail1234.ts.net", TAILNET],
                                  resolver=_resolver("10.1.2.3"))


@pytest.mark.parametrize("address", ["169.254.169.254", "127.0.0.1", "172.28.0.5"])
def test_a_trusted_name_never_reaches_metadata_loopback_or_the_platform(address):
    with pytest.raises(A2AEndpointUrlError):
        validate_a2a_endpoint_url("http://peer.tail1234.ts.net/a2a/x",
                                  trusted_entries=["*.tail1234.ts.net"],
                                  resolver=_resolver(address))


def test_a_name_outside_the_suffix_gets_no_trust_from_it():
    with pytest.raises(A2AEndpointUrlError):
        validate_a2a_endpoint_url("http://evil.example.com/a2a/x",
                                  trusted_entries=["*.tail1234.ts.net"],
                                  resolver=_resolver("100.70.1.2"))


def test_a_public_https_endpoint_is_unchanged():
    v = validate_a2a_endpoint_url("https://peer.example.com/a2a/x", trusted_entries=[TAILNET],
                                  resolver=_resolver("93.184.216.34"))
    assert v.addresses == ("93.184.216.34",)


# --------------------------------------------------------------------------- #
# The gateway guard
# --------------------------------------------------------------------------- #
# The route table as /proc/net/route prints it: a default route via 172.28.0.1
# and two connected /16s (agent and platform networks) plus a /24.
_ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t01001CAC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t00001CAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
eth1\t00001DAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
eth2\t00EE1FAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
"""


def test_the_route_table_yields_every_attached_networks_gateway():
    assert tn._parse_route_table(_ROUTES) == {"172.28.0.1", "172.29.0.1", "172.31.238.1"}


def test_an_entry_containing_an_attached_gateway_is_refused_on_write(monkeypatch):
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset({"172.31.238.1"}))
    with pytest.raises(tn.TrustedNetworkError) as exc:
        tn.set_entries(["172.31.238.0/24"])
    assert "gateway" in str(exc.value)
