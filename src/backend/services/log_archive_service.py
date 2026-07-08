"""
Log Archive Service for Trinity Vector Logs.

Handles automatic archival of old log files with compression.
"""
import os
import gzip
import shutil
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .archive_storage import get_archive_storage
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Configuration from environment
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))
LOG_ARCHIVE_ENABLED = os.getenv("LOG_ARCHIVE_ENABLED", "true").lower() == "true"
LOG_CLEANUP_HOUR = int(os.getenv("LOG_CLEANUP_HOUR", "3"))

LOG_DIR = Path("/data/logs")
TEMP_ARCHIVE_DIR = Path("/tmp/archives")


class LogArchiveService:
    """Service for archiving old Vector log files."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.storage = get_archive_storage()
        logger.info("Archive storage initialized")

    def start(self):
        """Start the archival scheduler."""
        if not LOG_ARCHIVE_ENABLED:
            logger.info("Log archival disabled (LOG_ARCHIVE_ENABLED=false)")
            return

        # Ensure temp archive directory exists for compression staging
        TEMP_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        # #1478: retention archives then unlink()s the originals. If the logs
        # volume is mounted read-only on this container the archive succeeds but
        # every delete fails (Errno 30) and the volume grows unbounded. Surface
        # that once at startup instead of only as silent per-file errors at
        # cleanup_hour.
        try:
            if LOG_DIR.exists() and not os.access(LOG_DIR, os.W_OK):
                logger.warning(
                    "Log directory %s is NOT writable — retention will archive old "
                    "logs but cannot delete the originals, so disk usage grows "
                    "unbounded (Issue #1478). Mount the logs volume read-write on "
                    "the backend container (docker-compose: 'trinity-logs:/data/logs', "
                    "not ':ro').",
                    LOG_DIR,
                )
        except OSError:
            pass  # never block startup on a permission probe

        # Schedule nightly archival
        self.scheduler.add_job(
            self.archive_old_logs,
            CronTrigger(hour=LOG_CLEANUP_HOUR, minute=0),
            id="log_archival",
            name="Nightly Log Archival",
            replace_existing=True,
            misfire_grace_time=3600,  # 1 hour grace period
        )

        self.scheduler.start()
        logger.info(
            f"Log archival scheduler started: Daily at {LOG_CLEANUP_HOUR:02d}:00 UTC "
            f"(retention: {LOG_RETENTION_DAYS} days)"
        )

    def stop(self):
        """Stop the archival scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Log archival scheduler stopped")

    async def archive_old_logs(
        self, retention_days: Optional[int] = None, delete_after_archive: bool = True
    ) -> Dict[str, Any]:
        """
        Archive log files older than retention period.

        Args:
            retention_days: Override default retention days
            delete_after_archive: Delete originals after successful archive

        Returns:
            Dict with archive statistics
        """
        if retention_days is None:
            retention_days = LOG_RETENTION_DAYS

        cutoff_date = datetime.utcnow() - timedelta(days=retention_days)

        if not LOG_DIR.exists():
            logger.warning(f"Log directory not found: {LOG_DIR}")
            return {"error": "Log directory not found"}

        files_processed = 0
        bytes_before = 0
        bytes_after = 0
        files_stored = 0
        errors = []

        # Find all log files older than retention period
        for log_file in LOG_DIR.glob("*.json"):
            try:
                # Extract date from filename: platform-2025-10-06.json or agents-2025-10-06.json
                file_date = self._extract_date_from_filename(log_file.name)

                if file_date and file_date < cutoff_date.date():
                    # Compress the file to temp directory
                    archive_path = TEMP_ARCHIVE_DIR / f"{log_file.stem}.json.gz"
                    original_size = log_file.stat().st_size

                    logger.info(f"Archiving {log_file.name} ({original_size / 1024 / 1024:.1f} MB)")

                    # Compress with integrity check
                    if await self._compress_file(log_file, archive_path):
                        compressed_size = archive_path.stat().st_size
                        bytes_before += original_size
                        bytes_after += compressed_size

                        # Store archive using storage backend
                        try:
                            metadata = {
                                "original_size": str(original_size),
                                "compressed_size": str(compressed_size),
                                "archived_at": utc_now_iso(),
                                "retention_days": str(retention_days),
                                "original_file": log_file.name,
                            }

                            await self.storage.store_archive(archive_path, metadata)
                            files_stored += 1

                        except Exception as e:
                            logger.error(f"Storage failed for {archive_path.name}: {e}")
                            errors.append(f"Storage failed: {archive_path.name}")
                            # Clean up temp file
                            if archive_path.exists():
                                archive_path.unlink()
                            continue

                        # Delete original if requested
                        if delete_after_archive:
                            log_file.unlink()
                            logger.info(f"Deleted original: {log_file.name}")

                        files_processed += 1
                    else:
                        errors.append(f"Compression failed: {log_file.name}")

            except Exception as e:
                logger.error(f"Error archiving {log_file.name}: {e}")
                errors.append(f"Error: {log_file.name} - {str(e)}")

        # Log summary
        bytes_saved = bytes_before - bytes_after
        logger.info(
            f"Archival complete: {files_processed} files, "
            f"{bytes_saved / 1024 / 1024:.1f} MB saved "
            f"({files_stored} files stored)"
        )

        return {
            "files_processed": files_processed,
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "bytes_saved": bytes_saved,
            "files_stored": files_stored,
            "errors": errors,
            "retention_days": retention_days,
            "cutoff_date": cutoff_date.isoformat(),
        }

    async def _compress_file(self, source: Path, dest: Path) -> bool:
        """
        Compress a file with gzip and verify integrity.

        Args:
            source: Source file path
            dest: Destination archive path

        Returns:
            True if compression and verification succeeded
        """
        try:
            # Calculate checksum of original
            original_checksum = self._calculate_checksum(source)

            # Compress using gzip level 9
            with open(source, 'rb') as f_in:
                with gzip.open(dest, 'wb', compresslevel=9) as f_out:
                    shutil.copyfileobj(f_in, f_out, length=1024*1024)  # 1MB chunks

            # Verify by decompressing and checking checksum
            with gzip.open(dest, 'rb') as f:
                data = f.read()
                decompressed_checksum = hashlib.sha256(data).hexdigest()

            if original_checksum != decompressed_checksum:
                logger.error(f"Integrity check failed for {dest}")
                dest.unlink()  # Delete corrupt archive
                return False

            return True

        except Exception as e:
            logger.error(f"Compression failed for {source}: {e}")
            if dest.exists():
                dest.unlink()
            return False

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024*1024), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _extract_date_from_filename(self, filename: str) -> Optional[datetime.date]:
        """
        Extract date from log filename.

        Expected format: platform-2025-10-06.json or agents-2025-10-06.json

        Args:
            filename: Log filename

        Returns:
            datetime.date object or None if parsing fails
        """
        try:
            # Remove extension and split by hyphen
            parts = filename.replace('.json', '').split('-')

            # Last 3 parts should be YYYY-MM-DD
            if len(parts) >= 4:
                year = int(parts[-3])
                month = int(parts[-2])
                day = int(parts[-1])
                return datetime(year, month, day).date()
        except (ValueError, IndexError):
            logger.debug(f"Could not extract date from filename: {filename}")

        return None

    def get_log_stats(self) -> Dict[str, Any]:
        """Get statistics about log files."""
        stats = {
            "log_files": [],
            "archive_files": [],
            "total_log_size": 0,
            "total_archive_size": 0,
            "oldest_log": None,
            "newest_log": None,
        }

        if LOG_DIR.exists():
            log_files = list(LOG_DIR.glob("*.json"))
            for log_file in log_files:
                file_size = log_file.stat().st_size
                file_date = self._extract_date_from_filename(log_file.name)

                stats["log_files"].append({
                    "name": log_file.name,
                    "size": file_size,
                    "date": file_date.isoformat() if file_date else None,
                })
                stats["total_log_size"] += file_size

                if file_date:
                    if not stats["oldest_log"] or file_date < datetime.fromisoformat(stats["oldest_log"]).date():
                        stats["oldest_log"] = file_date.isoformat()
                    if not stats["newest_log"] or file_date > datetime.fromisoformat(stats["newest_log"]).date():
                        stats["newest_log"] = file_date.isoformat()

        # Get archived files from storage backend
        try:
            archives = self.storage.list_archives()
            for archive in archives:
                stats["archive_files"].append({
                    "name": archive["name"],
                    "size": archive["size"],
                })
                stats["total_archive_size"] += archive["size"]
        except Exception as e:
            logger.error(f"Failed to list archives: {e}")

        return stats


# Global instance
log_archive_service = LogArchiveService()

