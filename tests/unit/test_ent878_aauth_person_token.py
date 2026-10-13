"""ent#878 — AAuth person-token binding through a person server (checkpoint A).

What is proven here, on recorded Hellō response shapes (verified live 2026-10-13):
  * the signed POST to the PS replays through the REAL `verifier.verify_request`
    — the #623 profile incl. `content-digest content-type`, exact body bytes;
  * 200 → Issued + cached per (agent, resource) until exp − 5 min, served only
    while `cnf` is the agent's current key; 202 → Pending with url/code/link and
    a Redis record that survives across calls; 401/403/408/410/429/400/5xx each a
    distinct refusal; the state machine on resume; cross-key reissue;
  * PS metadata rules (four required fields, issuer equality, token endpoints
    same-origin, `jwks_uri` off-host accepted), caches, negative cache;
  * the egress guards (redirect, encoding, wire cap, secret scrub);
  * T6: the per-agent key is HKDF-derived, equal across workers in a window,
    and `min_remaining` keeps a chain on one key;
  * audit details never carry the token, handle, url or code.

Sync tests with `asyncio.run` (tests/unit/pytest.ini has no asyncio_mode).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import a2a_client  # noqa: E402
from services.aauth import config, egress, httpsig, jose, keys, person_server, requirement, signer, verifier  # noqa: E402
from services.aauth import person_token as pt  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

pytestmark = pytest.mark.unit

OUR = "https://a.example"
PS = "https://ps.example"
RES = "https://res.example"
AGENT = "echo"
IDENTITY = "aauth:echo@a.example"
NOW = 1_800_000_000          # a window boundary (NOW % 3600 == 0)
PENDING_PATH = "/aauth/pending/abc"

HELLO_METADATA = {
    "issuer": PS,
    "person_token_endpoint": PS + "/aauth/token/person",
    "auth_token_endpoint": PS + "/aauth/token/auth",
    "interaction_endpoint": PS + "/auth",
    "revocation_endpoint": PS + "/aauth/revoke",
    "jwks_uri": "https://issuer.example/.well-known/jwks.json",   # a different host — accepted
    "accept_signature_algs": ["Ed25519", "ES256", "RS256"],
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRedis:
    def __init__(self):
        self.kv, self.sets, self.fail = {}, {}, False

    def _chk(self):
        if self.fail:
            raise RuntimeError("redis down")

    def get(self, k):
        self._chk()
        return self.kv.get(k)

    def set(self, k, v, ex=None, nx=False):
        self._chk()
        self.kv[k] = v
        return True

    def delete(self, k):
        self._chk()
        self.kv.pop(k, None)
        self.sets.pop(k, None)

    def exists(self, k):
        return 1 if k in self.kv else 0

    def sadd(self, k, m):
        self.sets.setdefault(k, set()).add(m)

    def srem(self, k, m):
        self.sets.get(k, set()).discard(m)

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def expire(self, k, ttl):
        pass

    def handles(self):
        return [k for k in self.kv if k.startswith("aauth:pt:handle:")]


class FakePS:
    """A scripted person server behind httpx.MockTransport."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        self.metadata = dict(HELLO_METADATA)
        self.metadata_status = 200
        self.metadata_fetches = 0
        self.posts, self.polls = [], []
        self.post_script, self.poll_script = [], []

    def person_token_for(self, agent_token: str, *, aud=RES, header=None, **claims):
        _, agent_claims, _, _ = jose.decode_unverified(agent_token)
        body = {"iss": PS, "dwk": "aauth-person.json", "aud": aud, "sub": "ps-directed-sub",
                "jti": "jti-" + os.urandom(4).hex(), "iat": NOW, "exp": NOW + 3600,
                "cnf": {"jwk": agent_claims["cnf"]["jwk"]}}
        body.update(claims)
        hdr = {"alg": "RS256", "typ": "aa-person+jwt", "kid": "ps-kid"}
        hdr.update(header or {})
        return jose.sign_jws(hdr, body, self.key)

    @staticmethod
    def ok(token, expires_in=3600):
        return (200, {"content-type": "application/json"}, json.dumps({"person_token": token, "expires_in": expires_in}))

    @staticmethod
    def pending(status="pending", *, url=PS + "/auth", code="ABCD-1234", location=PS + PENDING_PATH,
                retry_after="5", requirement_header="auto"):
        headers = {"content-type": "application/json", "cache-control": "no-store"}
        if requirement_header == "auto":
            requirement_header = f'requirement=interaction; url="{url}"; code="{code}"'
        if requirement_header is not None:
            headers["aauth-requirement"] = requirement_header
        if location is not None:
            headers["location"] = location
        if retry_after is not None:
            headers["retry-after"] = retry_after
        return (202, headers, json.dumps({"status": status}))

    @staticmethod
    def problem(status, error):
        return (status, {"content-type": "application/problem+json"}, json.dumps({"error": error}))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/.well-known/aauth-person.json":
            self.metadata_fetches += 1
            return httpx.Response(self.metadata_status, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(json.dumps(self.metadata).encode()))
        if request.method == "POST" and path == "/aauth/token/person":
            self.posts.append(request)
            script = self.post_script.pop(0)
        elif request.method == "GET" and path == PENDING_PATH:
            self.polls.append(request)
            script = self.poll_script.pop(0)
        else:
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        if callable(script):
            script = script(request)
        status, headers, body = script
        raw = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(raw))


