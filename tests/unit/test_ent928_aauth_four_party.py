"""ent#928 — AAuth four-party resources: the resource token's ``aud`` may name an
access server (draft §Federated Authorization Access, §PS–AS federation).

What is proven here, on synthetic keys and no network (every fetch is an
``httpx.MockTransport`` or an injected resolver; ``a2a_client.validate_endpoint``
is stubbed by the ent#879 harness):

  * ``resource_token.verify_resource_token`` accepts a four-party ``aud`` ONLY
    when it is a strict server identifier whose ``/.well-known/aauth-access.json``
    is fetchable and names itself as ``issuer``; the fetch happens only AFTER the
    resource's signature verifies; every refusal is the named check ``aud`` and
    no person-server exchange follows;
  * ``discovery.access_server_metadata``: the document rules, the pinned egress
    (a private address is refused by the same validator every other discovery
    fetch uses), positive and negative caches;
  * ``auth_token.inspect_auth_token``: ``iss``/``dwk`` per mode — a PS-minted
    token is refused in four-party, an AS-minted one in three-party, ``ps``
    must be ours in both; ``HeldAuthToken.iss`` is the real issuer;
  * the ladder end to end (real signer → egress → MockTransport): the resource
    token still goes to OUR person server's ``auth_token_endpoint``, the access
    server receives no request at all, the AS-minted auth token is held and
    reused on the second call with no PS trip, the pending (202 → poll) path
    inspects the eventual token with the same expectation, and the audit row
    says ``four-party`` and names the AS — never a token.

Sync tests with ``asyncio.run`` (tests/unit/pytest.ini has no asyncio_mode).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import a2a_client  # noqa: E402
from services.a2a_client import A2ACallError  # noqa: E402
from services.aauth import auth_token, discovery, jose, ps_client, resource_call, resource_token  # noqa: E402
from services.aauth import person_token as pt  # noqa: E402

from unit.test_ent879_aauth_resource_client import (  # noqa: E402,F401 — fixtures are discovered by name
    AGENT, NOW, OTHER, PATH, PRESENTED, PS, RES, FakePS2, FakeResource, ResourceKey,
    bind_person_token, call, harness, mint, presented_of, resume, verify, wired)

pytestmark = pytest.mark.unit

AS = "https://as.example"
ACCESS_METADATA = {"issuer": AS, "jwks_uri": AS + "/.well-known/jwks.json",
                   "auth_token_endpoint": AS + "/aauth/token/auth"}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeAccessServer:
    """An access server that publishes its metadata and records every other request (there must be none)."""

    def __init__(self, metadata=None, status=200):
        self.metadata = dict(ACCESS_METADATA) if metadata is None else metadata
        self.status = status
        self.metadata_fetches = 0
        self.other_requests = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/.well-known/aauth-access.json":
            self.metadata_fetches += 1
            body = self.metadata if isinstance(self.metadata, (bytes, str)) else json.dumps(self.metadata)
            raw = body.encode() if isinstance(body, str) else body
            return httpx.Response(self.status, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(raw))
        self.other_requests.append(request)
        return httpx.Response(599, stream=httpx.ByteStream(b"the agent must never dial the access server"))


class AccessResolver:
    """An injected ``access_server_resolver`` that records calls and answers per a script."""

    def __init__(self, answer=None, error=None):
        self.answer = dict(ACCESS_METADATA) if answer is None else answer
        self.error = error
        self.calls = []

    async def __call__(self, aud):
        self.calls.append(aud)
        if self.error is not None:
            raise self.error
        return self.answer


def as_auth_token_for(request: httpx.Request, *, sub=None, header=None, **claims):
    """What the PS relays after federating: an auth token MINTED BY THE ACCESS SERVER."""
    doc = json.loads(request.content.decode()) if request.content else {}
    _, pres, _, _ = jose.decode_unverified(doc["presented_token"])
    body = {"iss": AS, "ps": PS, "dwk": "aauth-access.json", "aud": RES, "sub": sub or pres["sub"],
            "scope": "senzing", "jti": "at-" + os.urandom(4).hex(), "iat": NOW, "exp": NOW + 3600,
            "cnf": {"jwk": pres["cnf"]["jwk"]}}
    body.update(claims)
    hdr = {"alg": "RS256", "typ": "aa-auth+jwt", "kid": "as-kid"}
    hdr.update(header or {})
    return jose.sign_jws(hdr, body, Ed25519PrivateKey.generate())


@pytest.fixture()
def four_party(wired, monkeypatch):  # noqa: F811 — the ent#879 fixture, by name
    """The ent#879 live harness plus an access server on a third host."""
    res, ps, access = wired["res"], wired["ps_fake"], FakeAccessServer()
    wired["as"] = access

    def route(request):
        host = request.headers.get("host") or request.url.host
        if host == "res.example":
            return res.handle(request)
        if host == "ps.example":
            return ps.handle(request)
        if host == "as.example":
            return access.handle(request)
        return httpx.Response(404, stream=httpx.ByteStream(b""))

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(a2a_client, "_http_client", factory)
    return wired


