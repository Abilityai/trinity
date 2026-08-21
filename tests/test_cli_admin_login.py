"""
CLI Admin Login Tests (test_cli_admin_login.py)

Unit tests for the `trinity login --admin` command.
Tests password-based admin authentication via /api/token.

Also covers #2322: a login can succeed at the HTTP level and still issue no
session (a second factor is pending), and the CLI must refuse it at the login
call rather than storing `None` in the profile and 401ing on every later
command. These live HERE rather than in `tests/unit/` on purpose —
`tests/requirements-test.txt` deliberately does not install `trinity_cli`
(an `-e ./src/cli` line breaks GitHub's dependency-graph updater), and the
#660 contract is that a bare `pytest unit/` runs green after installing only
that file. This module is already grandfathered in
`lint_root_test_placement_baseline.txt`, which a NEW root file would not be.

FAST TESTS - No backend required (mocked HTTP).
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from click.testing import CliRunner

from trinity_cli.config import (
    load_config,
    set_auth,
    set_current_profile,
)
from trinity_cli.main import cli


pytestmark = pytest.mark.smoke


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect config to a temp directory."""
    config_dir = tmp_path / ".trinity"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    monkeypatch.setattr("trinity_cli.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("trinity_cli.config.CONFIG_FILE", config_file)
    monkeypatch.delenv("TRINITY_URL", raising=False)
    monkeypatch.delenv("TRINITY_API_KEY", raising=False)
    monkeypatch.delenv("TRINITY_PROFILE", raising=False)
    return config_file


def _mock_response(status_code=200, json_data=None):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = json.dumps(json_data or {})
    return resp


class TestAdminLoginFlag:
    """Tests for `trinity login --admin`."""

    def test_admin_login_success(self, tmp_config):
        """--admin flag prompts for password and authenticates via /api/token."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True, "setup_completed": True})
        token_resp = _mock_response(200, {"access_token": "jwt-admin-token", "token_type": "bearer"})
        user_resp = _mock_response(200, {"username": "admin", "role": "admin", "email": "admin@example.com"})
        mcp_key_resp = _mock_response(200, {"api_key": "trinity_mcp_testkey"})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            # Sequence: GET auth/mode, POST /api/token (form), GET /api/users/me, POST /api/mcp/keys/ensure-default
            mock_client.get.side_effect = [auth_mode_resp, user_resp]
            mock_client.post.side_effect = [token_resp, mcp_key_resp]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        assert result.exit_code == 0
        assert "Logged in as admin" in result.output

        # Verify profile was saved
        config = load_config()
        profile = config["profiles"].get("localhost", {})
        assert profile.get("token") == "jwt-admin-token"

    def test_admin_login_wrong_password(self, tmp_config):
        """Wrong password returns error and exits non-zero."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True, "setup_completed": True})
        # Use 400 instead of 401 — 401 triggers client.sys.exit before our handler
        error_resp = _mock_response(400, {"detail": "Incorrect username or password"})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp]
            mock_client.post.side_effect = [error_resp]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="wrongpassword\n")

        assert result.exit_code == 1
        assert "Admin login failed" in result.output

    def test_admin_login_sends_form_data(self, tmp_config):
        """Admin login POSTs form-encoded data to /api/token, not JSON."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True})
        token_resp = _mock_response(200, {"access_token": "jwt-token", "token_type": "bearer"})
        user_resp = _mock_response(200, {"username": "admin", "role": "admin"})
        mcp_key_resp = _mock_response(200, {})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp, user_resp]
            mock_client.post.side_effect = [token_resp, mcp_key_resp]

            runner = CliRunner()
            runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="mypassword\n")

            # Find the /api/token call — it should use data= (form), not json=
            token_call = mock_client.post.call_args_list[0]
            assert "/api/token" in token_call.args[0]
            assert "data" in token_call.kwargs or (len(token_call.args) > 1)
            # Verify it was form-encoded (data kwarg), not json
            call_kwargs = token_call.kwargs
            assert "json" not in call_kwargs

    def test_admin_login_stores_user_info(self, tmp_config):
        """Admin login fetches and stores user info in profile."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True})
        token_resp = _mock_response(200, {"access_token": "jwt-token", "token_type": "bearer"})
        user_resp = _mock_response(200, {"username": "admin", "role": "admin", "email": "admin@example.com"})
        mcp_key_resp = _mock_response(200, {})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp, user_resp]
            mock_client.post.side_effect = [token_resp, mcp_key_resp]

            runner = CliRunner()
            runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        config = load_config()
        user = config["profiles"]["localhost"].get("user", {})
        assert user.get("role") == "admin"
        assert user.get("username") == "admin"

    def test_admin_login_with_profile(self, tmp_config):
        """--admin respects --profile flag."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True})
        token_resp = _mock_response(200, {"access_token": "jwt-token", "token_type": "bearer"})
        user_resp = _mock_response(200, {"username": "admin", "role": "admin"})
        mcp_key_resp = _mock_response(200, {})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp, user_resp]
            mock_client.post.side_effect = [token_resp, mcp_key_resp]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "--profile", "my-server",
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        assert result.exit_code == 0
        assert "my-server" in result.output
        config = load_config()
        assert "my-server" in config["profiles"]

    def test_without_admin_flag_prompts_for_email(self, tmp_config):
        """Without --admin, login prompts for email (not password)."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp]
            # Will fail at email request — that's fine, we just check the prompt
            mock_client.post.side_effect = [_mock_response(400, {"detail": "test stop"})]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--instance", "http://localhost:8000",
            ], input="user@example.com\n")

        # Should have prompted for Email, not Admin password
        assert "Email" in result.output

    def test_admin_login_user_info_fetch_failure_graceful(self, tmp_config):
        """If /api/users/me fails, login still succeeds with fallback user info."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True})
        token_resp = _mock_response(200, {"access_token": "jwt-token", "token_type": "bearer"})
        user_error_resp = _mock_response(500, {"detail": "internal error"})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)

            mock_client.get.side_effect = [auth_mode_resp, user_error_resp]
            # token call succeeds, mcp key call may or may not happen
            mock_client.post.side_effect = [token_resp, _mock_response(200, {})]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        # Should still succeed — fallback user info used
        assert "Logged in as admin" in result.output
        config = load_config()
        user = config["profiles"]["localhost"].get("user", {})
        assert user.get("username") == "admin"


_CHALLENGE = {
    "mfa_required": True,
    "mfa_enrolled": True,
    "enrollment_required": False,
    "challenge_token": "eyJ-challenge",
}


def _no_token_written(config_file):
    """No profile written, or none that received a token."""
    if not config_file.exists():
        return True
    config = load_config()
    return all(not p.get("token") for p in config.get("profiles", {}).values())


class TestLoginWithoutASession:
    """#2322 — HTTP 200 is not proof a session was issued.

    When enterprise 2FA defers a login, /token answers 200 with a challenge and
    no `access_token`. Reading that field blind wrote `None` into the profile
    and printed "Logged in as ...", so the refusal surfaced much later as
    unexplained 401s with nothing pointing back at the login. Both login paths
    are covered because they read the token separately.
    """

    def test_admin_login_refuses_a_pending_second_factor(self, tmp_config):
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True, "setup_completed": True})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = [auth_mode_resp]
            mock_client.post.side_effect = [_mock_response(200, _CHALLENGE)]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        assert result.exit_code == 1, "a login that issued no session must not exit 0"
        assert "second factor" in result.output.lower(), result.output
        assert "Logged in as" not in result.output, "must not claim success"
        assert _no_token_written(tmp_config), "a session-less login must not write a token"

    def test_admin_login_refuses_a_missing_token(self, tmp_config):
        """Same guard without the 2FA flag — any session-less 200."""
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True, "setup_completed": True})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = [auth_mode_resp]
            mock_client.post.side_effect = [_mock_response(200, {"token_type": "bearer"})]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--admin", "--instance", "http://localhost:8000",
            ], input="password\n")

        assert result.exit_code == 1
        assert "no access token" in result.output.lower(), result.output
        assert _no_token_written(tmp_config)

    def test_email_login_refuses_a_pending_second_factor(self, tmp_config):
        auth_mode_resp = _mock_response(200, {"email_auth_enabled": True, "setup_completed": True})
        request_resp = _mock_response(200, {"message": "Code sent", "expires_in_seconds": 600})

        with patch("trinity_cli.client.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.get.side_effect = [auth_mode_resp]
            mock_client.post.side_effect = [request_resp, _mock_response(200, _CHALLENGE)]

            runner = CliRunner()
            result = runner.invoke(cli, [
                "login", "--instance", "http://localhost:8000",
            ], input="admin@example.com\n123456\n")

        assert result.exit_code == 1
        assert "second factor" in result.output.lower(), result.output
        assert _no_token_written(tmp_config)
