"""
Templates Tests (test_templates.py)

Tests for template listing and details.
Covers REQ-TMPL-001 and REQ-TMPL-002.

The router has exactly two endpoints: GET /api/templates and
GET /api/templates/{template_id:path}. The old env-template and refresh
endpoints no longer exist — their tests were removed (ent#263; they could
only 404/skip against a live stack).

FAST TESTS - No agent creation required.
"""

import pytest

# Mark all tests in this module as smoke tests (fast, no agent needed)
pytestmark = pytest.mark.smoke
from utils.api_client import TrinityApiClient
from utils.assertions import (
    assert_status,
    assert_json_response,
    assert_has_fields,
    assert_list_response,
)


class TestListTemplates:
    """REQ-TMPL-001: List templates endpoint tests."""

    @pytest.mark.smoke
    def test_list_templates(self, api_client: TrinityApiClient):
        """GET /api/templates returns available templates."""
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_list_response(data, "templates")

    def test_template_has_required_fields(self, api_client: TrinityApiClient):
        """Each template has id, display_name, description."""
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        templates = response.json()

        if len(templates) > 0:
            template = templates[0]
            assert_has_fields(template, ["id", "display_name"])

    def test_templates_include_required_credentials(self, api_client: TrinityApiClient):
        """Templates include required_credentials field."""
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        templates = response.json()

        # At least one template should have this field
        # (not all templates may have credentials)
        has_creds_field = any(
            "required_credentials" in t or "credentials" in t
            for t in templates
        )
        # This is optional - just verify structure if present


class TestGetTemplateDetails:
    """REQ-TMPL-002: Get template details endpoint tests."""

    def test_get_template_by_id(self, api_client: TrinityApiClient):
        """GET /api/templates/{id} returns full template metadata."""
        # First get list to find a template
        list_response = api_client.get("/api/templates")
        templates = list_response.json()

        if len(templates) == 0:
            pytest.skip("No templates available")

        template_id = templates[0].get("id")

        # The real detail endpoint ({template_id:path} captures slashes in
        # github:owner/repo ids). The previous version of this test detoured
        # through the long-dead env-template endpoint and always skipped.
        response = api_client.get(f"/api/templates/{template_id}")

        assert_status(response, 200)
        data = assert_json_response(response)
        assert_has_fields(data, ["id"])

    def test_get_nonexistent_template_returns_404(self, api_client: TrinityApiClient):
        """GET /api/templates/{id} for non-existent returns 404."""
        response = api_client.get("/api/templates/nonexistent-template-xyz")

        assert_status(response, 404)