def agent_token_of(request: httpx.Request) -> str:
    return httpsig.parse_signature_key(request.headers.get("signature-key"))


@pytest.fixture()
def harness(monkeypatch):
    ps, redis = FakePS(), FakeRedis()
    instance_key = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(instance_key.public_key())
    kid = jose.thumbprint(jwk)
    state = {"issuer": OUR, "ps": PS, "ps_fake": ps, "redis": redis, "kid": kid, "jwk": {**jwk, "kid": kid}}

    monkeypatch.setattr(config, "active_issuer", lambda: state["issuer"])
    monkeypatch.setattr(config, "get_person_server", lambda: state["ps"])
    monkeypatch.setattr(keys, "get_instance_key",
                        lambda: keys.InstanceKey(kid=kid, public_jwk=state["jwk"], private_key=instance_key))
    monkeypatch.setattr(pt, "_get_redis", lambda: None if state.get("no_redis") else redis)

    async def _validate(url):
        host = httpx.URL(url).host
        return ValidatedPublicUrl(url=url, hostname=host, port=443, addresses=("93.184.216.34",))
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(ps.handle), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(a2a_client, "_http_client", factory)

    for mod in (signer, pt, person_server):
        mod.reset_cache()
    yield state
    for mod in (signer, pt, person_server):
        mod.reset_cache()


def request_token(now=NOW, agent=AGENT, resource=RES, **kw):
    return asyncio.run(pt.person_token(agent, resource, now=now, **kw))


def resume(handle, now=NOW, agent=AGENT, **kw):
    return asyncio.run(pt.resume(agent, handle, now=now, **kw))


# --------------------------------------------------------------------------- #
# requirement.py — the PS response grammar
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("header, expect", [
    ('requirement=interaction; url="https://ps.example/auth"; code="ABCD-1234"',
     ("interaction", {"url": "https://ps.example/auth", "code": "ABCD-1234"})),
    ('requirement=interaction;url="https://ps.example/a";code="abcd1234"',
     ("interaction", {"url": "https://ps.example/a", "code": "abcd1234"})),
    ("requirement=person-token", ("person-token", {})),
    ('requirement="interaction"; url="https://p.example/x\\"y\\\\z"; code="AB-CD"; extra=1, other; p=2',
     ("interaction", {"url": 'https://p.example/x"y\\z', "code": "AB-CD", "extra": "1"})),
])
def test_requirement_parses_dictionary_shape(header, expect):
    assert requirement.parse_aauth_requirement(header) == expect


@pytest.mark.parametrize("header", [
    None, "", "interaction", "requirement=(a b)", 'requirement=interaction; url="unterminated',
    "=interaction", "requirement=Interaction!", 'requirement=interaction; url="bad\\q"',
])
def test_requirement_refuses_malformed(header):
    with pytest.raises(requirement.RequirementFormatError):
        requirement.parse_aauth_requirement(header)


@pytest.mark.parametrize("value, expect", [
    (None, 5), ("7", 7), ("0", 1), ("600", 60), ("Wed, 21 Oct 2015 07:28:00 GMT", 5), ("x", 5), ("  3 ", 3),
])
def test_retry_after(value, expect):
    assert requirement.parse_retry_after(value) == expect