# --------------------------------------------------------------------------- #
# resource_token — the sink, four-party
# --------------------------------------------------------------------------- #
def test_four_party_aud_is_accepted_when_the_access_server_is_discoverable():
    rk, resolver = ResourceKey(), AccessResolver()
    got = verify(rk, mint(rk, aud=AS), access_server_resolver=resolver)
    assert got.aud == AS and got.as_url == AS and got.auth_mode == "four-party"
    assert got.ps == PS and got.sub == PRESENTED["sub"] and got.presented_jti == PRESENTED["jti"]
    assert resolver.calls == [AS]


def test_three_party_aud_is_unchanged_and_never_dials_anything():
    rk, resolver = ResourceKey(), AccessResolver(error=AssertionError("must not be called"))
    got = verify(rk, mint(rk), access_server_resolver=resolver)
    assert got.aud == PS and got.as_url is None and got.auth_mode == "three-party"
    assert resolver.calls == []


def test_four_party_access_server_is_fetched_only_after_the_signature_verifies():
    """A forged token must not be able to make this backend dial the host it names."""
    rk, resolver = ResourceKey(), AccessResolver()
    with pytest.raises(resource_token.ResourceTokenError) as ei:
        verify(rk, mint(rk, aud=AS, key=Ed25519PrivateKey.generate()), access_server_resolver=resolver)
    assert ei.value.check == "signature" and resolver.calls == []
    rk = ResourceKey()                                   # a fresh key log: a claim failure costs no fetch at all
    with pytest.raises(resource_token.ResourceTokenError) as ei:
        verify(rk, mint(rk, aud=AS, agent_jkt="someone-elses-key"), access_server_resolver=resolver)
    assert ei.value.check == "agent_jkt" and resolver.calls == [] and rk.calls == []


@pytest.mark.parametrize("error", [
    discovery.DiscoveryError("unknown_key", "could not fetch issuer metadata (endpoint_http_404)"),   # no document
    discovery.DiscoveryError("issuer_mismatch", "access server metadata names a different issuer"),
    discovery.DiscoveryError("issuer_missing", "access server metadata has no issuer"),
    discovery.DiscoveryError("unknown_key", "could not fetch issuer metadata (endpoint_private_address)"),
    discovery.DiscoveryError("unknown_key", "access server metadata fetch timed out"),
])
def test_four_party_aud_without_a_discoverable_access_server_is_the_named_refusal(error):
    rk, resolver = ResourceKey(), AccessResolver(error=error)
    with pytest.raises(resource_token.ResourceTokenError) as ei:
        verify(rk, mint(rk, aud=AS), access_server_resolver=resolver)
    assert ei.value.check == "aud" and error.code in ei.value.detail
    assert resolver.calls == [AS]


