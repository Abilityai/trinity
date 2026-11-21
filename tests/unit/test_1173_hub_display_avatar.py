"""Tests for GitHub template bundled avatar + portal display label (ent#1173).

AC-2: GitHub templates may ship avatar.webp/avatar.png, fetched via
_fetch_github_file_bytes and installed via _install_bundled_avatar_bytes.
Tests here call the real functions from services.agent_service.crud.

AC-4 (display_label on portal surfaces) is covered by the companion
frontend specs in src/frontend/tests/unit/:
  - portalBriefingDisplayLabel.spec.js
  - portalWorkCardDisplayLabel.spec.js
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Backend harness: load crud with heavy deps mocked.
# Shape follows test_1484_create_agent_characterization.py::_load_crud —
# deliberately NOT shared (learnings 2026-07-26: sys.modules leaks).
# ---------------------------------------------------------------------------

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_1173.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


class _NotFound(Exception):
    pass


def _purge_services(monkeypatch, keep):
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in keep:
            monkeypatch.delitem(sys.modules, key, raising=False)


def _load_crud(monkeypatch):
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    lifecycle_mod = MagicMock()
    lifecycle_mod.RESTRICTED_CAPABILITIES = []
    lifecycle_mod.FULL_CAPABILITIES = []

    capabilities_mod = MagicMock()
    capabilities_mod.AGENT_TMPFS_MOUNT = {}
    capabilities_mod.AGENT_DEFAULT_TMPDIR = "/home/developer/.tmp"
    capabilities_mod.AGENT_RESTART_POLICY = {"Name": "unless-stopped"}
    capabilities_mod.AGENT_LOG_CONFIG = {}
    capabilities_mod.normalize_cpu = MagicMock(side_effect=lambda v, d: v or d)
    capabilities_mod.normalize_memory = MagicMock(side_effect=lambda v, d: v or d)

    helpers_mod = MagicMock()
    helpers_mod.validate_base_image = MagicMock()
    helpers_mod.is_claude_runtime = MagicMock(return_value=False)
    helpers_mod.validate_runtime = MagicMock()

    settings_service = MagicMock()
    settings_service.resolve_github_pat = MagicMock(return_value=("pat", "global"))
    settings_service.resolve_github_pat_candidates = MagicMock(return_value=[])
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant")
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"})
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)
    settings_service.get_ephemeral_agent_quota = MagicMock(return_value=5)
    settings_service.get_ephemeral_ttl_ceiling_seconds = MagicMock(return_value=86400)

    pkg = "services.agent_service"
    sibling_mocks = {}
    for sib in [
        "api_key", "autonomy", "dashboard", "deploy", "file_sharing",
        "files", "folders", "helpers", "lifecycle", "mcp_tool_names",
        "metrics", "permissions", "queue", "read_only", "stats",
        "terminal", "capabilities", "ephemeral", "pull_mode",
    ]:
        mod = {
            "helpers": helpers_mod,
            "lifecycle": lifecycle_mod,
            "capabilities": capabilities_mod,
        }.get(sib, MagicMock())
        sibling_mocks[f"{pkg}.{sib}"] = mod

    agent_ssh_binding = MagicMock()
    agent_ssh_binding.AgentSshBindAddressError = type(
        "AgentSshBindAddressError", (Exception,), {})
    agent_ssh_binding.agent_ssh_port_binding = MagicMock(return_value=(2222, "127.0.0.1"))
    agent_ssh_binding.raise_for_ssh_bind_failure = MagicMock()

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": MagicMock(),
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": MagicMock(),
        "services.git_service": MagicMock(),
        "services.settings_service": settings_service,
        "services.github_service": MagicMock(),
        "services.entitlement_service": MagicMock(),
        "services.rate_limiter": MagicMock(),
        "services.agent_runtime_state": MagicMock(),
        "services.agent_auth": MagicMock(),
        "services.template_source_policy": MagicMock(),
        "services.agent_ssh_binding": agent_ssh_binding,
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_services(monkeypatch, mocks)
    import services.agent_service.crud as crud_mod
    return crud_mod, patcher


# ---------------------------------------------------------------------------
# Tests: _TemplateResolution.bundled_avatar_bytes field (AC-2 data carrier)
# ---------------------------------------------------------------------------

class TestTemplateResolutionBundledAvatarField:
    """The dataclass field that carries fetched bytes into create_agent_internal."""

    def test_field_exists_and_defaults_to_none(self, monkeypatch):
        """_TemplateResolution.bundled_avatar_bytes defaults to None (not fetched)."""
        crud, patcher = _load_crud(monkeypatch)
        try:
            tr = crud._TemplateResolution()
            assert tr.bundled_avatar_bytes is None
        finally:
            patcher.stop()

    def test_field_accepts_bytes(self, monkeypatch):
        """_TemplateResolution.bundled_avatar_bytes can hold bytes payload."""
        crud, patcher = _load_crud(monkeypatch)
        try:
            tr = crud._TemplateResolution()
            tr.bundled_avatar_bytes = b"\x89PNG\r\n"
            assert tr.bundled_avatar_bytes == b"\x89PNG\r\n"
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Tests: _fetch_github_file_bytes (AC-2 network fetch)
# ---------------------------------------------------------------------------

class TestFetchGithubFileBytes:
    """_fetch_github_file_bytes: best-effort GitHub contents API fetch."""

    def _load(self, monkeypatch):
        crud, patcher = _load_crud(monkeypatch)
        return crud._fetch_github_file_bytes, patcher

    def test_returns_bytes_on_200_with_valid_content(self, monkeypatch):
        """On HTTP 200 with base64-encoded content, returns the decoded bytes."""
        raw = b"png_data_here"
        encoded = base64.b64encode(raw).decode() + "\n"
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"content": encoded}

        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = fake_resp
                mock_client_cls.return_value = mock_client

                result = fn("owner/repo", None, "main", "avatar.png", 2 * 1024 * 1024)

            assert result == raw
        finally:
            patcher.stop()

    def test_returns_none_on_404(self, monkeypatch):
        """Non-200 response (like 404) → returns None, never raises."""
        fake_resp = MagicMock()
        fake_resp.status_code = 404

        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = fake_resp
                mock_client_cls.return_value = mock_client

                result = fn("owner/repo", "pat-token", "main", "avatar.webp", 2 * 1024 * 1024)

            assert result is None
        finally:
            patcher.stop()

    def test_returns_none_when_content_exceeds_max_bytes(self, monkeypatch):
        """Content that decodes to more than max_bytes → returns None."""
        big_raw = b"x" * 100
        encoded = base64.b64encode(big_raw).decode()
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"content": encoded}

        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = fake_resp
                mock_client_cls.return_value = mock_client

                result = fn("owner/repo", None, None, "avatar.png", 50)  # max=50, content=100

            assert result is None
        finally:
            patcher.stop()

    def test_returns_none_on_network_exception(self, monkeypatch):
        """Any exception during the request → returns None, never propagates."""
        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.side_effect = ConnectionError("network unreachable")
                mock_client_cls.return_value = mock_client

                result = fn("owner/repo", None, "main", "avatar.png", 2 * 1024 * 1024)

            assert result is None
        finally:
            patcher.stop()

    def test_includes_pat_in_authorization_header(self, monkeypatch):
        """When pat is provided, Authorization header is set."""
        fake_resp = MagicMock()
        fake_resp.status_code = 404

        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = fake_resp
                mock_client_cls.return_value = mock_client

                fn("owner/repo", "ghp_secret", "main", "avatar.webp", 2 * 1024 * 1024)

                call_kwargs = mock_client.get.call_args
                headers = call_kwargs[1]["headers"]
                assert "Authorization" in headers
                assert "ghp_secret" in headers["Authorization"]
        finally:
            patcher.stop()

    def test_omits_authorization_header_when_no_pat(self, monkeypatch):
        """When pat is None/empty, Authorization header is not set."""
        fake_resp = MagicMock()
        fake_resp.status_code = 404

        fn, patcher = self._load(monkeypatch)
        try:
            with patch("httpx.Client") as mock_client_cls:
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = fake_resp
                mock_client_cls.return_value = mock_client

                fn("owner/repo", None, "main", "avatar.png", 2 * 1024 * 1024)

                call_kwargs = mock_client.get.call_args
                headers = call_kwargs[1]["headers"]
                assert "Authorization" not in headers
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Tests: _install_bundled_avatar_bytes (AC-2 installation)
# ---------------------------------------------------------------------------

class TestInstallBundledAvatarBytes:
    """_install_bundled_avatar_bytes: write optimized avatar to the avatar store."""

    def _load(self, monkeypatch):
        crud, patcher = _load_crud(monkeypatch)
        return crud._install_bundled_avatar_bytes, patcher

    def test_writes_optimized_bytes_to_avatar_path(self, monkeypatch, tmp_path):
        """When avatar_path_or_none returns a path, optimized bytes are written there."""
        dest = tmp_path / "agent.webp"
        optimized = b"optimized_webp_data"

        fake_img = MagicMock()
        fake_img.__enter__ = MagicMock(return_value=fake_img)
        fake_img.__exit__ = MagicMock(return_value=False)
        fake_img.width = 100
        fake_img.height = 100

        crud, patcher = _load_crud(monkeypatch)
        fn = crud._install_bundled_avatar_bytes
        avatar_paths_mod = crud.avatar_paths  # the exact module object the function closes over
        try:
            with patch.object(avatar_paths_mod, "avatar_path_or_none", return_value=dest), \
                 patch.object(avatar_paths_mod, "ensure_avatar_store"), \
                 patch("PIL.Image.open", return_value=fake_img), \
                 patch("utils.image_optimize.optimize_avatar", return_value=optimized):
                fn("test-agent", b"raw_png_bytes")

            assert dest.read_bytes() == optimized
        finally:
            patcher.stop()

    def test_returns_early_when_no_valid_avatar_path(self, monkeypatch, tmp_path):
        """When avatar_path_or_none returns None (bad agent name), nothing is written."""
        crud, patcher = _load_crud(monkeypatch)
        fn = crud._install_bundled_avatar_bytes
        avatar_paths_mod = crud.avatar_paths
        try:
            with patch.object(avatar_paths_mod, "avatar_path_or_none", return_value=None), \
                 patch.object(avatar_paths_mod, "ensure_avatar_store") as mock_ensure:
                fn("../traversal-agent", b"some_bytes")

            mock_ensure.assert_not_called()
        finally:
            patcher.stop()

    def test_raises_for_oversized_image_dimensions(self, monkeypatch):
        """Image wider/taller than 4096×4096 total pixels raises ValueError."""
        fake_img = MagicMock()
        fake_img.__enter__ = MagicMock(return_value=fake_img)
        fake_img.__exit__ = MagicMock(return_value=False)
        fake_img.width = 5000
        fake_img.height = 5000  # 5000 * 5000 = 25M > 4096 * 4096 = ~16.7M

        crud, patcher = _load_crud(monkeypatch)
        fn = crud._install_bundled_avatar_bytes
        avatar_paths_mod = crud.avatar_paths
        try:
            dest = Path(tempfile.gettempdir()) / "agent.webp"
            with patch.object(avatar_paths_mod, "avatar_path_or_none", return_value=dest), \
                 patch("PIL.Image.open", return_value=fake_img):
                with pytest.raises(ValueError, match="4096x4096"):
                    fn("test-agent", b"big_image_bytes")
        finally:
            patcher.stop()


# ---------------------------------------------------------------------------
# Tests: _install_bundled_avatar_bytes through the real Pillow decoder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source_format", ["PNG", "WEBP"])
def test_install_decodes_a_real_bundled_image_into_a_webp_avatar(
        source_format: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The two formats a template may bundle (`avatar.png`, `avatar.webp`) are
    decoded, resized and re-encoded by the Pillow the backend image pins, with
    nothing mocked between the raw bytes and the file on disk. The tests above
    stub `Image.open` and `optimize_avatar`, so a Pillow upgrade that broke the
    header read, `thumbnail` or the WebP encoder would leave them green."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1024, 768), (40, 90, 160)).save(buf, format=source_format)
    dest = tmp_path / "agent.webp"

    crud, patcher = _load_crud(monkeypatch)
    avatar_paths_mod = crud.avatar_paths
    try:
        with patch.object(avatar_paths_mod, "avatar_path_or_none", return_value=dest), \
             patch.object(avatar_paths_mod, "ensure_avatar_store"):
            crud._install_bundled_avatar_bytes("test-agent", buf.getvalue())
    finally:
        patcher.stop()

    with Image.open(dest) as installed:
        assert installed.format == "WEBP"
        assert installed.size == (512, 384)
        pixel = installed.convert("RGB").getpixel((256, 192))
    assert isinstance(pixel, tuple)
    assert all(abs(got - want) <= 8 for got, want in zip(pixel, (40, 90, 160), strict=True))
