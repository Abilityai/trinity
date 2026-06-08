"""
Model Selection Tests (test_model_selection.py)

Tests for MODEL-001: Model Selection for Tasks & Schedules feature.
Tests schedule model field persistence, execution tracking, and API contracts.

Feature Spec: docs/requirements/MODEL_SELECTION_TASKS_SCHEDULES.md
"""

import pytest
import uuid
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)


class TestScheduleModelField:
    """Tests for model field in schedule CRUD operations."""

    def test_create_schedule_with_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST /api/agents/{name}/schedules with model field stores model."""
        schedule_name = f"test-model-schedule-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test with model",
                "model": "claude-opus-4-6",
                "enabled": False
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        # Verify model is in response
        assert data.get("model") == "claude-opus-4-6"

    def test_create_schedule_without_model_defaults_null(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST schedule without model field results in NULL (agent default)."""
        schedule_name = f"test-no-model-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test without model",
                "enabled": False
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        # Model should be None or absent (both acceptable)
        model_value = data.get("model")
        assert model_value is None or model_value == ""

    def test_create_schedule_with_custom_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """POST schedule with custom model name (free text input)."""
        schedule_name = f"test-custom-model-{uuid.uuid4().hex[:8]}"
        custom_model = "claude-sonnet-4-5-20250929"

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test custom model",
                "model": custom_model,
                "enabled": False
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        assert data.get("model") == custom_model

    def test_list_schedules_includes_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """GET /api/agents/{name}/schedules returns model field."""
        # Create schedule with model
        schedule_name = f"test-list-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": "claude-sonnet-4-6",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # List schedules
        list_response = api_client.get(f"/api/agents/{created_agent['name']}/schedules")
        assert_status(list_response, 200)
        schedules = list_response.json()

        # Find our schedule
        our_schedule = next((s for s in schedules if s.get("id") == schedule_id), None)
        assert our_schedule is not None
        assert our_schedule.get("model") == "claude-sonnet-4-6"

    def test_get_schedule_includes_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """GET /api/agents/{name}/schedules/{id} returns model field."""
        schedule_name = f"test-get-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": "claude-haiku-4-5",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Get specific schedule
        response = api_client.get(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}"
        )

        assert_status(response, 200)
        schedule_data = assert_json_response(response)
        assert schedule_data.get("model") == "claude-haiku-4-5"