@pytest.mark.parametrize("aud", [
    "http://as.example",              # not https
    "https://as.example:8443",        # a port
    "https://as.example/aauth",       # a path
    "https://as.example/",            # a trailing slash
    "https://AS.example",             # not lowercase
    "https://10.0.0.1",               # an IP literal
    "https://localhost",              # no dot
    "https://[::1]",
    "as.example",                     # no scheme
    "",
    123,
    ["https://as.example"],
])
def test_four_party_aud_that_is_not_a_server_identifier_is_refused_without_any_fetch(aud):
    rk, resolver = ResourceKey(), AccessResolver()
    with pytest.raises(resource_token.ResourceTokenError) as ei:
        verify(rk, mint(rk, aud=aud), access_server_resolver=resolver)
    assert ei.value.check == "aud" and resolver.calls == [] and rk.calls == []


def test_four_party_default_resolver_is_discovery(monkeypatch):
    """The sink reaches ``discovery.access_server_metadata`` at call time (a monkeypatch of the module works)."""
    seen = []

    async def fake(aud):
        seen.append(aud)
        return dict(ACCESS_METADATA)
    monkeypatch.setattr(discovery, "access_server_metadata", fake)
    rk = ResourceKey()
    assert verify(rk, mint(rk, aud=AS)).as_url == AS and seen == [AS]


# --------------------------------------------------------------------------- #
# discovery.access_server_metadata — the document rules, the egress, the caches
# --------------------------------------------------------------------------- #
def _access_client(access: FakeAccessServer, monkeypatch, validator=None):
    from utils.url_validation import ValidatedPublicUrl

    async def _validate(url):
        if validator is not None:
            return await validator(url)
        host = httpx.URL(url).host
        return ValidatedPublicUrl(url=url, hostname=host, port=443, addresses=("93.184.216.34",))
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(access.handle), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(a2a_client, "_http_client", factory)
    discovery.reset_cache()


def _fetch(aud=AS, **kw):
    return asyncio.run(discovery.access_server_metadata(aud, **kw))


def test_access_server_document_happy_path_and_positive_cache(monkeypatch):
    access = FakeAccessServer()
    _access_client(access, monkeypatch)
    assert _fetch() == ACCESS_METADATA
    assert _fetch() == ACCESS_METADATA
    assert access.metadata_fetches == 1 and access.other_requests == []
    discovery.reset_cache()


@pytest.mark.parametrize("metadata, status, code", [
    ({**ACCESS_METADATA, "issuer": OTHER}, 200, "issuer_mismatch"),
    ({**ACCESS_METADATA, "issuer": AS + "/"}, 200, "issuer_mismatch"),
    ({"jwks_uri": AS + "/jwks"}, 200, "issuer_missing"),
    ("not json {", 200, "unknown_key"),
    ([AS], 200, "unknown_key"),
    (ACCESS_METADATA, 404, "unknown_key"),                  # no aauth-access.json at all
    (ACCESS_METADATA, 500, "unknown_key"),
])
def test_access_server_document_refusals_are_negative_cached(monkeypatch, metadata, status, code):
    access = FakeAccessServer(metadata=metadata, status=status)
    _access_client(access, monkeypatch)
    with pytest.raises(discovery.DiscoveryError) as ei:
        _fetch()
    assert ei.value.code == code
    with pytest.raises(discovery.DiscoveryError) as again:
        _fetch()
    assert again.value.code == code and access.metadata_fetches == 1      # negative-cached
    discovery.reset_cache()


def test_access_server_fetch_goes_through_the_outbound_public_url_validator(monkeypatch):
    """A private / non-public address is refused by the same egress every discovery fetch uses."""
    access = FakeAccessServer()

    async def private(url):
        raise A2ACallError("endpoint_private_address", "The A2A endpoint resolves to a private address.")
    _access_client(access, monkeypatch, validator=private)
    with pytest.raises(discovery.DiscoveryError) as ei:
        _fetch()
    assert ei.value.code == "unknown_key" and "endpoint_private_address" in ei.value.detail
    assert access.metadata_fetches == 0
    # ... and the sink turns that into the named refusal, with the real resolver on the path
    rk = ResourceKey()
    with pytest.raises(resource_token.ResourceTokenError) as sink:
        verify(rk, mint(rk, aud=AS))
    assert sink.value.check == "aud" and "endpoint_private_address" in sink.value.detail
    discovery.reset_cache()


