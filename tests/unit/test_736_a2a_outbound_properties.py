"""#736 — Hypothesis properties for the outbound A2A path (`/edge-cases`).

Companion to ``test_736_a2a_outbound_edges.py`` (the discrete cases). Where that
file walks the boundaries one at a time, these sweep the domains that are
genuinely open-ended, and every one of them is open-ended for the same reason:
**the peer controls it**. A card, an RPC result and an error body are all
attacker-shaped JSON, and the client's stated contract for each is tolerance —
"a shape we do not recognise yields `unknown` rather than an exception", "never
raises, by contract, not by accident". A contract phrased that way is a
no-crash-total property, and a handful of hand-picked malformed payloads cannot
establish it.

WHAT THESE PROPERTIES DO AND DO NOT CLAIM
-----------------------------------------
P1–P3 and P8 are **totality** claims over peer-controlled input: no input in the
strategy raises, and the return is well-typed. They do not claim the parse is
*semantically* right for any particular peer dialect — the discrete file pins
that.

P4 has two halves that must not be confused. The bound (output never exceeds the
cap plus the marker) is a genuine invariant. The redaction half is narrower than
it looks: it shows that **exact-value** scrubbing removes a credential the peer
echoed verbatim, which is the case `sanitize_text`'s pattern list cannot cover
for an arbitrary operator-supplied secret. It does NOT — and cannot — claim
anything about a cooperating remote that base64s or splits the credential; the
module's own docstring names that residual, and no property can close it.

P5 is a structural claim about `_pinned_url` only. It is not a claim that the
connection goes to a validated address — that is a transport property and lives
in `test_736_a2a_outbound_transport.py`, driven through a real client.

P9 is the security-relevant one: adding an internal address to a resolver's
answer must never leave the URL acceptable. It is metamorphic rather than
example-based precisely because the interesting case is a MIXED answer, and
which record a resolver hands out is not ours to choose.

CI DETERMINISM (why profiles, not raw randomness)
-------------------------------------------------
The unit CI gate is a base-vs-head failing-test-ID diff, so a property that fails
on a rare draw is a new failing ID in head only — a red PR for a reason unrelated
to the change. The default ``ci`` profile is therefore derandomized with no
example database; ``HYPOTHESIS_PROFILE=explore`` restores randomized search with
far more examples and ``print_blob`` for reproducing a counterexample. Matrix
rows a property subsumes are ALSO pinned as ``@example`` (or in the edges file)
so they survive regardless of profile. Same rationale, same two profile names, as
``test_1771b_timestamp_helpers_properties.py``.

Sync throughout: ``tests/unit/pytest.ini`` overrides ``pyproject.toml``, so
``asyncio_mode = auto`` does not apply and a bare ``async def test_*`` would be
collected and never awaited.
"""
from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import a2a_client  # noqa: E402
from services.a2a_client import A2ACallError  # noqa: E402
from services.a2a_protocol import (  # noqa: E402
    UnsupportedProtocolVersion,
    resolve_dialect,
    text_from_parts,
)
from utils import url_validation as uv  # noqa: E402
from utils.url_validation import (  # noqa: E402
    A2AEndpointUrlError,
    canonical_host,
    validate_a2a_endpoint_url,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Hypothesis profiles — see the module docstring.
# --------------------------------------------------------------------------- #
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    # A loaded CI runner must not turn a data-generation stall into a head-only
    # red — the exact failure the `ci` profile exists to prevent.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "explore",
    max_examples=5000,
    deadline=None,
    derandomize=False,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "ci"))


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #
#: Anything `json.loads` can hand back. Bounded in width and depth only for
#: runtime — a 1 MiB body cap upstream means production depth is bounded too.
JSON = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=40),
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(st.text(max_size=20), children, max_size=5),
    max_leaves=25,
)

#: A "message"/"artifact"-shaped container, so the generator spends its budget on
#: the branch that has logic rather than on rejecting non-dicts. `JSON` above
#: covers the reject-everything half.
PART = st.fixed_dictionaries(
    {},
    optional={
        "kind": st.sampled_from(["text", "file", "data", ""]) | st.integers(),
        "text": st.text(max_size=60) | st.none() | st.integers(),
    },
)
CONTAINER = st.fixed_dictionaries({}, optional={"parts": st.lists(PART | JSON, max_size=6) | JSON})

