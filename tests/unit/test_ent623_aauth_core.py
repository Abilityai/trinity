"""ent#623 — AAuth agent identity: crypto, signing, verification, keys, discovery.

The wire format (agent-identity-only mode, self-hosted bootstrap):
  * agent token = compact JWS `{alg: Ed25519, typ: aa-agent+jwt, kid}` over
    `{iss, dwk: aauth-agent.json, sub: aauth:<local>@<host>, jti, iat, exp, cnf.jwk}`;
  * RFC 9421 signature labelled `sig` over `@method @authority @path signature-key`
    (+ `content-digest content-type` on POST), key conveyed by
    `Signature-Key: sig=jwt;jwt="…"`, `Signature` in STANDARD base64.

The interop vector in `fixtures/ent623_hellocoop_httpsig_vector.json` was signed
by the JS reference `@hellocoop/httpsig` 2.6.0 — the only way to catch a
misreading of the spec that our signer and verifier happen to share.

Sync tests with `asyncio.run` (tests/unit/pytest.ini has no asyncio_mode).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from services import a2a_client  # noqa: E402
from services.aauth import config, discovery, httpsig, jose, keys, signer, verifier  # noqa: E402
from utils.url_validation import ValidatedPublicUrl  # noqa: E402

pytestmark = pytest.mark.unit

ISS = "https://a.example"
IDENTITY = "aauth:echo@a.example"
NOW = 1_800_000_000
VECTOR = os.path.join(os.path.dirname(__file__), "fixtures", "ent623_hellocoop_httpsig_vector.json")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class Issuer:
    def __init__(self):
        self.key = Ed25519PrivateKey.generate()
        jwk = jose.public_jwk(self.key.public_key())
        self.kid = jose.thumbprint(jwk)
        self.jwk = {**jwk, "kid": self.kid}
        self.resolver_calls = []

    async def resolve(self, iss, kid):
        self.resolver_calls.append((iss, kid))
        if iss != ISS:
            raise discovery.DiscoveryError("issuer_mismatch", "unexpected issuer")
        if kid != self.kid:
            raise discovery.DiscoveryError("unknown_key", "no such kid")
        return self.jwk

    def token(self, agent_key, *, header=None, **claims):
        body = {
            "iss": ISS, "dwk": "aauth-agent.json", "sub": IDENTITY, "jti": uuid.uuid4().hex,
            "iat": NOW, "exp": NOW + 3600,
            "cnf": {"jwk": jose.public_jwk(agent_key.public_key())},
        }
        body.update(claims)
        hdr = {"alg": "Ed25519", "typ": "aa-agent+jwt", "kid": self.kid}
        hdr.update(header or {})
        return jose.sign_jws(hdr, body, self.key)


class MemReplay:
    def __init__(self, fail=False):
        self.seen, self.fail = set(), fail

    def claim(self, key, ttl):
        if self.fail:
            raise RuntimeError("redis down")
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


BODY = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "message/send",
                   "params": {"message": {"parts": [{"kind": "text", "text": "hi"}]}}}).encode()


def signed(agent_key, token, *, method="POST", authority="b.example", path="/a2a/bot",
           body=BODY, created=NOW, content_type="application/json"):
    h = httpsig.sign(method=method, authority_value=authority, path=path, agent_token=token,
                     body=body, content_type=content_type, created=created,
                     private_key=agent_key).as_dict()
    headers = {k.lower(): v for k, v in h.items()}
    if method == "POST":
        headers["content-type"] = content_type
    return headers


def run_verify(headers, *, issuer, body=BODY, method="POST", path="/a2a/bot", authority="b.example",
               trusted=(IDENTITY,), now=NOW, replay=None, content_length="auto", read_spy=None):
    if content_length == "auto":
        content_length = len(body) if method == "POST" else None

    async def read_body():
        if read_spy is not None:
            read_spy.append(True)
        return body

    req = verifier.InboundRequest(method=method, raw_path=path, headers=headers,
                                  content_length=content_length)
    return asyncio.run(verifier.verify_request(
        req, expected_authority=authority, trusted_identities=list(trusted),
        read_body=read_body, max_body_bytes=1024 * 1024, now=now,
        replay_store=replay if replay is not None else MemReplay(),
        key_resolver=issuer.resolve,
    ))


def refused(code, fn, *args, **kwargs):
    with pytest.raises(verifier.AAuthError) as ei:
        fn(*args, **kwargs)
    assert ei.value.code == code, (ei.value.code, ei.value.detail)
    return ei.value


@pytest.fixture()
def iss():
    return Issuer()


@pytest.fixture()
def agent():
    return Ed25519PrivateKey.generate()


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #
def test_signature_is_standard_base64_and_label_sig(iss, agent):
    h = signed(agent, iss.token(agent))
    assert h["signature"].startswith("sig=:") and h["signature"].endswith(":")
    raw = h["signature"][5:-1]
    assert base64.b64decode(raw, validate=True)          # std alphabet, padded
    assert "-" not in raw and "_" not in raw
    assert h["signature-key"].startswith('sig=jwt;jwt="')
    assert h["signature-input"] == (
        'sig=("@method" "@authority" "@path" "signature-key" "content-digest" "content-type");'
        f"created={NOW}"
    )
    assert "alg=" not in h["signature-input"] and "keyid=" not in h["signature-input"]


def test_get_covers_only_the_four_required_components(iss, agent):
    h = signed(agent, iss.token(agent), method="GET", body=b"")
    assert "content-digest" not in h
    assert h["signature-input"].startswith('sig=("@method" "@authority" "@path" "signature-key");')


def test_signature_base_is_byte_exact():
    base = httpsig.signature_base(
        {"@method": "POST", "@authority": "b.example", "@path": "/x", "signature-key": 'sig=jwt;jwt="t"'},
        ("@method", "@authority", "@path", "signature-key"),
        '("@method" "@authority" "@path" "signature-key");created=1',
    )
    assert base == (
        b'"@method": POST\n"@authority": b.example\n"@path": /x\n'
        b'"signature-key": sig=jwt;jwt="t"\n'
        b'"@signature-params": ("@method" "@authority" "@path" "signature-key");created=1'
    )


@pytest.mark.parametrize("host,port,expected", [
    ("B.Example", 443, "b.example"), ("b.example", None, "b.example"), ("b.example", 8443, "b.example:8443"),
])
def test_authority_drops_default_port_and_lowercases(host, port, expected):
    assert httpsig.authority(host, port) == expected


def test_token_header_and_claims(iss, agent):
    header, claims, _, _ = jose.decode_unverified(iss.token(agent))
    assert header == {"alg": "Ed25519", "typ": "aa-agent+jwt", "kid": iss.kid}
    assert claims["dwk"] == "aauth-agent.json"
    assert claims["cnf"]["jwk"]["alg"] == "Ed25519" and "d" not in claims["cnf"]["jwk"]


@pytest.mark.parametrize("value,ok", [
    ("https://a.example", True),
    ("https://abc-def.trycloudflare.com", True),
    ("http://a.example", False),
    ("https://a.example/", False),
    ("https://a.example:8443", False),
    ("https://A.example", False),
    ("https://a.example/path", False),
    ("https://localhost", False),
    ("https://u:p@a.example", False),
    ("", False),
    (None, False),
])
def test_issuer_identifier_rules(value, ok):
    assert (config.normalize_issuer(value) is not None) is ok


# --------------------------------------------------------------------------- #
# Interop with the JS reference implementation
# --------------------------------------------------------------------------- #
def test_request_signed_by_hellocoop_httpsig_verifies():
    vec = json.load(open(VECTOR))
    req = vec["request"]

    async def resolve(iss_, kid):
        assert iss_ == vec["issuer"] and kid == vec["issuer_jwk"]["kid"]
        return vec["issuer_jwk"]

    body = req["body"].encode()

    async def read_body():
        return body

    verified, got = asyncio.run(verifier.verify_request(
        verifier.InboundRequest(method=req["method"], raw_path=req["path"], headers=req["headers"],
                                content_length=len(body)),
        expected_authority=req["authority"], trusted_identities=["aauth:echo@a.example"],
        read_body=read_body, max_body_bytes=1 << 20, now=vec["created"],
        replay_store=MemReplay(), key_resolver=resolve,
    ))
    assert verified.identity == "aauth:echo@a.example"
    assert got == body


# --------------------------------------------------------------------------- #
# Verifier — happy path and every refusal
# --------------------------------------------------------------------------- #
def test_valid_request_verifies_and_returns_body(iss, agent):
    verified, body = run_verify(signed(agent, iss.token(agent)), issuer=iss)
    assert verified.identity == IDENTITY
    assert verified.issuer == ISS
    assert verified.key_thumbprint == jose.thumbprint(jose.public_jwk(agent.public_key()))
    assert body == BODY


def test_signer_output_verifies_end_to_end(iss, agent, monkeypatch):
    """The real signer (instance key + per-agent key cache) against the real verifier."""
    signer.reset_cache()
    monkeypatch.setattr(config, "active_issuer", lambda: ISS)
    monkeypatch.setattr(keys, "get_instance_key",
                        lambda: keys.InstanceKey(kid=iss.kid, public_jwk=iss.jwk, private_key=iss.key))
    headers = signer.sign_request(agent_name="echo", method="POST", url="https://b.example:443/a2a/bot",
                                  body=BODY, content_type="application/json", now=NOW)
    headers = {k.lower(): v for k, v in headers.items()}
    headers["content-type"] = "application/json"
    verified, _ = run_verify(headers, issuer=iss)
    assert verified.identity == "aauth:echo@a.example"


def test_missing_signature_key_is_invalid_signature(iss, agent):
    h = signed(agent, iss.token(agent))
    del h["signature-key"]
    err = refused("invalid_signature", run_verify, h, issuer=iss)
    assert err.trusted is False


def test_other_signature_key_scheme_is_unsupported(iss, agent):
    h = signed(agent, iss.token(agent))
    h["signature-key"] = 'sig=hwk;kty="OKP";crv="Ed25519";x="abc"'
    refused("unsupported_scheme", run_verify, h, issuer=iss)


@pytest.mark.parametrize("drop", ["content-digest", "content-type", "signature-key", "@authority"])
def test_uncovered_required_component_is_invalid_input(iss, agent, drop):
    h = signed(agent, iss.token(agent))
    h["signature-input"] = h["signature-input"].replace(f' "{drop}"', "").replace(f'"{drop}" ', "")
    refused("invalid_input", run_verify, h, issuer=iss)


def test_alg_signature_parameter_is_refused(iss, agent):
    h = signed(agent, iss.token(agent))
    h["signature-input"] += ';alg="ed25519"'
    refused("invalid_input", run_verify, h, issuer=iss)


def test_stale_and_future_created(iss, agent):
    token = iss.token(agent)
    refused("invalid_signature", run_verify, signed(agent, token, created=NOW - 61), issuer=iss)
    refused("clock_skew", run_verify, signed(agent, token, created=NOW + 61), issuer=iss)


def test_base64url_signature_is_refused(iss, agent):
    h = signed(agent, iss.token(agent))
    raw = base64.b64decode(h["signature"][5:-1])
    h["signature"] = "sig=:" + base64.urlsafe_b64encode(raw).decode().rstrip("=") + ":"
    refused("invalid_signature", run_verify, h, issuer=iss)


def test_other_labels_are_ignored_and_repeated_fields_join(iss, agent):
    h = signed(agent, iss.token(agent))
    h["signature"] = "web=:AAAA:, " + h["signature"]
    h["signature-input"] = 'web=("@authority");created=1, ' + h["signature-input"]
    verified, _ = run_verify(h, issuer=iss)
    assert verified.identity == IDENTITY


@pytest.mark.parametrize("header,code", [
    ({"alg": "EdDSA"}, "unsupported_algorithm"),
    ({"alg": "none"}, "unsupported_algorithm"),
    ({"alg": "HS256"}, "unsupported_algorithm"),
    ({"typ": "JWT"}, "invalid_jwt"),
    ({"jku": "https://evil.example/jwks"}, "invalid_jwt"),
    ({"kid": None}, "invalid_jwt"),
])
def test_token_header_refusals(iss, agent, header, code):
    refused(code, run_verify, signed(agent, iss.token(agent, header=header)), issuer=iss)


@pytest.mark.parametrize("claims,code", [
    ({"exp": NOW - 1}, "expired_jwt"),
    ({"iat": NOW + 120, "exp": NOW + 600}, "clock_skew"),             # issued in the future
    ({"iat": NOW - 3600, "exp": NOW + 90000}, "invalid_jwt"),        # lifetime > 24 h
    ({"dwk": "aauth-person.json"}, "invalid_jwt"),
    ({"iss": "http://a.example"}, "invalid_jwt"),
    ({"sub": "aauth:echo@b.example"}, "invalid_jwt"),                # sub host != iss host
    ({"sub": "aauth:planner+search@a.example"}, "invalid_jwt"),      # sub-agent
    ({"sub": "echo@a.example"}, "invalid_jwt"),
    ({"jti": ""}, "invalid_jwt"),
    ({"iat": "now"}, "invalid_jwt"),
])
def test_token_claim_refusals(iss, agent, claims, code):
    refused(code, run_verify, signed(agent, iss.token(agent, **claims)), issuer=iss)


def test_cnf_with_private_key_or_no_alg_is_invalid_key(iss, agent):
    jwk = jose.public_jwk(agent.public_key())
    for bad in ({**jwk, "d": "x"}, {k: v for k, v in jwk.items() if k != "alg"}, {**jwk, "crv": "X25519"}):
        refused("invalid_key", run_verify, signed(agent, iss.token(agent, cnf={"jwk": bad})), issuer=iss)


def test_untrusted_issuer_is_refused_without_any_fetch(iss, agent):
    err = refused("invalid_jwt", run_verify, signed(agent, iss.token(agent)), issuer=iss,
                  trusted=("aauth:echo@other.example",))
    assert iss.resolver_calls == []
    assert err.trusted is False and err.identity == IDENTITY


def test_an_email_entry_does_not_trust_its_domain(iss, agent):
    refused("invalid_jwt", run_verify, signed(agent, iss.token(agent)), issuer=iss,
            trusted=("bob@a.example",))
    assert iss.resolver_calls == []


def test_no_trusted_identities_refuses(iss, agent):
    refused("invalid_jwt", run_verify, signed(agent, iss.token(agent)), issuer=iss, trusted=())


def test_trusted_host_compare_ignores_host_case(iss, agent):
    verified, _ = run_verify(signed(agent, iss.token(agent)), issuer=iss, trusted=("aauth:echo@A.Example",))
    assert verified.identity == IDENTITY


def test_failures_before_the_token_verifies_are_not_trusted(iss, agent):
    """`trusted` is what lets the router write an audit row naming `sub`. Until
    the token's own signature verifies, `sub` is a string the caller typed — so
    a forged token pointed at a listed issuer must not be auditable, or anyone
    can fill `audit_log` with rows attributed to an identity they invented."""
    other = Issuer()   # token signed under a kid the issuer JWKS does not carry
    err = refused("unknown_key", run_verify, signed(agent, other.token(agent)), issuer=iss)
    assert err.trusted is False and err.identity == IDENTITY

    forger = Issuer()
    forger.kid = iss.kid                      # right kid, wrong key
    assert refused("invalid_jwt", run_verify, signed(agent, forger.token(agent)),
                   issuer=iss).trusted is False


def test_failures_after_the_token_verifies_are_trusted(iss, agent):
    token = iss.token(agent)
    thief = Ed25519PrivateKey.generate()
    assert refused("invalid_signature", run_verify, signed(thief, token), issuer=iss).trusted is True
    h = signed(agent, token)
    evil = BODY.replace(b"hi", b"rm")
    assert refused("invalid_signature", run_verify, h, issuer=iss, body=evil,
                   content_length=len(evil)).trusted is True


def test_request_signed_by_a_key_other_than_cnf_fails(iss, agent):
    token = iss.token(agent)
    thief = Ed25519PrivateKey.generate()      # holds the token, not its key
    err = refused("invalid_signature", run_verify, signed(thief, token), issuer=iss)
    assert err.trusted is True


def test_wrong_authority_or_path_fails(iss, agent):
    h = signed(agent, iss.token(agent))
    refused("invalid_signature", run_verify, h, issuer=iss, authority="evil.example")
    refused("invalid_signature", run_verify, h, issuer=iss, path="/a2a/other")


def test_tampered_body_fails_digest_after_signature(iss, agent):
    h = signed(agent, iss.token(agent))
    evil = BODY.replace(b"hi", b"rm")
    refused("invalid_signature", run_verify, h, issuer=iss, body=evil, content_length=len(evil))


def test_body_is_not_read_when_the_signature_fails(iss, agent):
    spy = []
    h = signed(agent, iss.token(agent))
    refused("invalid_signature", run_verify, h, issuer=iss, authority="evil.example", read_spy=spy)
    assert spy == []


def test_missing_or_oversized_content_length(iss, agent):
    h = signed(agent, iss.token(agent))
    refused("invalid_input", run_verify, h, issuer=iss, content_length=None)
    err = refused("invalid_input", run_verify, h, issuer=iss, content_length=2 * 1024 * 1024)
    assert err.status == 413


def test_replay_is_refused(iss, agent):
    store = MemReplay()
    h = signed(agent, iss.token(agent))
    run_verify(h, issuer=iss, replay=store)
    refused("invalid_signature", run_verify, h, issuer=iss, replay=store)


def test_replay_store_down_fails_closed(iss, agent):
    err = refused("replay_check_unavailable", run_verify, signed(agent, iss.token(agent)),
                  issuer=iss, replay=MemReplay(fail=True))
    assert err.status == 503


def test_two_different_posts_in_the_same_second_are_not_replays(iss, agent):
    store, token = MemReplay(), iss.token(agent)
    other = BODY.replace(b'"1"', b'"2"')
    run_verify(signed(agent, token), issuer=iss, replay=store)
    run_verify(signed(agent, token, body=other), issuer=iss, replay=store, body=other,
               content_length=len(other))


# --------------------------------------------------------------------------- #
# Signer
# --------------------------------------------------------------------------- #
@pytest.fixture()
def live_signer(monkeypatch, iss):
    signer.reset_cache()
    state = {"issuer": ISS}
    monkeypatch.setattr(config, "active_issuer", lambda: state["issuer"])
    monkeypatch.setattr(keys, "get_instance_key",
                        lambda: keys.InstanceKey(kid=iss.kid, public_jwk=iss.jwk, private_key=iss.key))
    yield state
    signer.reset_cache()


def test_token_is_cached_per_agent_and_retired_on_issuer_change(live_signer):
    t1, k1, ident = signer.agent_token("echo", now=NOW)
    assert ident == IDENTITY
    assert signer.agent_token("echo", now=NOW + 60)[0] == t1
    assert signer.agent_token("other", now=NOW)[0] != t1
    live_signer["issuer"] = "https://new.example"
    t2, _, ident2 = signer.agent_token("echo", now=NOW + 60)
    assert t2 != t1 and ident2 == "aauth:echo@new.example"


def test_token_refreshes_inside_the_margin(live_signer):
    t1, _, _ = signer.agent_token("echo", now=NOW)
    t2, _, _ = signer.agent_token("echo", now=NOW + signer.TOKEN_LIFETIME - signer.REFRESH_MARGIN + 1)
    assert t2 != t1


def test_signing_refused_when_inert_or_name_invalid(live_signer):
    with pytest.raises(signer.SigningUnavailable):
        signer.agent_token("planner+search", now=NOW)
    live_signer["issuer"] = None
    with pytest.raises(signer.SigningUnavailable):
        signer.agent_token("echo", now=NOW)


def test_sign_request_uses_the_logical_url(live_signer):
    h = signer.sign_request(agent_name="echo", method="GET", url="https://B.example:8443/a%20b/?q=1", now=NOW)
    token = httpsig.parse_signature_key(h["Signature-Key"])
    _, claims, _, _ = jose.decode_unverified(token)
    agent_pub = jose.jwk_to_public_key(claims["cnf"]["jwk"])
    sig_input = httpsig.parse_signature_input(h["Signature-Input"])
    base = httpsig.signature_base(
        {"@method": "GET", "@authority": "b.example:8443", "@path": "/a%20b/", "signature-key": h["Signature-Key"]},
        sig_input.components, sig_input.params,
    )
    jose.verify_ed25519(base, httpsig.parse_signature(h["Signature"]), agent_pub)


# --------------------------------------------------------------------------- #
# Instance key persistence
# --------------------------------------------------------------------------- #
class _FakeSettingsDb:
    def __init__(self):
        self.rows = {}

    def get_setting_value(self, key, default=None):
        return self.rows.get(key, default)

    def insert_setting_if_absent(self, key, value):
        if key in self.rows:
            return False
        self.rows[key] = value
        return True


@pytest.fixture()
def settings_db(monkeypatch):
    import database

    fake = _FakeSettingsDb()
    monkeypatch.setattr(database, "db", fake)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
    keys.reset_cache()
    yield fake
    keys.reset_cache()


def test_first_use_race_converges_on_one_key(settings_db, monkeypatch):
    """Worker B generates a key but loses the insert; it must adopt worker A's."""
    first = keys.get_instance_key()
    keys.reset_cache()                              # "another worker"
    real_insert = settings_db.insert_setting_if_absent
    calls = []
    monkeypatch.setattr(settings_db, "insert_setting_if_absent",
                        lambda k, v: calls.append(k) or real_insert(k, v))
    second = keys.get_instance_key()
    assert second.kid == first.kid
    assert calls == []                              # row existed → no insert attempted
    # The losing branch explicitly: row appears between the read and the insert.
    keys.reset_cache()
    stored = settings_db.rows.pop(keys.SIGNING_KEY_SETTING)
    original_get = settings_db.get_setting_value
    seq = iter([None])

    def racing_get(key, default=None):
        value = next(seq, "__real__")
        if value is None:
            settings_db.rows[keys.SIGNING_KEY_SETTING] = stored   # winner lands now
            return None
        return original_get(key, default)

    monkeypatch.setattr(settings_db, "get_setting_value", racing_get)
    assert keys.get_instance_key().kid == first.kid


