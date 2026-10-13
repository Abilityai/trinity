"""ent#879 — AAuth outbound resource client (checkpoint A: ladder + resource-token sink).

What is proven here, on synthetic keys and recorded Senzing/Hellō header shapes
(verified live 2026-10-13):
  * `discovery` resolves a resource's key under `aauth-resource.json`, cached
    per (issuer, dwk) — a key under the agent document never answers for the
    resource document;
  * `httpsig`/`signer` cover a resource's extra components (the supported pair
    only), carry a presented person/auth token in `Signature-Key` while the
    AGENT key signs, and refuse to sign under a rotated key;
  * `resource_meta`: issuer equality, same-origin jwks/control endpoints,
    tolerant `AAuth-Budget`, caches;
  * `resource_token.verify_resource_token`: the happy path and ONE RED TEST PER
    CHECK (wrong iss / dwk / aud / agent_jkt / ps / sub / presented_jti, expired,
    future iat, lifetime, bad typ / alg / header params / kid, unknown kid, bad
    signature, malformed) — the security sink;
  * the ladder: every transition in the module docstring of `resource_client`,
    each driven through the real `signer` → `egress.request_capped` →
    `httpx.MockTransport` path with the scripted resource reading what was
    actually signed; the exchange / cache / pending seams are monkeypatched
    (checkpoint B wires `ps_client`).

Sync tests with `asyncio.run` (tests/unit/pytest.ini has no asyncio_mode).
"""
from __future__ import annotations

import asyncio
import base64
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
from services.aauth import (auth_token, config, discovery, egress, httpsig, jose, keys,  # noqa: E402
                            person_server, ps_client, requirement, resource_allowlist,
                            resource_client as rc, resource_meta, resource_token, signer)
from services.aauth import person_token as pt  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

from unit.test_ent878_aauth_person_token import FakePS, FakeRedis  # noqa: E402

pytestmark = pytest.mark.unit

rc_save_pending = rc._save_pending          # the real seam, for tests that re-install it over Seams

OUR = "https://a.example"
PS = "https://ps.example"
RES = "https://res.example"
OTHER = "https://other.example"
AGENT = "echo"
NOW = 1_800_000_000
PATH = "/v1/search_entities"
BODY = json.dumps({"query": "ACME"}, separators=(",", ":")).encode()

SENZING_METADATA = {
    "issuer": RES,
    "jwks_uri": RES + "/.well-known/jwks.json",
    "access_mode": "auth-token",
    "authorization_endpoint": RES + "/authorize",
    "revocation_endpoint": RES + "/revoke",
    "usage_endpoint": RES + "/usage",
    "scopes_supported": ["senzing"],
    "budget_units": [{"unit": "credits", "decimals": 0, "max": 40}],
}


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _json(status, doc, headers=None):
    h = {"content-type": "application/json", **(headers or {})}
    return status, h, json.dumps(doc)


def _problem(status, error, detail=None, headers=None):
    h = {"content-type": "application/problem+json", **(headers or {})}
    return status, h, json.dumps({"error": error, "detail": detail or error})


def presented_of(request: httpx.Request):
    """(kind-agnostic) the token the request presented and its (sub, jti, cnf thumbprint)."""
    token = httpsig.parse_signature_key(request.headers.get("signature-key"))
    _, claims, _, _ = jose.decode_unverified(token)
    return token, claims


class FakeResource:
    """A scripted AAuth resource behind httpx.MockTransport (Senzing-shaped)."""

    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        jwk = jose.public_jwk(self.key.public_key())
        self.kid = jose.thumbprint(jwk)
        self.jwk = {**jwk, "kid": self.kid}
        self.metadata = dict(SENZING_METADATA)
        self.metadata_status = 200
        self.metadata_fetches = self.jwks_fetches = 0
        self.calls = []
        self.script = []

    def resource_token_for(self, request: httpx.Request, *, header=None, key=None, **claims):
        token, pres = presented_of(request)
        body = {"iss": RES, "dwk": "aauth-resource.json", "aud": PS, "sub": pres["sub"],
                "agent_jkt": jose.thumbprint(pres["cnf"]["jwk"]), "ps": PS, "presented_jti": pres["jti"],
                "scope": "senzing", "jti": "rt-" + os.urandom(4).hex(), "iat": NOW, "exp": NOW + 300}
        body.update(claims)
        hdr = {"alg": "Ed25519", "typ": "aa-resource+jwt", "kid": self.kid}
        hdr.update(header or {})
        return jose.sign_jws(hdr, body, key or self.key)

    # scripted answers ------------------------------------------------------
    @staticmethod
    def ok(doc=None, budget="cost=2, balance=38, unit=credits"):
        headers = {"aauth-budget": budget} if budget else {}
        return _json(200, doc if doc is not None else {"entities": [{"id": 1}]}, headers)

    @staticmethod
    def require(name, **params):
        extra = "".join(f'; {k}="{v}"' for k, v in params.items())
        return (401, {"aauth-requirement": f"requirement={name}{extra}",
                      "accept-signature": 'sig=("@method" "@authority" "@path" "signature-key")',
                      "content-type": "application/problem+json"},
                json.dumps({"error": "unauthorized"}))

    def require_auth(self, request, **claims):
        return self.require("auth-token", **{"resource-token": self.resource_token_for(request, **claims)})

    @staticmethod
    def signature_error(code):
        return (401, {"signature-error": f'error="{code}"'}, "")

    @staticmethod
    def deferred(location=RES + "/v1/pending/1", retry_after="5", requirement_header=None):
        headers = {"content-type": "application/json"}
        if location is not None:
            headers["location"] = location
        if retry_after is not None:
            headers["retry-after"] = retry_after
        if requirement_header is not None:
            headers["aauth-requirement"] = requirement_header
        return 202, headers, json.dumps({"status": "pending"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/.well-known/aauth-resource.json":
            self.metadata_fetches += 1
            return httpx.Response(self.metadata_status, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(json.dumps(self.metadata).encode()))
        if request.method == "GET" and path == "/.well-known/jwks.json":
            self.jwks_fetches += 1
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  stream=httpx.ByteStream(json.dumps({"keys": [self.jwk]}).encode()))
        self.calls.append(request)
        if not self.script:
            return httpx.Response(599, stream=httpx.ByteStream(b"script exhausted"))
        step = self.script.pop(0)
        if callable(step):
            step = step(request)
        status, headers, body = step
        raw = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(raw))


class Seams:
    """Recorders for the checkpoint-B seams of the ladder."""

    def __init__(self):
        self.held = None
        self.stored, self.forgotten, self.pending = [], [], []
        self.exchange_script = []
        self.exchanges = []
        self.handle = "h" * 32

    def install(self, monkeypatch):
        monkeypatch.setattr(rc, "_held_auth", lambda call: self.held)
        monkeypatch.setattr(rc, "_store_auth", lambda call, held: self.stored.append(held))
        monkeypatch.setattr(rc, "_forget_auth", lambda call: self.forgotten.append(call.resource))

        def save(call, kind, extra):
            self.pending.append((kind, {**extra, "method": call.method, "path": call.path, "body": call.body}))
            return self.handle

        monkeypatch.setattr(rc, "_save_pending", save)

        async def exchange(call, verified, presented):
            self.exchanges.append((verified, presented))
            step = self.exchange_script.pop(0)
            return step(call, verified, presented) if callable(step) else step

        monkeypatch.setattr(rc, "_exchange", exchange)
        return self


def auth_token_for(presented: rc.Presented, jkt_jwk, *, exp=NOW + 3600, **claims):
    body = {"iss": PS, "ps": PS, "dwk": "aauth-person.json", "aud": RES, "sub": presented.sub, "scope": "senzing",
            "jti": "at-" + os.urandom(4).hex(), "iat": NOW, "exp": exp, "cnf": {"jwk": jkt_jwk}}
    body.update(claims)
    return jose.sign_jws({"alg": "RS256", "typ": "aa-auth+jwt", "kid": "ps-kid"}, body, Ed25519PrivateKey.generate())


def issued(call, verified, presented):
    """An exchange script step: mint an auth token bound to the call's key."""
    jwk = jose.public_jwk(call.agent_info.key.public_key())
    held = auth_token.inspect_auth_token(auth_token_for(presented, jwk), resource=RES, ps=PS, agent_jkt=call.jkt,
                                         presented_sub=presented.sub, presented_exp=presented.exp, now=NOW)
    return auth_token.AuthTokenIssued(held)


