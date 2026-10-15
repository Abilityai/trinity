"""The client address behind the bundled proxy (abilityai/trinity-enterprise#873).

Every per-IP limiter and the audit `actor_ip` need the address of the caller,
not of the proxy in front of the backend. `utils/client_ip.py::client_ip` is the
one reader of forwarded headers for that purpose. Its contract, pinned here:

* forwarded headers are read only when the TCP peer is a trusted proxy —
  loopback, a bundled proxy container resolved by name, or `TRUSTED_PROXIES`
  (default RFC-1918) minus the two Trinity subnets. An agent-network address is
  trusted only by name; a bridge gateway is never a trusted peer;
* `X-Forwarded-For` is walked right to left, skipping trusted proxies and the
  opt-in `TRUSTED_PROXY_HOPS`; the first address that is neither is the client,
  and a token that is not an address stops the walk at the peer;
* `X-Real-IP` is read only when there is no `X-Forwarded-For` at all;
* the request path never resolves a name — a refresher fills the cache.

No real DNS here: the resolver's lookup function is replaced per test.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from utils import client_ip as cip

NGINX = "172.28.0.5"
CLOUDFLARED = "172.28.0.9"
GATEWAY = "172.28.0.1"
AGENT = "172.28.0.7"
BROWSER = "5.6.7.8"


def _req(peer, xff=None, real_ip=None):
    raw = []
    for value in ([xff] if isinstance(xff, str) else (xff or [])):
        raw.append((b"x-forwarded-for", value.encode()))
    if real_ip is not None:
        raw.append((b"x-real-ip", real_ip.encode()))
    client = None if peer is None else SimpleNamespace(host=peer)
    return SimpleNamespace(client=client, headers=Headers(raw=raw))


@pytest.fixture
def dns(monkeypatch):
    """A fake name table; `calls` counts lookups."""
    table = {"trinity-frontend": [NGINX], "trinity-cloudflared": [CLOUDFLARED]}
    calls = []

    def fake_getaddrinfo(host, *args, **kwargs):
        calls.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in table[host]]

    monkeypatch.setattr(cip, "_getaddrinfo", fake_getaddrinfo)
    return SimpleNamespace(table=table, calls=calls)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, dns):
    for var in ("TRUSTED_PROXIES", "TRUSTED_PROXY_HOSTS", "TRUSTED_PROXY_HOPS"):
        monkeypatch.delenv(var, raising=False)
    started = []
    monkeypatch.setattr(cip, "_start_refresher", lambda: started.append(1))
    cip._reset_cache_for_tests()
    cip._refresh_once()
    yield started
    cip._reset_cache_for_tests()


class TestUntrustedPeerCannotChooseItsAddress:
    @pytest.mark.parametrize("peer", ["1.2.3.4", "203.0.113.5"])
    def test_direct_caller_forging_xff(self, peer):
        assert cip.client_ip(_req(peer, xff="10.0.0.99")) == peer

    def test_rotating_forged_xff_is_one_bucket(self):
        keys = {cip.client_ip(_req("1.2.3.4", xff=f"10.0.0.{i}")) for i in range(1, 36)}
        assert keys == {"1.2.3.4"}

    def test_direct_caller_forging_x_real_ip(self):
        assert cip.client_ip(_req("1.2.3.4", real_ip="9.9.9.9")) == "1.2.3.4"

    def test_agent_peer_forging_xff(self):
        assert cip.client_ip(_req(AGENT, xff="9.9.9.9")) == AGENT

    def test_platform_subnet_peer_forging_xff(self):
        assert cip.client_ip(_req("172.29.0.3", xff="9.9.9.9")) == "172.29.0.3"

    def test_gateway_peer_is_never_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", GATEWAY)
        assert cip.client_ip(_req(GATEWAY, xff="9.9.9.9")) == GATEWAY

    def test_v4_mapped_peer_forging_is_normalised(self):
        assert cip.client_ip(_req("::ffff:1.2.3.4", xff="9.9.9.9")) == "1.2.3.4"

    def test_ipv6_peer_forging(self):
        assert cip.client_ip(_req("2001:db8::1", xff="9.9.9.9")) == "2001:db8::1"

    def test_operator_cidr_including_trinity_subnet_still_excludes_agents(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "172.16.0.0/12")
        assert cip.client_ip(_req(AGENT, xff="9.9.9.9")) == AGENT


class TestBundledProxyChains:
    def test_nginx_direct(self):
        assert cip.client_ip(_req(NGINX, xff=BROWSER, real_ip=BROWSER)) == BROWSER

    def test_caddy_hop_without_opt_in_stays_on_the_gateway(self):
        assert cip.client_ip(_req(NGINX, xff=f"{BROWSER}, {GATEWAY}")) == GATEWAY

    def test_caddy_hop_with_opt_in(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", GATEWAY)
        assert cip.client_ip(_req(NGINX, xff=f"{BROWSER}, {GATEWAY}")) == BROWSER

    def test_cloudflared_in_front_of_nginx(self):
        assert cip.client_ip(_req(NGINX, xff=f"{BROWSER}, {CLOUDFLARED}")) == BROWSER

    def test_cloudflared_straight_to_backend(self):
        assert cip.client_ip(_req(CLOUDFLARED, xff=BROWSER)) == BROWSER

    def test_agent_through_nginx_cannot_forge(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", GATEWAY)
        req = _req(NGINX, xff=f"9.9.9.9, {GATEWAY}, {AGENT}")
        assert cip.client_ip(req) == AGENT

    def test_x_real_ip_carrying_the_hop_is_not_preferred(self):
        # nginx sets X-Real-IP to its own peer — on the Caddy path that is the
        # gateway, never the browser. XFF wins whenever it is present.
        req = _req(NGINX, xff=f"{BROWSER}", real_ip=GATEWAY)
        assert cip.client_ip(req) == BROWSER

    def test_loopback_peer_is_trusted(self):
        assert cip.client_ip(_req("127.0.0.1", xff=BROWSER)) == BROWSER


class TestForwardedParsing:
    def test_leftmost_garbage_is_never_reached(self):
        assert cip.client_ip(_req(NGINX, xff=f"garbage, {BROWSER}")) == BROWSER

    @pytest.mark.parametrize("bad", ["garbage", "unknown", "_hidden", "", "1.2.3.4:80"])
    def test_malformed_token_stops_at_the_peer(self, bad):
        assert cip.client_ip(_req(NGINX, xff=f"9.9.9.9, {bad}")) == NGINX

    def test_multiple_xff_headers_are_joined_in_order(self):
        assert cip.client_ip(_req(NGINX, xff=["9.9.9.9", BROWSER])) == BROWSER

    def test_all_hops_trusted_falls_back_to_the_peer_not_x_real_ip(self):
        req = _req(NGINX, xff=f"{CLOUDFLARED}, 127.0.0.1", real_ip="9.9.9.9")
        assert cip.client_ip(req) == NGINX

    def test_v4_mapped_forwarded_token(self):
        assert cip.client_ip(_req(NGINX, xff="::ffff:5.6.7.8")) == BROWSER

    def test_x_real_ip_only_without_xff(self):
        assert cip.client_ip(_req(NGINX, real_ip=BROWSER)) == BROWSER

    @pytest.mark.parametrize("bad", ["not-an-ip", "evil\r\nx", CLOUDFLARED])
    def test_x_real_ip_invalid_or_trusted_is_ignored(self, bad):
        assert cip.client_ip(_req(NGINX, real_ip=bad)) == NGINX

    def test_no_client(self):
        assert cip.client_ip(_req(None, xff=BROWSER)) == "unknown"

    def test_unparseable_peer_returned_verbatim(self):
        # Starlette's TestClient reports "testclient"; it is not trusted.
        assert cip.client_ip(_req("testclient", xff=BROWSER)) == "testclient"


class TestTrustedSet:
    @pytest.mark.parametrize("peer", ["10.1.2.3", "192.168.1.1", "172.17.0.2"])
    def test_compat_default_trusts_operator_rfc1918(self, peer):
        assert cip.is_trusted_proxy(peer) is True
        assert cip.client_ip(_req(peer, xff=BROWSER)) == BROWSER

    @pytest.mark.parametrize("peer", [AGENT, GATEWAY, "172.29.0.1", "1.2.3.4"])
    def test_default_does_not_trust(self, peer):
        assert cip.is_trusted_proxy(peer) is False

    def test_trusted_proxies_override(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "203.0.113.0/24")
        assert cip.is_trusted_proxy("203.0.113.1") is True
        assert cip.is_trusted_proxy("10.0.0.1") is False

    def test_trusted_proxies_empty_keeps_loopback_and_names(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXIES", "")
        assert cip.is_trusted_proxy("10.0.0.1") is False
        assert cip.is_trusted_proxy("127.0.0.1") is True
        assert cip.is_trusted_proxy(NGINX) is True

    def test_invalid_entry_is_skipped(self, monkeypatch, caplog):
        monkeypatch.setenv("TRUSTED_PROXIES", "not-a-cidr, 203.0.113.0/24")
        assert cip.is_trusted_proxy("203.0.113.9") is True
        assert "not-a-cidr" in caplog.text

    def test_hops_do_not_make_a_peer_trusted(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "198.51.100.0/24")
        assert cip.is_trusted_proxy("198.51.100.1") is False


class TestNameResolution:
    def test_unresolvable_name_contributes_nothing(self, dns):
        dns.table.pop("trinity-frontend")
        cip._refresh_once()
        assert cip.is_trusted_proxy(NGINX) is False
        assert cip.client_ip(_req(NGINX, xff="9.9.9.9")) == NGINX

    def test_refresh_follows_a_changed_address(self, dns):
        dns.table["trinity-frontend"] = ["172.28.0.6"]
        cip._refresh_once()
        assert cip.is_trusted_proxy("172.28.0.6") is True
        # The old address may now be an agent's: it must lose trust.
        assert cip.is_trusted_proxy(NGINX) is False

    def test_hosts_env_empty_resolves_nothing(self, monkeypatch, dns):
        monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "")
        dns.calls.clear()
        cip._refresh_once()
        assert dns.calls == []
        assert cip.is_trusted_proxy(NGINX) is False

    def test_hosts_env_names_the_proxy(self, monkeypatch, dns):
        dns.table["my-lb"] = ["172.28.0.40"]
        monkeypatch.setenv("TRUSTED_PROXY_HOSTS", "my-lb")
        cip._refresh_once()
        assert cip.is_trusted_proxy("172.28.0.40") is True
        assert cip.is_trusted_proxy(NGINX) is False

    def test_request_path_never_resolves(self, dns):
        dns.calls.clear()
        for _ in range(20):
            cip.client_ip(_req(NGINX, xff=BROWSER))
        assert dns.calls == []

    def test_request_path_starts_the_refresher(self, _isolated):
        _isolated.clear()
        cip._reset_cache_for_tests()
        cip.client_ip(_req("1.2.3.4"))
        cip.client_ip(_req("1.2.3.4"))
        assert _isolated == [1]


class TestPortedFromRootFile:
    """The unit half of `tests/test_ip_rate_limit_fix.py` (#181), which the
    unit island never collected. Re-stated against the resolver: the default
    still trusts an operator's RFC-1918 proxy (e.g. `172.17.0.2`), XFF now wins
    over X-Real-IP, and a forwarded token that is not an address is never
    returned (it stops the walk at the peer)."""

    def test_direct_connection_ignores_xff(self):
        assert cip.client_ip(_req("1.2.3.4", xff="10.0.0.99")) == "1.2.3.4"

    def test_behind_trusted_proxy_both_headers(self):
        req = _req("172.17.0.2", xff=BROWSER, real_ip=BROWSER)
        assert cip.client_ip(req) == BROWSER

    def test_behind_trusted_proxy_xff_only(self):
        assert cip.client_ip(_req("172.17.0.2", xff="9.10.11.12")) == "9.10.11.12"

    def test_rightmost_untrusted_wins(self):
        req = _req("172.17.0.2", xff=f"1.1.1.1, {BROWSER}, 172.17.0.3")
        assert cip.client_ip(req) == BROWSER

    def test_non_address_token_is_never_returned(self):
        req = _req("172.17.0.2", xff="spoofed.1.1.1, real.client.1.2, 172.17.0.3")
        assert cip.client_ip(req) == "172.17.0.2"


class TestPublicRouterDelegates:
    """Every `public.py` limiter and the five routers importing
    `_get_client_ip` from it (a2a, aauth, files, setup, webhooks) use the
    resolver itself — no second copy of the trust decision."""

    def test_names_are_the_resolver(self):
        import routers.public as pub

        assert pub._get_client_ip is cip.client_ip
        assert pub._is_trusted_proxy is cip.is_trusted_proxy
        assert pub._get_trusted_networks is cip.trusted_networks
        assert not hasattr(pub, "_trusted_proxy_networks")

    def test_importers_get_the_resolver(self):
        import routers.setup as setup
        import routers.webhooks as webhooks

        assert setup._get_client_ip is cip.client_ip
        assert webhooks._get_client_ip is cip.client_ip

    def test_public_limiter_keys_on_the_forwarded_client(self):
        import routers.public as pub

        assert pub._get_client_ip(_req(NGINX, xff=BROWSER)) == BROWSER
        assert pub._get_client_ip(_req(AGENT, xff="9.9.9.9")) == AGENT