def test_access_server_document_is_cached_per_server_and_cleared_by_reset(monkeypatch):
    access = FakeAccessServer()
    _access_client(access, monkeypatch)
    _fetch()
    discovery.reset_cache()
    _fetch()
    assert access.metadata_fetches == 2


# --------------------------------------------------------------------------- #
# auth_token.inspect_auth_token — iss / dwk per mode
# --------------------------------------------------------------------------- #
def _key():
    key = Ed25519PrivateKey.generate()
    return key, jose.public_jwk(key.public_key())


def _auth(jwk, *, iss=AS, dwk="aauth-access.json", ps=PS, header=None, **claims):
    body = {"iss": iss, "ps": ps, "dwk": dwk, "aud": RES, "sub": "ps-directed-sub", "jti": "at-1", "iat": NOW,
            "exp": NOW + 3600, "cnf": {"jwk": jwk}, **claims}
    hdr = {"alg": "RS256", "typ": "aa-auth+jwt", "kid": "k", **(header or {})}
    return jose.sign_jws(hdr, body, Ed25519PrivateKey.generate())


def _inspect(raw, jwk, **kw):
    args = dict(resource=RES, ps=PS, agent_jkt=jose.thumbprint(jwk), presented_sub="ps-directed-sub",
                presented_exp=NOW + 900, now=NOW)
    args.update(kw)
    return auth_token.inspect_auth_token(raw, **args)


def test_four_party_auth_token_is_held_with_the_access_server_as_issuer():
    _, jwk = _key()
    held = _inspect(_auth(jwk), jwk, expected_iss=AS, expires_in=600)
    assert held.iss == AS and held.aud == RES and held.sub == "ps-directed-sub" and held.exp == NOW + 600
    assert held.key_thumbprint == jose.thumbprint(jwk) and held.token not in repr(held)


def test_three_party_auth_token_rules_hold_exactly_with_and_without_expected_iss():
    _, jwk = _key()
    three = _auth(jwk, iss=PS, dwk="aauth-person.json")
    assert _inspect(three, jwk).iss == PS                       # default: expected_iss = ps
    assert _inspect(three, jwk, expected_iss=PS).iss == PS      # explicit PS is still three-party
    assert _inspect(three, jwk, expected_iss=None).iss == PS


@pytest.mark.parametrize("expected_iss, token_kw, why", [
    (AS, dict(iss=PS, dwk="aauth-person.json"), "a PS-minted token answering a four-party exchange"),
    (AS, dict(iss=PS, dwk="aauth-access.json"), "four-party token whose iss is the PS"),
    (AS, dict(iss=AS, dwk="aauth-person.json"), "four-party token carrying the person document"),
    (AS, dict(iss=OTHER, dwk="aauth-access.json"), "an access server other than the resource token's aud"),
    (AS, dict(iss=AS, dwk="aauth-access.json", ps=OTHER), "four-party: ps is not ours"),
    (AS, dict(iss=AS, dwk="aauth-agent.json"), "four-party: not a server document at all"),
    (None, dict(iss=AS, dwk="aauth-access.json"), "an AS-minted token answering a three-party exchange"),
    (None, dict(iss=PS, dwk="aauth-access.json"), "three-party token carrying the access document"),
    (PS, dict(iss=AS, dwk="aauth-person.json"), "three-party: iss is an AS"),
    (None, dict(iss=PS, dwk="aauth-person.json", ps=OTHER), "three-party: ps is not ours"),
    (AS, dict(iss=AS, dwk="aauth-access.json", aud=OTHER), "four-party: aud is not the resource"),
    (AS, dict(iss=AS, dwk="aauth-access.json", sub="someone"), "four-party: sub is not the presented sub"),
    (AS, dict(iss=AS, dwk="aauth-access.json", exp=NOW + 24 * 3600), "four-party: lifetime > 1 h"),
])
def test_auth_token_iss_dwk_ps_rules_in_both_modes(expected_iss, token_kw, why):
    _, jwk = _key()
    with pytest.raises(ValueError):
        _inspect(_auth(jwk, **token_kw), jwk, expected_iss=expected_iss)


