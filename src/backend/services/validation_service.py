"""
Validation Service — Post-execution business validation (VALIDATE-001).

Runs a clean-context Claude session after task execution to verify
that the business task was actually completed correctly.

The validation session:
- Runs on the same agent (has access to workspace, tools, state)
- Uses a clean context (new Claude session, no carry-over from execution)
- Receives explicit auditor framing — this is a validation exercise
- Produces structured output: pass/fail with evidence

Flow:
    1. Execution completes successfully (technical status)
    2. If schedule.validation_enabled:
        a. Create validation execution record (linked to original)
        b. Build auditor prompt with execution context
        c. Run validation via TaskExecutionService
        d. Parse response and update business_status
        e. On failure: notify operator queue
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from database import db
from models import BusinessStatus
from services.task_execution_service import TaskExecutionService, TaskExecutionResult
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default validation prompt template
# ---------------------------------------------------------------------------

DEFAULT_VALIDATION_PROMPT = """You are an AUDITOR, not an executor. Your task is to VALIDATE whether the previous execution successfully completed its intended work.

## Important Context
- This is a POST-EXECUTION validation session
- You are reviewing work that was ALREADY ATTEMPTED by this agent in a previous execution
- You have access to the same workspace, tools, and files as the executor
- Your job is to VERIFY completion, not to DO the work

## Original Task
The agent was asked to perform:
```
{original_message}
```

## Execution Output
The execution produced this response:
```
{execution_response}
```

## Your Validation Task
1. Check if the work was actually completed (not just claimed to be done)
2. Verify any artifacts, files, or changes mentioned in the response exist
3. Check for completeness — were all parts of the task addressed?
4. Look for errors, partial completions, or hallucinated success claims

## Response Format
Respond with a JSON object (and nothing else):
```json
{{
  "status": "pass" | "fail" | "partial",
  "summary": "One sentence summary of validation result",
  "items": [
    {{
      "check": "What was verified",
      "result": "pass" | "fail",
      "evidence": "What was found or not found"
    }}
  ],
  "recommendation": "Action to take if failed (optional)"
}}
```

