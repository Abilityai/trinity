"""
Scheduler service for Trinity platform.

Manages scheduled task execution for agents using APScheduler.
Platform-managed scheduler that executes tasks by sending messages to agents.

Uses the execution queue to prevent parallel execution on the same agent.
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore
from croniter import croniter
import pytz
import httpx

from database import db, Schedule
from models import ActivityType, ExecutionSource
from services.execution_queue import get_execution_queue, QueueFullError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SchedulerService:
    """
    Manages scheduled task execution for agents.

    Uses APScheduler with AsyncIO for non-blocking execution.
    Tasks are stored in the database and synced to APScheduler on startup.
    """

    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._broadcast_callback: Optional[Callable] = None
        self._initialized = False

    def set_broadcast_callback(self, callback: Callable):
        """Set the WebSocket broadcast callback for real-time updates."""
        self._broadcast_callback = callback

    async def _broadcast(self, message: dict):
        """Broadcast a message to WebSocket clients."""
        if self._broadcast_callback:
            import json
            await self._broadcast_callback(json.dumps(message))

    def initialize(self):
        """Initialize the scheduler and load all enabled schedules."""
        if self._initialized:
            return

        # Create scheduler with memory job store
        jobstores = {
            'default': MemoryJobStore()
        }

        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            timezone=pytz.UTC
        )

        # Load all enabled schedules from database
        schedules = db.list_all_enabled_schedules()
        for schedule in schedules:
            self._add_job(schedule)

        # Start the scheduler
        self.scheduler.start()
        self._initialized = True
        logger.info(f"Scheduler initialized with {len(schedules)} enabled schedules")

    def shutdown(self):
        """Shutdown the scheduler gracefully."""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self._initialized = False
            logger.info("Scheduler shutdown")

    def _get_job_id(self, schedule_id: str) -> str:
        """Generate a unique job ID for APScheduler."""
        return f"schedule_{schedule_id}"

    def _parse_cron(self, cron_expression: str) -> Dict:
        """
        Parse a cron expression into APScheduler CronTrigger kwargs.

        Format: minute hour day month day_of_week
        Examples:
          - "0 9 * * *" = Every day at 9:00 AM
          - "*/15 * * * *" = Every 15 minutes
          - "0 0 * * 0" = Every Sunday at midnight
        """
        parts = cron_expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: {cron_expression}. Expected 5 parts.")

        return {
            'minute': parts[0],
            'hour': parts[1],
            'day': parts[2],
            'month': parts[3],
            'day_of_week': parts[4]
        }

    def _add_job(self, schedule: Schedule):
        """Add a schedule as an APScheduler job."""
        if not self.scheduler:
            return

        job_id = self._get_job_id(schedule.id)

        try:
            # Parse cron expression
            cron_kwargs = self._parse_cron(schedule.cron_expression)

            # Create timezone-aware trigger
            timezone = pytz.timezone(schedule.timezone) if schedule.timezone else pytz.UTC
            trigger = CronTrigger(timezone=timezone, **cron_kwargs)

            # Add the job
            self.scheduler.add_job(
                self._execute_schedule,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                replace_existing=True,
                name=f"{schedule.agent_name}:{schedule.name}"
            )

            # Calculate and store next run time
            next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
            if next_run:
                db.update_schedule_run_times(schedule.id, next_run_at=next_run)

            logger.info(f"Added schedule job: {job_id} ({schedule.name}) for agent {schedule.agent_name}")
        except Exception as e:
            logger.error(f"Failed to add schedule job {job_id}: {e}")

    def _remove_job(self, schedule_id: str):
        """Remove a schedule job from APScheduler."""
        if not self.scheduler:
            return

        job_id = self._get_job_id(schedule_id)
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed schedule job: {job_id}")
        except Exception as e:
            logger.warning(f"Failed to remove schedule job {job_id}: {e}")

    def _get_next_run_time(self, cron_expression: str, timezone: str = "UTC") -> Optional[datetime]:
        """Calculate the next run time for a cron expression."""
        try:
            tz = pytz.timezone(timezone) if timezone else pytz.UTC
            now = datetime.now(tz)
            cron = croniter(cron_expression, now)
            next_time = cron.get_next(datetime)
            return next_time
        except Exception as e:
            logger.error(f"Failed to calculate next run time: {e}")
            return None

    async def _execute_schedule(self, schedule_id: str):
        """
        Execute a scheduled task.

        This is called by APScheduler when a schedule is due.
        It sends the configured message to the agent's chat endpoint.
        Uses the execution queue to prevent parallel execution.
        """
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            logger.error(f"Schedule {schedule_id} not found")
            return

        if not schedule.enabled:
            logger.info(f"Schedule {schedule_id} is disabled, skipping")
            return

        logger.info(f"Executing schedule: {schedule.name} for agent {schedule.agent_name}")

        # Import activity service (avoid circular import)
        from services.activity_service import activity_service

        # Create queue execution request
        queue = get_execution_queue()
        queue_execution = queue.create_execution(
            agent_name=schedule.agent_name,
            message=schedule.message,
            source=ExecutionSource.SCHEDULE,
            source_user_id=str(schedule.owner_id) if schedule.owner_id else None
        )

        # Track schedule start activity
        schedule_activity_id = await activity_service.track_activity(
            agent_name=schedule.agent_name,
            activity_type=ActivityType.SCHEDULE_START,
            user_id=schedule.owner_id,
            triggered_by="schedule",
            details={
                "schedule_id": schedule.id,
                "schedule_name": schedule.name,
                "cron_expression": schedule.cron_expression,
                "queue_execution_id": queue_execution.id
            }
        )

        # Create execution record
        execution = db.create_schedule_execution(
            schedule_id=schedule.id,
            agent_name=schedule.agent_name,
            message=schedule.message,
            triggered_by="schedule"
        )

        if not execution:
            logger.error(f"Failed to create execution record for schedule {schedule_id}")
            return

        # Broadcast execution started
        await self._broadcast({
            "type": "schedule_execution_started",
            "agent": schedule.agent_name,
            "schedule_id": schedule.id,
            "execution_id": execution.id,
            "schedule_name": schedule.name
        })

        # Submit to queue (will wait if agent is busy)
        execution_success = False
        try:
            queue_result, queue_execution = await queue.submit(queue_execution, wait_if_busy=True)
            logger.info(f"[Schedule] Agent '{schedule.agent_name}' execution {queue_execution.id}: {queue_result}")
        except QueueFullError as e:
            error_msg = f"Agent queue full ({e.queue_length} waiting), skipping scheduled execution"
            logger.warning(f"[Schedule] {error_msg}")
            db.update_execution_status(
                execution_id=execution.id,
                status="failed",
                error=error_msg
            )
            await activity_service.complete_activity(
                activity_id=schedule_activity_id,
                status="failed",
                error=error_msg
            )
            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "failed",
                "error": error_msg
            })
            return

        try:
            # Send message to agent
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"http://agent-{schedule.agent_name}:8000/api/chat",
                    json={"message": schedule.message, "stream": False},
                    timeout=300.0  # 5 minute timeout for scheduled tasks
                )
                response.raise_for_status()
                result = response.json()

            # Extract response text
            response_text = result.get("response", str(result))
            if len(response_text) > 10000:
                response_text = response_text[:10000] + "... (truncated)"

            # Extract observability data from agent response
            session_data = result.get("session", {})
            metadata = result.get("metadata", {})
            execution_log = result.get("execution_log", [])

            # Context usage - use session_data.context_tokens (preferred) or metadata.input_tokens
            # NOTE: cache_creation_tokens and cache_read_tokens are SUBSETS of input_tokens
            # for billing purposes, NOT additional tokens. Do NOT sum them.
            context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
            context_max = session_data.get("context_window") or metadata.get("context_window", 200000)

            # Cost
            cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

            # Tool calls summary
            tool_calls_json = None
            if execution_log:
                import json
                tool_calls_json = json.dumps(execution_log)

            # Update execution status
            db.update_execution_status(
                execution_id=execution.id,
                status="success",
                response=response_text,
                context_used=context_used,
                context_max=context_max,
                cost=cost,
                tool_calls=tool_calls_json
            )

            # Update schedule last run time
            now = datetime.utcnow()
            next_run = self._get_next_run_time(schedule.cron_expression, schedule.timezone)
            db.update_schedule_run_times(schedule.id, last_run_at=now, next_run_at=next_run)

            logger.info(f"Schedule {schedule.name} executed successfully")
            execution_success = True

            # Track schedule completion
            await activity_service.complete_activity(
                activity_id=schedule_activity_id,
                status="completed",
                details={
                    "related_execution_id": execution.id,
                    "context_used": context_used,
                    "context_max": context_max,
                    "cost_usd": cost,
                    "tool_count": len(execution_log) if execution_log else 0,
                    "queue_execution_id": queue_execution.id
                }
            )

            # Broadcast execution completed
            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "success"
            })

        except httpx.HTTPError as e:
            error_msg = f"HTTP error: {str(e)}"
            logger.error(f"Schedule {schedule.name} execution failed: {error_msg}")

            # Track schedule failure
            await activity_service.complete_activity(
                activity_id=schedule_activity_id,
                status="failed",
                error=error_msg,
                details={"related_execution_id": execution.id}
            )

            db.update_execution_status(
                execution_id=execution.id,
                status="failed",
                error=error_msg
            )

            # Broadcast execution failed
            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "failed",
                "error": error_msg
            })

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Schedule {schedule.name} execution failed: {error_msg}")

            # Track schedule failure
            await activity_service.complete_activity(
                activity_id=schedule_activity_id,
                status="failed",
                error=error_msg,
                details={"related_execution_id": execution.id}
            )

            db.update_execution_status(
                execution_id=execution.id,
                status="failed",
                error=error_msg
            )

            # Broadcast execution failed
            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution.id,
                "status": "failed",
                "error": error_msg
            })

        finally:
            # Always release the queue slot when done
            await queue.complete(schedule.agent_name, success=execution_success)

    async def trigger_schedule(self, schedule_id: str) -> Optional[str]:
        """
        Manually trigger a schedule execution.

        Returns the execution ID if successful, None otherwise.
        """
        schedule = db.get_schedule(schedule_id)
        if not schedule:
            return None

        logger.info(f"Manually triggering schedule: {schedule.name}")

        # Create execution record
        execution = db.create_schedule_execution(
            schedule_id=schedule.id,
            agent_name=schedule.agent_name,
            message=schedule.message,
            triggered_by="manual"
        )

        if not execution:
            return None

        # Broadcast execution started
        await self._broadcast({
            "type": "schedule_execution_started",
            "agent": schedule.agent_name,
            "schedule_id": schedule.id,
            "execution_id": execution.id,
            "schedule_name": schedule.name,
            "triggered_by": "manual"
        })

        # Execute in background
        asyncio.create_task(self._execute_manual_trigger(schedule, execution.id))

        return execution.id

    async def _execute_manual_trigger(self, schedule: Schedule, execution_id: str):
        """Execute a manually triggered schedule. Uses execution queue."""
        # Create queue execution request
        queue = get_execution_queue()
        queue_execution = queue.create_execution(
            agent_name=schedule.agent_name,
            message=schedule.message,
            source=ExecutionSource.SCHEDULE,
            source_user_id=str(schedule.owner_id) if schedule.owner_id else None
        )

        # Submit to queue (will wait if agent is busy)
        execution_success = False
        try:
            queue_result, queue_execution = await queue.submit(queue_execution, wait_if_busy=True)
            logger.info(f"[ManualTrigger] Agent '{schedule.agent_name}' execution {queue_execution.id}: {queue_result}")
        except QueueFullError as e:
            error_msg = f"Agent queue full ({e.queue_length} waiting)"
            logger.warning(f"[ManualTrigger] {error_msg}")
            db.update_execution_status(
                execution_id=execution_id,
                status="failed",
                error=error_msg
            )
            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution_id,
                "status": "failed",
                "error": error_msg
            })
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"http://agent-{schedule.agent_name}:8000/api/chat",
                    json={"message": schedule.message, "stream": False},
                    timeout=300.0
                )
                response.raise_for_status()
                result = response.json()

            response_text = result.get("response", str(result))
            if len(response_text) > 10000:
                response_text = response_text[:10000] + "... (truncated)"

            # Extract observability data from agent response
            session_data = result.get("session", {})
            metadata = result.get("metadata", {})
            execution_log = result.get("execution_log", [])

            # Context usage - use session_data.context_tokens (preferred) or metadata.input_tokens
            # NOTE: cache_creation_tokens and cache_read_tokens are SUBSETS of input_tokens
            # for billing purposes, NOT additional tokens. Do NOT sum them.
            context_used = session_data.get("context_tokens") or metadata.get("input_tokens", 0)
            context_max = session_data.get("context_window") or metadata.get("context_window", 200000)

            # Cost
            cost = metadata.get("cost_usd") or session_data.get("total_cost_usd")

            # Tool calls summary
            tool_calls_json = None
            if execution_log:
                import json
                tool_calls_json = json.dumps(execution_log)

            db.update_execution_status(
                execution_id=execution_id,
                status="success",
                response=response_text,
                context_used=context_used,
                context_max=context_max,
                cost=cost,
                tool_calls=tool_calls_json
            )

            execution_success = True

            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution_id,
                "status": "success"
            })

        except Exception as e:
            error_msg = str(e)
            db.update_execution_status(
                execution_id=execution_id,
                status="failed",
                error=error_msg
            )

            await self._broadcast({
                "type": "schedule_execution_completed",
                "agent": schedule.agent_name,
                "schedule_id": schedule.id,
                "execution_id": execution_id,
                "status": "failed",
                "error": error_msg
            })

        finally:
            # Always release the queue slot when done
            await queue.complete(schedule.agent_name, success=execution_success)

    def add_schedule(self, schedule: Schedule):
        """Add a new schedule to the scheduler."""
        if schedule.enabled:
            self._add_job(schedule)

    def remove_schedule(self, schedule_id: str):
        """Remove a schedule from the scheduler."""
        self._remove_job(schedule_id)

    def update_schedule(self, schedule: Schedule):
        """Update an existing schedule in the scheduler."""
        self._remove_job(schedule.id)
        if schedule.enabled:
            self._add_job(schedule)

    def enable_schedule(self, schedule_id: str):
        """Enable a schedule and add it to the scheduler."""
        schedule = db.get_schedule(schedule_id)
        if schedule:
            db.set_schedule_enabled(schedule_id, True)
            schedule.enabled = True
            self._add_job(schedule)

    def disable_schedule(self, schedule_id: str):
        """Disable a schedule and remove it from the scheduler."""
        db.set_schedule_enabled(schedule_id, False)
        self._remove_job(schedule_id)

    def get_scheduler_status(self) -> Dict:
        """Get scheduler status information."""
        if not self.scheduler:
            return {"running": False, "jobs": 0}

        jobs = self.scheduler.get_jobs()
        return {
            "running": self.scheduler.running,
            "jobs": len(jobs),
            "job_list": [
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None
                }
                for job in jobs
            ]
        }


# Global scheduler instance
scheduler_service = SchedulerService()