@pytest.mark.parametrize("code, ok", [
    ("ABCD-1234", True), ("abcd1234", True), ("0123-4567-89AB", True), ("ABCI-1234", False),   # I is not Crockford
    ("AB", False), ("-ABCD1234", False), ("AB--CD12", False), ("A" * 33, False), ("", False), (None, False),
])
def test_interaction_code_rules(code, ok):
    assert requirement.interaction_code_ok(code) is ok
    if ok:
        assert requirement.normalize_code(code) == code.upper()


@pytest.mark.parametrize("url, ok", [
    ("https://ps.example/auth", True), ("https://ps.example/auth?x=1", False), ("https://ps.example/auth#f", False),
    ("http://ps.example/auth", False), ("https://u:p@ps.example/auth", False), ("https://ps.example:99999/x", False),
    ("https:///auth", False), ("https://ps.example/a b", False), ("https://ps.example/" + "a" * 2048, False),
])
def test_interaction_url_rules(url, ok):
    assert requirement.interaction_url_ok(url) is ok


def test_interaction_link_composition():
    assert requirement.interaction_link("https://ps.example/auth", "ABCD-1234") == "https://ps.example/auth?code=ABCD-1234"


# --------------------------------------------------------------------------- #
# config / keys / signer (T6)
# --------------------------------------------------------------------------- #
def test_person_server_setting_resolves_setting_then_env(monkeypatch):
    from services import settings_service as ss

    store = {}
    monkeypatch.setattr(ss.settings_service, "get_setting", lambda key: store.get(key))
    monkeypatch.setenv(config.PERSON_SERVER_ENV, "https://env.example")
    assert config.get_person_server() == "https://env.example"
    store[config.PERSON_SERVER_SETTING] = "https://person.hello.coop"
    assert config.get_person_server() == "https://person.hello.coop"
    store[config.PERSON_SERVER_SETTING] = "https://person.hello.coop/"      # strict: not repaired
    assert config.get_person_server() is None
    store[config.PERSON_SERVER_SETTING] = "http://person.hello.coop"
    assert config.get_person_server() is None


def test_instance_seed_is_the_private_key_seed(harness):
    seed = keys.instance_seed()
    assert isinstance(seed, bytes) and len(seed) == 32
    assert Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw() == \
        jose.jwk_to_public_key(keys.get_instance_key().public_jwk).public_bytes_raw()


def test_agent_key_is_derived_and_equal_across_workers_in_a_window(harness):
    k1, t1 = signer.current_agent_key(AGENT, now=NOW + 100)
    signer.reset_cache()                                     # "the other worker"
    k2, t2 = signer.current_agent_key(AGENT, now=NOW + 1800)
    assert t1 == t2 and k1.private_bytes_raw() == k2.private_bytes_raw()
    signer.reset_cache()
    _, t3 = signer.current_agent_key(AGENT, now=NOW + 3600)  # next window
    assert t3 != t1
    _, other = signer.current_agent_key("other", now=NOW + 100)
    assert other != t1
    # The token's cnf IS the derived key.
    signer.reset_cache()                                     # the clock above ran backwards
    info = signer.agent_token_info(AGENT, now=NOW + 100)
    _, claims, _, _ = jose.decode_unverified(info.token)
    assert jose.thumbprint(claims["cnf"]["jwk"]) == info.thumbprint == t1


def test_min_remaining_keeps_a_chain_on_one_key(harness):
    # 10 minutes before the window ends, a chain asking for 20 minutes rolls to
    # the next window's key — and every later step inside that margin reuses it.
    late = NOW + 3600 - 600
    k_chain, t_chain = signer.current_agent_key(AGENT, now=late, min_remaining=1200)
    assert signer.key_window(late, 1200) == signer.key_window(NOW + 3600)
    assert signer.current_agent_key(AGENT, now=late + 300, min_remaining=1200)[1] == t_chain
    assert signer.current_agent_key(AGENT, now=late + 500)[1] == t_chain
    signer.reset_cache()
    assert signer.current_agent_key(AGENT, now=NOW + 3600 + 1)[1] == t_chain   # the other worker, next window
    # The cached token is retired when its key window ends, even inside the margin.
    signer.reset_cache()
    first = signer.agent_token_info(AGENT, now=NOW + 3000)
    assert signer.agent_token_info(AGENT, now=NOW + 3599).thumbprint == first.thumbprint
    assert signer.agent_token_info(AGENT, now=NOW + 3600).thumbprint != first.thumbprint