def test_four_party_auth_token_bound_to_another_key_is_refused():
    _, jwk = _key()
    _, other = _key()
    with pytest.raises(ValueError):
        _inspect(_auth(jwk), other, expected_iss=AS)


# --------------------------------------------------------------------------- #
# the ladder, end to end — the PS federates; the agent never dials the AS
# --------------------------------------------------------------------------- #
def _four_party_resource(live):
    res = live["res"]
    res.script = [lambda req: res.require_auth(req, aud=AS), FakeResource.ok()]
    return res


def test_ladder_four_party_exchange_goes_to_our_person_server_and_never_the_access_server(four_party):
    live = four_party
    ps, access = live["ps2"], live["as"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(as_auth_token_for(req))]
    bind_person_token(live)
    res = _four_party_resource(live)
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == ["auth-token"]
    assert out.access_server == AS
    # the resource token went to OUR PS's auth_token_endpoint, carrying the AS aud untouched
    assert len(ps.auth_posts) == 1
    doc = json.loads(ps.auth_posts[0].content)
    _, rt, _, _ = jose.decode_unverified(doc["resource_token"])
    assert rt["aud"] == AS and rt["ps"] == PS and rt["iss"] == RES
    # the access server saw exactly one request: its metadata
    assert access.metadata_fetches == 1 and access.other_requests == []
    # the AS-minted token is what the resource was shown, and it is held under the real issuer
    token, claims = presented_of(res.calls[1])
    assert claims["iss"] == AS and claims["dwk"] == "aauth-access.json"
    held = ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=rt["agent_jkt"], now=NOW)
    assert held.token == token and held.iss == AS
    # audit: the mode and the AS, never a token
    details = resource_call.audit_details(AGENT, out)
    assert details["auth_mode"] == "four-party" and details["access_server"] == AS
    assert token not in json.dumps(details) and held.token not in json.dumps(details)


def test_ladder_four_party_second_call_reuses_the_held_token_with_no_ps_trip(four_party):
    live = four_party
    ps, access = live["ps2"], live["as"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(as_auth_token_for(req))]
    bind_person_token(live)
    _four_party_resource(live)
    assert call().outcome == "result"
    live["res"].script = [FakeResource.ok()]
    again = call()
    assert again.outcome == "result" and again.token_kind == "auth" and again.challenges == []
    assert again.access_server == AS                                   # read back from the held token's iss
    assert len(ps.auth_posts) == 1 and len(ps.posts) == 1 and access.metadata_fetches == 1
    assert resource_call.audit_details(AGENT, again)["auth_mode"] == "four-party"
    # the replay snapshot carries the mode too (no token)
    snap = resource_call.snapshot_of(again)
    assert snap["access_server"] == AS and resource_call.from_snapshot(snap, resource=RES, method="POST",
                                                                       path=PATH).access_server == AS