@pytest.fixture()
def harness(monkeypatch):
    res, ps, redis = FakeResource(), FakePS(), FakeRedis()
    instance_key = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(instance_key.public_key())
    kid = jose.thumbprint(jwk)
    state = {"issuer": OUR, "ps": PS, "res": res, "ps_fake": ps, "redis": redis, "kid": kid}

    monkeypatch.setattr(config, "active_issuer", lambda: state["issuer"])
    monkeypatch.setattr(config, "get_person_server", lambda: state["ps"])
    monkeypatch.setattr(keys, "get_instance_key",
                        lambda: keys.InstanceKey(kid=kid, public_jwk={**jwk, "kid": kid}, private_key=instance_key))
    monkeypatch.setattr(pt, "_get_redis", lambda: None if state.get("no_redis") else redis)
    monkeypatch.setattr(ps_client, "_get_redis", lambda: None if state.get("no_redis") else redis)
    monkeypatch.setattr(resource_allowlist, "allowed_resources", lambda: tuple(state.get("allowed", (RES,))))

    async def _validate(url):
        host = httpx.URL(url).host
        return ValidatedPublicUrl(url=url, hostname=host, port=443, addresses=("93.184.216.34",))
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)

    def route(request):
        host = request.headers.get("host") or request.url.host
        if host == "res.example":
            return res.handle(request)
        if host == "ps.example":
            return ps.handle(request)
        return httpx.Response(404, stream=httpx.ByteStream(b""))

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(a2a_client, "_http_client", factory)

    for mod in (signer, pt, person_server, resource_meta, discovery):
        mod.reset_cache()
    yield state
    for mod in (signer, pt, person_server, resource_meta, discovery):
        mod.reset_cache()


@pytest.fixture()
def seams(monkeypatch):
    return Seams().install(monkeypatch)


def call(method="POST", path=PATH, body=BODY, now=NOW, resource=RES, agent=AGENT, **kw):
    return asyncio.run(rc.call_resource(agent, resource=resource, method=method, path=path, body=body,
                                        justification="demo", now=now, **kw))


def bind_person_token(harness):
    """Make the PS answer the next person-token POST with a bound 200."""
    ps = harness["ps_fake"]
    ps.post_script.append(lambda req: FakePS.ok(ps.person_token_for(httpsig.parse_signature_key(
        req.headers.get("signature-key")))))


# --------------------------------------------------------------------------- #
# discovery: dwk
# --------------------------------------------------------------------------- #
def test_discovery_resolves_resource_key_under_resource_document(harness):
    res = harness["res"]
    got = asyncio.run(discovery.resolve_key(RES, res.kid, dwk="aauth-resource.json"))
    assert got == res.jwk and res.metadata_fetches == 1 and res.jwks_fetches == 1
    # cached per (issuer, dwk): the agent document is a different cache line and a different fetch
    with pytest.raises(discovery.DiscoveryError):
        asyncio.run(discovery.resolve_key(RES, res.kid))          # /.well-known/aauth-agent.json → 599
    assert asyncio.run(discovery.resolve_key(RES, res.kid, dwk="aauth-resource.json")) == res.jwk
    assert res.metadata_fetches == 1


def test_discovery_refuses_unknown_document():
    with pytest.raises(discovery.DiscoveryError):
        asyncio.run(discovery.resolve_key(RES, "k", dwk="aauth-evil.json"))
    with pytest.raises(discovery.DiscoveryError):
        asyncio.run(discovery.fetch_issuer_keys(RES, dwk="../etc/passwd"))


# --------------------------------------------------------------------------- #
# httpsig / signer: extra components, presented token, key pinning
# --------------------------------------------------------------------------- #
def test_components_for_adds_supported_extras_in_profile_order():
    assert httpsig.components_for("GET") == httpsig.REQUIRED_COMPONENTS
    assert httpsig.components_for("GET", ("content-type", "content-digest")) == \
        httpsig.REQUIRED_COMPONENTS + ("content-digest", "content-type")
    assert httpsig.components_for("POST", ("content-digest",)) == httpsig.REQUIRED_COMPONENTS + httpsig.BODY_COMPONENTS
    with pytest.raises(httpsig.SignatureFormatError) as ei:
        httpsig.components_for("GET", ("date",))
    assert ei.value.code == "unsupported_signature_components"


def test_sign_get_with_extra_components_covers_them():
    key = Ed25519PrivateKey.generate()
    h = httpsig.sign(method="GET", authority_value="res.example", path="/v1/x", agent_token="tok", body=b"",
                     content_type=None, created=NOW, private_key=key, extra_components=("content-digest", "content-type"))
    assert h.content_digest == httpsig.content_digest(b"")
    si = httpsig.parse_signature_input(h.signature_input)
    assert si.components == httpsig.REQUIRED_COMPONENTS + ("content-digest", "content-type")
    values = {"@method": "GET", "@authority": "res.example", "@path": "/v1/x",
              "signature-key": h.signature_key, "content-digest": h.content_digest, "content-type": ""}
    key.public_key().verify(httpsig.parse_signature(h.signature), httpsig.signature_base(values, si.components, si.params))


def test_sign_request_presented_token_is_carried_and_agent_key_signs(harness):
    info = signer.agent_token_info(AGENT, now=NOW)
    headers = signer.sign_request(agent_name=AGENT, method="POST", url=RES + PATH, body=BODY,
                                  content_type="application/json", now=NOW, presented_token="eyJhbGciOiJSUzI1NiJ9.e30.sig")
    assert httpsig.parse_signature_key(headers["Signature-Key"]) == "eyJhbGciOiJSUzI1NiJ9.e30.sig"
    si = httpsig.parse_signature_input(headers["Signature-Input"])
    values = {"@method": "POST", "@authority": "res.example", "@path": PATH, "signature-key": headers["Signature-Key"],
              "content-digest": headers["Content-Digest"], "content-type": "application/json"}
    info.key.public_key().verify(httpsig.parse_signature(headers["Signature"]),
                                 httpsig.signature_base(values, si.components, si.params))


def test_sign_request_refuses_rotated_key_and_unsupported_components(harness):
    info = signer.agent_token_info(AGENT, now=NOW)
    with pytest.raises(signer.KeyRotated):
        signer.sign_request(agent_name=AGENT, method="GET", url=RES + "/v1/x", now=NOW,
                            presented_token="t", expected_key_thumbprint="not-" + info.thumbprint)
    with pytest.raises(signer.SigningUnavailable) as ei:
        signer.sign_request(agent_name=AGENT, method="GET", url=RES + "/v1/x", now=NOW, extra_components=("date",))
    assert "unsupported_signature_components" in str(ei.value)
    # the pin accepts the current key
    assert "Signature" in signer.sign_request(agent_name=AGENT, method="GET", url=RES + "/v1/x", now=NOW,
                                              expected_key_thumbprint=info.thumbprint)


# --------------------------------------------------------------------------- #
# resource_meta
# --------------------------------------------------------------------------- #
def test_metadata_parses_senzing_shape():
    m = resource_meta.parse_metadata(SENZING_METADATA, RES)
    assert m.access_mode == "auth-token" and m.starts_with_person_token()
    assert m.control_paths() == ("/authorize", "/revoke", "/usage")
    assert m.scopes == ("senzing",) and m.signature_window == 60 and m.additional_signature_components == ()


@pytest.mark.parametrize("mutation, code", [
    ({"issuer": OTHER}, "resource_metadata_invalid"),
    ({"issuer": None}, "resource_metadata_invalid"),
    ({"jwks_uri": OTHER + "/.well-known/jwks.json"}, "resource_metadata_invalid:jwks_uri_origin"),
    ({"jwks_uri": "http://res.example/jwks"}, "resource_metadata_invalid"),
    ({"authorization_endpoint": OTHER + "/authorize"}, "resource_metadata_invalid"),
    ({"usage_endpoint": "ftp://res.example/usage"}, "resource_metadata_invalid"),
    ({"additional_signature_components": "content-type"}, "resource_metadata_invalid"),
    ({"additional_signature_components": ["Bad Component"]}, "resource_metadata_invalid"),
])
def test_metadata_refusals(mutation, code):
    doc = {**SENZING_METADATA, **mutation}
    with pytest.raises(resource_meta.ResourceMetadataError) as ei:
        resource_meta.parse_metadata(doc, RES)
    assert ei.value.code == code
    with pytest.raises(resource_meta.ResourceMetadataError):
        resource_meta.parse_metadata(["not", "an", "object"], RES)


def test_metadata_tolerates_unknown_access_mode_and_bad_window():
    m = resource_meta.parse_metadata({**SENZING_METADATA, "access_mode": "magic", "signature_window": "x",
                                      "additional_signature_components": ["content-type"]}, RES)
    assert m.access_mode == "unknown" and not m.starts_with_person_token()
    assert m.signature_window == 60 and m.additional_signature_components == ("content-type",)


def test_metadata_fetch_and_caches(harness):
    res = harness["res"]
    m = asyncio.run(resource_meta.metadata(RES, now=100.0))
    assert m.issuer == RES and res.metadata_fetches == 1
    asyncio.run(resource_meta.metadata(RES, now=200.0))
    assert res.metadata_fetches == 1                      # positive cache
    resource_meta.reset_cache()
    res.metadata_status = 500
    with pytest.raises(resource_meta.ResourceMetadataError) as ei:
        asyncio.run(resource_meta.metadata(RES, now=300.0))
    assert ei.value.code == "resource_unreachable"
    res.metadata_status = 200
    with pytest.raises(resource_meta.ResourceMetadataError):
        asyncio.run(resource_meta.metadata(RES, now=310.0))  # negative cache for 60 s
    assert asyncio.run(resource_meta.metadata(RES, now=400.0)).issuer == RES


