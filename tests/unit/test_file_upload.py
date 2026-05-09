"""
Tests for file upload support (Issue #354).

Tests Telegram adapter file parsing, download implementation,
and message router security validations.

Related flow: docs/memory/feature-flows/channel-adapters.md
"""

import os
import sys
import tempfile

# Set up temp database BEFORE any imports that trigger database initialization
_temp_dir = tempfile.mkdtemp()
os.environ.setdefault("TRINITY_DB_PATH", os.path.join(_temp_dir, "test.db"))

# Uses unit/conftest.py which sets up sys.path and handles utils shadowing

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Mark all tests as unit tests (no backend required)
pytestmark = pytest.mark.unit


class TestTelegramFileExtraction:
    """Test Telegram adapter file extraction from messages."""

    def test_extract_photo_largest_size(self):
        """Telegram photos: should extract largest size (last in array)."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()

        # Telegram sends array of photo sizes, smallest to largest
        message = {
            "photo": [
                {"file_id": "small_id", "file_size": 1000, "width": 100, "height": 100},
                {"file_id": "medium_id", "file_size": 5000, "width": 500, "height": 500},
                {"file_id": "large_id", "file_size": 50000, "width": 1000, "height": 1000},
            ],
            "caption": "Test photo",
        }

        files = adapter._extract_files(message)

        assert len(files) == 1
        assert files[0].id == "large_id"
        assert files[0].size == 50000
        assert files[0].mimetype == "image/jpeg"
        assert files[0].name == "photo.jpg"

    def test_extract_document(self):
        """Telegram documents: should extract file metadata."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()

        message = {
            "document": {
                "file_id": "doc_123",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 102400,
            },
        }

        files = adapter._extract_files(message)

        assert len(files) == 1
        assert files[0].id == "doc_123"
        assert files[0].name == "report.pdf"
        assert files[0].mimetype == "application/pdf"
        assert files[0].size == 102400

    def test_extract_no_media(self):
        """Text-only message: should return empty list."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()
        message = {"text": "Hello world"}

        files = adapter._extract_files(message)

        assert files == []

    def test_extract_photo_and_document(self):
        """Message with both photo and document: should extract both."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()

        message = {
            "photo": [{"file_id": "photo_id", "file_size": 1000}],
            "document": {
                "file_id": "doc_id",
                "file_name": "data.csv",
                "mime_type": "text/csv",
                "file_size": 500,
            },
        }

        files = adapter._extract_files(message)

        assert len(files) == 2
        # Photo first (order from _extract_files)
        assert files[0].id == "photo_id"
        assert files[1].id == "doc_id"


class TestTelegramFileDownload:
    """Test Telegram adapter download_file implementation."""

    @pytest.mark.asyncio
    async def test_download_file_success(self):
        """Successful two-step download via Bot API."""
        from adapters.telegram_adapter import TelegramAdapter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = TelegramAdapter()

        file = FileAttachment(
            id="AgACAgIAAxk",
            name="photo.jpg",
            mimetype="image/jpeg",
            size=5000,
            url="AgACAgIAAxk",
        )

        message = NormalizedMessage(
            sender_id="123",
            text="test",
            channel_id="456",
            timestamp="1234567890",
            metadata={"agent_name": "test-agent"},
        )

        mock_file_content = b"\xff\xd8\xff\xe0" + b"\x00" * 100  # JPEG header

        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter.httpx.AsyncClient") as MockClient:

            mock_db.get_telegram_bot_token.return_value = "bot123:token"

            mock_client = AsyncMock()
            MockClient.return_value.__aenter__.return_value = mock_client

            # First call: getFile returns file_path
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": True, "result": {"file_path": "photos/file_0.jpg"}},
            )

            # Second call: download returns file content
            mock_client.get.return_value = MagicMock(
                status_code=200,
                content=mock_file_content,
            )

            result = await adapter.download_file(file, message)

            assert result == mock_file_content
            mock_db.get_telegram_bot_token.assert_called_once_with("test-agent")

    @pytest.mark.asyncio
    async def test_download_file_no_agent_name(self):
        """Should return None if no agent_name in metadata."""
        from adapters.telegram_adapter import TelegramAdapter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = TelegramAdapter()

        file = FileAttachment(
            id="file_id",
            name="test.txt",
            mimetype="text/plain",
            size=100,
            url="file_id",
        )

        message = NormalizedMessage(
            sender_id="123",
            text="test",
            channel_id="456",
            timestamp="1234567890",
            metadata={},  # No agent_name
        )

        result = await adapter.download_file(file, message)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_file_getfile_fails(self):
        """Should return None if getFile API fails."""
        from adapters.telegram_adapter import TelegramAdapter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = TelegramAdapter()

        file = FileAttachment(
            id="file_id",
            name="test.txt",
            mimetype="text/plain",
            size=100,
            url="file_id",
        )

        message = NormalizedMessage(
            sender_id="123",
            text="test",
            channel_id="456",
            timestamp="1234567890",
            metadata={"agent_name": "test-agent"},
        )

        with patch("adapters.telegram_adapter.db") as mock_db, \
             patch("adapters.telegram_adapter.httpx.AsyncClient") as MockClient:

            mock_db.get_telegram_bot_token.return_value = "bot123:token"

            mock_client = AsyncMock()
            MockClient.return_value.__aenter__.return_value = mock_client

            # getFile returns error
            mock_client.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"ok": False, "description": "File not found"},
            )

            result = await adapter.download_file(file, message)

            assert result is None


