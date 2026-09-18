"""ent#623 — AAuth on the A2A seam: inbound task endpoint, card, discovery routes,
outbound registry, service and client.

What is proven here, AC by AC:
  * flag OFF → the signed request is just an unauthenticated bearer request
    (plain 401, no AAuth headers) — the bearer path is untouched;
  * flag ON → bearer still works on the same endpoint (dual acceptance), and a
    bearer-less 401 advertises how to sign;
  * allow-listed AAuth identity → task runs with NO Trinity user attached and the
    audit/activity rows name the caller; unlisted → 403; bad signature → 401
    problem+json BEFORE the JSON-RPC body is parsed; every missing/erroring
    allow-list provider refuses (fail-closed);
  * the card lists `aauth` after `bearerAuth`;
  * outbound: an `aauth` endpoint signs as the calling agent, never carries a
    credential, refuses human principals and an inert flag before any egress,
    and signs the exact bytes it sends to the registered host (not the pinned IP).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import dependencies as deps  # noqa: E402
import routers.a2a as a2a  # noqa: E402
import routers.aauth as aauth_router  # noqa: E402
from services import a2a_client, a2a_gate, a2a_outbound, a2a_outbound_service  # noqa: E402
from services.aauth import config, discovery, httpsig, inbound, jose, keys, signer, verifier  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

pytestmark = pytest.mark.unit

A_ISS = "https://a.example"
B_ISS = "https://b.example"
ECHO = "aauth:echo@a.example"


# --------------------------------------------------------------------------- #
# Callee (B) harness
# --------------------------------------------------------------------------- #
class _Provider:
    def __init__(self, identities=(ECHO,)):
        self.identities = list(identities)

    def is_inbound_allowed(self, agent_name, caller_identity):
        return True

    def inbound_identities(self, agent_name):
        return list(self.identities)


class _Replay:
    seen: set = set()

    def claim(self, key, ttl):
        if key in _Replay.seen:
            return False
        _Replay.seen.add(key)
        return True


@pytest.fixture()
def b(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    state = {"issuer": B_ISS, "exposed": {"bot"}, "dispatched": [], "audit": [], "activity": [],
             "owners": {}, "executions": {}}

    monkeypatch.setattr(a2a, "db", types.SimpleNamespace(
        get_a2a_exposed=lambda name: name in state["exposed"],
        can_user_access_agent=lambda user, name: True,
        get_execution=lambda eid: state["executions"].get(eid),
        cancel_queued_execution=lambda eid, reason=None: False,
    ))
    monkeypatch.setattr(a2a, "get_agent_container",
                        lambda name: types.SimpleNamespace(status="running", labels={}))

    async def _tmpl(name, container):
        return {"display_name": name, "capabilities": ["chat"]}
    monkeypatch.setattr(a2a, "_fetch_template_data", _tmpl)

    async def _dispatch(**kwargs):
        state["dispatched"].append(kwargs)
        return types.SimpleNamespace(execution_id="exec-1", status="success", response="pong", error=None)
    monkeypatch.setattr(a2a, "dispatch_and_await_terminal", _dispatch)

    class _Audit:
        async def log(self, **kwargs):
            state["audit"].append(kwargs)
    monkeypatch.setattr(a2a, "platform_audit_service", _Audit())

    class _Idem:
        def begin(self, scope, key):
            state.setdefault("scopes", []).append(scope)
            return types.SimpleNamespace(replay=False, in_flight=False, snapshot=None, key=None)

        def complete(self, *a):
            pass

        def fail(self, *a):
            pass
    monkeypatch.setattr(a2a, "idempotency_service", _Idem())
    monkeypatch.setattr(a2a.rate_limiter, "enforce", lambda *a, **k: None)

    monkeypatch.setattr(config, "active_issuer", lambda: state["issuer"])
    monkeypatch.setattr(verifier, "RedisReplayStore", _Replay)
    _Replay.seen = set()

    issuer_key = Ed25519PrivateKey.generate()
    issuer_jwk = jose.public_jwk(issuer_key.public_key())
    kid = jose.thumbprint(issuer_jwk)
    state["fetches"] = []

    async def _resolve(iss, k):
        state["fetches"].append(iss)
        if iss != A_ISS or k != kid:
            raise discovery.DiscoveryError("unknown_key", "nope")
        return {**issuer_jwk, "kid": kid}
    monkeypatch.setattr(discovery, "resolve_key", _resolve)

    async def _activity(agent_name, verified, **kw):
        state["activity"].append((agent_name, verified.identity, kw))
    monkeypatch.setattr(inbound, "record_activity", _activity)
    monkeypatch.setattr(inbound, "remember_execution",
                        lambda eid, ident: state["owners"].__setitem__(eid, ident))
    monkeypatch.setattr(inbound, "execution_owned_by",
                        lambda eid, ident: state["owners"].get(eid) == ident)

    a2a_gate.register_provider(_Provider())

    app = FastAPI()
    app.include_router(a2a.a2a_server_router)
    app.include_router(aauth_router.router)
    client = TestClient(app)

    def sign_as(local, body, *, path="/a2a/bot", tamper_body=None, extra=None):
        agent_key = Ed25519PrivateKey.generate()
        now = int(__import__("time").time())
        token = jose.sign_jws(
            {"alg": "Ed25519", "typ": "aa-agent+jwt", "kid": kid},
            {"iss": A_ISS, "dwk": "aauth-agent.json", "sub": f"aauth:{local}@a.example",
             "jti": os.urandom(8).hex(), "iat": now, "exp": now + 600,
             "cnf": {"jwk": jose.public_jwk(agent_key.public_key())}},
            issuer_key,
        )
        raw = json.dumps(body).encode()
        headers = httpsig.sign(method="POST", authority_value="b.example", path=path, agent_token=token,
                               body=raw, content_type="application/json", created=now,
                               private_key=agent_key).as_dict()
        headers["Content-Type"] = "application/json"
        headers.update(extra or {})
        return client.post(path, content=tamper_body if tamper_body is not None else raw, headers=headers)

    yield types.SimpleNamespace(http=client, app=app, state=state, sign_as=sign_as)
    a2a_gate.clear_provider()


def _send(text="ping", mid="m-1"):
    return {"jsonrpc": "2.0", "id": "r1", "method": "message/send",
            "params": {"message": {"messageId": mid, "parts": [{"kind": "text", "text": text}]}}}


def _bearer(b):
    user = types.SimpleNamespace(id=7, username="alice", email="alice@example.com",
                                 role="user", agent_name=None, mcp_key_id="k7")
    b.app.dependency_overrides[deps.get_current_user] = lambda: user
    return user


# ---- flag / dual acceptance ------------------------------------------------
def test_flag_off_signed_request_is_a_plain_bearer_401(b):
    b.state["issuer"] = None
    r = b.sign_as("echo", _send())
    assert r.status_code == 401
    assert "signature-error" not in r.headers
    assert "aauth-requirement" not in r.headers
    assert b.state["fetches"] == [] and b.state["dispatched"] == []


def test_flag_off_bearer_path_unchanged(b):
    b.state["issuer"] = None
    _bearer(b)
    r = b.http.post("/a2a/bot", json=_send())
    assert r.status_code == 200 and r.json()["result"]["status"]["state"] == "completed"


def test_flag_on_bearer_still_works(b):
    user = _bearer(b)
    r = b.http.post("/a2a/bot", json=_send())
    assert r.status_code == 200
    assert b.state["dispatched"][0]["source_user_id"] == user.id
    assert b.state["audit"][-1]["actor_user"] is user


def test_flag_on_unauthenticated_401_advertises_aauth(b):
    r = b.http.post("/a2a/bot", json=_send())
    assert r.status_code == 401
    assert r.headers["aauth-requirement"] == "requirement=agent-token"
    assert '"signature-key"' in r.headers["accept-signature"]
    assert r.headers["accept-signature-scheme"] == "jwt"


def test_signature_key_never_falls_back_to_bearer(b):
    _bearer(b)
    r = b.http.post("/a2a/bot", json=_send(), headers={"Signature-Key": "sig=jwt;jwt=\"x.y.z\""})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert b.state["dispatched"] == []


# ---- allow-listed caller ----------------------------------------------------
def test_listed_identity_runs_task_without_a_trinity_user(b):
    r = b.sign_as("echo", _send())
    assert r.status_code == 200, r.text
    assert r.json()["result"]["artifacts"][0]["parts"][0]["text"] == "pong"
    call = b.state["dispatched"][0]
    assert call["triggered_by"] == "a2a"
    assert call["source_user_id"] is None and call["source_user_email"] is None
    assert call["source_mcp_key_id"] is None
    row = b.state["audit"][-1]
    assert row["event_action"] == "a2a_task"
    assert row["actor_external_id"] == ECHO and "actor_user" not in row
    assert row["details"]["caller_identity"] == ECHO
    assert row["details"]["verification"] == "verified"
    assert row["details"]["allowlist"] == "allowed"
    assert b.state["activity"][0][1] == ECHO
    assert b.state["owners"] == {"exec-1": ECHO}
    # Its own namespace, distinct from any bearer principal's (ent#623).
    assert b.state["scopes"] == [f"a2a:bot:aauth|{ECHO}"]


def test_unlisted_identity_of_a_trusted_issuer_is_403(b):
    r = b.sign_as("counter", _send())
    assert r.status_code == 403
    assert "signature-error" not in r.headers
    assert b.state["dispatched"] == []
    row = b.state["audit"][-1]
    assert row["event_action"] == "a2a_aauth_denied"
    assert row["details"]["allowlist"] == "denied"
    assert row["actor_external_id"] == "aauth:counter@a.example"


def test_bad_signature_is_401_before_the_body_is_parsed(b):
    r = b.sign_as("echo", _send(), tamper_body=b"{not json at all")
    assert r.status_code == 401
    assert r.headers["signature-error"] == "error=invalid_signature"
    body = r.json()
    assert body["type"] == "urn:ietf:params:sig-error:invalid_signature"
    assert "jsonrpc" not in body                       # never reached the envelope parser
    assert b.state["dispatched"] == []
    assert b.state["audit"][-1]["event_action"] == "a2a_aauth_refused"


def test_non_exposed_agent_is_uniform_404(b):
    b.state["exposed"] = set()
    r = b.sign_as("echo", _send())
    assert r.status_code == 404 and b.state["fetches"] == []


@pytest.mark.parametrize("provider", [
    None,
    types.SimpleNamespace(is_inbound_allowed=lambda a, c: True),                     # no AAuth method
    types.SimpleNamespace(is_inbound_allowed=lambda a, c: True,
                          inbound_identities=lambda a: (_ for _ in ()).throw(RuntimeError("db down"))),
    types.SimpleNamespace(is_inbound_allowed=lambda a, c: True, inbound_identities=lambda a: ECHO),
    _Provider(identities=()),
])
def test_allowlist_provider_failures_refuse(b, provider):
    a2a_gate.clear_provider()
    if provider is not None:
        a2a_gate.register_provider(provider)
    r = b.sign_as("echo", _send())
    assert r.status_code == 401
    assert b.state["dispatched"] == [] and b.state["fetches"] == []


def test_unaudited_noise_before_the_issuer_pre_gate(b):
    b.http.post("/a2a/bot", json=_send(), headers={"Signature-Key": "sig=hwk;x=\"1\""})
    assert b.state["audit"] == []


def test_stream_is_refused_for_aauth(b):
    body = _send()
    body["method"] = "message/stream"
    r = b.sign_as("echo", body)
    assert r.status_code == 200
    assert r.json()["error"]["code"] == a2a._A2A_UNSUPPORTED
    assert b.state["dispatched"] == []


def test_tasks_get_is_scoped_to_the_identity_that_started_it(b):
    b.state["executions"]["exec-9"] = {"agent_name": "bot", "status": "success", "response": "x"}
    get = {"jsonrpc": "2.0", "id": "g", "method": "tasks/get", "params": {"id": "exec-9"}}
    r = b.sign_as("echo", get)
    assert r.json()["error"]["code"] == a2a._A2A_TASK_NOT_FOUND
    b.state["owners"]["exec-9"] = ECHO
    r = b.sign_as("echo", get)
    assert r.json()["result"]["status"]["state"] == "completed"


# ---- card + discovery documents ----------------------------------------------
def test_card_lists_aauth_after_bearer_only_when_live(b):
    card = b.http.get("/a2a/bot/.well-known/agent-card.json").json()
    assert card["security"] == [{"bearerAuth": []}, {"aauth": []}]
    assert card["securitySchemes"]["aauth"]["scheme"] == "signature"
    assert B_ISS + "/.well-known/aauth-resource.json" in card["securitySchemes"]["aauth"]["description"]
    b.state["issuer"] = None
    card = b.http.get("/a2a/bot/.well-known/agent-card.json").json()
    assert card["security"] == [{"bearerAuth": []}]
    assert "aauth" not in card["securitySchemes"]


def test_discovery_documents_404_when_inert(b):
    b.state["issuer"] = None
    for path in ("/.well-known/aauth-agent.json", "/.well-known/aauth-jwks.json",
                 "/.well-known/aauth-resource.json"):
        assert b.http.get(path).status_code == 404


def test_discovery_documents_when_live(b, monkeypatch):
    k = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(k.public_key())
    monkeypatch.setattr(keys, "jwks", lambda: {"keys": [{**jwk, "kid": "k1"}]})
    agent = b.http.get("/.well-known/aauth-agent.json").json()
    assert agent["issuer"] == B_ISS
    assert agent["jwks_uri"] == B_ISS + "/.well-known/aauth-jwks.json"
    assert b.http.get("/.well-known/aauth-jwks.json").json()["keys"][0]["kid"] == "k1"
    res = b.http.get("/.well-known/aauth-resource.json").json()
    assert res["access_mode"] == "agent-token"
    assert res["additional_signature_components"] == ["content-digest", "content-type"]


# --------------------------------------------------------------------------- #
# Outbound (A)
# --------------------------------------------------------------------------- #
PEER = ValidatedPublicUrl(url="https://b.example/a2a/bot", hostname="b.example", port=443,
                          addresses=("93.184.216.34",))


@pytest.fixture()
def registry(monkeypatch):
    rows = []
    monkeypatch.setattr(a2a_outbound, "_load_endpoint_records", lambda: [dict(r) for r in rows])
    monkeypatch.setattr(a2a_outbound, "_store_endpoint_records",
                        lambda records: (rows.clear(), rows.extend(dict(r) for r in records)))
    import utils.url_validation as uv

    def _validate(url):
        port = httpx.URL(url).port or 443
        return ValidatedPublicUrl(url=url, hostname=httpx.URL(url).host, port=port, addresses=("93.184.216.34",))
    monkeypatch.setattr(uv, "validate_a2a_endpoint_url", _validate)
    a2a_outbound.clear_provider()
    return rows


def test_registry_aauth_refuses_credentials_and_non_default_ports(registry):
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("peer", PEER.url, "tok", auth_scheme="aauth")
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("peer", "https://b.example:8443/a2a/bot", auth_scheme="aauth")
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("peer", PEER.url, auth_scheme="mtls")


def test_registry_switch_to_aauth_drops_credential_and_omitted_keeps_it(registry):
    a2a_outbound.upsert_endpoint("peer", PEER.url, "tok")
    assert registry[0]["credential"] == "tok"
    rec = a2a_outbound.upsert_endpoint("peer", PEER.url, auth_scheme="aauth")
    assert rec["auth_scheme"] == "aauth" and rec["has_credentials"] is False
    assert "credential" not in registry[0]
    rec = a2a_outbound.upsert_endpoint("Peer", PEER.url)                 # rename/repoint, scheme omitted
    assert rec["auth_scheme"] == "aauth"
    with pytest.raises(a2a_outbound.EndpointValidationError):
        a2a_outbound.upsert_endpoint("peer", PEER.url, "tok")          # still aauth → no credential
    ep = a2a_outbound.resolve_endpoint("bot", "peer")
    assert ep.auth_scheme == "aauth" and ep.credential is None


def test_registry_legacy_record_is_bearer_and_aauth_record_never_sends_a_secret(registry):
    registry.append({"id": "e1", "name": "old", "url": PEER.url, "credential": "tok"})
    registry.append({"id": "e2", "name": "odd", "url": PEER.url, "credential": "tok", "auth_scheme": "aauth"})
    old = a2a_outbound.resolve_endpoint("bot", "old")
    assert old.auth_scheme == "bearer" and old.credential == "tok"
    assert a2a_outbound.list_oss_endpoints()[0]["auth_scheme"] == "bearer"
    assert a2a_outbound.resolve_endpoint("bot", "odd").credential is None


@pytest.fixture()
def outbound(monkeypatch):
    a2a_outbound.clear_provider()
    a2a_client.clear_dialect_cache()
    monkeypatch.setattr(a2a_outbound_service, "is_outbound_enabled", lambda: True)
    monkeypatch.setattr(a2a_outbound_service, "_enforce_bounds", lambda agent_name: None)

    async def _no_activity(*a, **k):
        return None
    monkeypatch.setattr(a2a_outbound_service, "_record_activity", _no_activity)

    async def _validate(url):
        return PEER
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)

    ep = a2a_outbound.ResolvedEndpoint(id="e1", name="bravo", url=PEER.url, auth_scheme="aauth")

    class _P:
        def resolve_endpoint(self, agent, ref):
            return ep if ref == "bravo" else None

        def list_endpoints(self, agent):
            return []
    a2a_outbound.register_provider(_P())

    issuer_key = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(issuer_key.public_key())
    kid = jose.thumbprint(jwk)
    state = {"issuer": A_ISS, "requests": []}
    monkeypatch.setattr(config, "active_issuer", lambda: state["issuer"])
    monkeypatch.setattr(keys, "get_instance_key",
                        lambda: keys.InstanceKey(kid=kid, public_jwk={**jwk, "kid": kid}, private_key=issuer_key))
    signer.reset_cache()

    card = {"protocolVersion": "0.3.0", "name": "bot", "url": PEER.url}

    def handle(request):
        state["requests"].append(request)
        if request.method == "GET":
            if request.url.path == "/a2a/bot/.well-known/agent-card.json":
                return httpx.Response(200, stream=httpx.ByteStream(json.dumps(card).encode()))
            return httpx.Response(404)
        rpc = json.loads(request.content)
        result = {"kind": "task", "id": "t1", "status": {"state": "completed"},
                  "artifacts": [{"parts": [{"kind": "text", "text": "pong"}]}]}
        return httpx.Response(200, stream=httpx.ByteStream(
            json.dumps({"jsonrpc": "2.0", "id": rpc["id"], "result": result}).encode()))

    real_call = a2a_client.call_endpoint

    async def _call(**kwargs):
        kwargs["client_factory"] = lambda timeout: httpx.AsyncClient(
            transport=httpx.MockTransport(handle), timeout=timeout, follow_redirects=False, trust_env=False)
        return await real_call(**kwargs)
    monkeypatch.setattr(a2a_client, "call_endpoint", _call)

    state.update(issuer_jwk={**jwk, "kid": kid})
    yield state
    a2a_outbound.clear_provider()
    signer.reset_cache()


def _call(**kw):
    return asyncio.run(a2a_outbound_service.call_agent(
        agent_name="echo", endpoint_ref="bravo", message="ping", dedup_label="d1", **kw))


def test_outbound_aauth_refuses_a_human_principal_before_egress(outbound):
    for caller in (None, "someone-else"):
        with pytest.raises(a2a_outbound_service.A2AAAuthRefused) as ei:
            _call(caller_agent_name=caller)
        assert ei.value.status_code == 403 and ei.value.reason == "aauth_agent_only"
    assert outbound["requests"] == []


def test_outbound_aauth_refuses_when_the_instance_key_is_unreadable(outbound, monkeypatch):
    """The documented fail-closed state (ent#435 envelope no longer decrypts) must
    refuse before any egress — not raise `SigningUnavailable` out of `_rpc`, which
    no handler catches and which would 500 after the card fetch already went out."""
    def _dead():
        raise keys.SigningKeyUnavailable("unreadable")
    monkeypatch.setattr(keys, "get_instance_key", _dead)
    signer.reset_cache()
    with pytest.raises(a2a_outbound_service.A2AAAuthRefused) as ei:
        _call(caller_agent_name="echo")
    assert ei.value.reason == "aauth_disabled"
    assert outbound["requests"] == []


def test_outbound_signing_failure_mid_call_is_a_named_refusal(outbound, monkeypatch):
    """Flag flipped between the pre-check and the POST: a named A2ACallError the
    router maps, never an unhandled 500."""
    calls = []
    real = signer.sign_request

    def _flip(**kwargs):
        calls.append(1)
        raise signer.SigningUnavailable("flag flipped mid-call")
    monkeypatch.setattr(signer, "sign_request", _flip)
    with pytest.raises(a2a_client.A2ACallError) as ei:
        _call(caller_agent_name="echo")
    assert ei.value.reason == "aauth_signing_unavailable" and calls


def test_card_fetch_falls_back_only_when_the_card_is_absent(outbound, monkeypatch):
    """A timeout on the path-relative card must propagate: retrying the origin
    would add a second card budget and push the call past the 45 s total
    deadline, which cancels a POST the peer may already have accepted."""
    from utils.url_validation import ValidatedPublicUrl as _V

    seen = []

    async def _fetch(client, validated, card_url=None):
        seen.append(card_url)
        raise a2a_client.A2ACallError("timeout", "The A2A endpoint timed out.")
    monkeypatch.setattr(a2a_client, "fetch_card", _fetch)
    with pytest.raises(a2a_client.A2ACallError) as ei:
        asyncio.run(a2a_client.fetch_card_for(None, PEER, path_relative_first=True))
    assert ei.value.reason == "timeout"
    assert len(seen) == 1, "the origin card was fetched after a timeout"


def test_outbound_aauth_refuses_when_inert_before_egress(outbound):
    outbound["issuer"] = None
    with pytest.raises(a2a_outbound_service.A2AAAuthRefused) as ei:
        _call(caller_agent_name="echo")
    assert ei.value.status_code == 400 and ei.value.reason == "aauth_disabled"
    assert outbound["requests"] == []


def test_outbound_aauth_call_is_signed_over_the_bytes_sent(outbound):
    outcome = _call(caller_agent_name="echo")
    assert outcome.result.text == "pong" and outcome.auth_scheme == "aauth"
    assert a2a_outbound_service.audit_details(outcome)["auth"] == "aauth"
    card_req, rpc = outbound["requests"]
    assert card_req.url.path == "/a2a/bot/.well-known/agent-card.json"   # path-relative card first
    assert "authorization" not in rpc.headers and "authorization" not in card_req.headers
    assert "signature-key" not in card_req.headers                          # the card fetch is unsigned
    assert rpc.url.host == "93.184.216.34" and rpc.headers["host"] == "b.example"

    async def resolve(iss, kid):
        return outbound["issuer_jwk"]

    async def read_body():
        return rpc.content

    verified, body = asyncio.run(verifier.verify_request(
        verifier.InboundRequest(method="POST", raw_path=rpc.url.raw_path.decode(),
                                headers={k.lower(): v for k, v in rpc.headers.items()},
                                content_length=len(rpc.content)),
        expected_authority="b.example", trusted_identities=[ECHO],
        read_body=read_body, max_body_bytes=1 << 20,
        replay_store=_Replay(), key_resolver=resolve,
    ))
    assert verified.identity == "aauth:echo@a.example"
    assert json.loads(body)["params"]["message"]["parts"][0]["text"] == "ping"


def test_outbound_bearer_endpoint_is_unchanged(outbound, monkeypatch):
    ep = a2a_outbound.ResolvedEndpoint(id="e2", name="plain", url=PEER.url, credential="tok")

    class _P:
        def resolve_endpoint(self, agent, ref):
            return ep

        def list_endpoints(self, agent):
            return []
    a2a_outbound.register_provider(_P())
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return a2a_client.A2AResult(state="completed", text="ok", task_id=None, context_id=None,
                                    truncated=False, protocol_version="0.3", host="b.example")
    monkeypatch.setattr(a2a_client, "call_endpoint", fake)
    outcome = _call()                      # a human principal is fine for bearer
    assert "signer" not in seen and seen["credential"] == "tok"
    assert "auth" not in a2a_outbound_service.audit_details(outcome)