def test_metadata_fetch_refuses_non_json_and_redirect(harness, monkeypatch):
    res = harness["res"]
    res.metadata = "not json"
    with pytest.raises(resource_meta.ResourceMetadataError) as ei:
        asyncio.run(resource_meta.fetch_metadata(RES))
    assert ei.value.code == "resource_metadata_invalid"
    monkeypatch.setattr(res, "handle", lambda req: httpx.Response(302, headers={"location": OTHER},
                                                                  stream=httpx.ByteStream(b"")))
    with pytest.raises(resource_meta.ResourceMetadataError) as ei:
        asyncio.run(resource_meta.fetch_metadata(RES))
    assert ei.value.code == "resource_unreachable"


@pytest.mark.parametrize("value, expect", [
    ("cost=2, balance=38, unit=credits", resource_meta.Budget(2, 38, "credits")),
    ("cost=2", resource_meta.Budget(2, None, None)),
    ('balance=0, unit="credits"', resource_meta.Budget(None, 0, "credits")),
    ("cost=2.5", None), ("", None), (None, None), ("garbage;;", None), ("other=1", None),
    ("cost=(1 2)", None), ("x" * 600, None),
])
def test_parse_budget_is_tolerant(value, expect):
    assert resource_meta.parse_budget(value) == expect


# --------------------------------------------------------------------------- #
# resource_token — the sink: happy + one red per check
# --------------------------------------------------------------------------- #
class ResourceKey:
    def __init__(self):
        self.res = FakeResource()
        self.calls = []

    async def resolve(self, iss, kid, *, dwk=None):
        self.calls.append((iss, kid, dwk))
        if dwk != "aauth-resource.json" or iss != RES:
            raise discovery.DiscoveryError("unknown_key", "wrong document or issuer")
        if kid != self.res.kid:
            raise discovery.DiscoveryError("unknown_key", "no such kid")
        return self.res.jwk


AGENT_JKT = "agent-jkt-thumbprint"
PRESENTED = dict(sub="ps-directed-sub", jti="pt-jti-1")


def mint(rk: ResourceKey, *, header=None, key=None, **claims):
    body = {"iss": RES, "dwk": "aauth-resource.json", "aud": PS, "sub": PRESENTED["sub"], "agent_jkt": AGENT_JKT,
            "ps": PS, "presented_jti": PRESENTED["jti"], "scope": "senzing", "jti": "rt-1", "iat": NOW, "exp": NOW + 300}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not ...}
    hdr = {"alg": "Ed25519", "typ": "aa-resource+jwt", "kid": rk.res.kid}
    hdr.update(header or {})
    hdr = {k: v for k, v in hdr.items() if v is not ...}
    return jose.sign_jws(hdr, body, key or rk.res.key)


def verify(rk, token, **kw):
    args = dict(resource=RES, agent_jkt=AGENT_JKT, ps=PS, presented_sub=PRESENTED["sub"],
                presented_jti=PRESENTED["jti"], now=NOW, key_resolver=rk.resolve)
    args.update(kw)
    return asyncio.run(resource_token.verify_resource_token(token, **args))


def test_resource_token_happy_path():
    rk = ResourceKey()
    got = verify(rk, mint(rk))
    assert got.sub == PRESENTED["sub"] and got.presented_jti == PRESENTED["jti"] and got.scope == "senzing"
    assert got.aud == PS and got.jti == "rt-1" and rk.calls == [(RES, rk.res.kid, "aauth-resource.json")]
    assert got.token not in repr(got)


@pytest.mark.parametrize("check, mutate", [
    ("malformed", lambda rk: "not.a.jws.at.all"),
    ("malformed", lambda rk: "eyJ.eyJ"),
    ("alg", lambda rk: mint(rk, header={"alg": "EdDSA"})),
    ("alg", lambda rk: mint(rk, header={"alg": "none"})),
    ("typ", lambda rk: mint(rk, header={"typ": "aa-agent+jwt"})),
    ("header_params", lambda rk: mint(rk, header={"jwk": {"kty": "OKP"}})),
    ("header_params", lambda rk: mint(rk, header={"crit": ["b64"]})),
    ("kid", lambda rk: mint(rk, header={"kid": ...})),
    ("iss", lambda rk: mint(rk, iss=OTHER)),
    ("iss", lambda rk: mint(rk, iss=...)),
    ("dwk", lambda rk: mint(rk, dwk="aauth-agent.json")),
    ("aud", lambda rk: mint(rk, aud="https://as.example")),       # four-party: unsupported_aud
    ("aud", lambda rk: mint(rk, aud=...)),
    ("agent_jkt", lambda rk: mint(rk, agent_jkt="someone-elses-key")),
    ("ps", lambda rk: mint(rk, ps=OTHER)),
    ("sub", lambda rk: mint(rk, sub="another-person")),
    ("presented_jti", lambda rk: mint(rk, presented_jti="pt-jti-9")),
    ("timestamps", lambda rk: mint(rk, exp="soon")),
    ("timestamps", lambda rk: mint(rk, iat=True)),
    ("expired", lambda rk: mint(rk, iat=NOW - 600, exp=NOW)),
    ("clock_skew", lambda rk: mint(rk, iat=NOW + 120, exp=NOW + 420)),
    ("lifetime", lambda rk: mint(rk, iat=NOW, exp=NOW + 3601)),
    ("scope", lambda rk: mint(rk, scope=["senzing"])),
    ("unknown_kid", lambda rk: mint(rk, header={"kid": "someone-else"})),
    ("signature", lambda rk: mint(rk, key=Ed25519PrivateKey.generate())),
])
def test_resource_token_each_check_goes_red(check, mutate):
    rk = ResourceKey()
    with pytest.raises(resource_token.ResourceTokenError) as ei:
        verify(rk, mutate(rk))
    assert ei.value.check == check, (ei.value.check, ei.value.detail)
    assert check in resource_token.CHECKS


def test_resource_token_claims_are_checked_before_any_key_fetch():
    rk = ResourceKey()
    with pytest.raises(resource_token.ResourceTokenError):
        verify(rk, mint(rk, agent_jkt="x"))
    assert rk.calls == []


def test_resource_token_long_lifetime_warns_but_passes(caplog):
    rk = ResourceKey()
    with caplog.at_level("WARNING"):
        verify(rk, mint(rk, exp=NOW + 1800))
    assert any("SHOULD" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# auth_token inspection (T5: inspection only)
# --------------------------------------------------------------------------- #
def _inspect(raw, **kw):
    args = dict(resource=RES, ps=PS, agent_jkt=None, presented_sub="ps-directed-sub", presented_exp=NOW + 900, now=NOW)
    args.update(kw)
    return auth_token.inspect_auth_token(raw, **args)


def test_auth_token_inspection_binds_to_our_key_and_caps_expiry():
    key = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(key.public_key())
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 900)
    held = _inspect(auth_token_for(pres, jwk), agent_jkt=jose.thumbprint(jwk), expires_in=600)
    assert held.sub == "ps-directed-sub" and held.exp == NOW + 600 and held.key_thumbprint == jose.thumbprint(jwk)
    assert held.token not in repr(held)


@pytest.mark.parametrize("claims, header", [
    ({"iss": OTHER}, None), ({"aud": OTHER}, None), ({"sub": "someone"}, None), ({"dwk": "aauth-agent.json"}, None),
    ({"exp": NOW - 1}, None), ({"iat": NOW + 600}, None), ({"cnf": {"jwk": {"kty": "OKP"}}}, None),
    ({}, {"typ": "aa-person+jwt"}),
    ({"ps": OTHER}, None),                                  # minted for another person server
    ({"exp": NOW + 24 * 3600}, None),                       # draft: auth token lifetime ≤ 1 h
])
def test_auth_token_inspection_refusals(claims, header):
    key = Ed25519PrivateKey.generate()
    jwk = jose.public_jwk(key.public_key())
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 900)
    body = {"iss": PS, "ps": PS, "dwk": "aauth-person.json", "aud": RES, "sub": pres.sub, "jti": "at-1", "iat": NOW,
            "exp": NOW + 3600, "cnf": {"jwk": jwk}, **claims}
    hdr = {"alg": "RS256", "typ": "aa-auth+jwt", "kid": "k", **(header or {})}
    with pytest.raises(ValueError):
        _inspect(jose.sign_jws(hdr, body, key), agent_jkt=jose.thumbprint(jwk))
    other = jose.public_jwk(Ed25519PrivateKey.generate().public_key())
    with pytest.raises(ValueError):
        _inspect(auth_token_for(pres, jwk), agent_jkt=jose.thumbprint(other))     # bound to another key


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
def test_flag_off_raises_signing_unavailable(harness, seams):
    harness["issuer"] = None
    with pytest.raises(signer.SigningUnavailable):
        call()