def test_undecryptable_key_fails_closed_and_is_not_replaced(settings_db):
    settings_db.rows[keys.SIGNING_KEY_SETTING] = "not-an-envelope"
    with pytest.raises(keys.SigningKeyUnavailable):
        keys.get_instance_key()
    assert settings_db.rows[keys.SIGNING_KEY_SETTING] == "not-an-envelope"


def test_jwks_publishes_only_public_material(settings_db):
    doc = keys.jwks()
    assert len(doc["keys"]) == 1
    k = doc["keys"][0]
    assert set(k) == {"kty", "crv", "x", "alg", "kid", "use"}
    assert k["alg"] == "Ed25519" and k["kid"] == jose.thumbprint(k)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def _json(body, status=200):
    raw = json.dumps(body).encode()
    return httpx.Response(status, headers={"content-type": "application/json"}, stream=httpx.ByteStream(raw))


@pytest.fixture()
def pinned(monkeypatch):
    async def _validate(url):
        host = httpx.URL(url).host
        return ValidatedPublicUrl(url=url, hostname=host, port=443, addresses=("93.184.216.34",))
    monkeypatch.setattr(a2a_client, "validate_endpoint", _validate)
    discovery.reset_cache()
    yield
    discovery.reset_cache()


def _factory(routes, seen):
    def make(timeout):
        def handle(request):
            seen.append((request.headers.get("host"), request.url.path))
            return routes(request)
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=timeout,
                                 follow_redirects=False, trust_env=False)
    return make


