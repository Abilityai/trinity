"""
Tests for Vector log archival and retention.

Tests the log archive service, API endpoints, compression, and local storage.

Note: Service-level tests (TestArchiveService, TestLogArchiveIntegration) require
the log archive service to have write access to /data directory. These tests
will be skipped when running outside the proper container environment.
"""
import pytest
import json
import gzip
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
import tempfile
import shutil

# Add backend to path for service imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "backend"))

from testkit.assertions import assert_status, assert_status_in


# Skip condition for tests that require the log archive service filesystem
SKIP_SERVICE_TESTS = not (
    os.path.exists('/data') and os.access('/data', os.W_OK)
)
SKIP_SERVICE_REASON = "Requires writable /data directory (run inside container)"


# Test fixtures

@pytest.fixture
def test_log_dir(tmp_path):
    """Create a temporary log directory for testing."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    return log_dir


@pytest.fixture
def test_archive_dir(tmp_path):
    """Create a temporary archive directory for testing."""
    archive_dir = tmp_path / "archives"
    archive_dir.mkdir()
    return archive_dir


@pytest.fixture
def mock_log_files(test_log_dir):
    """Create mock log files with different dates."""
    files = []

    # Create files for different dates
    dates = [
        datetime.utcnow().date(),  # Today
        (datetime.utcnow() - timedelta(days=30)).date(),  # 30 days ago
        (datetime.utcnow() - timedelta(days=95)).date(),  # 95 days ago
        (datetime.utcnow() - timedelta(days=180)).date(),  # 180 days ago
    ]

    for i, date in enumerate(dates):
        # Create platform and agent log files
        for log_type in ["platform", "agents"]:
            file_name = f"{log_type}-{date.isoformat()}.json"
            file_path = test_log_dir / file_name

            # Write sample log entries
            log_entries = [
                {
                    "timestamp": f"{date}T{hour:02d}:00:00Z",
                    "level": "info",
                    "message": f"Test log entry {i}-{hour}",
                    "container_name": f"trinity-backend" if log_type == "platform" else f"agent-test",
                }
                for hour in range(24)
            ]

            with open(file_path, 'w') as f:
                for entry in log_entries:
                    f.write(json.dumps(entry) + '\n')

            files.append(file_path)

    return files


# =========================================================================
# Test: API Authentication
# =========================================================================

class TestLogArchiveAuthentication:
    """Test that all log endpoints require admin authentication."""

    def test_stats_requires_auth(self, unauthenticated_client):
        """Test GET /api/logs/stats requires authentication."""
        response = unauthenticated_client.get("/api/logs/stats", auth=False)
        assert_status(response, 401)

    def test_retention_config_requires_auth(self, unauthenticated_client):
        """Test GET /api/logs/retention requires authentication."""
        response = unauthenticated_client.get("/api/logs/retention", auth=False)
        assert_status(response, 401)

    def test_update_retention_requires_auth(self, unauthenticated_client):
        """Test PUT /api/logs/retention requires authentication."""
        response = unauthenticated_client.put(
            "/api/logs/retention",
            json={"retention_days": 30, "archive_enabled": True, "cleanup_hour": 3},
            auth=False
        )
        assert_status(response, 401)

    def test_archive_requires_auth(self, unauthenticated_client):
        """Test POST /api/logs/archive requires authentication."""
        response = unauthenticated_client.post("/api/logs/archive", json={}, auth=False)
        assert_status(response, 401)

    def test_health_requires_auth(self, unauthenticated_client):
        """Test GET /api/logs/health requires authentication."""
        response = unauthenticated_client.get("/api/logs/health", auth=False)
        assert_status(response, 401)


# =========================================================================
# Test: Stats Endpoint
# =========================================================================

class TestLogStats:
    """Test log statistics endpoint."""

    def test_stats_returns_expected_fields(self, api_client):
        """Test that stats endpoint returns expected structure."""
        response = api_client.get("/api/logs/stats")

        assert_status(response, 200)
        data = response.json()

        # Check required fields
        assert "log_files" in data
        assert "archive_files" in data
        assert "total_log_size" in data
        assert "total_log_size_mb" in data
        assert "total_archive_size" in data
        assert "total_archive_size_mb" in data
        assert "log_file_count" in data
        assert "archive_file_count" in data

    def test_stats_log_files_structure(self, api_client):
        """Test that log files array has expected structure."""
        response = api_client.get("/api/logs/stats")

        assert_status(response, 200)
        data = response.json()

        if data["log_file_count"] > 0:
            log_file = data["log_files"][0]
            assert "name" in log_file
            assert "size" in log_file
            assert "date" in log_file or log_file["date"] is None


# =========================================================================
# Test: Retention Configuration
# =========================================================================

class TestRetentionConfig:
    """Test retention configuration endpoints."""

    def test_get_retention_returns_defaults(self, api_client):
        """Test GET /api/logs/retention returns default values."""
        response = api_client.get("/api/logs/retention")

        assert_status(response, 200)
        data = response.json()

        assert "retention_days" in data
        assert "archive_enabled" in data
        assert "cleanup_hour" in data

        assert isinstance(data["retention_days"], int)
        assert isinstance(data["archive_enabled"], bool)
        assert isinstance(data["cleanup_hour"], int)
        assert 0 <= data["cleanup_hour"] <= 23

    def test_update_retention_validates_days_minimum(self, api_client):
        """Test that retention days must be at least 1."""
        response = api_client.put(
            "/api/logs/retention",
            json={"retention_days": 0, "archive_enabled": True, "cleanup_hour": 3}
        )

        assert_status_in(response, [400, 422])

    def test_update_retention_validates_days_maximum(self, api_client):
        """Test that retention days cannot exceed 3650."""
        response = api_client.put(
            "/api/logs/retention",
            json={"retention_days": 9999, "archive_enabled": True, "cleanup_hour": 3}
        )

        assert_status_in(response, [400, 422])

    def test_update_retention_validates_hour_range(self, api_client):
        """Test that cleanup hour must be 0-23."""
        response = api_client.put(
            "/api/logs/retention",
            json={"retention_days": 90, "archive_enabled": True, "cleanup_hour": 25}
        )

        assert_status_in(response, [400, 422])

    def test_update_retention_succeeds(self, api_client):
        """Test successful retention update."""
        response = api_client.put(
            "/api/logs/retention",
            json={"retention_days": 30, "archive_enabled": True, "cleanup_hour": 2}
        )

        assert_status(response, 200)
        data = response.json()

        assert data["retention_days"] == 30
        assert data["archive_enabled"] is True
        assert data["cleanup_hour"] == 2


# =========================================================================
# Test: Archive Service
# =========================================================================

@pytest.mark.skipif(SKIP_SERVICE_TESTS, reason=SKIP_SERVICE_REASON)
class TestArchiveService:
    """Test log archive service functionality.

    These tests require the log archive service to have filesystem access.
    They will be skipped when /data directory is not writable.
    """

    @pytest.mark.asyncio
    async def test_extract_date_from_filename(self):
        """Test date extraction from log filenames."""
        from services.log_archive_service import LogArchiveService

        service = LogArchiveService()

        # Valid filenames
        assert service._extract_date_from_filename("platform-2025-10-06.json") == datetime(2025, 10, 6).date()
        assert service._extract_date_from_filename("agents-2026-01-05.json") == datetime(2026, 1, 5).date()

        # Invalid filenames
        assert service._extract_date_from_filename("platform.json") is None
        assert service._extract_date_from_filename("invalid-name.json") is None
        assert service._extract_date_from_filename("platform-2025-99-99.json") is None

    @pytest.mark.asyncio
    async def test_compression_reduces_size(self, test_log_dir, test_archive_dir):
        """Test that compression significantly reduces file size."""
        from services.log_archive_service import LogArchiveService

        # Create a large test file
        test_file = test_log_dir / "platform-2025-01-01.json"
        with open(test_file, 'w') as f:
            for i in range(10000):
                f.write(json.dumps({
                    "timestamp": f"2025-01-01T{i % 24:02d}:00:00Z",
                    "message": f"Test log entry {i}" * 10,
                    "level": "info"
                }) + '\n')

        original_size = test_file.stat().st_size
        archive_path = test_archive_dir / "platform-2025-01-01.json.gz"

        service = LogArchiveService()
        success = await service._compress_file(test_file, archive_path)

        assert success
        assert archive_path.exists()

        compressed_size = archive_path.stat().st_size
        compression_ratio = compressed_size / original_size

        # Should achieve at least 50% compression
        assert compression_ratio < 0.5, f"Compression ratio {compression_ratio:.2%} is too high"

    @pytest.mark.asyncio
    async def test_compression_integrity_check(self, test_log_dir, test_archive_dir):
        """Test that compression integrity check works."""
        from services.log_archive_service import LogArchiveService

        test_file = test_log_dir / "platform-2025-01-01.json"
        with open(test_file, 'w') as f:
            f.write(json.dumps({"test": "data"}) + '\n')

        archive_path = test_archive_dir / "platform-2025-01-01.json.gz"

        service = LogArchiveService()
        success = await service._compress_file(test_file, archive_path)

        assert success

        # Verify we can decompress and get original content
        with gzip.open(archive_path, 'rb') as f:
            decompressed = f.read()

        with open(test_file, 'rb') as f:
            original = f.read()

        assert decompressed == original


# =========================================================================
# Test: Manual Archive Endpoint
# =========================================================================

class TestManualArchive:
    """Test manual archive trigger endpoint."""

    def test_archive_endpoint_exists(self, api_client):
        """Test that archive endpoint is available."""
        response = api_client.post(
            "/api/logs/archive",
            json={"retention_days": 365, "delete_after_archive": False}
        )

        # Should succeed or return an expected error (not 404)
        assert response.status_code != 404

    def test_archive_returns_expected_fields(self, api_client):
        """Test that archive response has expected structure."""
        response = api_client.post(
            "/api/logs/archive",
            json={"retention_days": 365, "delete_after_archive": False}
        )

        assert_status(response, 200)
        data = response.json()

        assert "status" in data
        assert "files_processed" in data
        assert "bytes_saved" in data
        assert "files_stored" in data
        assert "retention_days" in data

    def test_archive_respects_retention_days_param(self, api_client):
        """Test that archive respects custom retention_days parameter."""
        response = api_client.post(
            "/api/logs/archive",
            json={"retention_days": 30, "delete_after_archive": False}
        )

        assert_status(response, 200)
        data = response.json()

        assert data["retention_days"] == 30


# =========================================================================
# Test: Health Endpoint
# =========================================================================

class TestLogServiceHealth:
    """Test log service health endpoint."""

    def test_health_returns_scheduler_status(self, api_client):
        """Test that health endpoint returns scheduler status."""
        response = api_client.get("/api/logs/health")

        assert_status(response, 200)
        data = response.json()

        assert "scheduler_running" in data
        assert "archive_enabled" in data
        assert "retention_days" in data
        assert "cleanup_hour" in data

        assert isinstance(data["scheduler_running"], bool)
        assert isinstance(data["archive_enabled"], bool)


# =========================================================================
# Test: Integration
# =========================================================================

class TestLogArchiveIntegration:
    """Integration tests for log archival workflow."""

    @pytest.mark.skipif(SKIP_SERVICE_TESTS, reason=SKIP_SERVICE_REASON)
    @pytest.mark.asyncio
    async def test_full_archive_workflow(self, test_log_dir, test_archive_dir):
        """Test complete archive workflow with file operations."""
        from services.log_archive_service import LogArchiveService
        from services.archive_storage import LocalArchiveStorage

        # Patch the service to use test directories
        with patch('services.log_archive_service.LOG_DIR', test_log_dir), \
             patch('services.log_archive_service.TEMP_ARCHIVE_DIR', test_log_dir / "temp"):

            # Create temp directory
            (test_log_dir / "temp").mkdir(exist_ok=True)

            # Create old log file
            old_date = (datetime.utcnow() - timedelta(days=100)).date()
            old_file = test_log_dir / f"platform-{old_date.isoformat()}.json"

            with open(old_file, 'w') as f:
                for i in range(100):
                    f.write(json.dumps({"message": f"Log {i}"}) + '\n')

            # Create recent log file
            recent_date = datetime.utcnow().date()
            recent_file = test_log_dir / f"platform-{recent_date.isoformat()}.json"

            with open(recent_file, 'w') as f:
                f.write(json.dumps({"message": "Recent log"}) + '\n')

            # Run archival with 90-day retention
            service = LogArchiveService()
            service.storage = LocalArchiveStorage(archive_path=str(test_archive_dir))

            result = await service.archive_old_logs(
                retention_days=90,
                delete_after_archive=True
            )

            # Verify results
            assert result["files_processed"] >= 1
            assert result["bytes_saved"] > 0

            # Old file should be archived and deleted
            assert not old_file.exists()
            assert (test_archive_dir / f"platform-{old_date.isoformat()}.json.gz").exists()

            # Recent file should remain
            assert recent_file.exists()

    def test_retention_config_persists_runtime_changes(self, api_client):
        """Test that runtime retention changes persist until restart."""
        # Update retention
        update_response = api_client.put(
            "/api/logs/retention",
            json={"retention_days": 45, "archive_enabled": True, "cleanup_hour": 4}
        )

        assert_status(update_response, 200)

        # Verify change persisted
        get_response = api_client.get("/api/logs/retention")

        assert_status(get_response, 200)
        data = get_response.json()

        assert data["retention_days"] == 45
        assert data["cleanup_hour"] == 4


# =========================================================================
# Test: Validation
# =========================================================================

class TestArchiveValidation:
    """Test validation of archive parameters."""

    def test_archive_validates_retention_days_negative(self, api_client):
        """Test that negative retention days are rejected."""
        response = api_client.post(
            "/api/logs/archive",
            json={"retention_days": -1, "delete_after_archive": True}
        )

        assert_status_in(response, [400, 422])

    def test_archive_validates_retention_days_excessive(self, api_client):
        """Test that excessive retention days are rejected."""
        response = api_client.post(
            "/api/logs/archive",
            json={"retention_days": 5000, "delete_after_archive": True}
        )

        assert_status_in(response, [400, 422])


# =========================================================================
# Test: Error Handling
# =========================================================================

@pytest.mark.skipif(SKIP_SERVICE_TESTS, reason=SKIP_SERVICE_REASON)
class TestArchiveErrorHandling:
    """Test error handling in archive service.

    These tests require the log archive service to be importable.
    They will be skipped when /data directory is not writable.
    """

    @pytest.mark.asyncio
    async def test_archive_handles_missing_log_directory(self):
        """Test graceful handling when log directory doesn't exist."""
        from services.log_archive_service import LogArchiveService

        with patch('services.log_archive_service.LOG_DIR', Path("/nonexistent/path")):
            service = LogArchiveService()
            result = await service.archive_old_logs()

            assert "error" in result
            assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_checksum_calculation(self):
        """Test file checksum calculation."""
        from services.log_archive_service import LogArchiveService

        service = LogArchiveService()

        # Create test file
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write("test content")
            test_file = Path(f.name)

        try:
            checksum = service._calculate_checksum(test_file)

            # Should return valid SHA256 hex string
            assert isinstance(checksum, str)
            assert len(checksum) == 64
            assert all(c in '0123456789abcdef' for c in checksum)

        finally:
            test_file.unlink()