Begin your validation now. Remember: you are AUDITING, not executing."""


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------

class ValidationStatus(str, Enum):
    """Result status from validation."""
    PASS = "pass"
    FAIL = "fail"
    PARTIAL = "partial"
    ERROR = "error"  # Validation itself failed (timeout, parse error, etc.)


@dataclass
class ValidationResult:
    """Parsed result from validation execution."""
    status: ValidationStatus
    summary: str
    items: list  # List of {check, result, evidence} dicts
    recommendation: Optional[str] = None
    raw_response: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation Service
# ---------------------------------------------------------------------------

class ValidationService:
    """Service for running post-execution business validation."""

    def __init__(self, task_execution_service: TaskExecutionService = None):
        """Initialize validation service.

        Args:
            task_execution_service: Optional TaskExecutionService instance.
                If not provided, creates a new one.
        """
        self._task_service = task_execution_service or TaskExecutionService()

    async def validate_execution(
        self,
        execution_id: str,
        agent_name: str,
        schedule_id: str,
        original_message: str,
        execution_response: str,
        custom_prompt: Optional[str] = None,
        timeout_seconds: int = 120,
    ) -> ValidationResult:
        """Run validation on a completed execution.

        Creates a validation execution record, runs the auditor session,
        parses the result, and updates the original execution's business_status.

        Args:
            execution_id: The original execution to validate.
            agent_name: The agent to run validation on.
            schedule_id: The schedule that triggered the original execution.
            original_message: The original task message.
            execution_response: The response from the original execution.
            custom_prompt: Optional custom auditor prompt (uses default if None).
            timeout_seconds: Timeout for validation task.

        Returns:
            ValidationResult with status, summary, and item details.
        """
        # 1. Mark original execution as pending validation
        db.update_business_status(execution_id, BusinessStatus.PENDING_VALIDATION)

        # 2. Build the auditor prompt
        validation_prompt = self._build_validation_prompt(
            original_message=original_message,
            execution_response=execution_response,
            custom_prompt=custom_prompt,
        )

        # 3. Create validation execution record
        validation_execution = db.create_validation_execution(
            validates_execution_id=execution_id,
            agent_name=agent_name,
            schedule_id=schedule_id,
            message=validation_prompt[:500] + "..." if len(validation_prompt) > 500 else validation_prompt,
            timeout_seconds=timeout_seconds,
        )

        if not validation_execution:
            logger.error(f"Failed to create validation execution for {execution_id}")
            return ValidationResult(
                status=ValidationStatus.ERROR,
                summary="Failed to create validation execution record",
                items=[],
                raw_response=None,
            )

        validation_execution_id = validation_execution.id

        try:
            # 4. Run validation via TaskExecutionService
            result = await self._task_service.execute_task(
                agent_name=agent_name,
                message=validation_prompt,
                triggered_by="validation",
                timeout_seconds=timeout_seconds,
                execution_id=validation_execution_id,
            )

            # 5. Parse validation response
            validation_result = self._parse_validation_response(result)

            # 6. Update business status based on result
            business_status = self._map_validation_to_business_status(validation_result.status)
            db.update_business_status(
                execution_id=execution_id,
                business_status=business_status,
                validation_execution_id=validation_execution_id,
            )

            # 7. Notify operator on failure
            if validation_result.status in (ValidationStatus.FAIL, ValidationStatus.PARTIAL):
                await self._notify_operator_on_failure(
                    execution_id=execution_id,
                    agent_name=agent_name,
                    validation_result=validation_result,
                )

            return validation_result

        except Exception as e:
            logger.error(f"Validation failed for execution {execution_id}: {e}")

            # Mark as failed validation
            db.update_business_status(
                execution_id=execution_id,
                business_status=BusinessStatus.FAILED_VALIDATION,
                validation_execution_id=validation_execution_id,
            )

            return ValidationResult(
                status=ValidationStatus.ERROR,
                summary=f"Validation error: {str(e)}",
                items=[],
                raw_response=None,
            )

    def _build_validation_prompt(
        self,
        original_message: str,
        execution_response: str,
        custom_prompt: Optional[str] = None,
    ) -> str:
        """Build the auditor prompt for validation.

        Args:
            original_message: The original task message.
            execution_response: The response from the original execution.
            custom_prompt: Optional custom prompt template.

        Returns:
            The formatted validation prompt.
        """
        template = custom_prompt or DEFAULT_VALIDATION_PROMPT

        # Truncate response if too long (keep first 10K chars)
        truncated_response = execution_response
        if execution_response and len(execution_response) > 10000:
            truncated_response = execution_response[:10000] + "\n\n[... response truncated for validation ...]"

        return template.format(
            original_message=original_message,
            execution_response=truncated_response or "(no response)",
        )

    def _parse_validation_response(self, result: TaskExecutionResult) -> ValidationResult:
        """Parse the validation response from Claude.

        Attempts to extract JSON from the response. Falls back to text analysis
        if JSON parsing fails.

        Args:
            result: The TaskExecutionResult from validation execution.

        Returns:
            Parsed ValidationResult.
        """
        raw_response = result.response or ""

        # Check for execution failure. #679: a CANCELLED validation turn never
        # actually validated anything — treat it as a non-success ERROR, not an
        # empty response to be parsed as a validation verdict.
        if result.status in ("failed", "cancelled"):
            return ValidationResult(
                status=ValidationStatus.ERROR,
                summary=f"Validation execution {result.status}: {result.error or 'Unknown error'}",
                items=[],
                raw_response=raw_response,
            )

        # Try to extract JSON from response
        try:
            # Look for JSON object in the response
            json_match = re.search(r'\{[\s\S]*\}', raw_response)
            if json_match:
                data = json.loads(json_match.group())

                status_str = data.get("status", "error").lower()
                status = ValidationStatus(status_str) if status_str in [s.value for s in ValidationStatus] else ValidationStatus.ERROR

                return ValidationResult(
                    status=status,
                    summary=data.get("summary", "No summary provided"),
                    items=data.get("items", []),
                    recommendation=data.get("recommendation"),
                    raw_response=raw_response,
                )

        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse validation JSON: {e}")

        # Fallback: analyze text for pass/fail indicators
        response_lower = raw_response.lower()

        if any(word in response_lower for word in ["pass", "successful", "verified", "confirmed", "complete"]):
            if not any(word in response_lower for word in ["fail", "error", "missing", "incomplete", "not found"]):
                return ValidationResult(
                    status=ValidationStatus.PASS,
                    summary="Validation passed (inferred from text)",
                    items=[],
                    raw_response=raw_response,
                )

        if any(word in response_lower for word in ["fail", "error", "missing", "incomplete", "not found", "not done"]):
            return ValidationResult(
                status=ValidationStatus.FAIL,
                summary="Validation failed (inferred from text)",
                items=[],
                raw_response=raw_response,
            )

        # Default to partial if unclear
        return ValidationResult(
            status=ValidationStatus.PARTIAL,
            summary="Validation result unclear — manual review recommended",
            items=[],
            raw_response=raw_response,
        )

    def _map_validation_to_business_status(self, validation_status: ValidationStatus) -> str:
        """Map validation status to business status.

        Args:
            validation_status: The validation result status.

        Returns:
            The corresponding BusinessStatus value.
        """
        mapping = {
            ValidationStatus.PASS: BusinessStatus.VALIDATED,
            ValidationStatus.FAIL: BusinessStatus.FAILED_VALIDATION,
            ValidationStatus.PARTIAL: BusinessStatus.FAILED_VALIDATION,
            ValidationStatus.ERROR: BusinessStatus.FAILED_VALIDATION,
        }
        return mapping.get(validation_status, BusinessStatus.FAILED_VALIDATION)

    async def _notify_operator_on_failure(
        self,
        execution_id: str,
        agent_name: str,
        validation_result: ValidationResult,
    ):
        """Notify operator queue when validation fails.

        Writes to the agent's operator-queue.json file for the
        OperatorQueueSyncService to pick up.

        Args:
            execution_id: The original execution that failed validation.
            agent_name: The agent name.
            validation_result: The validation result details.
        """
        try:
            from services.agent_client import AgentClient

            # Build the notification payload
            notification = {
                "id": f"val_{execution_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
                "type": "alert",
                "priority": "high",
                "status": "pending",
                "title": "Validation Failed",
                "question": f"Execution validation failed: {validation_result.summary}",
                "context": {
                    "execution_id": execution_id,
                    "validation_status": validation_result.status.value,
                    "summary": validation_result.summary,
                    "items": validation_result.items,
                    "recommendation": validation_result.recommendation,
                },
                "created_at": utc_now_iso(),
            }

            # Read existing queue, append, write back
            client = AgentClient(agent_name)
            queue_path = ".trinity/operator-queue.json"

            try:
                result = await client.read_file(queue_path)
                if result.get("success") and result.get("content"):
                    queue_data = json.loads(result["content"])
                else:
                    queue_data = []
            except Exception:
                queue_data = []

            queue_data.append(notification)

            await client.write_file(
                queue_path,
                json.dumps(queue_data, indent=2),
                platform=True  # Allow writes to .trinity directory
            )
            logger.info(f"Added validation failure notification to operator queue for agent '{agent_name}'")

        except Exception as e:
            # Best effort — don't fail validation because notification failed
            logger.warning(f"Failed to notify operator queue for agent '{agent_name}': {e}")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_validation_service: Optional[ValidationService] = None


def get_validation_service() -> ValidationService:
    """Get the singleton ValidationService instance."""
    global _validation_service
    if _validation_service is None:
        _validation_service = ValidationService()
    return _validation_service
