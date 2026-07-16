"""#1615 — password SSH auth is removed from the endpoint surface.

The bug: `POST /api/agents/{name}/ssh-access` with `auth_method: "password"`
returned **HTTP 500**. `ssh_service.set_container_password` did a function-level
`import crypt`, and `crypt` was removed from the stdlib in Python 3.13 (PEP 594),
so the import raised before any work happened. Independently, the agent base
image's sshd runs `PasswordAuthentication no`, so a password login could never
succeed even with a hash set — the option was dead code presenting as a working
choice.

`tests/unit/test_ssh_service.py` guards the *service* layer (helpers deleted, no
`crypt` import). These tests guard the *router* — the surface that produced the
500 an operator actually saw.

The handler is a plain `async def`, so it's called directly here rather than
through a TestClient: the assertions are about its validation branches, and the
auth dependency is FastAPI's job, not this test's.

Issue: https://github.com/abilityai/trinity/issues/1615
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
if _backend not in sys.path:
    sys.path.insert(0, _backend)

pytestmark = pytest.mark.unit

PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexampleexampleexampleexampleexample test@example.com"


@pytest.fixture()
def ssh(monkeypatch):
    """The real handler, with Docker/Redis/settings stubbed out."""
    from fastapi import HTTPException  # noqa: F401  (re-exported for tests)
    from models import SshAccessRequest, User  # noqa: F401
    from routers import agent_ssh

    container = types.SimpleNamespace(
        status="running",
        attrs={"Config": {"Labels": {"trinity.ssh-port": "2222"}}},
    )
    monkeypatch.setattr(agent_ssh, "get_agent_container", lambda name: container)

    async def _reload(c):
        return None

    monkeypatch.setattr(agent_ssh, "container_reload", _reload)

    calls: dict = {}

    class _Svc:
        async def inject_ssh_key(self, agent_name, key):
            calls["injected"] = (agent_name, key)
            return True

        def store_key_metadata(self, *a, **kw):
            calls["stored"] = (a, kw)

        def store_credential_metadata(self, *a, **kw):
            calls["stored"] = (a, kw)

    # The handler imports these lazily *inside* the function body, so seeding
    # sys.modules is what reaches it.
    monkeypatch.setitem(sys.modules, "services.ssh_service", types.SimpleNamespace(
        get_ssh_service=lambda: _Svc(),
        get_ssh_host=lambda: "ssh.example.com",
        SSH_ACCESS_MAX_TTL_HOURS=24,
    ))
    monkeypatch.setitem(sys.modules, "services.settings_service", types.SimpleNamespace(
        get_ops_setting=lambda *a, **kw: True,
    ))

    admin = types.SimpleNamespace(id=1, username="admin", email="admin@example.com", role="admin")

    async def call(**body_kwargs):
        from models import SshAccessRequest
        return await agent_ssh.create_ssh_access(
            agent_name="bot",
            body=SshAccessRequest(**body_kwargs),
            current_user=admin,
        )

    return types.SimpleNamespace(call=call, calls=calls)


async def _status_of(ssh, **body):
    """Invoke the handler and return the HTTP status it produced."""
    from fastapi import HTTPException
    try:
        await ssh.call(**body)
        return 200
    except HTTPException as e:
        return e.status_code


@pytest.mark.asyncio
async def test_password_auth_is_refused_with_400_not_500(ssh):
    """The reported symptom was a 500 (ModuleNotFoundError: crypt). An explicit
    password request must now be a clean, intentional 400.

    Asserts the *reason*, not just the code: without the auth-method guard a
    password request falls through to the key path and 400s on the missing
    `public_key` — the right status for the wrong reason, which would let the
    guard be deleted with this test still green.
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ssh.call(auth_method="password", ttl_hours=1, public_key=PUBKEY)
    assert exc.value.status_code == 400
    assert exc.value.status_code != 500
    assert "no longer supported" in str(exc.value.detail).lower(), (
        "password was refused for an incidental reason, not the #1615 guard"
    )


@pytest.mark.asyncio
async def test_password_refusal_names_the_alternative(ssh):
    """A dead-end 400 would just relocate the debugging session. The message has
    to say what to do instead — key auth is the point of the removal."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ssh.call(auth_method="password")
    detail = str(exc.value.detail).lower()
    assert "no longer supported" in detail
    assert "public_key" in detail or "ssh-keygen" in detail


@pytest.mark.asyncio
async def test_password_request_never_touches_the_container(ssh):
    """Fail before any Docker work — the old path crashed mid-request."""
    await _status_of(ssh, auth_method="password")
    assert "injected" not in ssh.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PASSWORD", "Password", "pAsSwOrD"])
async def test_password_refusal_is_case_insensitive(ssh, method):
    """The router lowercases before comparing. A cased variant must hit the same
    explicit refusal, not fall through and fail confusingly on a missing key."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ssh.call(auth_method=method)
    assert exc.value.status_code == 400
    assert "no longer supported" in str(exc.value.detail).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["totp", "kerberos", "none"])
async def test_unknown_auth_methods_are_refused(ssh, method):
    """Only "key" is valid — an unknown method must not reach the key path."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await ssh.call(auth_method=method)
    assert exc.value.status_code == 400
    assert "no longer supported" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_key_auth_still_works(ssh):
    """Guard against 'fixed' by refusing everything."""
    result = await ssh.call(auth_method="key", ttl_hours=1, public_key=PUBKEY)
    assert result["auth_method"] == "key"
    assert "injected" in ssh.calls, "key was never injected into the container"


@pytest.mark.asyncio
async def test_key_auth_is_the_default(ssh):
    """A request that omits auth_method entirely gets key auth, not a 400."""
    result = await ssh.call(ttl_hours=1, public_key=PUBKEY)
    assert result["auth_method"] == "key"


@pytest.mark.asyncio
async def test_response_never_carries_a_private_key_or_password(ssh):
    """#175 removed server-side keypair generation; #1615 removed passwords.
    Neither field may reappear in the response."""
    result = await ssh.call(ttl_hours=1, public_key=PUBKEY)
    blob = str(result).lower()
    assert "private_key" not in blob
    assert "password" not in blob