#: A credential shaped the way the store actually accepts one
#: (`_HEADER_SAFE_CREDENTIAL`: printable ASCII, no whitespace) and long enough
#: that its appearance in an output is not a coincidence.
CREDENTIAL = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E), min_size=12, max_size=60
)

PUBLIC_V4 = "93.184.216.34"
PEER_URL = "https://peer.example.com/a2a/bot"

#: Every internal family the predicate is supposed to refuse. Drawn from, not
#: hardcoded into, P9 so a new range added to `_is_internal_address` can be
#: added in one place.
INTERNAL_ADDRESSES = st.sampled_from([
    "127.0.0.1", "10.0.0.1", "172.28.0.5", "172.29.0.5", "192.168.1.1",
    "169.254.169.254", "0.0.0.0", "224.0.0.1", "100.64.0.1",
    "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    # ent#393 — the mapped form of the one range the stdlib does not refuse for
    # us, so the only one whose mapped handling is Trinity's own code.
    "::ffff:100.64.0.1", "::ffff:100.127.255.254",
    "64:ff9b::7f00:1", "2002:7f00:1::", "2001:db8::1",
])
PUBLIC_ADDRESSES = st.sampled_from([
    "93.184.216.34", "1.1.1.1", "8.8.8.8", "2606:4700:4700::1111", "2001:4860:4860::8888",
])


@contextlib.contextmanager
def resolving_to(addresses):
    """Point every hostname at `addresses`, as a CONTEXT MANAGER not a fixture.

    Deliberately not `monkeypatch`: a function-scoped fixture is created once for
    the whole `@given` run and is NOT reset between generated examples, which
    Hypothesis rejects with a health check for good reason. Suppressing that
    check would work here only by accident (every example happens to re-set the
    same attribute); a context manager entered inside the test body is correct by
    construction and keeps the property honest if a future example needs a
    different resolver mid-run.
    """
    def _getaddrinfo(host, port, *a, **k):
        out = []
        for addr in addresses:
            if ":" in addr:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (addr, 0, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0)))
        return out

    original = uv.socket.getaddrinfo
    uv.socket.getaddrinfo = _getaddrinfo
    try:
        yield
    finally:
        uv.socket.getaddrinfo = original


# =========================================================================== #
# P1 — resolve_dialect is total on non-strings, and only ever answers v0.3
# =========================================================================== #
@given(JSON)
@example(1.0)          # a peer emitting protocolVersion as a JSON *number*
@example(None)
@example(True)
@example(["0.3"])
def test_P1_resolve_dialect_never_returns_an_unclaimed_dialect(raw):
    """Two claims in one, because they are the same safety argument.

    Totality: a card field is peer-controlled, so a type the parser did not
    anticipate must not raise out of `call_endpoint` — only
    `UnsupportedProtocolVersion` is caught there, and anything else would escape
    the orchestrator's `except A2ACallError` and the router's error map as a
    peer-triggerable 500.

    Range: the module documents v1.0 as *defined but not claimed*. If this ever
    returns the v1.0 dialect, a credential goes out using method names no peer
    has ever exercised against us — so "never anything but v0.3, or a named
    refusal" is the invariant, not "it parses versions correctly".
    """
    try:
        dialect = resolve_dialect(raw)
    except UnsupportedProtocolVersion:
        return  # a named refusal is the other legal answer
    assert dialect.version == "0.3"
    assert dialect.send_message == "message/send"
    assert dialect.get_task == "tasks/get"
    assert dialect.header is None


@given(st.text(max_size=30))
def test_P1_a_string_version_either_resolves_to_v03_or_is_named(raw):
    try:
        assert resolve_dialect(raw).version == "0.3"
    except UnsupportedProtocolVersion as exc:
        # The refusal is operator-facing; it must say what was seen and must not
        # be an empty or generic message.
        assert "0.3" in str(exc)


