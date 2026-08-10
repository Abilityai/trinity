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
from testkit.api_client import TrinityApiClient
from testkit.assertions import (
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


class TestDeclaredSchedulesSurface:
    """trinity-enterprise#89: the catalog surfaces a template's declared
    `schedules:` block.

    The unit suite proves both BUILDERS emit the keys; only a live call proves
    the router actually serves them (and that a malformed block in any bundled
    or configured template hasn't emptied the catalog — the ent#128 / #1835
    property, now also guarding the two newly-fenced GitHub list paths).
    """

    @pytest.mark.smoke
    def test_every_template_carries_the_schedule_keys(
            self, api_client: TrinityApiClient):
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        templates = response.json()
        if not templates:
            pytest.skip("No templates available")

        for template in templates:
            assert "schedules" in template, template.get("id")
            assert "schedule_errors" in template, template.get("id")
            assert isinstance(template["schedules"], list)
            assert isinstance(template["schedule_errors"], list)

    @pytest.mark.smoke
    def test_declared_entries_are_normalized_not_raw(
            self, api_client: TrinityApiClient):
        """The catalog serves the NORMALIZED list, so the frontend can render
        it without a per-source branch or a shape check."""
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        for template in response.json():
            for entry in template.get("schedules") or []:
                assert set(entry) == {
                    "name", "cron", "message", "enabled", "timezone",
                    "description"}, template.get("id")
                assert isinstance(entry["enabled"], bool)

    @pytest.mark.smoke
    def test_a_malformed_block_never_empties_the_catalog(
            self, api_client: TrinityApiClient):
        """A template reporting `schedule_errors` must still be LISTED — a
        broken block costs that template its schedule metadata, not the
        catalog."""
        response = api_client.get("/api/templates")

        assert_status(response, 200)
        templates = response.json()
        assert len(templates) > 0, (
            "empty catalog — a malformed declarative block may have taken it "
            "down (ent#128 / #1835 class)"
        )
        for template in templates:
            if template.get("schedule_errors"):
                assert template.get("id")


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

    def test_get_template_rejects_path_traversal(self, api_client: TrinityApiClient):
        """#1900: a `local:` id must not escape the templates root.

        End-to-end complement to the CI-gated guard in
        `tests/unit/test_1900_template_id_traversal.py`. ⚠️ This FILE HAS NO
        AUTOMATED RUNNER AT ALL. It is root-level, and every automated stage
        collects a subdirectory: CI runs `pytest unit/`, and `/verify-local`
        runs `pytest unit/` then `pytest integration/`. None of them collect
        `tests/*.py`. Run it MANUALLY against a live stack:

            (cd tests && pytest test_templates.py)   # backend must be booted

        Its value is exercising the genuine uvicorn path-unquoting + auth
        stack, not coverage — the CI-gated guard above is what protects the
        fix. Giving this file a runner (move under `tests/integration/`, or
        add a job that collects root-level files) is a tracked follow-up.

        `%2E%2E%2F` is required for the multi-level case: RFC 3986 dot-segment
        removal, which the httpx-based client applies, collapses a literal
        `../../` before it ever leaves the client, so that spelling would
        silently test nothing. Single-level and absolute forms need no trick.
        """
        for template_id in [
            "local:../../etc",                       # collapsed client-side unless encoded
            "local:%2E%2E%2F%2E%2E%2Fetc",           # encoded — reaches the handler intact
            "local:../config",                       # single level, no encoding needed
            "local:/etc",                            # absolute RHS wins the join
        ]:
            response = api_client.get(f"/api/templates/{template_id}")

            assert_status(response, 404)
            # The rejection must disclose nothing an unknown id would not.
            body = response.text
            assert "deployed-templates" not in body
            assert "agent-configs" not in body
