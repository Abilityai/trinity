"""
abilityai/trinity-enterprise#885 — the trusted-networks list is an OUTBOUND
endpoint allowlist, and nothing else.

Operator ruling 2026-10-13 (ent#885 comment + body "Kept"): an A2A endpoint on
a private / CGNAT address (a Tailscale peer in ``100.64.0.0/10``) or on plain
``http://`` may be registered and called only when its host is on the admin
list; a host off the list gets the v0.9.5 SSRF refusal. Both sides are proven
here through the two production readers of the list — the register-time check
in ``a2a_outbound.upsert_endpoint`` and the per-call re-validation in
``a2a_client.validate_endpoint`` — with the list written through the real
``set_entries`` into the real unit SQLite, exactly as the admin route does.
Neither reader is handed ``trusted_entries=``: the point is that the STORED
list is what admits the host.

Related flow: docs/memory/feature-flows/a2a-outbound-call.md
"""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import urlsplit

import pytest

from services import a2a_client, a2a_outbound
from services import a2a_trusted_networks as tn
from utils import url_validation

pytestmark = pytest.mark.unit

TAILNET = "100.64.0.0/10"
PEER_URL = "http://peer.tail1234.ts.net/a2a/x"
PEER_ADDR = "100.70.1.2"


@pytest.fixture()
def tailnet_dns(monkeypatch):
    """The peer's name resolves to a CGNAT address, as a tailnet name does."""
    def resolve(host, port=None, *a, **k):
        if host == "peer.tail1234.ts.net":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PEER_ADDR, port or 0))]
        raise socket.gaierror(f"unexpected lookup: {host}")
    monkeypatch.setattr(url_validation.socket, "getaddrinfo", resolve)


@pytest.fixture()
def admin_list(monkeypatch):
    """Write the list the way the admin route does; empty it again afterwards."""
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset())

    def _set(entries):
        tn.set_entries(entries)
        tn._entries_cache.update(at=float("-inf"), entries=[])

    yield _set
    _set([])


def _call_time(url):
    return asyncio.run(a2a_client.validate_endpoint(url))


# --------------------------------------------------------------------------- #
# Off the list: the v0.9.5 refusal, at register time and at call time
# --------------------------------------------------------------------------- #
def test_a_host_off_the_list_is_refused_at_register_and_call_time(tailnet_dns, admin_list):
    admin_list([])
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("tail-peer", PEER_URL, None)
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call_time(PEER_URL)
    assert exc.value.reason == "endpoint_not_https"
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call_time("https://peer.tail1234.ts.net/a2a/x")
    assert exc.value.reason == "endpoint_private_address"


def test_another_host_on_a_listed_network_but_not_the_listed_name_is_refused(tailnet_dns, admin_list):
    """A `*.suffix` entry admits names under it only; a CIDR entry admits a
    name only when it resolves inside it. Neither admits a stranger's name."""
    admin_list(["*.other-tailnet.ts.net"])
    with pytest.raises(a2a_client.A2ACallError) as exc:
        _call_time(PEER_URL)
    assert exc.value.reason == "endpoint_not_https"


# --------------------------------------------------------------------------- #
# On the list: registered and called, on CGNAT and plain http
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("entries", [[TAILNET], ["*.tail1234.ts.net"], ["peer.tail1234.ts.net"]])
def test_a_host_on_the_list_is_registered_and_called(tailnet_dns, admin_list, entries, monkeypatch):
    admin_list(entries)
    # The registry's encrypted at-rest store (needs CREDENTIAL_ENCRYPTION_KEY) is
    # not under test; the validator in front of it is. Record what reached it.
    stored = []
    monkeypatch.setattr(a2a_outbound, "_store_endpoint_records", lambda records: stored.append(records))
    try:
        record = a2a_outbound.upsert_endpoint("tail-peer", PEER_URL, None)
    except a2a_outbound.EndpointValidationError as exc:
        pytest.fail(f"a listed host was refused at register time: {exc}")
    assert urlsplit(record["url"]).hostname == "peer.tail1234.ts.net"
    assert stored and any(urlsplit(r["url"]).hostname == "peer.tail1234.ts.net" for r in stored[-1])
    validated = _call_time(PEER_URL)
    assert validated.addresses == (PEER_ADDR,) and validated.port == 80


def test_the_list_is_read_fresh_after_a_write(tailnet_dns, admin_list):
    """Admitting, then un-listing, refuses the next call: the allowance is the
    stored row, not a process-lifetime cache."""
    admin_list([TAILNET])
    assert _call_time(PEER_URL).addresses == (PEER_ADDR,)
    admin_list([])
    with pytest.raises(a2a_client.A2ACallError):
        _call_time(PEER_URL)