class TestMessageRouterFileValidation:
    """Test message router file validation (size, MIME)."""

    def test_format_file_size(self):
        """Test human-readable file size formatting."""
        from adapters.message_router import _format_file_size

        assert _format_file_size(500) == "500 B"
        assert _format_file_size(1024) == "1 KB"
        assert _format_file_size(1536) == "2 KB"  # Rounded
        assert _format_file_size(1048576) == "1.0 MB"
        assert _format_file_size(5242880) == "5.0 MB"

    def test_magic_available_flag(self):
        """Check that magic library availability is tracked."""
        from services import upload_service

        # _MAGIC_AVAILABLE moved to upload_service when file-upload code was
        # extracted from adapters/message_router (services/upload_service.py:26-28).
        assert hasattr(upload_service, "_MAGIC_AVAILABLE")
        assert isinstance(upload_service._MAGIC_AVAILABLE, bool)


class TestParseMessageWithFiles:
    """Test parse_message populates files field correctly."""

    def test_parse_message_with_photo(self):
        """parse_message should populate files for photos."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()

        raw_event = {
            "_bot_id": "bot123",
            "_bot_username": "testbot",
            "_agent_name": "test-agent",
            "message": {
                "message_id": 1,
                "from": {"id": 123, "is_bot": False, "username": "testuser"},
                "chat": {"id": 456, "type": "private"},
                "date": 1234567890,
                "photo": [
                    {"file_id": "photo_id", "file_size": 5000, "width": 800, "height": 600}
                ],
                "caption": "Check this out",
            },
        }

        result = adapter.parse_message(raw_event)

        assert result is not None
        assert len(result.files) == 1
        assert result.files[0].id == "photo_id"
        assert "photo" in result.text.lower() or "check this out" in result.text.lower()

    def test_parse_message_file_only_no_text(self):
        """parse_message should work with file-only messages (no caption)."""
        from adapters.telegram_adapter import TelegramAdapter

        adapter = TelegramAdapter()

        raw_event = {
            "_bot_id": "bot123",
            "_bot_username": "testbot",
            "_agent_name": "test-agent",
            "message": {
                "message_id": 1,
                "from": {"id": 123, "is_bot": False},
                "chat": {"id": 456, "type": "private"},
                "date": 1234567890,
                "document": {
                    "file_id": "doc_id",
                    "file_name": "data.json",
                    "mime_type": "application/json",
                    "file_size": 1000,
                },
                # No caption, no text
            },
        }

        result = adapter.parse_message(raw_event)

        assert result is not None
        assert len(result.files) == 1
        # Should have placeholder text for file-only messages
        assert result.text  # Not empty


# =============================================================================
# Phase 2 (Issue #487) — workspace delivery
# =============================================================================


class TestFilenameSanitization:
    """Test the _sanitize_filename helper used during workspace delivery."""

    def test_strips_path_traversal_unix(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        result = _sanitize_filename("../../etc/passwd", "fid1", used)
        assert "/" not in result
        assert ".." not in result
        assert result == "passwd"

    def test_strips_absolute_path(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        result = _sanitize_filename("/etc/passwd", "fid1", used)
        assert "/" not in result
        assert result == "passwd"

    def test_unicode_normalize_fullwidth(self):
        """Fullwidth unicode chars are normalized via NFKC."""
        from adapters.message_router import _sanitize_filename
        used: set = set()
        # Fullwidth dot/slash/period sequences NFKC-normalize to ASCII.
        # Ensure traversal attempts encoded with unicode variants don't survive.
        traversal = "．．／etc／passwd"  # ＦＵＬＬＷＩＤＴＨ ../etc/passwd
        result = _sanitize_filename(traversal, "fid1", used)
        assert "/" not in result
        assert ".." not in result

    def test_unicode_normalize_preserves_content(self):
        """Standard unicode names normalize cleanly."""
        from adapters.message_router import _sanitize_filename
        used: set = set()
        result = _sanitize_filename("café.txt", "fid1", used)
        # NFKC keeps café intact (é is already NFKC-normal)
        assert result.endswith(".txt")
        assert "caf" in result

    def test_truncates_long_filename_preserving_extension(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        long_name = ("a" * 300) + ".txt"
        result = _sanitize_filename(long_name, "fid1", used)
        assert len(result) <= 200
        assert result.endswith(".txt")

    def test_truncates_long_no_extension(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        long_name = "x" * 300
        result = _sanitize_filename(long_name, "fid1", used)
        assert len(result) <= 200

    def test_collision_dedup(self):
        """Same sanitized name twice gets -1, -2 suffix before extension."""
        from adapters.message_router import _sanitize_filename
        used: set = set()
        first = _sanitize_filename("data.csv", "fid1", used)
        used.add(first)
        second = _sanitize_filename("data.csv", "fid2", used)
        used.add(second)
        third = _sanitize_filename("data.csv", "fid3", used)

        assert first == "data.csv"
        assert second == "data-1.csv"
        assert third == "data-2.csv"

    def test_collision_dedup_no_extension(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        first = _sanitize_filename("README", "fid1", used)
        used.add(first)
        second = _sanitize_filename("README", "fid2", used)
        assert first == "README"
        assert second == "README-1"

    def test_empty_name_fallback(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        assert _sanitize_filename("", "abc123", used) == "file_abc123"

    def test_dot_only_fallback(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        assert _sanitize_filename("...", "abc123", used) == "file_abc123"

    def test_hidden_dotfile_rejected(self):
        """Dotfiles like .env / .gitignore fall back to file_{id} (#222 parity)."""
        from adapters.message_router import _sanitize_filename
        used: set = set()
        assert _sanitize_filename(".env", "F001", used) == "file_F001"
        used = set()
        assert _sanitize_filename(".gitignore", "F002", used) == "file_F002"
        used = set()
        assert _sanitize_filename(".mcp.json", "F003", used) == "file_F003"

    def test_strips_unsafe_chars(self):
        from adapters.message_router import _sanitize_filename
        used: set = set()
        result = _sanitize_filename("my file<>?.txt", "fid1", used)
        # Spaces and angle brackets get sanitized
        assert "<" not in result
        assert ">" not in result
        assert "?" not in result
        assert result.endswith(".txt")


class TestFileDeliveryFormat:
    """Test the chat injection format includes uploader attribution."""

    @pytest.mark.asyncio
    async def test_injection_includes_verified_email(self):
        from adapters.message_router import ChannelMessageRouter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier = MagicMock(return_value="telegram:bot:user")
        adapter.download_file = AsyncMock(return_value=b"col1,col2\n1,2\n")

        message = NormalizedMessage(
            sender_id="user-123",
            text="see attached",
            channel_id="chat-456",
            timestamp="1234567890",
            files=[FileAttachment(id="fid1", name="data.csv", mimetype="text/csv", size=14, url="fid1")],
            metadata={"agent_name": "test-agent"},
        )

        container = MagicMock()
        router = ChannelMessageRouter()

        with patch("adapters.message_router.container_exec_run", new=AsyncMock()), \
             patch("services.upload_service.container_put_archive", new=AsyncMock(return_value=True)), \
             patch("services.upload_service.platform_audit_service") as mock_audit:
            mock_audit.log = AsyncMock()
            descriptions, upload_dir, all_failed, _image_data = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-abc",
                verified_email="alice@example.com",
            )

        assert all_failed is False
        assert upload_dir is not None
        joined = "\n".join(descriptions)
        assert "[File uploaded by alice@example.com]" in joined
        assert "data.csv" in joined
        assert "saved to /home/developer/uploads/" in joined

    @pytest.mark.asyncio
    async def test_injection_falls_back_to_source_id_without_email(self):
        from adapters.message_router import ChannelMessageRouter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier = MagicMock(return_value="telegram:bot42:user99")
        adapter.download_file = AsyncMock(return_value=b"hello world")

        message = NormalizedMessage(
            sender_id="user-99",
            text="",
            channel_id="chat-1",
            timestamp="1234567890",
            files=[FileAttachment(id="fid1", name="note.txt", mimetype="text/plain", size=11, url="fid1")],
            metadata={"agent_name": "test-agent"},
        )

        container = MagicMock()
        router = ChannelMessageRouter()

        with patch("adapters.message_router.container_exec_run", new=AsyncMock()), \
             patch("services.upload_service.container_put_archive", new=AsyncMock(return_value=True)), \
             patch("services.upload_service.platform_audit_service") as mock_audit:
            mock_audit.log = AsyncMock()
            descriptions, _, all_failed, _image_data = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-x",
                verified_email=None,
            )

        assert all_failed is False
        joined = "\n".join(descriptions)
        assert "[File uploaded by telegram:bot42:user99]" in joined
        assert "note.txt" in joined


