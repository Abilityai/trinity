"""trinity-enterprise#838 — trusted internal networks for A2A.

Pins, in four layers:

* **The admin's list** (`services/a2a_trusted_networks.normalize_entries`):
  a tailnet CIDR, an exact host and a `*.domain` are accepted; the platform's
  own Docker networks, loopback, link-local (cloud metadata), anything broader
  than /8 and a one-label wildcard are refused by name.
* **Outbound** (`validate_a2a_endpoint_url`): an untrusted endpoint is refused
  exactly as before; a trusted one may be private/CGNAT and `http://`; a trusted
  NAME that resolves outside the declared CIDRs, or onto metadata, is refused.
* **The source** (`internal_source`): a direct peer in a CIDR is trusted; the
  public tunnel's headers never are; `X-Real-IP` is read only from the platform
  proxy, so an agent on the bridge cannot name its own source.
* **The A2A door**, through the real router: an internal-scope agent is
  unexposed (card 404, keyed 404, anonymous 401) to anyone outside; a trusted
  caller runs a task with no key, attributed to its address, and reads only its
  own tasks; keyless off means a key again; public scope is untouched.
"""
from __future__ import annotations

import json
import socket
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
from services import a2a_gate  # noqa: E402
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
# The source
# --------------------------------------------------------------------------- #
def _req(peer, headers=None):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=peer),
                                 headers={k.lower(): v for k, v in (headers or {}).items()})


def test_a_direct_peer_inside_a_cidr_is_trusted(monkeypatch):
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset())
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset())
    assert tn.internal_source(_req("100.70.1.2"), [TAILNET]) == "100.70.1.2"
    assert tn.internal_source(_req("8.8.8.8"), [TAILNET]) is None


def test_the_public_tunnels_headers_are_never_trusted(monkeypatch):
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset())
    for h in ("CF-Connecting-IP", "Cf-Ray"):
        assert tn.internal_source(_req("100.70.1.2", {h: "x"}), [TAILNET]) is None


def test_x_real_ip_is_read_only_from_the_platform_proxy(monkeypatch):
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset({"172.28.0.10"}))
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset())
    # The proxy forwards a tailnet caller.
    assert tn.internal_source(_req("172.28.0.10", {"X-Real-IP": "100.70.1.2"}), [TAILNET]) == "100.70.1.2"
    # An agent on the same bridge names its own source: not read.
    assert tn.internal_source(_req("172.28.0.44", {"X-Real-IP": "100.70.1.2"}), [TAILNET]) is None


def test_ipv4_mapped_peers_match_their_ipv4_cidr(monkeypatch):
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset())
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset())
    assert tn.internal_source(_req("::ffff:100.70.1.2"), [TAILNET]) == "::ffff:100.70.1.2"


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


def test_a_source_that_is_an_attached_gateway_is_never_trusted(monkeypatch):
    """#838 live test: a host-forwarded call (published port, hairpin NAT)
    arrives from the gateway of a network the backend sits on. A CIDR covering
    that network made the host itself a keyless caller."""
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset())
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset({"172.31.238.1"}))
    assert tn.internal_source(_req("172.31.238.1"), ["172.31.238.0/24"]) is None
    assert tn.internal_source(_req("172.31.238.7"), ["172.31.238.0/24"]) == "172.31.238.7"


def test_an_entry_containing_an_attached_gateway_is_refused_on_write(monkeypatch):
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset({"172.31.238.1"}))
    with pytest.raises(tn.TrustedNetworkError) as exc:
        tn.set_entries(["172.31.238.0/24"])
    assert "gateway" in str(exc.value)