def test_agent_token_tuple_api_and_refresh_margin_preserved(harness):
    t1, k1, ident = signer.agent_token(AGENT, now=NOW)
    assert ident == IDENTITY
    assert signer.agent_token(AGENT, now=NOW + 60)[0] == t1
    assert signer.agent_token(AGENT, now=NOW + 60, min_remaining=3000)[0] == t1
    assert signer.agent_token(AGENT, now=NOW + 60, min_remaining=3541)[0] != t1   # < min_remaining left


# --------------------------------------------------------------------------- #
# person_server.py — metadata rules + caches
# --------------------------------------------------------------------------- #
def test_metadata_happy_path_accepts_off_host_jwks(harness):
    meta = asyncio.run(person_server.metadata(PS))
    assert meta.person_token_endpoint == PS + "/aauth/token/person"
    assert meta.jwks_uri == "https://issuer.example/.well-known/jwks.json"
    assert meta.interaction_origins() == {"https://ps.example:443"}
    assert asyncio.run(person_server.metadata(PS)) is meta
    assert harness["ps_fake"].metadata_fetches == 1


@pytest.mark.parametrize("patch, code", [
    ({"issuer": "https://other.example"}, "person_server_metadata_invalid"),
    ({"auth_token_endpoint": None}, "person_server_metadata_invalid"),
    ({"jwks_uri": "http://issuer.example/jwks"}, "person_server_metadata_invalid"),
    ({"person_token_endpoint": "https://evil.example/token"}, "person_server_metadata_invalid"),
    ({"interaction_endpoint": "ftp://ps.example/auth"}, "person_server_metadata_invalid"),
    ("not-json", "person_server_metadata_invalid"),
    (503, "person_server_unreachable"),
])
def test_metadata_refusals_are_negative_cached(harness, patch, code):
    ps = harness["ps_fake"]
    if patch == "not-json":
        ps.metadata = "{{"
        ps.handle = lambda req: httpx.Response(200, stream=httpx.ByteStream(b"{{"))  # type: ignore[assignment]
    elif isinstance(patch, int):
        ps.metadata_status = patch
    else:
        ps.metadata.update(patch)
        for k, v in list(patch.items()):
            if v is None:
                ps.metadata.pop(k)
    with pytest.raises(person_server.PersonServerError) as ei:
        asyncio.run(person_server.metadata(PS, now=1000.0))
    assert ei.value.code == code
    with pytest.raises(person_server.PersonServerError):
        asyncio.run(person_server.metadata(PS, now=1000.0 + 30))      # negative-cached, no refetch
    assert ps.metadata_fetches <= 1
    assert request_token().code == code


# --------------------------------------------------------------------------- #
# egress.py — the guard set, pinned
# --------------------------------------------------------------------------- #
def _egress(handler, *, max_bytes=1024, secret=None, url="https://ps.example/x"):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as c:
            return await egress.request_capped(c, "GET", url, max_bytes=max_bytes, headers={"Accept": "a"}, secret=secret)
    return asyncio.run(run())


def test_egress_returns_every_status_with_headers_and_capped_body(harness):
    seen = {}

    def handler(req):
        seen.update(host=req.headers["host"], sni=req.extensions.get("sni_hostname"), url=str(req.url),
                    enc=req.headers["accept-encoding"])
        return httpx.Response(401, headers={"Signature-Error": "error=invalid_signature", "X-A": "1", "x-a": "2"},
                              stream=httpx.ByteStream(b'{"error":"invalid_signature"}'))
    resp = _egress(handler)
    assert (resp.status, resp.body) == (401, b'{"error":"invalid_signature"}')
    assert resp.headers["signature-error"] == "error=invalid_signature"
    assert resp.headers["x-a"] == "1, 2"
    assert seen == {"host": "ps.example", "sni": "ps.example", "url": "https://93.184.216.34/x", "enc": "identity"}


@pytest.mark.parametrize("handler, code", [
    (lambda r: httpx.Response(302, headers={"location": "https://ps.example/y"}), "redirect"),
    (lambda r: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"x")), "encoding"),
    (lambda r: httpx.Response(200, headers={"content-length": "5000"}), "too_large"),
    (lambda r: httpx.Response(200, stream=httpx.ByteStream(b"x" * 5000)), "too_large"),
])
def test_egress_guards(harness, handler, code):
    with pytest.raises(egress.EgressError) as ei:
        _egress(handler)
    assert ei.value.code == code