@pytest.mark.parametrize("path, ok", [
    (PATH, True), ("/v1/a-b_c.d~e:f@g", True), ("/", True),
    ("v1/x", False), ("/v1/../x", False), ("/v1//x", False), ("/v1/x?y=1", False), ("/v1/x#f", False),
    ("/v1/%2e%2e/x", False), ("/.well-known/jwks.json", False), ("/v1/x y", False), ("/" + "a" * 600, False),
    ("", False), (None, False),
])
def test_path_ok_matrix(path, ok):
    assert rc.path_ok(path) is ok


def test_input_refusals_before_any_egress(harness, seams):
    res = harness["res"]
    assert call(resource="http://res.example").code == "invalid_resource"
    assert call(method="PUT").code == "invalid_method"
    assert call(path="/v1/../x").code == "invalid_path"
    assert call(path="/authorize").code == "invalid_path"           # a declared control endpoint
    assert call(path="/authorize/").code == "invalid_path"          # ... with one trailing slash
    assert res.calls == []


def test_metadata_and_component_refusals(harness, seams):
    res = harness["res"]
    res.metadata = {**SENZING_METADATA, "issuer": OTHER}
    out = call()
    assert out.outcome == "refusal" and out.code == "resource_metadata_invalid"
    resource_meta.reset_cache()
    res.metadata = {**SENZING_METADATA, "additional_signature_components": ["date"]}
    assert call().code == "unsupported_signature_components"
    assert res.calls == []


