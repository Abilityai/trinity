"""Sign-in buckets and sign-in audit rows use the proxy-aware client address
(abilityai/trinity-enterprise#873).

The five sign-in handlers keyed their per-IP bucket and the `actor_ip` of
`login_failed` / `login_success` on `request.client.host` — the bundled proxy
for every browser user. They now read `utils/client_ip.py::client_ip`.

Two layers: the admin `/token` handler is DRIVEN (auth.py loaded with the
#591 split-test stubs) and its bucket key and audit `actor_ip` are asserted;
the AST guard pins that none of the five handlers reads the TCP peer itself.
"""
from __future__ import annotations

import ast
import asyncio
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

# Sibling import — the unit dir is not implicitly importable.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_login_rate_limit_split import FakeRedis, auth_module  # noqa: E402,F401 — fixture
from utils import client_ip as cip  # noqa: E402

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

NGINX = "172.28.0.5"
BROWSER = "5.6.7.8"

SIGN_IN_HANDLERS = {
    "routers/auth.py": ("login", "verify_email_login_code", "request_access"),
    "client_portal/router.py": ("portal_auth_request", "portal_auth_verify"),
}


@pytest.fixture(autouse=True)
def _resolver(monkeypatch):
    table = {"trinity-frontend": [NGINX]}

    def fake_getaddrinfo(host, *a, **k):
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in table[host]]

    for var in ("TRUSTED_PROXIES", "TRUSTED_PROXY_HOSTS", "TRUSTED_PROXY_HOPS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cip, "_getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(cip, "_start_refresher", lambda: None)
    cip._reset_cache_for_tests()
    cip._refresh_once()
    yield
    cip._reset_cache_for_tests()


def _request(peer, xff=None):
    raw = [(b"x-forwarded-for", xff.encode())] if xff else []
    return SimpleNamespace(
        client=SimpleNamespace(host=peer),
        headers=Headers(raw=raw),
        scope={"path": "/token"},
        state=SimpleNamespace(request_id="r1"),
    )


def _failed_admin_login(auth_module, monkeypatch, request):
    fr = FakeRedis()
    monkeypatch.setattr(auth_module, "_redis_client", fr)
    monkeypatch.setattr(auth_module, "get_redis_client", lambda: fr)
    monkeypatch.setattr(auth_module, "is_setup_completed", lambda: True)
    monkeypatch.setattr(auth_module, "authenticate_user", lambda u, p: None)
    audited = []

    async def log(**kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(auth_module, "platform_audit_service", SimpleNamespace(log=log))
    form = SimpleNamespace(username="admin", password="wrong")
    with pytest.raises(auth_module.HTTPException) as exc:
        asyncio.run(auth_module.login(request, form))
    assert exc.value.status_code == 401
    return fr, audited


class TestAdminLoginKeysOnTheClient:
    def test_behind_the_bundled_proxy(self, auth_module, monkeypatch):
        fr, audited = _failed_admin_login(auth_module, monkeypatch, _request(NGINX, xff=BROWSER))
        assert fr.get(f"login_attempts_ip:{BROWSER}") == "1"
        assert fr.get(f"login_attempts_ip:{NGINX}") is None
        assert [a["actor_ip"] for a in audited] == [BROWSER]

    def test_direct_caller_cannot_choose_its_bucket(self, auth_module, monkeypatch):
        fr, audited = _failed_admin_login(
            auth_module, monkeypatch, _request("203.0.113.9", xff="198.51.100.1"))
        assert fr.get("login_attempts_ip:203.0.113.9") == "1"
        assert fr.get("login_attempts_ip:198.51.100.1") is None
        assert [a["actor_ip"] for a in audited] == ["203.0.113.9"]


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"handler {name} not found")


def _reads_peer(fn) -> bool:
    for node in ast.walk(fn):
        if (isinstance(node, ast.Attribute) and node.attr == "host"
                and isinstance(node.value, ast.Attribute) and node.value.attr == "client"):
            return True
    return False


def _calls(fn, name) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
               for n in ast.walk(fn))


@pytest.mark.parametrize(
    "path,handler",
    [(p, h) for p, hs in SIGN_IN_HANDLERS.items() for h in hs],
)
def test_sign_in_handler_reads_the_resolver(path, handler):
    tree = ast.parse((_BACKEND / path).read_text())
    fn = _function(tree, handler)
    assert not _reads_peer(fn), f"{path}::{handler} reads request.client.host"
    assert _calls(fn, "resolve_client_ip"), f"{path}::{handler} does not call the resolver"
    imported = {
        (n.module, a.name, a.asname)
        for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
        for a in n.names
    }
    assert ("utils.client_ip", "client_ip", "resolve_client_ip") in imported
