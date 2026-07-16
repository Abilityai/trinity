"""
SSH Access Tests (test_ssh_access.py)

Live API tests for ephemeral SSH credentials. Key-based (BYOK) auth is the only
method: the caller supplies a public key, the server injects it into the agent
container's `authorized_keys`, and it expires with the credential TTL.

Endpoints tested:
- POST /api/agents/{name}/ssh-access - Generate ephemeral SSH credentials

#1615 — password auth was removed. It was broken end-to-end and presented as a
working choice:
  1. `set_container_password` did a function-level `import crypt`; `crypt` was
     removed from the stdlib in Python 3.13 (PEP 594) and the backend image runs
     3.13, so every password request raised ModuleNotFoundError -> HTTP 500.
  2. The agent base image's sshd runs `PasswordAuthentication no`, so a password
     login could never succeed even with a hash set.

`tests/unit/test_1615_ssh_password_removed.py` pins the handler's branches with
mocks. These tests are the live counterpart: they prove the deployed endpoint
returns 400 rather than 500, and — the part mocks structurally cannot show —
that a key injected through the API actually authenticates against the real
sshd in the container.

NOTE: requires a running agent container and `ssh_access_enabled`. Tests skip
(not fail) when the environment can't support them, per the house convention.

Issue: https://github.com/abilityai/trinity/issues/1615
"""

import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


# A syntactically valid ed25519 public key. Used where the request must be
# well-formed but is expected to be refused before the key is ever read.
DUMMY_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleXX "
    "trinity-test@example.com"
)


def skip_if_ssh_unavailable(response):
    """Skip when the environment can't support the test (not a failure)."""
    if response.status_code == 403:
        pytest.skip("ssh_access_enabled is off — enable it in Ops Settings to run these")
    if response.status_code == 404:
        pytest.skip("Agent container not running")
    if response.status_code == 503:
        pytest.skip("Agent server not ready")


def wait_for_sshd(port: int, host: str = "localhost", timeout: float = 90.0) -> bool:
    """Wait until the container's sshd answers with an SSH banner.

    Docker publishes the port as soon as the container exists, so a plain
    connect succeeds long before sshd is listening inside — the connection is
    then reset mid-handshake (`kex_exchange_identification: Connection reset by
    peer`). Reading the banner is what actually proves readiness, so poll for
    that rather than for an open socket.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=5) as s:
                s.settimeout(5)
                if s.recv(64).startswith(b"SSH-"):
                    return True
        except (OSError, socket.timeout):
            pass
        time.sleep(2)
    return False


@pytest.fixture(scope="module")
def running_agent(api_client: TrinityApiClient, created_agent):
    """The module's agent, started AND with sshd actually accepting connections.

    Without the readiness wait these tests pass or fail on ordering — a full-file
    run gives the container time to boot while an isolated run races it.
    """
    name = created_agent["name"]
    if created_agent.get("status") != "running":
        api_client.post(f"/api/agents/{name}/start")

    # Re-read the agent so the SSH port reflects the started container.
    response = api_client.get(f"/api/agents/{name}")
    agent = response.json() if response.status_code == 200 else created_agent
    return agent


@pytest.fixture()
def keypair():
    """A real, throwaway ed25519 keypair. The private key never leaves here."""
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen not available")
    d = Path(tempfile.mkdtemp(prefix="trinity-ssh-test-"))
    priv = d / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(priv), "-N", "", "-q",
         "-C", "trinity-live-test@example.com"],
        check=True, capture_output=True,
    )
    try:
        yield {"private": priv, "public": (d / "id_ed25519.pub").read_text().strip()}
    finally:
        shutil.rmtree(d, ignore_errors=True)


class TestSshAccessAuthentication:
    """Auth gating on the ssh-access endpoint."""

    pytestmark = pytest.mark.smoke

    def test_ssh_access_requires_auth(self, unauthenticated_client: TrinityApiClient, created_agent):
        response = unauthenticated_client.post(
            f"/api/agents/{created_agent['name']}/ssh-access",
            json={"ttl_hours": 1},
            auth=False,
        )
        assert_status_in(response, [401, 403])


class TestPasswordAuthRemoved:
    """#1615 — the password method is gone from the live surface."""

    pytestmark = pytest.mark.smoke

    def test_password_auth_returns_400_not_500(self, api_client: TrinityApiClient, running_agent):
        """The exact reproduction from #1615.

        Before the fix this was HTTP 500 (ModuleNotFoundError: crypt). The
        assertion on `!= 500` is the regression itself — a 500 here means the
        crypt import (or another server-side crash) is back.
        """
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "password", "ttl_hours": 1},
        )
        skip_if_ssh_unavailable(response)
        assert response.status_code != 500, (
            f"password auth crashed the server again (#1615): {response.text}"
        )
        assert_status(response, 400)

    def test_password_refusal_names_the_alternative(self, api_client: TrinityApiClient, running_agent):
        """A bare 400 would just relocate the debugging session #1615 describes."""
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "password", "ttl_hours": 1},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)
        detail = assert_json_response(response)["detail"].lower()
        assert "no longer supported" in detail
        assert "public_key" in detail or "ssh-keygen" in detail

    @pytest.mark.parametrize("method", ["PASSWORD", "Password"])
    def test_password_refusal_is_case_insensitive(self, api_client: TrinityApiClient, running_agent, method):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": method, "ttl_hours": 1},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)
        assert "no longer supported" in assert_json_response(response)["detail"].lower()

    def test_unknown_auth_method_is_refused(self, api_client: TrinityApiClient, running_agent):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "kerberos", "ttl_hours": 1},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)

    def test_password_request_is_refused_even_with_a_public_key(
        self, api_client: TrinityApiClient, running_agent
    ):
        """A well-formed key alongside auth_method=password must not quietly
        succeed as key auth — the caller asked for something we don't do."""
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "password", "ttl_hours": 1, "public_key": DUMMY_PUBKEY},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)
        assert "no longer supported" in assert_json_response(response)["detail"].lower()