def test_agent_token_resource_answers_directly_with_budget(harness, seams):
    res = harness["res"]
    res.metadata = {k: v for k, v in SENZING_METADATA.items() if k != "access_mode"}
    res.script = [FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.status == 200 and out.token_kind == "agent" and out.challenges == []
    assert out.budget == resource_meta.Budget(2, 38, "credits") and json.loads(out.body) == {"entities": [{"id": 1}]}
    req = res.calls[0]
    _, claims = presented_of(req)
    assert claims["iss"] == OUR                                         # the agent token
    assert req.headers["aauth-capabilities"] == "interaction" and req.headers["content-type"] == "application/json"
    assert req.content == BODY and req.headers["content-digest"] == httpsig.content_digest(BODY)
    assert harness["ps_fake"].posts == []                               # no PS trip for an agent-token resource


def test_agent_token_resource_extra_components_are_covered(harness, seams):
    res = harness["res"]
    res.metadata = {**{k: v for k, v in SENZING_METADATA.items() if k != "access_mode"},
                    "additional_signature_components": ["content-type", "content-digest"]}
    res.script = [FakeResource.ok()]
    out = call(method="GET", body=None)
    assert out.outcome == "result"
    si = httpsig.parse_signature_input(res.calls[0].headers["signature-input"])
    assert si.components == httpsig.REQUIRED_COMPONENTS + ("content-digest", "content-type")


def test_person_token_challenge_then_result_presents_person_token(harness, seams):
    res, ps = harness["res"], harness["ps_fake"]
    res.metadata = {k: v for k, v in SENZING_METADATA.items() if k != "access_mode"}
    res.script = [FakeResource.require("person-token"), FakeResource.ok()]
    bind_person_token(harness)
    out = call()
    assert out.outcome == "result" and out.token_kind == "person" and out.challenges == ["person-token"]
    assert len(res.calls) == 2 and len(ps.posts) == 1
    token, claims = presented_of(res.calls[1])
    assert claims["iss"] == PS and token == pt.held_token(AGENT, RES, now=NOW).token
    # the AGENT key signed, under the person token in Signature-Key
    info = signer.agent_token_info(AGENT, now=NOW)
    req = res.calls[1]
    si = httpsig.parse_signature_input(req.headers["signature-input"])
    values = {"@method": "POST", "@authority": "res.example", "@path": PATH, "signature-key": req.headers["signature-key"],
              "content-digest": req.headers["content-digest"], "content-type": "application/json"}
    info.key.public_key().verify(httpsig.parse_signature(req.headers["signature"]),
                                 httpsig.signature_base(values, si.components, si.params))


def test_auth_token_mode_starts_at_the_person_token(harness, seams):
    res = harness["res"]
    res.script = [FakeResource.ok()]
    bind_person_token(harness)
    out = call()
    assert out.outcome == "result" and out.token_kind == "person" and len(res.calls) == 1
    _, claims = presented_of(res.calls[0])
    assert claims["iss"] == PS


def test_person_token_pending_surfaces_interaction_required(harness, seams):
    res = harness["res"]
    res.script = []
    harness["ps_fake"].post_script.append(FakePS.pending())
    out = call()
    assert out.outcome == "interaction_required" and out.at == "person_server"
    assert out.link == PS + "/auth?code=ABCD-1234" and out.handle and out.retry_after == 5
    assert res.calls == []


def test_person_server_refusals_map(harness, seams):
    harness["ps_fake"].post_script.append(FakePS.problem(403, "denied"))
    out = call()
    assert out.outcome == "denied" and out.reason == "denied"
    harness["ps"] = None
    out = call()
    assert out.outcome == "refusal" and out.code == "person_server_unconfigured"


def test_full_chain_person_to_auth_token(harness, seams):
    res = harness["res"]
    res.script = [res.require_auth, FakeResource.ok()]
    bind_person_token(harness)
    seams.exchange_script = [issued]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == ["auth-token"]
    assert len(res.calls) == 2 and len(seams.exchanges) == 1 and len(seams.stored) == 1
    verified, presented = seams.exchanges[0]
    assert presented.kind == "person" and verified.presented_jti == presented.jti and verified.sub == presented.sub
    token, claims = presented_of(res.calls[1])
    assert token == seams.stored[0].token and claims["aud"] == RES
    assert seams.stored[0].token not in out.body


def test_cached_auth_token_is_presented_first_with_no_ps_trip(harness, seams):
    res = harness["res"]
    info = signer.agent_token_info(AGENT, now=NOW)
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 900)
    held = auth_token.inspect_auth_token(auth_token_for(pres, jose.public_jwk(info.key.public_key())), resource=RES,
                                         ps=PS, agent_jkt=info.thumbprint, presented_sub=pres.sub,
                                         presented_exp=pres.exp, now=NOW)
    seams.held = held
    res.script = [FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == []
    assert presented_of(res.calls[0])[0] == held.token and harness["ps_fake"].posts == [] and seams.exchanges == []


def test_tampered_resource_token_is_refused_before_the_ps(harness, seams):
    res = harness["res"]
    res.script = [lambda req: res.require_auth(req, agent_jkt="someone-elses-key")]
    bind_person_token(harness)
    seams.exchange_script = [issued]
    out = call()
    assert out.outcome == "refusal" and out.code == "resource_token_invalid:agent_jkt" and out.status == 401
    assert seams.exchanges == [] and seams.stored == []


@pytest.mark.parametrize("mutation, check", [
    ({"ps": OTHER}, "ps"), ({"presented_jti": "nope"}, "presented_jti"), ({"sub": "nope"}, "sub"),
    ({"iss": OTHER}, "iss"), ({"exp": NOW - 1}, "expired"),
])
def test_tampered_resource_token_variants_through_the_ladder(harness, seams, mutation, check):
    res = harness["res"]
    res.script = [lambda req: res.require_auth(req, **mutation)]
    bind_person_token(harness)
    assert call().code == f"resource_token_invalid:{check}"


def test_auth_token_demanded_of_an_agent_token_is_unexpected(harness, seams):
    res = harness["res"]
    res.metadata = {k: v for k, v in SENZING_METADATA.items() if k != "access_mode"}
    res.script = [res.require_auth]
    out = call()
    assert out.code == "unexpected_challenge" and seams.exchanges == []


def test_auth_token_challenge_without_token_or_ps(harness, seams):
    res = harness["res"]
    res.script = [FakeResource.require("auth-token")]
    bind_person_token(harness)
    assert call().code == "resource_token_missing"
    res.script = [res.require_auth]
    harness["ps"] = None
    out = call()
    assert out.code == "person_server_unconfigured"


def test_exchange_pending_interaction_and_approval(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [res.require_auth]
    seams.exchange_script = [auth_token.ExchangePending("interaction", PS + "/aauth/pending/x", 7,
                                                        url=PS + "/auth", code="ABCD-1234")]
    out = call()
    assert out.outcome == "interaction_required" and out.handle == seams.handle and out.retry_after == 7
    assert out.link == PS + "/auth?code=ABCD-1234" and out.challenges == ["auth-token"]
    kind, record = seams.pending[0]
    assert kind == "ps_interaction" and record["body"] == BODY and record["pending_url"] == PS + "/aauth/pending/x"
    held = pt.held_token(AGENT, RES, now=NOW).token
    assert record["presented_kind"] == "person"
    assert held not in str(record) and seams.exchanges[0][0].token not in str(record)

    res.script = [res.require_auth]
    seams.exchange_script = [auth_token.ExchangePending("approval", PS + "/aauth/pending/y", 5)]
    out = call()
    assert out.outcome == "pending" and out.requirement == "approval" and out.at == "person_server"
    assert seams.pending[1][0] == "ps_approval"


def test_exchange_pending_unrecordable_is_a_refusal(harness, seams, monkeypatch):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [res.require_auth]
    seams.exchange_script = [auth_token.ExchangePending("approval", PS + "/p", 5)]
    monkeypatch.setattr(rc, "_save_pending", rc_save_pending)

    def boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(ps_client, "save_pending", boom)
    assert call().code == "state_unavailable"


def test_expired_presented_token_gets_one_fresh_chain(harness, seams):
    res, ps = harness["res"], harness["ps_fake"]
    bind_person_token(harness)
    bind_person_token(harness)
    res.script = [res.require_auth, res.require_auth, FakeResource.ok()]
    seams.exchange_script = [auth_token.ExchangeRefused("expired_presented_token", "stale", 400), issued]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth"
    assert out.challenges == ["auth-token", "ps:expired_presented_token", "auth-token"]
    assert len(ps.posts) == 2 and len(res.calls) == 3 and len(seams.exchanges) == 2
    first, second = seams.exchanges[0][1], seams.exchanges[1][1]
    assert first.jti != second.jti                                      # a FRESH person token was presented
    assert seams.exchanges[1][0].presented_jti == second.jti


def test_expired_presented_token_twice_is_a_refusal(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    bind_person_token(harness)
    res.script = [res.require_auth, res.require_auth]
    seams.exchange_script = [auth_token.ExchangeRefused("expired_presented_token", "stale", 400),
                             auth_token.ExchangeRefused("expired_presented_token", "stale", 400)]
    out = call()
    assert out.outcome == "refusal" and out.code == "ps_rejected:expired_presented_token" and out.status == 400


def test_exchange_denials_and_ps_unreachable(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [res.require_auth]
    seams.exchange_script = [auth_token.ExchangeRefused("user_unreachable", "no channel", 403)]
    out = call()
    assert out.outcome == "denied" and out.reason == "user_unreachable" and out.do_not_retry
    res.script = [res.require_auth]
    seams.exchange_script = [auth_token.ExchangeRefused("as_unreachable", "down", 502)]
    out = call()
    assert out.outcome == "refusal" and out.code == "ps_unreachable" and out.status == 502


def test_step_up_on_a_held_auth_token_once(harness, seams):
    res = harness["res"]
    info = signer.agent_token_info(AGENT, now=NOW)
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 900)
    seams.held = auth_token.inspect_auth_token(auth_token_for(pres, jose.public_jwk(info.key.public_key())),
                                               resource=RES, ps=PS, agent_jkt=info.thumbprint,
                                               presented_sub=pres.sub, presented_exp=pres.exp, now=NOW)
    res.script = [res.require_auth, FakeResource.ok()]
    seams.exchange_script = [issued]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == ["auth-token"]
    assert seams.exchanges[0][1].kind == "auth" and seams.exchanges[0][1].token == seams.held.token
    # a second step-up in the same call is a loop
    res.script = [res.require_auth, res.require_auth]
    seams.exchange_script = [issued, issued]
    assert call().code == "step_up_loop"


def test_expired_auth_token_falls_back_to_the_person_token_once(harness, seams):
    res = harness["res"]
    info = signer.agent_token_info(AGENT, now=NOW)
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 900)
    seams.held = auth_token.inspect_auth_token(auth_token_for(pres, jose.public_jwk(info.key.public_key())),
                                               resource=RES, ps=PS, agent_jkt=info.thumbprint,
                                               presented_sub=pres.sub, presented_exp=pres.exp, now=NOW)
    bind_person_token(harness)
    res.script = [FakeResource.signature_error("expired_jwt"), FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.token_kind == "person" and out.challenges == ["signature-error:expired_jwt"]
    assert seams.forgotten == [RES]
    res.script = [FakeResource.signature_error("expired_jwt"), FakeResource.signature_error("expired_jwt")]
    out = call()
    assert out.code == "signature_rejected"


def test_agent_token_challenge_retries_once_with_the_agent_token(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.require("agent-token"), FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.token_kind == "agent" and out.challenges == ["agent-token"]
    assert presented_of(res.calls[1])[1]["iss"] == OUR
    res.script = [FakeResource.require("agent-token"), FakeResource.require("agent-token")]
    assert call().code == "agent_token_rejected"


def test_person_token_rejected_after_presenting_it(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.require("person-token")]
    assert call().code == "person_token_rejected"


def test_unknown_and_malformed_requirements_are_never_retried(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.require("payment")]
    out = call()
    assert out.code == "unsupported_requirement:payment" and len(res.calls) == 1
    res.script = [(401, {"aauth-requirement": "requirement=(a b)"}, "")]
    assert call().code == "unsupported_requirement"


def test_resource_deferred_202_returns_a_handle_without_polling(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.deferred(retry_after="9")]
    out = call()
    assert out.outcome == "pending" and out.at == "resource" and out.requirement == "deferred" and out.retry_after == 9
    kind, record = seams.pending[0]
    assert kind == "resource_polling" and record["pending_url"] == RES + "/v1/pending/1" and record["token_kind"] == "person"
    assert len(res.calls) == 1
    res.script = [FakeResource.deferred(location="/v1/pending/rel")]
    assert seams.pending.__len__() == 1 and call().outcome == "pending" and seams.pending[1][1]["pending_url"] == RES + "/v1/pending/rel"


def test_resource_deferred_refusals(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.deferred(location=OTHER + "/p")]
    assert call().code == "resource_protocol_error"
    res.script = [FakeResource.deferred(location=None)]
    assert call().code == "resource_protocol_error"
    res.script = [FakeResource.deferred(requirement_header="requirement=clarification")]
    assert call().code == "unsupported_requirement:clarification"
    assert seams.pending == []


def test_resource_deferred_with_auth_token_exchanges_then_records(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [lambda req: FakeResource.deferred(requirement_header=
                  f'requirement=auth-token; resource-token="{res.resource_token_for(req)}"')]
    seams.exchange_script = [issued]
    out = call()
    assert out.outcome == "pending" and out.challenges == ["auth-token"] and seams.pending[0][1]["token_kind"] == "auth"
    assert len(seams.stored) == 1


@pytest.mark.parametrize("script, outcome, key, value", [
    (_problem(402, "payment_required", "pay 2 credits"), "refusal", "code", "payment_required"),
    (_problem(403, "denied", "the person said no"), "denied", "reason", "denied"),
    (_problem(403, "forbidden_thing"), "denied", "reason", "forbidden"),
    (_problem(408, "expired"), "expired", "reason", "request_expired"),
    (_problem(410, "gone"), "refusal", "code", "gone"),
    (_problem(429, "slow_down", headers={"retry-after": "12"}), "refusal", "code", "rate_limited"),
    (_problem(500, "boom"), "refusal", "code", "resource_error"),
    ((204, {}, ""), "result", "status", 204),
])
def test_status_mapping(harness, seams, script, outcome, key, value):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [script]
    out = call()
    assert out.outcome == outcome and getattr(out, key) == value
    if outcome == "refusal" and out.code == "rate_limited":
        assert out.retry_after == 12
    if outcome == "denied":
        assert out.do_not_retry and len(out.detail) <= 500


def test_round_and_time_budgets(harness, seams, monkeypatch):
    res = harness["res"]
    bind_person_token(harness)
    monkeypatch.setattr(rc, "MAX_RESOURCE_SENDS", 1)
    res.script = [FakeResource.require("agent-token"), FakeResource.ok()]
    assert call().code == "too_many_rounds"
    out = call(budget_seconds=0)
    assert out.code == "timeout" and not out.possibly_executed
    monkeypatch.setattr(rc, "MAX_RESOURCE_SENDS", 6)
    monkeypatch.setattr(rc, "MAX_PS_EXCHANGES", 0)
    res.script = [res.require_auth]
    assert call().code == "too_many_rounds"


def test_timeout_after_a_post_is_possibly_executed(harness, seams, monkeypatch):
    res = harness["res"]
    bind_person_token(harness)

    res.script = [FakeResource.ok()]
    assert call().outcome == "result"                        # metadata + person token held before egress is cut

    async def slow(*a, **kw):
        raise egress.EgressError("timeout", "The resource timed out.")
    monkeypatch.setattr(egress, "request_capped", slow)
    out = call()
    assert out.code == "timeout" and out.possibly_executed
    assert call(method="GET", body=None).possibly_executed is False


def test_tokens_are_scrubbed_from_the_body_and_errors(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    stray = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"
    res.script = [lambda req: FakeResource.ok({"echo": presented_of(req)[0], "stray": stray,
                                              "agent": signer.agent_token_info(AGENT, now=NOW).token})]
    out = call()
    held = pt.held_token(AGENT, RES, now=NOW).token
    assert held not in out.body and stray not in out.body and signer.agent_token_info(AGENT, now=NOW).token not in out.body
    assert "[redacted-token]" in out.body and "[redacted-jws]" in out.body
    assert rc.scrub_tokens(f"x {stray} y", set()) == "x [redacted-jws] y"


def test_egress_refusals_name_the_resource(harness, seams, monkeypatch):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [(302, {"location": OTHER}, "")]
    out = call()
    assert out.code == "redirect" and "The resource returned a redirect" in out.detail
    res.script = [(200, {"content-encoding": "gzip"}, "x")]
    assert call().code == "encoding"


def test_person_token_forget_drops_the_held_token(harness, seams):
    res = harness["res"]
    bind_person_token(harness)
    res.script = [FakeResource.ok()]
    call()
    assert pt.held_token(AGENT, RES, now=NOW) is not None
    assert pt.forget(AGENT, RES) is True and pt.held_token(AGENT, RES, now=NOW) is None
    assert pt.forget(AGENT, RES) is False


# =========================================================================== #
# Checkpoint B — allow-list (sink), ps_client exchange + cache + records, resume
# =========================================================================== #
class FakePS2(FakePS):
    """ent#878's fake PS plus the auth-token endpoint and its pending URL."""

    AUTH_PENDING = "/aauth/pending/auth-1"

    def __init__(self):
        super().__init__()
        self.auth_posts, self.auth_polls = [], []
        self.auth_script, self.auth_poll_script = [], []

    def auth_token_for(self, request: httpx.Request, *, sub=None, cnf=None, header=None, **claims):
        doc = json.loads(request.content.decode()) if request.content else {}
        _, pres, _, _ = jose.decode_unverified(doc["presented_token"])
        body = {"iss": PS, "ps": PS, "dwk": "aauth-person.json", "aud": RES, "sub": sub or pres["sub"], "scope": "senzing",
                "jti": "at-" + os.urandom(4).hex(), "iat": NOW, "exp": NOW + 3600,
                "cnf": {"jwk": cnf or pres["cnf"]["jwk"]}}
        body.update(claims)
        hdr = {"alg": "RS256", "typ": "aa-auth+jwt", "kid": "ps-kid"}
        hdr.update(header or {})
        return jose.sign_jws(hdr, body, self.key)

    @staticmethod
    def auth_ok(token, expires_in=3600):
        return (200, {"content-type": "application/json"}, json.dumps({"auth_token": token, "expires_in": expires_in}))

    @staticmethod
    def auth_pending(requirement_header="auto", *, location=PS + "/aauth/pending/auth-1", retry_after="5"):
        headers = {"content-type": "application/json"}
        if requirement_header == "auto":
            requirement_header = f'requirement=interaction; url="{PS}/auth"; code="WXYZ-5678"'
        if requirement_header is not None:
            headers["aauth-requirement"] = requirement_header
        if location is not None:
            headers["location"] = location
        if retry_after is not None:
            headers["retry-after"] = retry_after
        return (202, headers, json.dumps({"status": "pending"}))

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/aauth/token/auth":
            self.auth_posts.append(request)
            step = self.auth_script.pop(0)
        elif request.method == "GET" and path == self.AUTH_PENDING:
            self.auth_polls.append(request)
            step = self.auth_poll_script.pop(0)
        else:
            return super().handle(request)
        if callable(step):
            step = step(request)
        status, headers, body = step
        raw = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(raw))


@pytest.fixture()
def wired(harness):
    """The harness with a PS that speaks the auth-token endpoint and NO seams."""
    ps = FakePS2()
    harness["ps_fake"] = ps
    harness["ps2"] = ps
    return harness


def _wired_route(harness, monkeypatch):
    res, ps = harness["res"], harness["ps_fake"]

    def route(request):
        host = request.headers.get("host") or request.url.host
        return res.handle(request) if host == "res.example" else ps.handle(request)

    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    monkeypatch.setattr(a2a_client, "_http_client", factory)


@pytest.fixture()
def live(wired, monkeypatch):
    _wired_route(wired, monkeypatch)
    return wired


def resume(handle, now=NOW, agent=AGENT, **kw):
    return asyncio.run(rc.resume(agent, handle, now=now, **kw))


# --------------------------------------------------------------------------- #
# resource_allowlist — fail-closed matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, expect", [
    ('["https://res.example"]', (RES,)),
    ('["https://res.example", "https://other.example"]', (RES, OTHER)),
    ("https://res.example, https://other.example", (RES, OTHER)),
    ('["https://res.example", "https://res.example"]', (RES,)),
    (None, ()), ("", ()), ("   ", ()), ("[]", ()), ("{}", ()), ("[1]", ()), ('"https://res.example"', ()),
    ("not json [", ()), ('["https://res.example", "https://bad"]', ()),          # one bad line empties the list
    ('["http://res.example"]', ()), ('["https://res.example:8443"]', ()), ('["https://res.example/v1"]', ()),
    ('["https://RES.example"]', ()), ('["https://res.example/"]', ()), ("x" * 20000, ()),
    ("[" + ",".join(f'"https://h{i}.example"' for i in range(101)) + "]", ()),
])
def test_allowlist_parse_matrix(raw, expect):
    assert resource_allowlist.parse_allowlist(raw) == expect


def test_allowlist_resolution_settings_then_env_fail_closed(monkeypatch):
    from services.settings_service import settings_service

    monkeypatch.setattr(settings_service, "get_setting", lambda key: '["https://res.example"]')
    monkeypatch.setenv("AAUTH_RESOURCE_ALLOWLIST", "https://other.example")
    assert resource_allowlist.allowed_resources() == (RES,)                     # settings win
    assert resource_allowlist.is_allowed(RES) and not resource_allowlist.is_allowed(OTHER)
    monkeypatch.setattr(settings_service, "get_setting", lambda key: None)
    assert resource_allowlist.allowed_resources() == (OTHER,)                   # env fallback
    monkeypatch.delenv("AAUTH_RESOURCE_ALLOWLIST")
    assert resource_allowlist.allowed_resources() == ()

    def boom(key):
        raise RuntimeError("db down")
    monkeypatch.setattr(settings_service, "get_setting", boom)
    assert resource_allowlist.allowed_resources() == () and not resource_allowlist.is_allowed(RES)   # unreadable → none


@pytest.mark.parametrize("probe", [OTHER, "https://res.example.evil", "https://evil.res.example", "https://res.example/v1",
                                   "http://res.example", "https://res.example:443", "", None, 5])
def test_allowlist_origin_equality_not_prefix(probe, monkeypatch):
    monkeypatch.setattr(resource_allowlist, "allowed_resources", lambda: (RES,))
    assert resource_allowlist.is_allowed(probe) is False
    assert resource_allowlist.is_allowed(RES) is True


def test_ladder_refuses_unlisted_resource_before_any_egress(harness, seams):
    harness["allowed"] = ()
    out = call()
    assert out.outcome == "refusal" and out.code == "resource_not_allowed" and out.resource == RES
    assert harness["res"].metadata_fetches == 0 and harness["res"].calls == [] and harness["ps_fake"].posts == []
    harness["allowed"] = (OTHER,)
    assert call().code == "resource_not_allowed"


# --------------------------------------------------------------------------- #
# ps_client — the auth-token cache
# --------------------------------------------------------------------------- #
def _held(info, exp=NOW + 3600):
    pres = rc.Presented("person", "t", "ps-directed-sub", "pt-1", NOW + 3600)
    return auth_token.inspect_auth_token(auth_token_for(pres, jose.public_jwk(info.key.public_key()), exp=exp),
                                         resource=RES, ps=PS, agent_jkt=info.thumbprint, presented_sub=pres.sub,
                                         presented_exp=pres.exp, now=NOW)


def test_auth_cache_round_trip_margin_key_and_outage(harness):
    info = signer.agent_token_info(AGENT, now=NOW)
    held = _held(info)
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW) is None
    assert ps_client.store_auth_token(AGENT, held, now=NOW) is True
    got = ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW)
    assert got.token == held.token and got.sub == held.sub and got.scope == "senzing"
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW + 3600 - 299) is None
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint="other-key", now=NOW) is None
    assert ps_client.held_auth_token(AGENT, resource=OTHER, key_thumbprint=info.thumbprint, now=NOW) is None
    assert ps_client.held_auth_token("other-agent", resource=RES, key_thumbprint=info.thumbprint, now=NOW) is None
    assert ps_client.store_auth_token(AGENT, _held(info, exp=NOW + 100), now=NOW) is False     # already inside the margin
    ps_client.forget_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint)
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW) is None
    harness["no_redis"] = True
    assert ps_client.store_auth_token(AGENT, held, now=NOW) is False
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW) is None