def test_ladder_three_party_audit_says_three_party(four_party):
    live = four_party
    ps = live["ps2"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    bind_person_token(live)
    res = live["res"]
    res.script = [res.require_auth, FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.access_server is None
    details = resource_call.audit_details(AGENT, out)
    assert details["auth_mode"] == "three-party" and "access_server" not in details
    assert live["as"].metadata_fetches == 0


def test_ladder_four_party_refuses_a_ps_minted_token_from_the_exchange(four_party):
    """The PS answered a four-party exchange with its OWN token: not the AS's → refused, nothing held."""
    live = four_party
    ps = live["ps2"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]        # iss=PS, dwk=person
    bind_person_token(live)
    _four_party_resource(live)
    out = call()
    assert out.outcome == "refusal" and out.code == "ps_rejected:auth_token_invalid"
    info_jkt = json.loads(ps.auth_posts[0].content)
    _, rt, _, _ = jose.decode_unverified(info_jkt["resource_token"])
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=rt["agent_jkt"], now=NOW) is None


def test_ladder_four_party_aud_without_metadata_is_refused_before_any_ps_call(four_party):
    live = four_party
    live["as"].status = 404
    bind_person_token(live)
    _four_party_resource(live)
    out = call()
    assert out.outcome == "refusal" and out.code == "resource_token_invalid:aud" and out.status == 401
    assert live["ps2"].auth_posts == []
    assert live["as"].metadata_fetches == 1 and live["as"].other_requests == []


def test_ladder_four_party_aud_with_mismatched_issuer_is_refused_before_any_ps_call(four_party):
    live = four_party
    live["as"].metadata = {**ACCESS_METADATA, "issuer": OTHER}
    bind_person_token(live)
    _four_party_resource(live)
    out = call()
    assert out.outcome == "refusal" and out.code == "resource_token_invalid:aud"
    assert "issuer_mismatch" in out.detail and live["ps2"].auth_posts == []


def test_ladder_four_party_pending_exchange_polls_with_the_same_expectation(four_party):
    """A 202 from the PS: the record carries the AS, and the eventual token is held to the four-party rule."""
    live = four_party
    ps = live["ps2"]
    ps.auth_script = [FakePS2.auth_pending()]
    bind_person_token(live)
    res = _four_party_resource(live)
    out = call()
    assert out.outcome == "interaction_required" and out.at == "person_server"
    record = ps_client.load_pending(AGENT, out.handle)
    assert record["as_url"] == AS and record["kind"] == "ps_interaction"
    first_post = ps.auth_posts[0]
    # a PS-minted token on the poll is refused for a four-party record ...
    ps.auth_poll_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(first_post))]
    again = resume(out.handle, now=NOW + 10)
    assert again.outcome == "refusal" and again.code == "ps_rejected:auth_token_invalid"
    # ... and the AS-minted one completes the stored request
    ps.auth_poll_script = [lambda req: FakePS2.auth_ok(as_auth_token_for(first_post))]
    done = resume(out.handle, now=NOW + 20)
    assert done.outcome == "result" and done.token_kind == "auth" and done.access_server == AS
    assert len(ps.auth_posts) == 1 and live["as"].other_requests == []
    assert presented_of(res.calls[-1])[1]["iss"] == AS


def test_pending_record_without_as_url_is_three_party(four_party):
    """An older record (pre-ent#928) has no ``as_url``: the poll holds the PS rule, exactly as before."""
    live = four_party
    ps = live["ps2"]
    ps.auth_script = [FakePS2.auth_pending()]
    bind_person_token(live)
    res = live["res"]
    res.script = [res.require_auth, FakeResource.ok()]
    out = call()
    record = ps_client.load_pending(AGENT, out.handle)
    assert record["as_url"] is None
    del record["as_url"]
    ps_client.update_pending(out.handle, record, now=NOW)
    first_post = ps.auth_posts[0]
    ps.auth_poll_script = [lambda req: FakePS2.auth_ok(as_auth_token_for(first_post))]
    assert resume(out.handle, now=NOW + 10).code == "ps_rejected:auth_token_invalid"
    ps.auth_poll_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(first_post))]
    done = resume(out.handle, now=NOW + 20)
    assert done.outcome == "result" and done.access_server is None


def test_audit_details_never_carry_a_token_in_four_party(four_party):
    live = four_party
    ps = live["ps2"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(as_auth_token_for(req))]
    bind_person_token(live)
    _four_party_resource(live)
    out = call()
    details = json.dumps(resource_call.audit_details(AGENT, out, method="POST", path=PATH))
    for req in live["res"].calls:
        assert presented_of(req)[0] not in details
    assert pt.held_token(AGENT, RES, now=NOW).token not in details