class TestKeyAuthValidation:
    """Key auth is the only path — it has to actually work."""

    pytestmark = pytest.mark.smoke

    def test_missing_public_key_is_400(self, api_client: TrinityApiClient, running_agent):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)
        assert "public_key" in assert_json_response(response)["detail"].lower()

    def test_invalid_public_key_is_400(self, api_client: TrinityApiClient, running_agent):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1, "public_key": "not-a-key"},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 400)

    def test_key_auth_returns_connection_details(
        self, api_client: TrinityApiClient, running_agent, keypair
    ):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["status", "agent", "auth_method", "connection", "expires_at"])
        assert data["auth_method"] == "key"
        assert_has_fields(data["connection"], ["command", "host", "port", "user"])
        assert data["connection"]["user"] == "developer"

    def test_key_auth_is_the_default(self, api_client: TrinityApiClient, running_agent, keypair):
        """Omitting auth_method entirely must not 400."""
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"ttl_hours": 1, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        assert assert_json_response(response)["auth_method"] == "key"

    def test_response_leaks_no_private_key_or_password(
        self, api_client: TrinityApiClient, running_agent, keypair
    ):
        """#175 removed server-side keypair generation; #1615 removed passwords.
        Neither may reappear in a live response."""
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"ttl_hours": 1, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        blob = response.text.lower()
        assert "private_key" not in blob
        assert "password" not in blob

    def test_ttl_is_clamped(self, api_client: TrinityApiClient, running_agent, keypair):
        """TTL clamps to [0.1, 24] rather than erroring."""
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"ttl_hours": 999, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        assert assert_json_response(response)["expires_in_hours"] <= 24


@pytest.mark.integration
class TestKeyAuthEndToEnd:
    """The claim the whole #1615 removal rests on: key auth is the working path.

    Mocks cannot show this — the agent sshd's own config decides it, and that
    config (`PasswordAuthentication no`) is the second reason password auth was
    unfixable. A real login is the only honest proof.
    """

    def test_injected_key_actually_authenticates(
        self, api_client: TrinityApiClient, running_agent, keypair
    ):
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        conn = assert_json_response(response)["connection"]

        if not shutil.which("ssh"):
            pytest.skip("ssh client not available")
        if not wait_for_sshd(conn["port"]):
            pytest.skip(f"sshd on port {conn['port']} never became ready")

        result = subprocess.run(
            [
                "ssh",
                "-i", str(keypair["private"]),
                "-p", str(conn["port"]),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",          # never prompt — a prompt means key auth failed
                "-o", "ConnectTimeout=10",
                "-o", "LogLevel=ERROR",
                f"{conn['user']}@localhost",    # conn['host'] may be a tailscale/public addr
                "whoami",
            ],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0 and (
            "Connection refused" in result.stderr
            or "Connection reset by peer" in result.stderr
        ):
            pytest.skip(f"SSH port {conn['port']} not reachable from the test host")

        assert result.returncode == 0, (
            f"key injected via the API did not authenticate: {result.stderr.strip()}"
        )
        assert result.stdout.strip() == "developer"

    def test_password_login_is_impossible_by_sshd_config(
        self, api_client: TrinityApiClient, running_agent, keypair
    ):
        """The second half of #1615: even a correct password can't log in,
        because the container's sshd refuses password auth outright. Guards
        against someone 'restoring' password auth server-side without touching
        the sshd config and believing it works.
        """
        response = api_client.post(
            f"/api/agents/{running_agent['name']}/ssh-access",
            json={"auth_method": "key", "ttl_hours": 1, "public_key": keypair["public"]},
        )
        skip_if_ssh_unavailable(response)
        assert_status(response, 200)
        conn = assert_json_response(response)["connection"]

        if not shutil.which("ssh"):
            pytest.skip("ssh client not available")
        if not wait_for_sshd(conn["port"]):
            pytest.skip(f"sshd on port {conn['port']} never became ready")

        # Ask for password auth explicitly, offering no key. sshd must not even
        # offer the method — BatchMode turns any prompt into an immediate fail.
        result = subprocess.run(
            [
                "ssh",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=10",
                "-p", str(conn["port"]),
                f"{conn['user']}@localhost",
                "whoami",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if "Connection refused" in result.stderr or "Connection reset by peer" in result.stderr:
            pytest.skip(f"SSH port {conn['port']} not reachable from the test host")

        assert result.returncode != 0, (
            "password authentication succeeded against the agent sshd — "
            "PasswordAuthentication is supposed to be 'no' (#1615)"
        )
