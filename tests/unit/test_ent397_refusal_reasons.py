"""Refusal reasons are set at the raise site, not reconstructed from prose (ent#397).

`validate_a2a_endpoint_url` derived its machine-readable `reason` by
substring-matching its own human message — the fragility `A2AEndpointUrlError`'s
docstring explicitly warns about. The DNS-failure message interpolates the
hostname and `canonical_host` passes hosts containing spaces, so a host spelled
`an internal address.example` was reported `endpoint_private_address` when it had
merely failed to resolve.

No bypass: both refusals map to the same HTTP status, so nothing is admitted that
should not be. What breaks is diagnosis — the operator is told the opposite of
what happened, and any consumer branching on `reason` branches wrongly.

These pin the structure, not just the symptom: every refusal carries its kind from
the point it is raised, the kinds map 1:1 onto the shipped reason codes, and the
message stays free to be reworded without moving a code. Plus the property the
messages already had and must keep — a refusal never echoes a resolved address,
which would turn it into a topology oracle.
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys

import pytest

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
sys.path.insert(0, _backend_path)

_spec = importlib.util.spec_from_file_location(
    "backend_url_validation_ent397",
    os.path.join(_backend_path, "utils", "url_validation.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_STUBBED_MODULE_NAMES = ["backend_url_validation_ent397"]
_PRE_STUB = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
sys.modules["backend_url_validation_ent397"] = _mod
_spec.loader.exec_module(_mod)

validate = _mod.validate_a2a_endpoint_url
A2AEndpointUrlError = _mod.A2AEndpointUrlError
PublicUrlRefusal = _mod.PublicUrlRefusal

PUBLIC_V4 = "93.184.216.34"
INTERNAL_V4 = "10.0.0.7"

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    try:
        yield
    finally:
        for name, value in _PRE_STUB.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


@pytest.fixture
def resolves(monkeypatch):
    def _set(*addresses):
        def _getaddrinfo(host, port, *a, **k):
            out = []
            for addr in addresses:
                fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
                sockaddr = (addr, 0, 0, 0) if ":" in addr else (addr, 0)
                out.append((fam, socket.SOCK_STREAM, 6, "", sockaddr))
            return out

        monkeypatch.setattr(_mod.socket, "getaddrinfo", _getaddrinfo)

    return _set


@pytest.fixture
def dns_fails(monkeypatch):
    def _boom(host, port, *a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(_mod.socket, "getaddrinfo", _boom)


# --------------------------------------------------------------------------- #
# The reported defect
# --------------------------------------------------------------------------- #
def test_D12_a_dns_failure_is_never_misreported_as_a_private_address(dns_fails):
    """The sweep's own case (pinned strict-xfail on the PR #2178 branch): the
    hostname is interpolated into the DNS message, so a host whose NAME contains
    the words the old matcher looked for stole another refusal's code."""
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://an internal address.example/a2a")
    assert exc.value.reason == "endpoint_dns_failure"


@pytest.mark.parametrize("host", [
    "an internal address.example",
    "resolves to an internal address.example",
    "could not be resolved.example",
    "must use HTTPS.example",
])
def test_a_hostile_hostname_cannot_steal_another_refusals_code(dns_fails, host):
    """Every phrase the old implementation matched on, spelled as a hostname."""
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(f"https://{host}/a2a")
    assert exc.value.reason == "endpoint_dns_failure", host


def test_a_real_private_address_still_reports_private_address(resolves):
    resolves(INTERNAL_V4)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert exc.value.reason == "endpoint_private_address"


# --------------------------------------------------------------------------- #
# Every refusal, by the path that raises it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("", "endpoint_invalid"),
    ("   ", "endpoint_invalid"),
    ("http://peer.example.com/a2a", "endpoint_not_https"),
    ("ftp://peer.example.com/a2a", "endpoint_not_https"),
    ("https://user:pw@peer.example.com/a2a", "endpoint_invalid"),
    ("https://peer.example.com:notaport/a2a", "endpoint_invalid"),
    ("https://peer.example.com:99999/a2a", "endpoint_invalid"),
    ("https:///a2a", "endpoint_invalid"),
])
def test_each_refusal_reports_its_own_reason(resolves, url, expected):
    resolves(PUBLIC_V4)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(url)
    assert exc.value.reason == expected, url


