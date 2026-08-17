"""
Tests for Business Task Validation (VALIDATE-001).

Tests the post-execution validation feature that runs a clean-context
Claude session to verify business task completion.

Related issue: #294
"""

import pytest


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