def test_fetch_issuer_keys_happy_path_is_pinned(pinned, iss):
    seen = []

    def routes(req):
        if req.url.path == "/.well-known/aauth-agent.json":
            return _json({"issuer": ISS, "jwks_uri": ISS + "/.well-known/aauth-jwks.json"})
        return _json({"keys": [iss.jwk, {"kty": "RSA", "kid": "rsa"}]})

    got = asyncio.run(discovery.fetch_issuer_keys(ISS, client_factory=_factory(routes, seen)))
    assert list(got) == [iss.kid]
    assert seen == [("a.example", "/.well-known/aauth-agent.json"), ("a.example", "/.well-known/aauth-jwks.json")]


@pytest.mark.parametrize("metadata,code", [
    ({"jwks_uri": ISS + "/jwks"}, "issuer_missing"),
    ({"issuer": "https://evil.example", "jwks_uri": ISS + "/jwks"}, "issuer_mismatch"),
    ({"issuer": ISS, "jwks_uri": "https://evil.example/jwks"}, "unknown_key"),
    ({"issuer": ISS, "jwks_uri": "http://a.example/jwks"}, "unknown_key"),
    ({"issuer": ISS}, "unknown_key"),
])
def test_fetch_issuer_keys_refusals(pinned, metadata, code):
    seen = []
    with pytest.raises(discovery.DiscoveryError) as ei:
        asyncio.run(discovery.fetch_issuer_keys(ISS, client_factory=_factory(lambda r: _json(metadata), seen)))
    assert ei.value.code == code
    assert len(seen) == 1                    # never followed to a foreign jwks_uri