# =========================================================================== #
# P2 — text_from_parts is total (its docstring says "Never raises")
# =========================================================================== #
@given(st.one_of(JSON, CONTAINER))
@example({"parts": [{"kind": "text", "text": "a"}]})
@example({"parts": "not a list"})
@example(None)
def test_P2_text_from_parts_is_total_and_always_returns_a_string(container):
    out = text_from_parts(container)
    assert isinstance(out, str)
    # It strips the joined result, so no output can be surrounded by whitespace.
    assert out == out.strip()


@given(CONTAINER)
def test_P2_text_from_parts_only_ever_emits_declared_text_parts(container):
    """Every character of the output came from a `{"kind":"text","text":str}`
    part. Guards against a future 'be more helpful' change that started
    stringifying `data`/`file` parts — which is how peer-controlled bytes reach
    an agent's context without anyone deciding they should."""
    out = text_from_parts(container)
    if not out:
        return
    parts = container.get("parts")
    allowed = [
        p["text"] for p in parts
        if isinstance(p, dict) and p.get("kind") == "text" and isinstance(p.get("text"), str)
    ]
    assert out == "\n".join(allowed).strip()


# =========================================================================== #
# P3 — _parse_task is total over peer-controlled results
# =========================================================================== #
@given(JSON)
@example({"status": {"state": "working"}})
@example({"kind": "message", "parts": [{"kind": "text", "text": "hi"}]})
def test_P3_parse_task_is_total_and_well_typed(payload):
    state, text, task_id, context_id = a2a_client._parse_task(payload)
    assert isinstance(state, str) and state != ""
    assert text is None or isinstance(text, str)
    assert task_id is None or isinstance(task_id, str)
    assert context_id is None or isinstance(context_id, str)


@given(JSON)
def test_P3_a_non_dict_result_is_always_the_unknown_state(payload):
    assume(not isinstance(payload, dict))
    assert a2a_client._parse_task(payload) == ("unknown", None, None, None)


# =========================================================================== #
# P4 — sanitize_outbound_text: bounded, and exact-value redaction holds
# =========================================================================== #
_CAP = a2a_client.A2A_MAX_RESPONSE_CHARS
_MARKER = "\n…[truncated by Trinity]"


@given(st.text(max_size=2000), CREDENTIAL)
def test_P4_the_output_is_always_bounded(text, credential):
    out, truncated = a2a_client.sanitize_outbound_text(text, credential)
    if not text:
        assert out == text and truncated is False
        return
    assert isinstance(out, str)
    assert len(out) <= _CAP + len(_MARKER)
    assert truncated is (out.endswith(_MARKER))


@given(
    st.lists(st.text(max_size=80), min_size=1, max_size=6),
    CREDENTIAL,
)
def test_P4_a_credential_the_peer_echoes_back_never_survives(chunks, credential):
    """The load-bearing half, and the reason `sanitize_text` alone is not enough.

    `sanitize_text` matches *patterns* (`sk-`, `ghp_`, `Bearer …`). A partner's
    credential is an arbitrary operator-supplied string that matches none of
    them, so a leak test written with a `trinity_mcp_`-shaped secret exercises
    only the case that already worked. Here the secret is drawn from the shape
    the store actually accepts and is planted at every position in the response,
    including the seams between chunks.
    """
    text = credential.join(chunks)
    assume(len(text) <= 2 * _CAP)
    out, _ = a2a_client.sanitize_outbound_text(text, credential)
    assert credential not in (out or "")


@given(st.text(min_size=1, max_size=200))
def test_P4_an_absent_credential_still_bounds_and_never_crashes(text):
    """`credential` is Optional and is genuinely None for an endpoint registered
    without one — the `or ""` fallback must not become a crash or a no-op that
    skips the platform-pattern pass."""
    out, truncated = a2a_client.sanitize_outbound_text(text, None)
    assert isinstance(out, str)
    assert len(out) <= _CAP + len(_MARKER)


# =========================================================================== #
# P5 — _pinned_url structure
# =========================================================================== #
_SAFE_SEG = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=12,
)


