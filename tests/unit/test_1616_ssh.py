"""
Issue #1616 — SSH-access reliability: expired-key enforcement + safe inject/remove.

Two halves:

1. **Real-shell tests (NOT mocked).** The security-relevant behaviour lives in
   the shell layer: docker-py ``shlex.split``s a string ``cmd`` and the container
   then runs it through ``sh -c`` (two unquoting layers). A pure ``Mock`` on
   ``container_exec_run`` records the call but never executes the script, so it
   cannot prove idempotency, injection-safety, or exact-line removal. These tests
   capture the EXACT ``(cmd, environment)`` the service generates and run it
   through a real ``sh`` against a temp HOME — only the static ``/home/developer``
   base is rebased to the temp dir; every quoting / env / grep / awk token is
   verbatim. (Engineering finding; learnings.md #1664.)

2. **Cleanup-sweep tests (mocked).** The expired-credential sweep is the security
   fix: an expired ephemeral key must be removed from the ``authorized_keys`` file
   ``sshd`` reads, not just from Redis. Verifies near-expiry rows trigger removal,
   live rows do not, and the path is fail-open.

Module under test: src/backend/services/ssh_service.py
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# The container's real home path, baked into the generated scripts.
_CONTAINER_HOME = "/home/developer"


def _load_ssh_service(container_exec_run):
    """Load ssh_service with a caller-supplied ``container_exec_run`` and mocked
    redis/docker, mirroring the loader in test_ssh_service.py."""
    mock_redis = Mock()
    mock_redis.from_url = Mock(return_value=Mock())
    mock_get_container = Mock(return_value=Mock())

    with patch.dict(
        "sys.modules",
        {
            "redis": mock_redis,
            "services.docker_service": Mock(get_agent_container=mock_get_container),
            "services.docker_utils": Mock(container_exec_run=container_exec_run),
        },
    ):
        # No sys.modules mutation needed: the module is built directly from its
        # file via spec_from_file_location (not `import`), so a cached
        # services.ssh_service entry is irrelevant and left untouched.
        spec = importlib.util.spec_from_file_location(
            "ssh_service", f"{_BACKEND}/services/ssh_service.py"
        )
        mod = importlib.util.module_from_spec(spec)
        mod.redis = mock_redis
        mod.get_agent_container = mock_get_container
        mod.container_exec_run = container_exec_run
        spec.loader.exec_module(mod)
        return mod, mock_get_container


class _RealShellRunner:
    """Async ``container_exec_run`` replacement that runs the generated script
    through a real ``sh`` against ``home``.

    Only the static ``/home/developer`` base is rebased to the temp HOME — the
    part that carries no caller data. The env-var handling, quoting, ``grep`` and
    ``awk`` are executed exactly as the service emits them, so this exercises the
    same shell layers a live container would.
    """

    def __init__(self, home: Path):
        self.home = home
        self.calls = []

    @property
    def authorized_keys(self) -> Path:
        return self.home / ".ssh" / "authorized_keys"

    async def __call__(self, container, cmd, user=None, workdir=None, environment=None):
        self.calls.append({"cmd": cmd, "environment": environment})
        assert isinstance(cmd, list), "cmd must be a list (bypasses shlex.split)"
        run_cmd = [c.replace(_CONTAINER_HOME, str(self.home)) for c in cmd]
        proc = subprocess.run(
            run_cmd,
            env={**os.environ, **(environment or {})},
            capture_output=True,
            text=True,
        )
        return Mock(exit_code=proc.returncode, output=(proc.stdout + proc.stderr).encode())


def _lines(runner: _RealShellRunner):
    return runner.authorized_keys.read_text().splitlines() if runner.authorized_keys.exists() else []


def _perms(path: Path) -> int:
    return path.stat().st_mode & 0o777


# ---------------------------------------------------------------------------
# 1. Real-shell inject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_appends_and_is_idempotent(tmp_path):
    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()

    key = "ssh-ed25519 AAAAkey trinity-ephemeral-agent-111"

    assert await svc.inject_ssh_key("agent", key) is True
    assert _lines(runner) == [key]
    # authorized_keys must be 600 for sshd StrictModes.
    assert _perms(runner.authorized_keys) == 0o600
    # .ssh dir must be 700.
    assert _perms(runner.home / ".ssh") == 0o700

    # Repeat inject of the SAME key does not duplicate the line (append-if-absent).
    assert await svc.inject_ssh_key("agent", key) is True
    assert _lines(runner) == [key]

    # A different key is appended alongside.
    key2 = "ssh-ed25519 AAAAkey2 trinity-ephemeral-agent-222"
    assert await svc.inject_ssh_key("agent", key2) is True
    assert _lines(runner) == [key, key2]


@pytest.mark.asyncio
async def test_inject_skip_if_present_false_always_appends(tmp_path):
    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()

    key = "ssh-ed25519 AAAAkey trinity-ephemeral-agent-333"
    await svc.inject_ssh_key("agent", key, skip_if_present=False)
    await svc.inject_ssh_key("agent", key, skip_if_present=False)
    # Without the guard the same key is appended twice.
    assert _lines(runner) == [key, key]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adversarial",
    [
        "ssh-ed25519 AAAAa $(touch {SENTINEL}) trinity-ephemeral-a-1",
        "ssh-ed25519 AAAAb `touch {SENTINEL}` trinity-ephemeral-a-2",
        "ssh-ed25519 AAAAc single'quote trinity-ephemeral-a-3",
        'ssh-ed25519 AAAAd double"quote trinity-ephemeral-a-4',
        "ssh-ed25519 AAAAe semi; touch {SENTINEL} trinity-ephemeral-a-5",
        "ssh-ed25519 AAAAf back\\slash trinity-ephemeral-a-6",
        "ssh-ed25519 AAAAg pipe | touch {SENTINEL} trinity-ephemeral-a-7",
        "ssh-ed25519 AAAAh amp && touch {SENTINEL} trinity-ephemeral-a-8",
        "ssh-ed25519 AAAAi dollar $HOME trinity-ephemeral-a-9",
    ],
)
async def test_inject_is_injection_proof(tmp_path, adversarial):
    """A key whose text embeds shell metacharacters is stored VERBATIM and
    executes nothing — the value never re-enters shell parsing (#1616)."""
    sentinel = tmp_path / "PWNED"
    key = adversarial.format(SENTINEL=sentinel)

    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()

    assert await svc.inject_ssh_key("agent", key) is True
    # No command substitution / shell execution happened.
    assert not sentinel.exists(), f"shell injection executed for: {key!r}"
    # The key is stored literally, exactly as supplied.
    assert _lines(runner) == [key]


# ---------------------------------------------------------------------------
# 1b. Real-shell remove — exact last-field, over-delete-proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_matches_exact_last_field_not_superset(tmp_path):
    """remove_ssh_key removes only the line whose LAST field equals the comment,
    never a neighbour whose comment merely contains it as a substring (#1616)."""
    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()

    exact = "ssh-ed25519 AAAA1 trinity-ephemeral-agent-100"
    superset = "ssh-ed25519 AAAA2 trinity-ephemeral-agent-1000"  # comment is a superstring
    other = "ssh-ed25519 AAAA3 trinity-ephemeral-agent-200"
    for k in (exact, superset, other):
        await svc.inject_ssh_key("agent", k)
    assert len(_lines(runner)) == 3

    assert await svc.remove_ssh_key("agent", "trinity-ephemeral-agent-100") is True
    remaining = _lines(runner)
    assert exact not in remaining
    assert superset in remaining  # NOT over-deleted
    assert other in remaining
    # File perms preserved after the temp-file swap.
    assert _perms(runner.authorized_keys) == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meta_comment",
    ["meta.[*a", "a/b\\c", "$(id)", "`id`", 'q"uote', "semi;colon"],
)
async def test_remove_metachar_comment_only_removes_its_own_line(tmp_path, meta_comment):
    """A comment containing sed/regex/shell metacharacters removes only its own
    line and executes nothing (#1616)."""
    sentinel = tmp_path / "PWNED"
    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()

    target = f"ssh-ed25519 AAAAtarget {meta_comment}"
    neighbour = "ssh-ed25519 AAAAneighbour trinity-ephemeral-keepme-1"
    await svc.inject_ssh_key("agent", target)
    await svc.inject_ssh_key("agent", neighbour)

    assert await svc.remove_ssh_key("agent", meta_comment) is True
    assert not sentinel.exists(), "metachar comment caused shell execution"
    remaining = _lines(runner)
    assert target not in remaining
    assert neighbour in remaining