# --------------------------------------------------------------------------- #
# The A2A door, through the real router
# --------------------------------------------------------------------------- #
@pytest.fixture()
def door(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = {
        "exposed": {"inner", "outer"},
        "scope": {"inner": {"scope": "internal", "keyless": True},
                  "outer": {"scope": "public", "keyless": True}},
        "executions": {},
        "dispatched": [],
    }
    fake_db = types.SimpleNamespace(
        get_a2a_exposed=lambda n: n in state["exposed"],
        get_a2a_scope=lambda n: state["scope"].get(n, {"scope": "public", "keyless": True}),
        can_user_access_agent=lambda u, n: True,
        get_execution=lambda eid: state["executions"].get(eid),
        get_nevermined_config=lambda n: None,
    )
    monkeypatch.setattr(a2a, "db", fake_db)
    monkeypatch.setattr(a2a.a2a_payment_gate, "is_priced", lambda n, db=None: None)
    monkeypatch.setattr(a2a, "get_agent_container",
                        lambda n: types.SimpleNamespace(status="running", labels={}))

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": []}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    async def _dispatch(**kw):
        state["dispatched"].append(kw)
        state["executions"]["exec-1"] = {"agent_name": kw["agent_name"], "status": "success",
                                         "triggered_by": "a2a", "source_host": kw.get("source_host"),
                                         "response": "ok"}
        return types.SimpleNamespace(execution_id="exec-1", status="success", response="ok", error=None)
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _dispatch)

    class _Audit:
        async def log(self, **kw):
            return None
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    class _Idem:
        def begin(self, scope, key):
            state["idem_scope"] = scope
            return types.SimpleNamespace(replay=False, in_flight=False, snapshot=None, key=None)

        def complete(self, *a):
            pass

        def fail(self, *a):
            pass
    monkeypatch.setattr(a2a, "idempotency_service", _Idem())
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a, **k: None)
    a2a_gate.clear_provider()

    monkeypatch.setattr(tn, "get_entries", lambda: [TAILNET])
    monkeypatch.setattr(tn, "_proxy_ips", lambda: frozenset())
    monkeypatch.setattr(tn, "_local_gateways", lambda: frozenset())
    monkeypatch.setattr(tn, "internal_base_url", lambda: "http://inst.tail1234.ts.net")

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    principal = {"user": None}
    app.dependency_overrides[deps.get_user_or_anonymous] = lambda: principal["user"]
    peer = {"ip": "8.8.8.8"}

    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(peer["ip"], 40000))
        await app(scope, receive, send)

    http = TestClient(asgi)

    def call(agent, method="message/send", params=None, *, src="8.8.8.8", key=False, headers=None):
        peer["ip"] = src
        principal["user"] = (types.SimpleNamespace(id=1, username="alice", email="a@example.com",
                                                   role="user", agent_name=None, mcp_key_id="k1")
                             if key else None)
        if params is None:
            params = {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}],
                                  "messageId": "m1"}}
        return http.post(f"/a2a/{agent}", headers=headers or {},
                         content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))

    def card(agent, *, src="8.8.8.8", headers=None):
        peer["ip"] = src
        return http.get(f"/a2a/{agent}/.well-known/agent-card.json", headers=headers or {})

    return types.SimpleNamespace(call=call, card=card, state=state)


def test_an_internal_agent_is_unexposed_to_everyone_outside(door):
    assert door.card("inner").status_code == 404
    assert door.call("inner", key=True).status_code == 404
    assert door.call("inner").status_code == 401
    assert door.state["dispatched"] == []


def test_the_public_tunnel_is_outside_even_from_a_trusted_address(door):
    assert door.card("inner", src="100.70.1.2", headers={"Cf-Ray": "x"}).status_code == 404
    assert door.call("inner", src="100.70.1.2", headers={"CF-Connecting-IP": "1.2.3.4"}).status_code == 401


def test_a_trusted_caller_runs_a_task_without_a_key_attributed_to_its_address(door):
    r = door.call("inner", src="100.70.1.2")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"]["state"] == "completed"
    (kw,) = door.state["dispatched"]
    assert kw["source_host"] == "100.70.1.2" and kw["triggered_by"] == "a2a"
    assert kw["source_user_id"] is None and kw["source_mcp_key_id"] is None
    assert door.state["idem_scope"] == "a2a:inner:host:100.70.1.2"


def test_a_keyless_caller_reads_only_its_own_tasks(door):
    door.call("inner", src="100.70.1.2")
    mine = door.call("inner", "tasks/get", {"id": "exec-1"}, src="100.70.1.2")
    assert mine.json()["result"]["id"] == "exec-1"
    other = door.call("inner", "tasks/get", {"id": "exec-1"}, src="100.70.9.9")
    assert other.json()["error"]["message"] == "Task not found"


def test_the_internal_card_advertises_the_internal_url(door):
    r = door.card("inner", src="100.70.1.2")
    assert r.status_code == 200
    assert r.json()["url"] == "http://inst.tail1234.ts.net/a2a/inner"


def test_keyless_off_means_a_trusted_caller_needs_a_key(door):
    door.state["scope"]["inner"]["keyless"] = False
    assert door.call("inner", src="100.70.1.2").status_code == 401
    assert door.call("inner", src="100.70.1.2", key=True).status_code == 200
    assert door.state["dispatched"][0]["source_host"] is None


def test_public_scope_is_untouched(door):
    assert door.card("outer").status_code == 200
    assert door.call("outer", key=True).status_code == 200
    assert door.call("outer", src="100.70.1.2").status_code == 401  # no key, no payment: as today
    assert door.state["dispatched"][0]["source_host"] is None


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_scope_defaults_to_public_and_round_trips():
    import uuid

    from database import db
    from db.engine import get_engine
    from db.tables import users
    from sqlalchemy import insert
    from utils.helpers import utc_now_iso

    owner = f"owner-838-{uuid.uuid4().hex[:6]}"
    with get_engine().begin() as conn:
        conn.execute(insert(users).values(username=owner, role="admin", email=f"{owner}@example.com",
                                          created_at=utc_now_iso(), updated_at=utc_now_iso()))
    agent = f"agent-838-{uuid.uuid4().hex[:8]}"
    assert db.get_a2a_scope(agent) == {"scope": "public", "keyless": False}  # no row
    assert db.register_agent_owner(agent, owner)
    assert db.get_a2a_scope(agent) == {"scope": "public", "keyless": True}
    assert db.set_a2a_scope(agent, scope="internal", keyless=False)
    assert db.get_a2a_scope(agent) == {"scope": "internal", "keyless": False}
    with pytest.raises(ValueError):
        db.set_a2a_scope(agent, scope="everyone", keyless=True)