@given(
    host=_SAFE_SEG,
    path=st.lists(_SAFE_SEG, max_size=3),
    query=st.text(alphabet="abc=&123", max_size=12),
    fragment=st.text(alphabet="abc123", max_size=8),
    port=st.one_of(st.none(), st.integers(min_value=1, max_value=65535)),
    address=st.sampled_from([PUBLIC_V4, "1.1.1.1", "2606:4700::1111", "::1"]),
)
def test_P5_pinning_swaps_the_host_and_preserves_the_request(host, path, query, fragment, port, address):
    """The pin must change exactly one thing. A helper that also dropped the
    query would silently send a different request to the peer; one that kept the
    fragment would send bytes no server should ever see."""
    authority = f"{host}.example" + (f":{port}" if port else "")
    url = f"https://{authority}/" + "/".join(path)
    if query:
        url += f"?{query}"
    if fragment:
        url += f"#{fragment}"

    pinned = a2a_client._pinned_url(url, address)
    p = urlsplit(pinned)

    assert p.scheme == "https"
    assert p.fragment == ""                       # never sent on the wire
    assert p.query == urlsplit(url).query         # preserved verbatim
    assert p.path == (urlsplit(url).path or "/")  # empty path normalised to "/"
    assert "@" not in p.netloc                    # no userinfo can be introduced
    assert p.hostname == address.lower()          # the validated address, bracketed if v6
    assert p.port == port
    if ":" in address:
        assert p.netloc.startswith("[")           # IPv6 authority must be bracketed


@given(
    # min_size=6 deliberately: a 1-2 character userinfo occurs by chance inside
    # `https://93.184.216.34/a2a`, so a shorter draw would fail the substring
    # assertion without any credential having survived. The bound is about the
    # assertion being meaningful, not about keeping the test green — the netloc
    # assertion below is exact and holds at every length.
    userinfo=st.text(
        alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A), min_size=6, max_size=20
    ),
    address=st.sampled_from([PUBLIC_V4, "2606:4700::1111"]),
)
def test_P5_userinfo_in_the_source_url_never_reaches_the_pinned_one(userinfo, address):
    """`resolve_rpc_target` refuses a card URL embedding credentials by name
    rather than relying on this — but the reason it can is that this holds."""
    pinned = a2a_client._pinned_url(f"https://{userinfo}:s3cret@peer.example.com/a2a", address)
    netloc = urlsplit(pinned).netloc
    # The exact claim: the rebuilt authority is the address (plus port), nothing else.
    assert "@" not in netloc
    assert userinfo not in netloc
    assert "s3cret" not in pinned
    assert "peer.example.com" not in pinned


# =========================================================================== #
# P6 — default-port equivalence, the property the card comparison rests on
# =========================================================================== #
@given(port=st.one_of(st.none(), st.integers(min_value=1, max_value=65535)))
@example(port=None)
@example(port=443)
def test_P6_making_the_default_port_explicit_never_changes_the_origin(port):
    """Trinity's own card emits no explicit port, so if `https://h` and
    `https://h:443` compared unequal, Trinity would be unreachable by its own
    rule and #738 federation would be dead on arrival.

    Port 0 is excluded from the strategy, and that exclusion is a FINDING, not a
    bound chosen to keep the test green: `_validate_public_https_url` coalesces
    `:0` to 443 while `_same_origin` compares it as literal 0, so the property
    genuinely fails there. It is pinned as a strict-xfail in
    `test_736_a2a_outbound_edges.py::test_D7_...` rather than swept under this
    strategy.
    """
    explicit = 443 if port is None else port
    a = f"https://peer.example.com{'' if port is None else f':{port}'}/a2a"
    b = f"https://peer.example.com:{explicit}/a2a"
    assert a2a_client._same_origin(a, b) is True
    assert a2a_client._same_origin(a, a) is True   # reflexive


@given(st.integers(min_value=1, max_value=65535), st.integers(min_value=1, max_value=65535))
def test_P6_a_different_port_is_a_different_origin(left, right):
    assume(left != right)
    assume({left, right} != {443})  # 443 vs implicit-default is P6's other half
    assert a2a_client._same_origin(
        f"https://peer.example.com:{left}/a2a", f"https://peer.example.com:{right}/a2a"
    ) is False