def test_the_length_cap_reports_invalid(resolves):
    resolves(PUBLIC_V4)
    too_long = "https://peer.example.com/" + "a" * _mod._REGISTRY_URL_MAX_LEN
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate(too_long)
    assert exc.value.reason == "endpoint_invalid"


def test_an_unparseable_resolver_record_reports_private_address(monkeypatch):
    """The "refuse rather than skip" branch: a record the stdlib cannot parse is
    not one we are going to vet. It keeps the code it has always carried."""
    monkeypatch.setattr(_mod.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not-an-ip", 0))])
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert exc.value.reason == "endpoint_private_address"


def test_an_empty_resolver_result_reports_dns_failure(monkeypatch):
    monkeypatch.setattr(_mod.socket, "getaddrinfo", lambda *a, **k: [])
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert exc.value.reason == "endpoint_dns_failure"


# --------------------------------------------------------------------------- #
# The structure, so the defect cannot come back a different way
# --------------------------------------------------------------------------- #
def test_the_shared_validator_raises_kinds_not_bare_value_errors():
    """A raise site added without a kind falls back to `endpoint_invalid`, which
    is honest but wrong for a real refusal — so pin that they all carry one."""
    import ast

    src = open(os.path.join(_backend_path, "utils", "url_validation.py")).read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_validate_public_https_url")
    bare = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
        and getattr(n.exc.func, "id", None) == "ValueError"
    ]
    assert bare == [], f"bare ValueError raised at lines {bare}; use PublicUrlRefusal(kind, …)"


def test_every_kind_is_mapped_to_a_shipped_reason():
    """No kind may fall through to the `endpoint_invalid` default by accident —
    a new one is a visible edit to the table."""
    for kind in _mod.PUBLIC_URL_REFUSAL_KINDS:
        assert kind in _mod._A2A_REASON_BY_KIND, kind
    for reason in _mod._A2A_REASON_BY_KIND.values():
        assert reason in _mod.A2A_URL_REASONS, reason


def test_a_refusal_without_a_kind_is_reported_as_invalid(monkeypatch):
    """The defensive arm: a `ValueError` from a path predating ent#397 must still
    become an `A2AEndpointUrlError`, not escape as a raw ValueError."""
    monkeypatch.setattr(_mod, "_validate_public_https_url",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("something old")))
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert exc.value.reason == "endpoint_invalid"
    assert "something old" in str(exc.value)


def test_the_kind_survives_for_the_template_registry_caller(resolves):
    """`PublicUrlRefusal` subclasses `ValueError`, so the sibling validator —
    which catches nothing and lets it propagate — is unchanged in type and text."""
    resolves(INTERNAL_V4)
    with pytest.raises(ValueError) as exc:
        _mod.validate_template_registry_url("https://registry.example.com/index.json")
    assert isinstance(exc.value, PublicUrlRefusal)
    assert exc.value.kind == "private_address"
    assert "Template registry URL resolves to an internal address" in str(exc.value)


# --------------------------------------------------------------------------- #
# The property the messages already had
# --------------------------------------------------------------------------- #
def test_refusal_message_never_echoes_the_resolved_address(resolves):
    """A refusal is shown to an operator; echoing the resolved IP back would turn
    it into a topology oracle. Unchanged by ent#397 — pinned because this fix
    edits every one of those messages' raise sites."""
    resolves(INTERNAL_V4)
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert INTERNAL_V4 not in str(exc.value)

    resolves("169.254.169.254")
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://peer.example.com/a2a")
    assert "169.254" not in str(exc.value)


def test_the_dns_message_still_names_the_host_the_operator_typed(dns_fails):
    """The hostname interpolation is the vector, not the defect — it stays,
    because an operator debugging a typo needs to see which name failed."""
    with pytest.raises(A2AEndpointUrlError) as exc:
        validate("https://typo.example.com/a2a")
    assert "typo.example.com" in str(exc.value)