def test_egress_scrubs_the_secret_from_transport_errors(harness):
    def handler(req):
        raise httpx.ConnectError("boom carrying eyJSECRETSECRET.token.sig")
    with pytest.raises(egress.EgressError) as ei:
        _egress(handler, secret="eyJSECRETSECRET.token.sig")
    assert ei.value.code == "unreachable" and "SECRETSECRET" not in ei.value.detail


def test_egress_refuses_what_the_validator_refuses(harness, monkeypatch):
    async def _validate(url):
        raise a2a_client.A2ACallError("endpoint_private", "private address")
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)
    with pytest.raises(egress.EgressError) as ei:
        _egress(lambda r: httpx.Response(200))
    assert ei.value.code == "endpoint_private"


# --------------------------------------------------------------------------- #
# person_token — issuance, cache, the signed POST
# --------------------------------------------------------------------------- #
def test_200_issues_caches_and_serves_held_token(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    out = request_token()
    assert isinstance(out, pt.PersonTokenIssued) and not out.cached
    assert (out.aud, out.sub, out.iss) == (RES, "ps-directed-sub", PS)
    assert out.key_thumbprint == signer.current_agent_key(AGENT, now=NOW)[1]
    assert out.exp == NOW + 3600
    again = request_token(now=NOW + 100)
    assert again.cached and again.jti == out.jti and len(ps.posts) == 1
    held = pt.held_token(AGENT, RES, now=NOW + 100)
    assert held is not None and held.jti == out.jti and held.token.startswith("eyJ")
    assert pt.held_token_by_jti(AGENT, out.jti, now=NOW + 100) is held
    assert pt.held_token("other", RES, now=NOW + 100) is None
    assert "eyJ" not in repr(held) and "eyJ" not in repr(out)


def test_held_token_refreshes_at_the_margin_and_on_key_change(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req), exp=NOW + 1000)))
    first = request_token()
    assert first.exp == NOW + 1000
    assert pt.held_token(AGENT, RES, now=NOW + 1000 - 301) is not None
    assert pt.held_token(AGENT, RES, now=NOW + 1000 - 300) is None
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    assert not request_token(now=NOW + 1000 - 299).cached and len(ps.posts) == 2
    # Next hour window: a new agent key — the held token's cnf no longer matches.
    assert pt.held_token(AGENT, RES, now=NOW + 3600) is None


def test_expires_in_and_agent_token_exp_clamp_the_held_exp(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req)), expires_in=120))
    assert request_token().exp == NOW + 120
    pt.reset_cache()
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req), iat=NOW, exp=NOW + 3600)))
    out = request_token(now=NOW + 1)
    assert out.exp <= signer.agent_token_info(AGENT, now=NOW + 1).exp