# =========================================================================== #
# P7 — canonical_host is idempotent
# =========================================================================== #
@given(st.text(max_size=60))
@example("EXAMPLE.com.")
@example("ⓔxample.com")
@example("")
def test_P7_canonical_host_is_idempotent(host):
    """The whole point of the helper is that ONE form is what gets validated,
    resolved and connected to (`_same_origin` re-derives it independently). If
    canonicalising twice differed from once, the comparison and the connection
    could disagree about which host was approved."""
    once = canonical_host(host)
    if once is None:
        return
    assert canonical_host(once) == once


# =========================================================================== #
# P8 — the RPC error discriminator is total and never invents a success
# =========================================================================== #
@given(st.dictionaries(st.text(max_size=12), JSON, max_size=5))
@example({"error": {"code": -32001, "message": "boom"}})
@example({"result": {"status": {"state": "completed"}}})
@example({})
def test_P8_rpc_discrimination_is_total(body):
    """`_raise_for_rpc_error` is the file's own "single most important line for
    correctness": an A2A error rides a **200**, so a client that returns
    whatever it finds hands the agent an error object as if it were an answer.

    The invariant is therefore that there is no third outcome — it either raises
    `A2ACallError`, or returns something that is not None. Returning None would
    become `_parse_task(None)` → a fabricated `unknown` success.
    """
    try:
        result = a2a_client._raise_for_rpc_error(body, "SEKRETVALUE123")
    except A2ACallError as exc:
        assert exc.reason in {"remote_error", "rpc_invalid"}
        assert isinstance(exc.detail, str) and exc.detail
        return
    assert result is not None


@given(JSON, CREDENTIAL)
def test_P8_a_remote_error_message_never_echoes_the_credential(message, credential):
    """The remote's error text is interpolated into an exception the calling LLM
    reads through the 502 body, so it takes the same redaction as the response
    body — the peer choosing to echo the credential back inside an error is the
    obvious way around a scrubber applied only to results."""
    body = {"error": {"code": -1, "message": f"failed: {credential} {message}"}}
    with pytest.raises(A2ACallError) as exc:
        a2a_client._raise_for_rpc_error(body, credential)
    assert exc.value.reason == "remote_error"
    assert credential not in exc.value.detail


# =========================================================================== #
# P9 — one internal record poisons the whole answer (metamorphic)
# =========================================================================== #
@given(
    publics=st.lists(PUBLIC_ADDRESSES, min_size=0, max_size=3),
    internal=INTERNAL_ADDRESSES,
    position=st.integers(min_value=0, max_value=3),
)
def test_P9_adding_an_internal_record_always_refuses_the_url(publics, internal, position):
    """Metamorphic rather than example-based, because the interesting case is a
    MIXED answer: which record a resolver hands the socket is not our choice, so
    a host resolving to both is a DNS-level smuggle, not a host with one usable
    address. The property is monotone — no set of public records can rescue a
    URL once one internal record is present.
    """
    addresses = list(publics)
    addresses.insert(min(position, len(addresses)), internal)

    with resolving_to(addresses):
        with pytest.raises(A2AEndpointUrlError) as exc:
            validate_a2a_endpoint_url(PEER_URL)
    assert exc.value.reason == "endpoint_private_address"
    # The refusal is operator-visible: naming the address it found would turn it
    # into a topology oracle.
    for addr in addresses:
        assert addr not in str(exc.value)


@given(publics=st.lists(PUBLIC_ADDRESSES, min_size=1, max_size=3))
def test_P9_an_all_public_answer_is_accepted_and_every_address_is_returned(publics):
    """The other half — without this, P9 would pass against a validator that
    refused everything. The addresses are not decoration: `a2a_client` pins the
    connection to one of THEM, which is what closes the rebinding window."""
    with resolving_to(publics):
        validated = validate_a2a_endpoint_url(PEER_URL)
    assert len(validated.addresses) == len(publics)
    for addr in validated.addresses:
        assert not uv._is_internal_address(ipaddress.ip_address(addr))