def test_auth_cache_value_never_logged_in_repr_and_unreadable_is_not_held(harness, monkeypatch):
    """A Redis read error is "not held" for the ladder — one more exchange, never an unsigned call."""
    harness["redis"].fail = True
    with pytest.raises(RuntimeError):
        ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint="k", now=NOW)
    res = harness["res"]
    res.metadata = {k: v for k, v in SENZING_METADATA.items() if k != "access_mode"}
    harness["redis"].fail = False
    res.script = [FakeResource.ok()]
    harness["redis"].fail = True
    out = call()                                   # cache read raises → not held → agent token → 200
    assert out.outcome == "result" and out.token_kind == "agent"


# --------------------------------------------------------------------------- #
# ps_client — pending records in ent#878's namespace
# --------------------------------------------------------------------------- #
def _record(**over):
    rec = {"kind": "ps_interaction", "agent": AGENT, "our_issuer": OUR, "ps_issuer": PS, "resource": RES,
           "method": "POST", "path": PATH, "body": BODY.decode(), "content_type": "application/json",
           "key_thumbprint": "k", "challenges": ["auth-token"], "created_at": NOW, "expires_at": NOW + 1800,
           "pending_url": PS + FakePS2.AUTH_PENDING, "retry_after": 5, "url": PS + "/auth", "code": "WXYZ-5678",
           "status": "pending", "presented_jti": "pt-1", "presented_kind": "person", "presented_sub": "ps-directed-sub",
           "presented_exp": NOW + 900, "next_poll_at": NOW}
    rec.update(over)
    return rec