def test_the_signed_post_replays_through_the_verifier(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    request_token()
    req = ps.posts[0]
    body = req.content
    assert json.loads(body) == {"resource": RES, "capabilities": ["interaction"]}
    assert req.headers["prefer"] == "wait=5" and req.headers["content-type"] == "application/json"
    sig_input = httpsig.parse_signature_input(req.headers["signature-input"])
    assert sig_input.components == ("@method", "@authority", "@path", "signature-key", "content-digest", "content-type")

    async def read_body():
        return body

    async def resolver(iss, kid):
        assert (iss, kid) == (OUR, harness["kid"])
        return harness["jwk"]

    class Replay:
        def claim(self, key, ttl):
            return True

    inbound = verifier.InboundRequest(method="POST", raw_path="/aauth/token/person",
                                      headers={k.lower(): v for k, v in req.headers.items()},
                                      content_length=len(body))
    verified, got = asyncio.run(verifier.verify_request(
        inbound, expected_authority="ps.example", trusted_identities=[IDENTITY], read_body=read_body,
        max_body_bytes=65536, now=NOW, replay_store=Replay(), key_resolver=resolver))
    assert verified.identity == IDENTITY and got == body
    assert verified.key_thumbprint == signer.current_agent_key(AGENT, now=NOW)[1]


def test_agent_token_is_refreshed_before_the_ps_request(harness):
    ps = harness["ps_fake"]
    stale = signer.agent_token_info(AGENT, now=NOW)            # 60 min left at NOW
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    request_token(now=NOW + 3600 - 1100)                       # 18 min left: < 20 min → refreshed
    assert agent_token_of(ps.posts[0]) != stale.token


# --------------------------------------------------------------------------- #
# person_token — the pending handoff and its state machine
# --------------------------------------------------------------------------- #
def _start_pending(harness, **pending_kw):
    ps = harness["ps_fake"]
    ps.post_script.append(ps.pending(**pending_kw))
    out = request_token()
    assert isinstance(out, pt.PendingInteraction), out
    return out


def test_202_returns_link_and_persists_a_record(harness):
    out = _start_pending(harness)
    assert out.created and out.status == "pending" and out.retry_after == 5
    assert (out.url, out.code, out.link) == (PS + "/auth", "ABCD-1234", PS + "/auth?code=ABCD-1234")
    assert out.expires_at == NOW + pt.PENDING_TTL and pt.HANDLE_RE.match(out.handle)
    redis = harness["redis"]
    assert len(redis.handles()) == 1 and out.handle not in "".join(redis.handles())   # hashed key
    record = json.loads(redis.kv[redis.handles()[0]])
    assert record["agent"] == AGENT and record["pending_url"] == PS + PENDING_PATH and "person_token" not in record
    assert pt.audit_action(out) == "aauth_person_token_pending"


def test_second_call_before_next_poll_returns_the_same_handle_without_network(harness):
    ps = harness["ps_fake"]
    out = _start_pending(harness)
    again = request_token(now=NOW + 2)
    assert again.handle == out.handle and not again.created and len(ps.posts) == 1 and ps.polls == []
    assert pt.audit_action(again) is None
    # After the interval the pending hit resumes with a poll — and a terminal
    # answer is returned, never a second POST (no second Hellō code).
    ps.poll_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    done = request_token(now=NOW + 6)
    assert isinstance(done, pt.PersonTokenIssued) and len(ps.posts) == 1 and len(ps.polls) == 1
    assert harness["redis"].handles() == []
    assert ps.polls[0].headers["prefer"] == "wait=5"


def test_resume_drives_the_state_machine(harness):
    ps = harness["ps_fake"]
    out = _start_pending(harness)
    ps.poll_script.append(ps.pending("interacting", retry_after="3", requirement_header=None))
    again = resume(out.handle, now=NOW + 5)
    assert again.status == "interacting" and again.retry_after == 3 and again.link == out.link
    assert ps.polls[0].method == "GET" and ps.polls[0].url.path == PENDING_PATH
    assert ps.polls[0].headers["prefer"] == "wait=15"
    sig_input = httpsig.parse_signature_input(ps.polls[0].headers["signature-input"])
    assert sig_input.components == ("@method", "@authority", "@path", "signature-key")
    assert resume(out.handle, now=NOW + 6).status == "interacting" and len(ps.polls) == 1   # early → no poll
    ps.poll_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req))))
    done = resume(out.handle, now=NOW + 9)
    assert isinstance(done, pt.PersonTokenIssued) and not done.cached
    assert harness["redis"].handles() == []
    assert resume(out.handle, now=NOW + 10).code == "pending_unknown"


@pytest.mark.parametrize("status, error, code, deleted", [
    (403, "denied", "denied", True), (403, "abandoned", "abandoned", True), (403, "revoked", "revoked", True),
    (403, "weird-thing", "denied", True), (408, "expired", "expired", True), (410, "invalid_code", "gone", True),
    (500, "server_error", "ps_error", False), (401, "invalid_signature", "signature_rejected", False),
])
def test_resume_terminal_and_transient_answers(harness, status, error, code, deleted):
    ps = harness["ps_fake"]
    out = _start_pending(harness)
    ps.poll_script.append(ps.problem(status, error))
    got = resume(out.handle, now=NOW + 5)
    assert isinstance(got, pt.PersonTokenRefused) and got.code == code and got.ps_status == status
    assert (harness["redis"].handles() == []) is deleted
    assert pt.audit_action(got) == ("aauth_person_token_denied" if code in pt.TERMINAL_REFUSALS else "aauth_person_token_refused")