@pytest.mark.asyncio
async def test_remove_missing_file_is_noop_success(tmp_path):
    runner = _RealShellRunner(tmp_path)
    service, _ = _load_ssh_service(runner)
    svc = service.SshService()
    # No authorized_keys yet.
    assert await svc.remove_ssh_key("agent", "trinity-ephemeral-x") is True
    assert runner.calls[-1]["environment"]["TRINITY_SSH_COMMENT"] == "trinity-ephemeral-x"


# ---------------------------------------------------------------------------
# 2. Expired-credential cleanup sweep (mocked — the security fix)
# ---------------------------------------------------------------------------


def _load_with_redis(redis_client):
    """Load ssh_service with a mocked redis client and a no-op exec."""
    mod, _ = _load_ssh_service(AsyncMock())
    svc = mod.SshService()
    svc.redis_client = redis_client
    return mod, svc


@pytest.mark.asyncio
async def test_cleanup_removes_near_expiry_key_from_file(tmp_path):
    redis_client = Mock()
    redis_client.scan_iter.return_value = ["ssh_access:agentA:trinity-ephemeral-agentA-1"]
    redis_client.ttl.return_value = 30  # within the 0..60 near-expiry window
    redis_client.get.return_value = json.dumps(
        {
            "agent_name": "agentA",
            "auth_type": "key",
            "credential_id": "trinity-ephemeral-agentA-1",
        }
    )

    _, svc = _load_with_redis(redis_client)
    svc.remove_ssh_key = AsyncMock(return_value=True)

    cleaned = await svc.cleanup_expired_credentials()

    assert cleaned == 1
    svc.remove_ssh_key.assert_awaited_once_with("agentA", "trinity-ephemeral-agentA-1")