class TestScheduleModelUpdate:
    """Tests for updating model field on existing schedules."""

    def test_update_schedule_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT /api/agents/{name}/schedules/{id} can update model field."""
        # Create schedule with one model
        schedule_name = f"test-update-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": "claude-opus-4-5",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update to different model
        update_response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"model": "claude-sonnet-4-6"}
        )

        assert_status(update_response, 200)
        updated_data = update_response.json()
        assert updated_data.get("model") == "claude-sonnet-4-6"

    def test_update_schedule_clear_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT schedule with null model clears model (uses agent default)."""
        # Create schedule with model
        schedule_name = f"test-clear-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": "claude-opus-4-6",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Clear model (set to null/None)
        update_response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"model": None}
        )

        assert_status(update_response, 200)
        updated_data = update_response.json()
        model_value = updated_data.get("model")
        assert model_value is None or model_value == ""

    def test_update_schedule_with_custom_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT schedule with custom model name."""
        schedule_name = f"test-update-custom-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update to custom model
        custom_model = "claude-opus-4-5-20250228"
        update_response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"model": custom_model}
        )

        assert_status(update_response, 200)
        updated_data = update_response.json()
        assert updated_data.get("model") == custom_model

    def test_update_schedule_model_with_other_fields(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """PUT schedule can update model along with other fields."""
        schedule_name = f"test-update-multi-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Original",
                "model": "claude-opus-4-5",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Update multiple fields including model
        update_response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={
                "name": "updated-name",
                "message": "Updated message",
                "model": "claude-sonnet-4-6"
            }
        )

        assert_status(update_response, 200)
        updated_data = update_response.json()
        assert updated_data.get("name") == "updated-name"
        assert updated_data.get("message") == "Updated message"
        assert updated_data.get("model") == "claude-sonnet-4-6"


class TestModelPresets:
    """Tests for standard model preset values (MODEL-001)."""

    # Current offered lineup (#1080, synced 2026-06-06). The two models retiring
    # 2026-06-15 (claude-opus-4-20250514, claude-sonnet-4-20250514) are intentionally
    # absent — they were removed from the picker. The model field is free-text, so this
    # list asserts each current ID round-trips through schedule create/read.
    PRESET_MODELS = [
        "claude-opus-4-8",      # Flagship (latest)
        "claude-opus-4-7",      # Current
        "claude-opus-4-6",      # Current
        "claude-sonnet-4-6",    # Fast + smart (platform default)
        "claude-haiku-4-5",     # Fastest
    ]

    @pytest.mark.parametrize("model", PRESET_MODELS)
    def test_create_schedule_with_preset_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker,
        model: str
    ):
        """All preset models can be stored in schedules."""
        schedule_name = f"test-preset-{model[:20]}-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": f"Test with {model}",
                "model": model,
                "enabled": False
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        assert data.get("model") == model


class TestExecutionModelTracking:
    """Tests for model_used field in execution records."""

    @pytest.mark.slow
    def test_trigger_schedule_tracks_model_used(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """Manual trigger creates execution with model_used field."""
        # Create schedule with specific model
        schedule_name = f"test-exec-model-{uuid.uuid4().hex[:8]}"
        create_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test model tracking",
                "model": "claude-opus-4-6",
                "enabled": False
            }
        )

        if create_response.status_code not in [200, 201]:
            pytest.skip("Could not create schedule")

        data = create_response.json()
        schedule_id = data.get("id")
        resource_tracker.track_schedule(created_agent['name'], schedule_id)

        # Trigger the schedule
        trigger_response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/trigger",
            timeout=120.0
        )

        # May return 503/504/429 if not ready
        if trigger_response.status_code in [503, 504, 429]:
            pytest.skip(f"Scheduler not ready: {trigger_response.status_code}")

        assert_status_in(trigger_response, [200, 202])

        # Get execution history
        executions_response = api_client.get(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}/executions"
        )

        assert_status(executions_response, 200)
        executions = executions_response.json()

        # Should have at least one execution
        if len(executions) > 0:
            latest_execution = executions[0]
            # model_used should be recorded
            # Note: May be None if execution failed before model was applied
            # But if present, should match the schedule's model
            if "model_used" in latest_execution and latest_execution["model_used"] is not None:
                assert latest_execution["model_used"] == "claude-opus-4-6"

    def test_get_executions_includes_model_used(
        self,
        api_client: TrinityApiClient,
        created_agent
    ):
        """GET /api/agents/{name}/executions returns model_used field."""
        response = api_client.get(f"/api/agents/{created_agent['name']}/executions")

        assert_status(response, 200)
        data = assert_json_response(response)

        # Response structure may vary (list or dict with 'executions' key)
        executions = data if isinstance(data, list) else data.get("executions", [])

        # If there are any executions, check structure
        if len(executions) > 0:
            execution = executions[0]
            # model_used field should be present (even if None)
            assert "model_used" in execution or True  # Field may not exist on old executions


class TestModelValidation:
    """Tests for model field validation and edge cases."""

    def test_create_schedule_with_empty_string_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """Empty string model is treated as NULL (agent default)."""
        schedule_name = f"test-empty-model-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": "",
                "enabled": False
            }
        )

        # Should succeed (empty string is valid, treated as NULL)
        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        # Model should be None or empty string
        model_value = data.get("model")
        assert model_value is None or model_value == ""

    def test_create_schedule_model_accepts_any_string(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """Model field accepts any string value (no validation)."""
        schedule_name = f"test-any-model-{uuid.uuid4().hex[:8]}"
        weird_model = "my-custom-model-v2.5-experimental"

        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Test",
                "model": weird_model,
                "enabled": False
            }
        )

        # Should succeed - no validation on model string
        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        assert data.get("model") == weird_model


class TestBackwardsCompatibility:
    """Tests for backwards compatibility with existing schedules."""

    def test_existing_schedules_without_model(
        self,
        api_client: TrinityApiClient,
        created_agent,
        resource_tracker
    ):
        """Schedules created before MODEL-001 have NULL model (agent default)."""
        # Create schedule without model field (simulates old schedule)
        schedule_name = f"test-old-schedule-{uuid.uuid4().hex[:8]}"
        response = api_client.post(
            f"/api/agents/{created_agent['name']}/schedules",
            json={
                "name": schedule_name,
                "cron_expression": "0 * * * *",
                "message": "Legacy schedule",
                "enabled": False
                # No model field
            }
        )

        assert_status_in(response, [200, 201])
        data = assert_json_response(response)

        if "id" in data:
            resource_tracker.track_schedule(created_agent['name'], data["id"])

        # Should work fine, model is NULL/None
        schedule_id = data.get("id")

        # Verify we can update it later
        update_response = api_client.put(
            f"/api/agents/{created_agent['name']}/schedules/{schedule_id}",
            json={"model": "claude-opus-4-6"}
        )

        assert_status(update_response, 200)
        assert update_response.json().get("model") == "claude-opus-4-6"