def test_pending_records_are_kind_and_agent_scoped(harness):
    handle = ps_client.save_pending(AGENT, _record(), now=NOW)
    assert pt.HANDLE_RE.match(handle) and len(harness["redis"].handles()) == 1
    got = ps_client.load_pending(AGENT, handle)
    assert got["kind"] == "ps_interaction" and got["body"] == BODY.decode() and got["scope"].startswith("879\n")
    assert ps_client.load_pending("other-agent", handle) is None
    assert ps_client.load_pending(AGENT, "nope") is None and ps_client.load_pending(AGENT, None) is None
    # ent#878's resume refuses our kinds; our loader refuses its kind
    out = asyncio.run(pt.resume(AGENT, handle, now=NOW))
    assert out.code == "pending_unknown"
    with pytest.raises(ValueError):
        ps_client.save_pending(AGENT, _record(kind="person_token"), now=NOW)
    with pytest.raises(ValueError):
        ps_client.save_pending("other-agent", _record(), now=NOW)
    ps_client.delete_pending(handle, got)
    assert ps_client.load_pending(AGENT, handle) is None
    harness["no_redis"] = True
    with pytest.raises(RuntimeError):
        ps_client.save_pending(AGENT, _record(), now=NOW)
    with pytest.raises(RuntimeError):
        ps_client.load_pending(AGENT, handle)


# --------------------------------------------------------------------------- #
# ps_client — the exchange against the PS auth-token endpoint
# --------------------------------------------------------------------------- #
def _exchange(live, **kw):
    """Drive the real exchange: a bound person token, a resource token minted for it."""
    bind_person_token(live)
    res = live["res"]
    res.script = [res.require_auth]
    return call(**kw)


def test_exchange_posts_the_signed_request_shape_and_holds_the_token(live):
    ps = live["ps2"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    bind_person_token(live)
    res = live["res"]
    res.script = [res.require_auth, FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == ["auth-token"]
    req = ps.auth_posts[0]
    doc = json.loads(req.content)
    assert set(doc) == {"resource_token", "presented_token", "justification", "capabilities"}
    assert doc["capabilities"] == ["interaction"] and doc["justification"] == "demo"
    assert doc["presented_token"] == pt.held_token(AGENT, RES, now=NOW).token
    _, rt, _, _ = jose.decode_unverified(doc["resource_token"])
    assert rt["dwk"] == "aauth-resource.json" and rt["ps"] == PS
    assert req.headers["prefer"].startswith("wait=") and req.headers["content-type"] == "application/json"
    _, sk = presented_of(req)
    assert sk["iss"] == OUR                                   # the PS leg is signed with the AGENT token
    # held in Redis, presented on the next call with no PS trip at all
    info = signer.agent_token_info(AGENT, now=NOW)
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW).token == \
        presented_of(res.calls[1])[0]
    res.script = [FakeResource.ok()]
    again = call()
    assert again.outcome == "result" and again.token_kind == "auth" and again.challenges == []
    assert len(ps.auth_posts) == 1 and len(ps.posts) == 1


def test_exchange_refuses_an_auth_token_bound_elsewhere(live):
    ps = live["ps2"]
    other = jose.public_jwk(Ed25519PrivateKey.generate().public_key())
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req, cnf=other))]
    out = _exchange(live)
    assert out.code == "ps_rejected:auth_token_invalid" and out.status == 200
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req, sub="someone-else"))]
    assert _exchange(live).code == "ps_rejected:auth_token_invalid"
    ps.auth_script = [(200, {"content-type": "application/json"}, "[]")]
    assert _exchange(live).code == "ps_rejected:auth_token_invalid"
    info = signer.agent_token_info(AGENT, now=NOW)
    assert ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=info.thumbprint, now=NOW) is None


def test_exchange_pending_interaction_records_and_relays(live):
    ps = live["ps2"]
    ps.auth_script = [FakePS2.auth_pending()]
    out = _exchange(live)
    assert out.outcome == "interaction_required" and out.link == PS + "/auth?code=WXYZ-5678" and out.at == "person_server"
    rec = ps_client.load_pending(AGENT, out.handle)
    assert rec["kind"] == "ps_interaction" and rec["pending_url"] == PS + FakePS2.AUTH_PENDING and rec["body"] == BODY.decode()
    assert rec["presented_sub"] == "ps-directed-sub" and rec["presented_exp"] > NOW
    held = pt.held_token(AGENT, RES, now=NOW).token
    assert held not in json.dumps(rec)
    ps.auth_script = [FakePS2.auth_pending(requirement_header=None, retry_after="9")]
    out = _exchange(live)
    assert out.outcome == "pending" and out.requirement == "approval" and out.retry_after == 9


@pytest.mark.parametrize("script, code", [
    (FakePS2.auth_pending('requirement=interaction; url="https://evil.example/auth"; code="WXYZ-5678"'), "ps_rejected:ps_protocol_error"),
    (FakePS2.auth_pending('requirement=interaction; url="https://ps.example/auth"; code="ILOU"'), "ps_rejected:ps_protocol_error"),
    (FakePS2.auth_pending("requirement=(x)"), "ps_rejected:ps_protocol_error"),
    (FakePS2.auth_pending("requirement=payment"), "ps_rejected:unsupported_requirement"),
    (FakePS2.auth_pending(location="https://evil.example/p"), "ps_rejected:ps_protocol_error"),
    (FakePS.problem(400, "invalid_resource_token"), "ps_rejected:invalid_resource_token"),
    (FakePS.problem(400, "weird!"), "ps_rejected:ps_error"),
    ((401, {"signature-error": 'error="invalid_signature"'}, ""), "ps_rejected:signature_rejected"),
    (FakePS.problem(502, "as_unreachable"), "ps_unreachable"),
    (FakePS.problem(500, "boom"), "ps_rejected:ps_error"),
])
def test_exchange_refusal_mapping(live, script, code):
    live["ps2"].auth_script = [script]
    out = _exchange(live)
    assert out.outcome == "refusal" and out.code == code


@pytest.mark.parametrize("script, reason", [
    (FakePS.problem(403, "user_unreachable"), "user_unreachable"),
    (FakePS.problem(403, "denied"), "denied"),
    (FakePS.problem(403, "other"), "denied"),
])
def test_exchange_denials(live, script, reason):
    live["ps2"].auth_script = [script]
    out = _exchange(live)
    assert out.outcome == "denied" and out.reason == reason and out.do_not_retry