@pytest.mark.asyncio
async def test_cleanup_leaves_live_key_untouched(tmp_path):
    redis_client = Mock()
    redis_client.scan_iter.return_value = ["ssh_access:agentA:trinity-ephemeral-agentA-1"]
    redis_client.ttl.return_value = 3600  # far from expiry
    redis_client.get.return_value = json.dumps(
        {"agent_name": "agentA", "auth_type": "key", "credential_id": "c1"}
    )

    _, svc = _load_with_redis(redis_client)
    svc.remove_ssh_key = AsyncMock(return_value=True)

    cleaned = await svc.cleanup_expired_credentials()

    assert cleaned == 0
    svc.remove_ssh_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_is_fail_open_on_redis_error():
    redis_client = Mock()
    redis_client.scan_iter.return_value = ["ssh_access:agentA:c1"]
    redis_client.ttl.return_value = 10
    redis_client.get.side_effect = RuntimeError("redis down")

    _, svc = _load_with_redis(redis_client)
    svc.remove_ssh_key = AsyncMock(return_value=True)

    # Must not raise; the bad key is skipped.
    cleaned = await svc.cleanup_expired_credentials()
    assert cleaned == 0
    svc.remove_ssh_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_alias_delegates():
    redis_client = Mock()
    redis_client.scan_iter.return_value = []
    _, svc = _load_with_redis(redis_client)
    assert await svc.cleanup_expired_keys() == 0


@pytest.mark.asyncio
async def test_cleanup_uses_scan_not_keys():
    """The sweep must iterate with SCAN, never the blocking KEYS — the backend
    Redis ACL user is `-@dangerous` and `KEYS` raises NoPermissionError, which
    would make the whole sweep fail-open to 0 every cycle (#1616, caught live)."""
    redis_client = Mock()
    redis_client.scan_iter.return_value = ["ssh_access:agentA:c1"]
    redis_client.ttl.return_value = 30
    redis_client.get.return_value = json.dumps(
        {"agent_name": "agentA", "auth_type": "key", "credential_id": "c1"}
    )
    # Make KEYS blow up the way the real ACL does, so a regression to it fails loudly.
    redis_client.keys.side_effect = AssertionError("KEYS is blocked for the backend ACL user")

    _, svc = _load_with_redis(redis_client)
    svc.remove_ssh_key = AsyncMock(return_value=True)

    cleaned = await svc.cleanup_expired_credentials()
    assert cleaned == 1
    redis_client.scan_iter.assert_called_once()
    redis_client.keys.assert_not_called()


# ---------------------------------------------------------------------------
# 3. cleanup_service wiring — the sweep is registered and fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_service_sweep_records_count(monkeypatch):
    import services.cleanup_service as cs

    fake_ssh = Mock()
    fake_ssh.cleanup_expired_credentials = AsyncMock(return_value=2)
    monkeypatch.setattr(
        "services.ssh_service.get_ssh_service", lambda: fake_ssh, raising=False
    )

    svc = cs.CleanupService()
    report = cs.CleanupReport()
    await svc._sweep_expired_ssh_credentials(report)

    assert report.ssh_credentials_expired == 2
    assert report.total >= 2
    assert report.to_dict()["ssh_credentials_expired"] == 2


@pytest.mark.asyncio
async def test_cleanup_service_sweep_is_fail_open(monkeypatch):
    import services.cleanup_service as cs

    fake_ssh = Mock()
    fake_ssh.cleanup_expired_credentials = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(
        "services.ssh_service.get_ssh_service", lambda: fake_ssh, raising=False
    )

    svc = cs.CleanupService()
    report = cs.CleanupReport()
    # Must not raise — the sweep owns its try/except.
    await svc._sweep_expired_ssh_credentials(report)
    assert report.ssh_credentials_expired == 0


def test_cleanup_service_registers_the_sweep():
    """The sweep is actually invoked by the cycle orchestrator (guards against a
    method that exists but is never called — the exact #1616 dead-code class)."""
    src = Path(_BACKEND, "services", "cleanup_service.py").read_text()
    assert "await self._sweep_expired_ssh_credentials(report)" in src


def test_ssh_service_never_uses_blocking_keys_command():
    """Durable static guard: ssh_service must NEVER call `redis_client.keys(` —
    the backend Redis ACL user is `-@dangerous`, so `KEYS` raises
    NoPermissionError at runtime (invisible to a mocked unit test that stubs
    `.keys`). Every keyspace scan must go through `scan_iter` (#1616)."""
    src = Path(_BACKEND, "services", "ssh_service.py").read_text()
    assert "redis_client.keys(" not in src, (
        "ssh_service uses the ACL-blocked KEYS command; use scan_iter (#1616)"
    )
    assert "scan_iter(" in src