def test_resume_429_backs_off_and_keeps_the_record(harness):
    ps = harness["ps_fake"]
    out = _start_pending(harness)
    ps.poll_script.append((429, {"retry-after": "4"}, json.dumps({"error": "slow_down"})))
    got = resume(out.handle, now=NOW + 5)
    assert isinstance(got, pt.PendingInteraction) and got.status == "slow_down" and got.retry_after == 10
    assert len(harness["redis"].handles()) == 1
    assert resume(out.handle, now=NOW + 7).retry_after == 10 and len(ps.polls) == 1


def test_handle_is_scoped_to_the_agent_and_fails_closed(harness):
    out = _start_pending(harness)
    assert resume(out.handle, agent="sibling").code == "pending_unknown"
    assert resume("not-a-handle").code == "pending_unknown"
    assert resume("A" * 32).code == "pending_unknown"
    harness["no_redis"] = True
    assert resume(out.handle).code == "state_unavailable"


def test_resume_with_a_rolled_key_discards_and_reissues(harness):
    ps = harness["ps_fake"]
    out = _start_pending(harness)
    original = agent_token_of(ps.posts[0])
    # The PS binds the token to the key of the ORIGINAL POST; by the time the
    # person approves, this worker's window has rolled.
    ps.poll_script.append(lambda req: ps.ok(ps.person_token_for(original, iat=NOW + 3600, exp=NOW + 7200)))
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req), iat=NOW + 3600, exp=NOW + 7200)))
    got = resume(out.handle, now=NOW + 3600 + 5)
    assert isinstance(got, pt.PersonTokenIssued) and got.reissued_for_key and len(ps.posts) == 2
    assert got.key_thumbprint == signer.current_agent_key(AGENT, now=NOW + 3600 + 5)[1]
    assert harness["redis"].handles() == []


# --------------------------------------------------------------------------- #
# person_token — refusals before and after the POST
# --------------------------------------------------------------------------- #
def test_inert_raises_and_unconfigured_or_bad_resource_refuse(harness):
    harness["ps"] = None
    assert request_token().code == "person_server_unconfigured"
    harness["ps"] = PS
    assert request_token(resource="https://res.example/path").code == "invalid_resource"
    assert request_token(resource="http://res.example").code == "invalid_resource"
    harness["issuer"] = None
    with pytest.raises(signer.SigningUnavailable):
        request_token()
    assert harness["ps_fake"].posts == []


@pytest.mark.parametrize("script, code, ps_error", [
    ((401, {"signature-error": "error=invalid_signature"}, ""), "signature_rejected", "invalid_signature"),
    (FakePS.problem(400, "invalid_request"), "ps_error", "invalid_request"),
    (FakePS.problem(400, "<script>"), "ps_error", "unparseable"),
    (FakePS.problem(503, "x"), "ps_error", "x"),
    (FakePS.problem(403, "denied"), "denied", "denied"),
    (FakePS.pending(requirement_header="requirement=person-token"), "unsupported_requirement", "person-token"),
    (FakePS.pending(requirement_header="requirement=interaction; url=1"), "ps_protocol_error", None),
    (FakePS.pending(url=PS + "/auth?x=1"), "ps_protocol_error", None),
    (FakePS.pending(url="https://evil.example/auth"), "ps_protocol_error", None),
    (FakePS.pending(code="ABCI"), "ps_protocol_error", None),
    (FakePS.pending(location="https://evil.example/pending"), "ps_protocol_error", None),
    (FakePS.pending(location=None), "ps_protocol_error", None),
])
def test_post_refusals_are_named_and_nothing_is_recorded(harness, script, code, ps_error):
    ps = harness["ps_fake"]
    ps.post_script.append(script)
    got = request_token()
    assert isinstance(got, pt.PersonTokenRefused) and got.code == code, got
    assert got.ps_error == ps_error
    assert harness["redis"].handles() == [] and pt.held_token(AGENT, RES, now=NOW) is None


