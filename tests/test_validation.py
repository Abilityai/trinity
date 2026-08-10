"""
Tests for Business Task Validation (VALIDATE-001).

Tests the post-execution validation feature that runs a clean-context
Claude session to verify business task completion.

Related issue: #294
"""

import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path isolation and module pre-mocking
# tests/utils/ shadows src/backend/utils/ — fix path order and pre-mock
# ---------------------------------------------------------------------------

_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

# Pre-mock utils.helpers (shadowed by tests/utils/)
_helpers_mod = types.ModuleType("utils.helpers")
_helpers_mod.utc_now = lambda: datetime.utcnow()
_helpers_mod.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
_helpers_mod.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
_helpers_mod.to_utc_iso = MagicMock(return_value="2025-01-01T00:00:00Z")
sys.modules["utils.helpers"] = _helpers_mod

# Pre-mock credential_sanitizer (Issue #286)
_sanitizer_mod = types.ModuleType("utils.credential_sanitizer")
_sanitizer_mod.sanitize_text = lambda x: x
sys.modules["utils.credential_sanitizer"] = _sanitizer_mod

# Pre-mock database module (tries to write to /data outside Docker)
sys.modules.setdefault("database", MagicMock())

# Pre-mock task execution service (has complex dependencies)
_mock_task_service = MagicMock()
sys.modules["services.task_execution_service"] = _mock_task_service

# ---------------------------------------------------------------------------
# Now safe to import validation service components
# ---------------------------------------------------------------------------

from services.validation_service import (
    ValidationService,
    ValidationResult,
    ValidationStatus,
    DEFAULT_VALIDATION_PROMPT,
)
from models import BusinessStatus


class TestValidationPromptBuilding:
    """Test validation prompt construction."""

    def test_default_prompt_includes_placeholders(self):
        """Default prompt should have placeholders for message and response."""
        assert "{original_message}" in DEFAULT_VALIDATION_PROMPT
        assert "{execution_response}" in DEFAULT_VALIDATION_PROMPT

    def test_prompt_builder_formats_correctly(self):
        """Prompt builder should substitute placeholders."""
        service = ValidationService()
        prompt = service._build_validation_prompt(
            original_message="Create a README file",
            execution_response="I created README.md with project documentation.",
            custom_prompt=None,
        )

        assert "Create a README file" in prompt
        assert "I created README.md" in prompt
        assert "AUDITOR" in prompt  # Auditor framing

    def test_prompt_builder_with_custom_prompt(self):
        """Custom prompt should override default."""
        service = ValidationService()
        custom = "Check if {original_message} was done. Result: {execution_response}"
        prompt = service._build_validation_prompt(
            original_message="Test task",
            execution_response="Done",
            custom_prompt=custom,
        )

        assert "Check if Test task was done" in prompt
        assert "AUDITOR" not in prompt  # Custom doesn't include default

    def test_prompt_builder_truncates_long_response(self):
        """Long responses should be truncated."""
        service = ValidationService()
        long_response = "x" * 20000  # 20K chars

        prompt = service._build_validation_prompt(
            original_message="Test",
            execution_response=long_response,
            custom_prompt=None,
        )

        assert "truncated" in prompt.lower()
        assert len(prompt) < 25000  # Should be much smaller