def test_exchange_expired_presented_token_runs_one_fresh_chain_for_real(live):
    ps, res = live["ps2"], live["res"]
    ps.auth_script = [FakePS.problem(400, "expired_presented_token"), lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    bind_person_token(live)
    bind_person_token(live)
    res.script = [res.require_auth, res.require_auth, FakeResource.ok()]
    out = call()
    assert out.outcome == "result" and out.token_kind == "auth" and len(ps.posts) == 2 and len(ps.auth_posts) == 2
    first, second = (json.loads(r.content)["presented_token"] for r in ps.auth_posts)
    assert first != second


def test_exchange_person_server_failures(live, monkeypatch):
    ps = live["ps2"]
    bind_person_token(live)                         # the person token is cached from a healthy PS first
    live["res"].script = [FakeResource.ok()]
    assert call().outcome == "result"
    ps.metadata_status = 500
    person_server.reset_cache()
    live["res"].script = [live["res"].require_auth]
    out = call()
    assert out.outcome == "refusal" and out.code == "ps_rejected:person_server_unreachable"
    ps.metadata_status = 200
    person_server.reset_cache()
    live["ps"] = None
    live["res"].script = [live["res"].require_auth]
    assert call().code == "person_server_unconfigured"


# --------------------------------------------------------------------------- #
# resume — the pending handle
# --------------------------------------------------------------------------- #
def _pending_handle(live):
    live["ps2"].auth_script = [FakePS2.auth_pending(retry_after="0")]
    out = _exchange(live)
    assert out.outcome == "interaction_required"
    return out.handle


def test_resume_polls_then_resends_the_stored_request(live):
    ps, res = live["ps2"], live["res"]
    handle = _pending_handle(live)
    ps.auth_poll_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(httpx.Request(
        "POST", PS, content=json.dumps({"presented_token": pt.held_token(AGENT, RES, now=NOW).token}).encode())))]
    res.script = [FakeResource.ok()]
    out = resume(handle, now=NOW + 1)
    assert out.outcome == "result" and out.token_kind == "auth" and out.challenges == ["auth-token"]
    sent = res.calls[-1]
    assert sent.method == "POST" and sent.url.path == PATH and sent.content == BODY        # the STORED request
    assert presented_of(sent)[1]["aud"] == RES and presented_of(sent)[1]["iss"] == PS
    assert ps_client.load_pending(AGENT, handle) is None                                  # consumed
    assert ps.auth_polls[0].headers["prefer"] == "wait=15"
    res.script = [FakeResource.ok()]
    assert call().challenges == [] and len(ps.auth_posts) == 1                              # cached afterwards


def test_resume_states(live):
    ps = live["ps2"]
    handle = _pending_handle(live)
    # still pending: same handle, refreshed interval, url/code kept
    ps.auth_poll_script = [FakePS2.auth_pending(retry_after="20")]
    out = resume(handle, now=NOW + 1)
    assert out.outcome == "interaction_required" and out.handle == handle and out.retry_after == 20
    assert out.link == PS + "/auth?code=WXYZ-5678"
    # early poll: no network
    polls = len(ps.auth_polls)
    assert resume(handle, now=NOW + 2).handle == handle and len(ps.auth_polls) == polls
    # slow down: +5 s
    ps.auth_poll_script = [FakePS.problem(429, "slow_down")]
    out = resume(handle, now=NOW + 30)
    assert out.outcome == "interaction_required" and out.retry_after == 25
    # denied: terminal, record gone
    ps.auth_poll_script = [FakePS.problem(403, "abandoned")]
    out = resume(handle, now=NOW + 60)
    assert out.outcome == "denied" and out.reason == "abandoned" and ps_client.load_pending(AGENT, handle) is None
    assert resume(handle, now=NOW + 61).reason == "handle_unknown"


@pytest.mark.parametrize("script, outcome, key, value", [
    (FakePS.problem(408, "expired"), "expired", "reason", "pending_expired"),
    (FakePS.problem(410, "gone"), "refusal", "code", "gone"),
    (FakePS.problem(502, "as_unreachable"), "refusal", "code", "ps_unreachable"),
    (FakePS.problem(500, "boom"), "refusal", "code", "ps_rejected:ps_error"),
])
def test_resume_terminal_and_transient_refusals(live, script, outcome, key, value):
    handle = _pending_handle(live)
    live["ps2"].auth_poll_script = [script]
    out = resume(handle, now=NOW + 1)
    assert out.outcome == outcome and getattr(out, key) == value
    gone = ps_client.load_pending(AGENT, handle) is None
    assert gone is (outcome == "expired" or value == "gone")      # transient PS errors keep the record


def test_resume_refuses_unknown_wrong_agent_rotated_and_repointed(live):
    handle = _pending_handle(live)
    assert resume("x" * 32).outcome == "expired" and resume("x" * 32).reason == "handle_unknown"
    assert resume(handle, agent="other-agent").reason == "handle_unknown"
    out = resume(handle, now=NOW + 3600)                   # a new key window
    assert out.outcome == "refusal" and out.code == "key_rotated" and ps_client.load_pending(AGENT, handle) is None
    handle = _pending_handle(live)
    live["allowed"] = ()
    assert resume(handle, now=NOW + 1).code == "resource_not_allowed"
    live["allowed"] = (RES,)
    handle = _pending_handle(live)
    live["ps"] = "https://ps2.example"
    assert resume(handle, now=NOW + 1).reason == "handle_unknown"
    live["ps"] = PS
    live["no_redis"] = True
    assert resume(handle, now=NOW + 1).code == "state_unavailable"
    live["no_redis"] = False
    live["issuer"] = None
    with pytest.raises(signer.SigningUnavailable):
        resume(handle)


def test_resume_resource_deferred_poll(live):
    ps, res = live["ps2"], live["res"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    bind_person_token(live)
    res.script = [res.require_auth, FakeResource.deferred(retry_after="0")]
    out = call()
    assert out.outcome == "pending" and out.at == "resource"
    handle = out.handle
    res.script = [FakeResource.deferred(retry_after="7")]
    again = resume(handle, now=NOW + 1)
    assert again.outcome == "pending" and again.handle == handle and again.retry_after == 7
    poll = res.calls[-1]
    assert poll.method == "GET" and poll.url.path == "/v1/pending/1"
    assert presented_of(poll)[0] == ps_client.held_auth_token(AGENT, resource=RES, key_thumbprint=signer.agent_token_info(AGENT, now=NOW).thumbprint, now=NOW).token
    res.script = [FakeResource.ok({"done": True})]
    done = resume(handle, now=NOW + 10)
    assert done.outcome == "result" and json.loads(done.body) == {"done": True} and ps_client.load_pending(AGENT, handle) is None
    # the auth token the deferral used is gone from the cache → named refusal, call again
    res.script = [res.require_auth, FakeResource.deferred(retry_after="0")]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    ps_client.forget_auth_token(AGENT, resource=RES, key_thumbprint=signer.agent_token_info(AGENT, now=NOW).thumbprint)
    handle = call().handle
    ps_client.forget_auth_token(AGENT, resource=RES, key_thumbprint=signer.agent_token_info(AGENT, now=NOW).thumbprint)
    assert resume(handle, now=NOW + 1).code == "token_unavailable"


def test_pending_records_share_ent878_open_interaction_budget(live):
    """M1: resource-client records count against ent#878's per-agent bound — so
    they are checked against it too. Three open → the fourth is refused with the
    same code ent#878 uses; a terminal outcome frees the slot."""
    ps, res = live["ps2"], live["res"]
    ps.auth_script = [lambda req: FakePS2.auth_ok(ps.auth_token_for(req))]
    bind_person_token(live)
    res.script = [res.require_auth, FakeResource.deferred(retry_after="0")]
    handles = [call().handle]
    for _ in range(pt.MAX_PENDING_PER_AGENT - 1):
        res.script = [FakeResource.deferred(retry_after="0")]
        out = call()
        assert out.outcome == "pending"
        handles.append(out.handle)
    assert len(set(handles)) == pt.MAX_PENDING_PER_AGENT
    res.script = [FakeResource.deferred(retry_after="0")]
    out = call()
    assert out.outcome == "refusal" and out.code == "too_many_pending"
    assert len(live["redis"].handles()) == pt.MAX_PENDING_PER_AGENT       # nothing was written
    # the same bound ent#878 enforces, from the same set
    assert asyncio.run(pt.person_token(AGENT, "https://other-res.example", now=NOW)).code == "too_many_pending"
    # a terminal outcome frees a slot
    res.script = [FakeResource.ok({"done": True})]
    assert resume(handles[0], now=NOW + 1).outcome == "result"
    res.script = [FakeResource.deferred(retry_after="0")]
    assert call().outcome == "pending"


def test_call_budget_clock_starts_at_entry(harness, seams, monkeypatch):
    """L3: slow discovery spends the call's budget — the send is cut, not granted a fresh 25 s."""
    res = harness["res"]
    bind_person_token(harness)
    real = resource_meta.metadata

    async def slow(resource):
        await asyncio.sleep(0.7)
        return await real(resource)
    monkeypatch.setattr(resource_meta, "metadata", slow)
    out = call(budget_seconds=1.0)
    assert out.code == "timeout" and res.calls == []


def test_pending_consent_is_recorded_or_refused_never_half(live, monkeypatch):
    live["ps2"].auth_script = [FakePS2.auth_pending()]
    live["redis"].fail = True
    bind_person_token(live)
    live["res"].script = [live["res"].require_auth]
    pt.reset_cache()
    live["redis"].fail = False
    live["res"].script = [FakeResource.ok()]
    call()                                                      # person token held
    live["res"].script = [live["res"].require_auth]
    live["redis"].fail = True
    out = call()
    assert out.outcome == "refusal" and out.code == "state_unavailable"
