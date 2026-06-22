"""
Log management API endpoints.

Provides admin-only access to log statistics, retention configuration,
and manual archival operations.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from models import ArchiveRequest, RetentionConfig, User
from dependencies import get_current_user
from services.log_archive_service import log_archive_service, LOG_RETENTION_DAYS
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/logs", tags=["logs"])


# Dependencies

def require_admin(current_user: User = Depends(get_current_user)):
    """Require admin role for log management."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# Endpoints

@router.get("/stats")
async def get_log_stats(current_user: User = Depends(require_admin)):
    """
    Get statistics about log files.

    Returns information about active log files and archives including
    sizes, counts, and date ranges.
    """
    try:
        stats = log_archive_service.get_log_stats()
        return {
            "log_files": stats["log_files"],
            "archive_files": stats["archive_files"],
            "total_log_size": stats["total_log_size"],
            "total_log_size_mb": round(stats["total_log_size"] / 1024 / 1024, 2),
            "total_archive_size": stats["total_archive_size"],
            "total_archive_size_mb": round(stats["total_archive_size"] / 1024 / 1024, 2),
            "oldest_log": stats["oldest_log"],
            "newest_log": stats["newest_log"],
            "log_file_count": len(stats["log_files"]),
            "archive_file_count": len(stats["archive_files"]),
        }
    except Exception as e:
        logger.error(f"Failed to get log stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get log stats: {str(e)}")


@router.get("/retention", response_model=RetentionConfig)
async def get_retention_config(current_user: User = Depends(require_admin)):
    """
    Get current retention configuration.

    Returns the active retention policy settings.
    """
    import os

    return RetentionConfig(
        retention_days=int(os.getenv("LOG_RETENTION_DAYS", "90")),
        archive_enabled=os.getenv("LOG_ARCHIVE_ENABLED", "true").lower() == "true",
        cleanup_hour=int(os.getenv("LOG_CLEANUP_HOUR", "3")),
    )


@router.put("/retention", response_model=RetentionConfig)
async def update_retention_config(
    config: RetentionConfig,
    current_user: User = Depends(require_admin)
):
    """
    Update retention configuration.

    Note: This only updates the runtime configuration. To persist changes,
    update the environment variables in docker-compose.yml or .env file.
    """
    import os

    # Update runtime environment variables
    os.environ["LOG_RETENTION_DAYS"] = str(config.retention_days)
    os.environ["LOG_ARCHIVE_ENABLED"] = str(config.archive_enabled).lower()
    os.environ["LOG_CLEANUP_HOUR"] = str(config.cleanup_hour)

    logger.info(
        f"Retention config updated by {current_user.username}: "
        f"{config.retention_days} days, archive={config.archive_enabled}"
    )

    # Reschedule the job with new cleanup hour if scheduler is running
    if log_archive_service.scheduler.running:
        try:
            # Remove and re-add the job with new hour
            log_archive_service.scheduler.remove_job("log_archival")
        except Exception:
            pass  # Job might not exist

        from apscheduler.triggers.cron import CronTrigger
        log_archive_service.scheduler.add_job(
            log_archive_service.archive_old_logs,
            CronTrigger(hour=config.cleanup_hour, minute=0),
            id="log_archival",
            name="Nightly Log Archival",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    return config


@router.post("/archive")
async def trigger_archive(
    request: ArchiveRequest = ArchiveRequest(),
    current_user: User = Depends(require_admin)
):
    """
    Manually trigger log archival.

    Archives log files older than the specified retention period.
    Optionally override the retention days for this operation.
    """
    try:
        logger.info(
            f"Manual archive triggered by {current_user.username} "
            f"(retention_days={request.retention_days or LOG_RETENTION_DAYS})"
        )

        result = await log_archive_service.archive_old_logs(
            retention_days=request.retention_days,
            delete_after_archive=request.delete_after_archive,
        )

        return {
            "status": "success",
            "files_processed": result["files_processed"],
            "bytes_before": result["bytes_before"],
            "bytes_after": result["bytes_after"],
            "bytes_saved": result["bytes_saved"],
            "bytes_saved_mb": round(result["bytes_saved"] / 1024 / 1024, 2),
            "files_stored": result["files_stored"],
            "errors": result["errors"],
            "retention_days": result["retention_days"],
            "cutoff_date": result["cutoff_date"],
        }

    except Exception as e:
        logger.error(f"Archive failed: {e}")
        raise HTTPException(status_code=500, detail=f"Archive failed: {str(e)}")


@router.get("/health")
async def log_service_health(current_user: User = Depends(require_admin)):
    """
    Get health status of log archival service.

    Returns information about the scheduler and storage configuration.
    """
    import os

    return {
        "scheduler_running": log_archive_service.scheduler.running if log_archive_service.scheduler else False,
        "archive_enabled": os.getenv("LOG_ARCHIVE_ENABLED", "true").lower() == "true",
        "archive_path": os.getenv("LOG_ARCHIVE_PATH", "/data/archives"),
        "retention_days": int(os.getenv("LOG_RETENTION_DAYS", "90")),
        "cleanup_hour": int(os.getenv("LOG_CLEANUP_HOUR", "3")),
    }
