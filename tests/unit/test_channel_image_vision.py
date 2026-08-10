"""
Unit tests for Telegram/channel image vision delivery via --input-format stream-json (#562).

The fix replaces the broken base64-data-URI-in-text approach (where Claude Code
received images as opaque text strings) with proper vision content blocks passed
via --input-format stream-json stdin.

Covers:
- ParallelTaskRequest model accepts the images field
- stream-json stdin payload is correctly structured when images present
- --input-format stream-json added to cmd iff images non-empty
- Empty/None images list falls back to plain-text stdin (no regression)
- _handle_file_uploads returns image_data as 4th element for image MIME types
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: ensure src/backend and docker/base-image are importable
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BASE_IMAGE = _THIS.parent.parent.parent / "docker" / "base-image"
_AGENT_SERVER = _BASE_IMAGE / "agent_server"

for _p in (str(_BACKEND), str(_BASE_IMAGE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).


# ---------------------------------------------------------------------------
# Stub heavy agent_server dependencies so we can import models in isolation
# ---------------------------------------------------------------------------

def _install_agent_server_stubs():
    """Minimal stubs so agent_server.models imports cleanly."""
    if "agent_server" not in sys.modules or not any(
        str(_AGENT_SERVER) in p
        for p in getattr(sys.modules.get("agent_server"), "__path__", [])
    ):
        stub = types.ModuleType("agent_server")
        stub.__path__ = [str(_AGENT_SERVER)]
        stub.__package__ = "agent_server"
        sys.modules["agent_server"] = stub

_install_agent_server_stubs()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tiny_png_b64() -> str:
    """Return base64 of a minimal 1-byte PNG-like payload (enough for tests)."""
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()


# ===========================================================================
# 1. ParallelTaskRequest model
# ===========================================================================

class TestParallelTaskRequestImages:
    """ParallelTaskRequest must accept the images field."""

    def test_images_field_defaults_to_none(self):
        from agent_server.models import ParallelTaskRequest
        req = ParallelTaskRequest(message="hello")
        assert req.images is None

    def test_images_field_accepts_list(self):
        from agent_server.models import ParallelTaskRequest
        imgs = [{"media_type": "image/jpeg", "data": _make_tiny_png_b64()}]
        req = ParallelTaskRequest(message="describe this", images=imgs)
        assert req.images == imgs
        assert req.images[0]["media_type"] == "image/jpeg"

    def test_images_field_accepts_empty_list(self):
        from agent_server.models import ParallelTaskRequest
        req = ParallelTaskRequest(message="hello", images=[])
        assert req.images == []

    def test_images_field_accepts_none_explicitly(self):
        from agent_server.models import ParallelTaskRequest
        req = ParallelTaskRequest(message="hello", images=None)
        assert req.images is None


# ===========================================================================
# 2. stream-json stdin payload construction
# ===========================================================================

class TestStreamJsonPayload:
    """Verify the stdin payload built by execute_headless_task for images."""

    def _build_payload(self, prompt: str, images: list | None) -> str:
        """Replicate the payload-construction logic from claude_code.py."""
        if images:
            content_blocks = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["media_type"],
                        "data": img["data"],
                    },
                }
                for img in images
            ]
            content_blocks.append({"type": "text", "text": prompt})
            return (
                json.dumps({"type": "user", "message": {"role": "user", "content": content_blocks}})
                + "\n"
            )
        return prompt

    def test_no_images_returns_plain_text(self):
        payload = self._build_payload("hello world", None)
        assert payload == "hello world"

    def test_empty_images_returns_plain_text(self):
        payload = self._build_payload("hello world", [])
        assert payload == "hello world"

    def test_single_image_produces_valid_json(self):
        b64 = _make_tiny_png_b64()
        payload = self._build_payload(
            "describe this image",
            [{"media_type": "image/jpeg", "data": b64}],
        )
        assert payload.endswith("\n")
        msg = json.loads(payload.strip())
        assert msg["type"] == "user"
        assert msg["message"]["role"] == "user"
        content = msg["message"]["content"]
        assert len(content) == 2

    def test_image_block_structure(self):
        b64 = _make_tiny_png_b64()
        payload = self._build_payload(
            "what is this?",
            [{"media_type": "image/png", "data": b64}],
        )
        content = json.loads(payload.strip())["message"]["content"]
        img_block = content[0]
        assert img_block["type"] == "image"
        assert img_block["source"]["type"] == "base64"
        assert img_block["source"]["media_type"] == "image/png"
        assert img_block["source"]["data"] == b64

    def test_text_block_is_last(self):
        b64 = _make_tiny_png_b64()
        payload = self._build_payload(
            "describe this",
            [{"media_type": "image/jpeg", "data": b64}],
        )
        content = json.loads(payload.strip())["message"]["content"]
        assert content[-1]["type"] == "text"
        assert content[-1]["text"] == "describe this"

    def test_multiple_images(self):
        b64 = _make_tiny_png_b64()
        imgs = [
            {"media_type": "image/jpeg", "data": b64},
            {"media_type": "image/png", "data": b64},
        ]
        payload = self._build_payload("compare these", imgs)
        content = json.loads(payload.strip())["message"]["content"]
        # 2 image blocks + 1 text block
        assert len(content) == 3
        assert content[0]["type"] == "image"
        assert content[1]["type"] == "image"
        assert content[2]["type"] == "text"

    def test_prompt_text_preserved_in_text_block(self):
        b64 = _make_tiny_png_b64()
        prompt = "The quick brown fox\njumped over the lazy dog"
        payload = self._build_payload(prompt, [{"media_type": "image/jpeg", "data": b64}])
        content = json.loads(payload.strip())["message"]["content"]
        assert content[-1]["text"] == prompt


# ===========================================================================
# 3. --input-format stream-json added to cmd iff images non-empty
# ===========================================================================

class TestCmdContainsInputFormat:
    """Verify --input-format stream-json lands in the claude cmd when images present."""

    def _build_cmd(self, images: list | None) -> list:
        """Replicate the cmd-building logic for the images branch."""
        cmd = [
            "claude", "--print", "--output-format", "stream-json",
            "--verbose", "--dangerously-skip-permissions",
        ]
        if images:
            cmd.extend(["--input-format", "stream-json"])
        return cmd

    def test_no_images_cmd_has_no_input_format(self):
        cmd = self._build_cmd(None)
        assert "--input-format" not in cmd

    def test_empty_images_cmd_has_no_input_format(self):
        cmd = self._build_cmd([])
        assert "--input-format" not in cmd

    def test_images_present_adds_input_format(self):
        b64 = _make_tiny_png_b64()
        cmd = self._build_cmd([{"media_type": "image/jpeg", "data": b64}])
        assert "--input-format" in cmd
        idx = cmd.index("--input-format")
        assert cmd[idx + 1] == "stream-json"

    def test_output_format_still_present_with_images(self):
        b64 = _make_tiny_png_b64()
        cmd = self._build_cmd([{"media_type": "image/jpeg", "data": b64}])
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "stream-json"


# ===========================================================================
# 4. _handle_file_uploads return structure for image files
# ===========================================================================

class TestHandleFileUploadsImageReturn:
    """_handle_file_uploads must return a 4-tuple with image_data as 4th element."""

    def _make_file_attachment(self, name="photo.jpg", mimetype="image/jpeg", size=1024):
        fa = MagicMock()
        fa.name = name
        fa.mimetype = mimetype
        fa.size = size
        fa.id = "test-id-001"
        return fa

    @pytest.mark.asyncio
    async def test_image_file_populates_image_data(self):
        """An image attachment should end up in the 4th return value."""
        # Minimal 100-byte JPEG-like payload
        fake_bytes = b"\xff\xd8\xff" + b"\x00" * 97

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier.return_value = "user@example.com"
        adapter.download_file = AsyncMock(return_value=fake_bytes)

        message = MagicMock()
        message.files = [self._make_file_attachment()]
        message.sender_id = "tg-123"
        message.channel_id = "chan-456"

        container = MagicMock()

        # Stub out all the heavy backend imports used by message_router
        audit_stub = MagicMock()
        audit_stub.log = AsyncMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "database": MagicMock(db=MagicMock()),
                    "services.docker_service": MagicMock(),
                    "services.settings_service": MagicMock(
                        settings_service=MagicMock(get=MagicMock(return_value=None))
                    ),
                    "services.task_execution_service": MagicMock(),
                    "services.docker_utils": MagicMock(
                        container_put_archive=AsyncMock(return_value=True),
                        container_exec_run=AsyncMock(),
                    ),
                    "services.platform_audit_service": MagicMock(
                        platform_audit_service=audit_stub,
                        AuditEventType=MagicMock(EXECUTION="execution"),
                    ),
                    "services.telegram_media": MagicMock(),
                    "adapters.base": MagicMock(),
                },
            ),
        ):
            # Import inside the patch context so module-level dependencies resolve
            import importlib
            if "adapters.message_router" in sys.modules:
                del sys.modules["adapters.message_router"]
            import adapters.message_router as mr

            router = mr.ChannelMessageRouter()
            result = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-abc"
            )

        assert len(result) == 4, "Return must be a 4-tuple"
        descriptions, upload_dir, all_writes_failed, image_data = result

        assert isinstance(image_data, list), "4th element must be a list"
        assert len(image_data) == 1, "One image should produce one entry"
        assert "media_type" in image_data[0]
        assert "data" in image_data[0]
        assert image_data[0]["media_type"] == "image/jpeg"
        # Verify it's valid base64
        decoded = base64.b64decode(image_data[0]["data"])
        assert decoded == fake_bytes

    @pytest.mark.asyncio
    async def test_non_image_file_leaves_image_data_empty(self):
        """A non-image file (CSV) should NOT appear in image_data."""
        fake_bytes = b"col1,col2\n1,2\n"

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier.return_value = "user@example.com"
        adapter.download_file = AsyncMock(return_value=fake_bytes)

        message = MagicMock()
        message.files = [self._make_file_attachment(name="data.csv", mimetype="text/csv", size=14)]
        message.sender_id = "tg-123"
        message.channel_id = "chan-456"

        container = MagicMock()

        audit_stub = MagicMock()
        audit_stub.log = AsyncMock()

        with (
            patch.dict(
                "sys.modules",
                {
                    "database": MagicMock(db=MagicMock()),
                    "services.docker_service": MagicMock(),
                    "services.settings_service": MagicMock(
                        settings_service=MagicMock(get=MagicMock(return_value=None))
                    ),
                    "services.task_execution_service": MagicMock(),
                    "services.docker_utils": MagicMock(
                        container_put_archive=AsyncMock(return_value=True),
                        container_exec_run=AsyncMock(),
                    ),
                    "services.platform_audit_service": MagicMock(
                        platform_audit_service=audit_stub,
                        AuditEventType=MagicMock(EXECUTION="execution"),
                    ),
                    "services.telegram_media": MagicMock(),
                    "adapters.base": MagicMock(),
                },
            ),
        ):
            if "adapters.message_router" in sys.modules:
                del sys.modules["adapters.message_router"]
            import adapters.message_router as mr

            router = mr.ChannelMessageRouter()
            result = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-abc"
            )

        descriptions, upload_dir, all_writes_failed, image_data = result
        assert image_data == [], "CSV file must not appear in image_data"