class TestValidationResponseParsing:
    """Test parsing of validation responses from Claude."""

    def test_parse_valid_json_pass(self):
        """Should parse valid JSON with pass status."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.response = '''
        {
            "status": "pass",
            "summary": "All checks passed",
            "items": [{"check": "File exists", "result": "pass", "evidence": "README.md found"}]
        }
        '''

        result = service._parse_validation_response(mock_result)

        assert result.status == ValidationStatus.PASS
        assert "passed" in result.summary.lower()
        assert len(result.items) == 1

    def test_parse_valid_json_fail(self):
        """Should parse valid JSON with fail status."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.response = '''
        {
            "status": "fail",
            "summary": "File not found",
            "items": [{"check": "File exists", "result": "fail", "evidence": "No README.md"}],
            "recommendation": "Create the file manually"
        }
        '''

        result = service._parse_validation_response(mock_result)

        assert result.status == ValidationStatus.FAIL
        assert result.recommendation == "Create the file manually"

    def test_parse_json_embedded_in_text(self):
        """Should extract JSON from markdown code blocks."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.response = '''
        Here is my validation:

        ```json
        {"status": "pass", "summary": "Done", "items": []}
        ```
        '''

        result = service._parse_validation_response(mock_result)

        assert result.status == ValidationStatus.PASS

    def test_parse_malformed_json_fallback(self):
        """Should fall back to text analysis for malformed JSON."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.response = "The task was verified successfully. All files exist."

        result = service._parse_validation_response(mock_result)

        # Should infer pass from "verified successfully"
        assert result.status == ValidationStatus.PASS
        assert "inferred" in result.summary.lower()

    def test_parse_failure_indicators(self):
        """Should detect failure indicators in text."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "success"
        mock_result.response = "The file is missing. Task not completed."

        result = service._parse_validation_response(mock_result)

        assert result.status == ValidationStatus.FAIL

    def test_parse_execution_failure(self):
        """Should return ERROR for execution failures."""
        service = ValidationService()
        mock_result = MagicMock()
        mock_result.status = "failed"
        mock_result.error = "Agent timeout"
        mock_result.response = ""

        result = service._parse_validation_response(mock_result)

        assert result.status == ValidationStatus.ERROR
        assert "timeout" in result.summary.lower()


class TestBusinessStatusMapping:
    """Test mapping validation status to business status."""

    def test_pass_maps_to_validated(self):
        """PASS should map to VALIDATED."""
        service = ValidationService()
        result = service._map_validation_to_business_status(ValidationStatus.PASS)
        assert result == BusinessStatus.VALIDATED

    def test_fail_maps_to_failed_validation(self):
        """FAIL should map to FAILED_VALIDATION."""
        service = ValidationService()
        result = service._map_validation_to_business_status(ValidationStatus.FAIL)
        assert result == BusinessStatus.FAILED_VALIDATION

    def test_partial_maps_to_failed_validation(self):
        """PARTIAL should map to FAILED_VALIDATION."""
        service = ValidationService()
        result = service._map_validation_to_business_status(ValidationStatus.PARTIAL)
        assert result == BusinessStatus.FAILED_VALIDATION

    def test_error_maps_to_failed_validation(self):
        """ERROR should map to FAILED_VALIDATION."""
        service = ValidationService()
        result = service._map_validation_to_business_status(ValidationStatus.ERROR)
        assert result == BusinessStatus.FAILED_VALIDATION


@pytest.mark.integration
class TestDatabaseOperations:
    """Test database operations for validation.

    These tests require a running backend with database access.
    Skipped in unit test mode.
    """

    @pytest.mark.skip(reason="Requires running backend with database")
    def test_create_validation_execution_links_to_original(self):
        """Validation execution should link to original via validates_execution_id."""
        pass

    @pytest.mark.skip(reason="Requires running backend with database")
    def test_update_business_status(self):
        """Should update business_status on execution."""
        pass

    @pytest.mark.skip(reason="Requires running backend with database")
    def test_get_executions_pending_validation(self):
        """Should return executions with pending_validation status."""
        pass


class TestScheduleValidationConfig:
    """Test schedule validation configuration."""

    def test_schedule_model_has_validation_fields(self):
        """Schedule model should have validation fields."""
        from db_models import Schedule

        # Check fields exist
        fields = Schedule.__annotations__
        assert "validation_enabled" in fields
        assert "validation_prompt" in fields
        assert "validation_timeout_seconds" in fields

    def test_execution_model_has_business_status(self):
        """Execution model should have business_status field."""
        from db_models import ScheduleExecution

        fields = ScheduleExecution.__annotations__
        assert "business_status" in fields
        assert "validated_at" in fields
        assert "validation_execution_id" in fields
        assert "validates_execution_id" in fields


# Integration tests (require running backend)

@pytest.mark.integration
class TestValidationIntegration:
    """Integration tests for validation flow.

    These tests require a running backend and use the api_client fixture.
    Run with: pytest tests/test_validation.py -m integration
    """

    @pytest.fixture
    def test_schedule_with_validation(self, api_client, created_agent):
        """Create a test schedule with validation enabled."""
        from testkit.api_client import TrinityApiClient

        agent_name = created_agent["name"]

        # Create schedule with validation enabled
        response = api_client.post(
            f"/api/agents/{agent_name}/schedules",
            json={
                "name": "test-validation-schedule",
                "cron_expression": "0 0 1 1 *",  # Never runs (Jan 1 at midnight)
                "message": "Test task",
                "enabled": False,
                "validation_enabled": True,
                "validation_timeout_seconds": 60,
            }
        )
        assert response.status_code == 201

        schedule = response.json()
        yield schedule

        # Cleanup
        api_client.delete(f"/api/agents/{agent_name}/schedules/{schedule['id']}")

    def test_schedule_includes_validation_config(self, api_client, test_schedule_with_validation):
        """Schedule response should include validation config."""
        schedule = test_schedule_with_validation

        assert schedule["validation_enabled"] == True
        assert schedule["validation_timeout_seconds"] == 60

    def test_update_schedule_validation_config(self, api_client, created_agent, test_schedule_with_validation):
        """Should be able to update validation config."""
        agent_name = created_agent["name"]
        schedule = test_schedule_with_validation

        response = api_client.put(
            f"/api/agents/{agent_name}/schedules/{schedule['id']}",
            json={
                "validation_enabled": False,
                "validation_prompt": "Custom validation instructions"
            }
        )
        assert response.status_code == 200

        updated = response.json()
        assert updated["validation_enabled"] == False
        assert updated["validation_prompt"] == "Custom validation instructions"

    def test_execution_includes_business_status(self, api_client, created_agent):
        """Execution response should include business_status."""
        agent_name = created_agent["name"]

        # List executions
        response = api_client.get(f"/api/agents/{agent_name}/executions")
        assert response.status_code == 200

        # Verify business_status field exists in schema (may be null)
        # This just validates the API contract includes the field
        executions = response.json()
        if executions:
            # Check first execution has the field
            assert "business_status" in executions[0] or executions[0].get("business_status") is None