class TestFileDeliveryFailures:
    """Test workspace write failure handling (AC6)."""

    @pytest.mark.asyncio
    async def test_all_writes_fail_signals_abort(self):
        """All container writes failing returns all_writes_failed=True."""
        from adapters.message_router import ChannelMessageRouter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier = MagicMock(return_value="telegram:bot:user")
        adapter.download_file = AsyncMock(return_value=b"data")

        message = NormalizedMessage(
            sender_id="user",
            text="",
            channel_id="chat",
            timestamp="1234567890",
            files=[
                FileAttachment(id="fid1", name="a.txt", mimetype="text/plain", size=4, url="fid1"),
                FileAttachment(id="fid2", name="b.txt", mimetype="text/plain", size=4, url="fid2"),
            ],
            metadata={"agent_name": "test-agent"},
        )

        container = MagicMock()
        router = ChannelMessageRouter()

        with patch("adapters.message_router.container_exec_run", new=AsyncMock()), \
             patch("services.upload_service.container_put_archive", new=AsyncMock(return_value=False)), \
             patch("services.upload_service.platform_audit_service") as mock_audit:
            mock_audit.log = AsyncMock()
            descriptions, _, all_failed, _image_data = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-y",
                verified_email="user@example.com",
            )

        assert all_failed is True
        # Failure markers present
        assert any("[File upload failed]" in d for d in descriptions)

    @pytest.mark.asyncio
    async def test_partial_failure_keeps_descriptions_and_proceeds(self):
        """One file fails, one succeeds — all_writes_failed=False."""
        from adapters.message_router import ChannelMessageRouter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier = MagicMock(return_value="telegram:bot:user")
        adapter.download_file = AsyncMock(return_value=b"data")

        message = NormalizedMessage(
            sender_id="user",
            text="",
            channel_id="chat",
            timestamp="1234567890",
            files=[
                FileAttachment(id="fid1", name="ok.txt", mimetype="text/plain", size=4, url="fid1"),
                FileAttachment(id="fid2", name="bad.txt", mimetype="text/plain", size=4, url="fid2"),
            ],
            metadata={"agent_name": "test-agent"},
        )

        container = MagicMock()
        router = ChannelMessageRouter()

        # First put_archive call succeeds, second fails
        put_mock = AsyncMock(side_effect=[True, False])

        with patch("adapters.message_router.container_exec_run", new=AsyncMock()), \
             patch("services.upload_service.container_put_archive", new=put_mock), \
             patch("services.upload_service.platform_audit_service") as mock_audit:
            mock_audit.log = AsyncMock()
            descriptions, _, all_failed, _image_data = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-z",
                verified_email="user@example.com",
            )

        assert all_failed is False
        joined = "\n".join(descriptions)
        assert "[File uploaded by user@example.com]: ok.txt" in joined
        assert "[File upload failed]: bad.txt" in joined

    @pytest.mark.asyncio
    async def test_validation_only_failures_do_not_signal_abort(self):
        """Pure validation rejections (download None) do NOT trigger all_writes_failed."""
        from adapters.message_router import ChannelMessageRouter
        from adapters.base import FileAttachment, NormalizedMessage

        adapter = MagicMock()
        adapter.channel_type = "telegram"
        adapter.get_source_identifier = MagicMock(return_value="telegram:bot:user")
        # download returns None — pre-write rejection, no write attempted
        adapter.download_file = AsyncMock(return_value=None)

        message = NormalizedMessage(
            sender_id="user",
            text="",
            channel_id="chat",
            timestamp="1234567890",
            files=[FileAttachment(id="fid1", name="x.txt", mimetype="text/plain", size=4, url="fid1")],
            metadata={"agent_name": "test-agent"},
        )

        container = MagicMock()
        router = ChannelMessageRouter()

        with patch("adapters.message_router.container_exec_run", new=AsyncMock()), \
             patch("services.upload_service.container_put_archive", new=AsyncMock(return_value=True)), \
             patch("services.upload_service.platform_audit_service") as mock_audit:
            mock_audit.log = AsyncMock()
            descriptions, _, all_failed, _image_data = await router._handle_file_uploads(
                adapter, message, "test-agent", container, "session-q",
                verified_email=None,
            )

        # Download failed for the only file — no write attempted, so not "all writes failed".
        # The agent will see a "download failed" description and respond normally.
        assert all_failed is False
        assert any("download failed" in d for d in descriptions)