def test_fetch_issuer_keys_refuses_redirects_and_http_errors(pinned):
    for resp in (httpx.Response(302, headers={"location": "https://evil.example"}), httpx.Response(404)):
        with pytest.raises(discovery.DiscoveryError):
            asyncio.run(discovery.fetch_issuer_keys(ISS, client_factory=_factory(lambda r, x=resp: x, [])))


def test_resolve_key_caches_and_rate_limits_refetch(iss):
    discovery.reset_cache()
    calls = []

    async def fetch(issuer):
        calls.append(issuer)
        return {iss.kid: iss.jwk}

    assert asyncio.run(discovery.resolve_key(ISS, iss.kid, now=1000.0, fetcher=fetch)) == iss.jwk
    assert asyncio.run(discovery.resolve_key(ISS, iss.kid, now=1010.0, fetcher=fetch)) == iss.jwk
    assert len(calls) == 1
    with pytest.raises(discovery.DiscoveryError):                # unknown kid, too soon to refetch
        asyncio.run(discovery.resolve_key(ISS, "other", now=1020.0, fetcher=fetch))
    assert len(calls) == 1
    with pytest.raises(discovery.DiscoveryError):                # unknown kid, one refetch allowed
        asyncio.run(discovery.resolve_key(ISS, "other", now=1100.0, fetcher=fetch))
    assert len(calls) == 2
    discovery.reset_cache()


def test_resolve_key_negative_caches_failures(iss):
    discovery.reset_cache()
    calls = []

    async def fetch(issuer):
        calls.append(issuer)
        raise discovery.DiscoveryError("unknown_key", "down")

    for t in (1000.0, 1030.0):
        with pytest.raises(discovery.DiscoveryError):
            asyncio.run(discovery.resolve_key(ISS, iss.kid, now=t, fetcher=fetch))
    assert len(calls) == 1
    discovery.reset_cache()


def test_resolve_key_times_out(iss, monkeypatch):
    discovery.reset_cache()
    monkeypatch.setattr(discovery, "DISCOVERY_DEADLINE", 0.05)

    async def slow(issuer):
        await asyncio.sleep(1)

    with pytest.raises(discovery.DiscoveryError) as ei:
        asyncio.run(discovery.resolve_key(ISS, iss.kid, now=1.0, fetcher=slow))
    assert ei.value.code == "unknown_key"
    discovery.reset_cache()