def test_relative_location_and_missing_retry_after_and_plain_202(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(ps.pending(location=PENDING_PATH, retry_after=None, requirement_header=None))
    out = request_token()
    assert isinstance(out, pt.PendingInteraction) and out.retry_after == 5 and out.link is None
    record = json.loads(harness["redis"].kv[harness["redis"].handles()[0]])
    assert record["pending_url"] == PS + PENDING_PATH


def test_redis_down_refuses_before_any_post_and_at_persist(harness):
    ps = harness["ps_fake"]
    harness["no_redis"] = True
    assert request_token().code == "state_unavailable" and ps.posts == []
    harness["no_redis"] = False
    harness["redis"].fail = True
    assert request_token().code == "state_unavailable" and ps.posts == []
    harness["redis"].fail = False
    redis = harness["redis"]
    original_set = redis.set

    def flaky_set(k, v, **kw):
        if k.startswith("aauth:pt:handle:"):
            raise RuntimeError("redis down")
        return original_set(k, v, **kw)
    redis.set = flaky_set
    ps.post_script.append(ps.pending())
    got = request_token()
    assert got.code == "state_unavailable" and got.ps_status == 202 and len(ps.posts) == 1


def test_open_interactions_per_agent_are_capped(harness):
    ps = harness["ps_fake"]
    for i in range(pt.MAX_PENDING_PER_AGENT):
        ps.post_script.append(ps.pending())
        assert isinstance(request_token(resource=f"https://r{i}.example"), pt.PendingInteraction)
    assert request_token(resource="https://r9.example").code == "too_many_pending"
    assert isinstance(request_token(resource="https://r0.example", now=NOW + 1), pt.PendingInteraction)  # existing one


def test_egress_failures_and_timeouts_are_unreachable(harness, monkeypatch):
    ps = harness["ps_fake"]

    def boom(req):
        raise httpx.ConnectError("nope")
    ps.handle = boom  # type: ignore[assignment]
    assert request_token().code == "person_server_unreachable"
    person_server.reset_cache()
    ps.handle = FakePS.handle.__get__(ps)  # type: ignore[assignment]
    asyncio.run(person_server.metadata(PS))                  # cached, so only the POST is slow below
    monkeypatch.setattr(pt, "DEADLINE_SLACK", 0)

    async def slow(*a, **k):
        await asyncio.sleep(0.3)
    monkeypatch.setattr(egress, "request_capped", slow)
    assert request_token(wait=0).code == "person_server_unreachable"


@pytest.mark.parametrize("header, claims", [
    ({"typ": "aa-agent+jwt"}, {}), ({}, {"dwk": "aauth-agent.json"}), ({}, {"iss": "https://other.example"}),
    ({}, {"aud": "https://other.example"}), ({}, {"exp": NOW - 1}), ({}, {"iat": NOW - 7200}),
    ({}, {"sub": ""}), ({}, {"cnf": {"jwk": jose.public_jwk(Ed25519PrivateKey.generate().public_key())}}),
])
def test_held_token_checks_each_broken_once(harness, header, claims):
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req), header=header, **claims)))
    got = request_token()
    assert isinstance(got, pt.PersonTokenRefused) and got.code == "token_invalid" and got.ps_status == 200
    assert pt.held_token(AGENT, RES, now=NOW) is None


def test_non_json_or_non_jws_200_is_token_invalid(harness):
    ps = harness["ps_fake"]
    ps.post_script.append((200, {}, "not json"))
    assert request_token().code == "token_invalid"
    ps.post_script.append((200, {}, json.dumps({"person_token": "abc"})))
    assert request_token().code == "token_invalid"


# --------------------------------------------------------------------------- #
# audit helpers
# --------------------------------------------------------------------------- #
def test_audit_details_name_the_outcome_and_never_the_secret_strings(harness):
    ps = harness["ps_fake"]
    ps.post_script.append(ps.pending())
    pending = request_token()
    ps.post_script.append(lambda req: ps.ok(ps.person_token_for(agent_token_of(req), aud="https://r2.example")))
    issued = request_token(resource="https://r2.example")
    refused = pt.PersonTokenRefused("denied", "x", ps_status=403, ps_error="denied", resource=RES, iss=PS)
    for outcome in (pending, issued, refused):
        d = pt.audit_details(AGENT, outcome, execution_id="exec-1", trigger_source="mcp")
        blob = json.dumps(d)
        assert "eyJ" not in blob and pending.handle not in blob and "ABCD-1234" not in blob and "/auth" not in blob
        assert d["execution_id"] == "exec-1" and d["trigger_source"] == "mcp" and d["ps_issuer"] == PS
    assert pt.audit_details(AGENT, issued)["token_jti"] == issued.jti
    assert pt.audit_details(AGENT, pending)["pending"] is True
    assert pt.audit_details(AGENT, refused)["reason"] == "denied"
    assert pt.audit_action(issued) == "aauth_person_token_issued"
    assert pt.audit_action(request_token(resource="https://r2.example", now=NOW + 1)) is None
