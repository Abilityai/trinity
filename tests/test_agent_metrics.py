"""
Agent Custom Metrics Tests (test_agent_metrics.py)

Tests for Trinity agent custom metrics endpoint.
Covers REQ-METRICS-001 (9.9 Agent Custom Metrics) and §49 (the read contract).

Feature Flow: agent-custom-metrics.md

Live tier: these run against a real stack. `GET /api/agents/{name}/metrics` is
the ent#479 **store-only** read — definitions (ent#477) joined to the points
recorded under their names (ent#478), with one stale rule. It no longer proxies
`metrics.json` out of the container, so the legacy `status` / `has_metrics` /
`values` body is gone and a STOPPED agent answers exactly like a running one.
"""

import pytest

from testkit.api_client import TrinityApiClient
from testkit.assertions import (
    assert_status,
    assert_status_in,
    assert_json_response,
    assert_has_fields,
)

# The ent#479 body. `status` and `has_metrics` are deliberately NOT here — the
# read is store-only, so there is no container status to report.
BODY_FIELDS = [
    "agent_name",
    "declared",
    "window",
    "generated_at",
    "metrics",
    "findings",
    "findings_evaluated_at",
    "policy",
    "stale_rule",
    "message",
]

RETIRED_BODY_FIELDS = ["status", "has_metrics", "values", "definitions"]

VALID_TYPES = ["counter", "gauge", "percentage", "status", "duration", "bytes"]
VALID_FRESHNESS = ["fresh", "stale", "no_cadence", "no_points"]


class TestAgentMetricsAuthentication:
    """Tests for agent metrics authentication requirements."""

    pytestmark = pytest.mark.smoke

    def test_metrics_requires_auth(self, unauthenticated_client: TrinityApiClient):
        """GET /api/agents/{name}/metrics requires authentication."""
        response = unauthenticated_client.get("/api/agents/test-agent/metrics", auth=False)
        assert_status(response, 401)


class TestAgentMetricsEndpoint:
    """Tests for GET /api/agents/{name}/metrics endpoint (ent#479 shape)."""

    def test_metrics_returns_structure(self, api_client: TrinityApiClient, created_agent: dict):
        """The read returns the ent#479 body, not the retired proxy body."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = assert_json_response(response)

        assert_has_fields(data, BODY_FIELDS)
        assert data["agent_name"] == agent_name
        assert isinstance(data["declared"], bool)
        assert isinstance(data["metrics"], list)
        assert isinstance(data["findings"], list)

    def test_retired_proxy_fields_are_gone(self, api_client: TrinityApiClient, created_agent: dict):
        """`status` / `has_metrics` / `values` were the metrics.json proxy's body (D-010)."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        present = [field for field in RETIRED_BODY_FIELDS if field in data]
        assert present == [], (
            f"retired metrics.json proxy fields still in the body: {present}"
        )

    def test_stale_rule_is_stated(self, api_client: TrinityApiClient, created_agent: dict):
        """The body names the one stale rule so no consumer invents a second one."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        assert data["stale_rule"] == "2x cadence"

    def test_window_is_resolved(self, api_client: TrinityApiClient, created_agent: dict):
        """Every read states the window it resolved, including the default `auto`."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        window = response.json()["window"]

        assert_has_fields(window, ["kind", "since", "until", "hours"])
        assert isinstance(window["hours"], (int, float))
        assert window["hours"] > 0

    def test_explicit_window_is_honoured(self, api_client: TrinityApiClient, created_agent: dict):
        """A named window comes back as the window that was resolved."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics?window=7d")

        assert_status(response, 200)
        assert response.json()["window"]["kind"] == "7d"

    def test_invalid_window_is_named_422(self, api_client: TrinityApiClient, created_agent: dict):
        """Bad input fails with a named reason code, never a generic 500."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics?window=fortnight")

        assert_status(response, 422)
        detail = response.json().get("detail")
        assert isinstance(detail, dict), f"expected a typed detail, got {detail!r}"
        assert detail.get("reason") == "window_invalid"

    def test_undeclared_metric_filter_is_named_422(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """Filtering on a metric the agent never declared names the fix."""
        agent_name = created_agent["name"]
        response = api_client.get(
            f"/api/agents/{agent_name}/metrics?metric=definitely-not-declared"
        )

        assert_status(response, 422)
        detail = response.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("reason") == "metric_undeclared"

    def test_empty_state_names_the_next_action(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """An agent declaring nothing gets copy naming the next step, never a blank."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        if not data["declared"]:
            assert data["message"], "undeclared agent must carry empty-state copy"
            assert "template.yaml" in data["message"]
        else:
            assert data["message"] is None

    def test_nonexistent_agent_returns_404(self, api_client: TrinityApiClient):
        """GET /api/agents/{name}/metrics returns 404 for nonexistent agent."""
        response = api_client.get("/api/agents/nonexistent-agent-xyz123/metrics")
        assert_status(response, 404)


class TestAgentMetricsStopped:
    """A stopped agent answers exactly like a running one — the read is store-only."""

    def test_stopped_agent_metrics(self, api_client: TrinityApiClient, stopped_agent: dict):
        """Stopped agents keep their numbers (the proxy used to 400 here)."""
        agent_name = stopped_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        assert_has_fields(data, BODY_FIELDS)
        assert data["agent_name"] == agent_name
        assert "status" not in data
        assert "has_metrics" not in data


class TestMetricEntries:
    """Each metric entry: registry identity + latest + freshness + series."""

    def _metrics(self, api_client: TrinityApiClient, agent_name: str) -> list:
        response = api_client.get(f"/api/agents/{agent_name}/metrics")
        assert_status(response, 200)
        return response.json()["metrics"]

    def test_entry_shape(self, api_client: TrinityApiClient, created_agent: dict):
        """Every entry carries identity, freshness and the series block."""
        for entry in self._metrics(api_client, created_agent["name"]):
            assert_has_fields(entry, [
                "name", "type", "label", "unit", "direction", "aggregation",
                "cadence_seconds", "status", "last_point_at", "stale",
                "freshness", "stale_after", "series_count", "latest",
                "latest_by_series", "series", "chart", "stats",
            ])

    def test_metric_type_validation(self, api_client: TrinityApiClient, created_agent: dict):
        """Metric definitions have valid types."""
        for entry in self._metrics(api_client, created_agent["name"]):
            assert entry["type"] in VALID_TYPES, \
                f"Metric type '{entry['type']}' should be one of {VALID_TYPES}"

    def test_freshness_vocabulary(self, api_client: TrinityApiClient, created_agent: dict):
        """`freshness` is the one stale rule's vocabulary; `stale` may be None."""
        for entry in self._metrics(api_client, created_agent["name"]):
            assert entry["freshness"] in VALID_FRESHNESS
            assert entry["stale"] in (True, False, None)

    def test_never_measured_is_not_stale(self, api_client: TrinityApiClient, created_agent: dict):
        """A metric with no points reads `no_points`, never `stale` (§49.1)."""
        for entry in self._metrics(api_client, created_agent["name"]):
            if entry["latest"] is None:
                assert entry["freshness"] == "no_points"
                assert entry["stale"] is False
                assert entry["last_point_at"] is None
                assert entry["message"], "an empty metric names how to record one"

    def test_no_cadence_cannot_be_stale(self, api_client: TrinityApiClient, created_agent: dict):
        """Without a declared cadence there is no deadline to miss."""
        for entry in self._metrics(api_client, created_agent["name"]):
            if not entry.get("cadence_seconds") and entry["latest"] is not None:
                assert entry["freshness"] == "no_cadence"
                assert entry["stale"] is None
                assert entry["stale_after"] is None

    def test_latest_shape(self, api_client: TrinityApiClient, created_agent: dict):
        """`latest` is the folded value across dimension series, with its stamp."""
        for entry in self._metrics(api_client, created_agent["name"]):
            latest = entry["latest"]
            if latest is not None:
                assert_has_fields(latest, ["value", "ts", "dims"])
                assert latest["ts"] == entry["last_point_at"]
                assert entry["series_count"] >= 1

    def test_percentage_metric_value_range(self, api_client: TrinityApiClient, created_agent: dict):
        """Percentage metric values should be 0-100."""
        for entry in self._metrics(api_client, created_agent["name"]):
            latest = entry["latest"]
            if entry["type"] == "percentage" and latest and isinstance(
                    latest["value"], (int, float)):
                assert 0 <= latest["value"] <= 100, \
                    f"Percentage metric '{entry['name']}' should be 0-100, " \
                    f"got {latest['value']}"


class TestRetiredMetrics:
    """`include_retired` is the only way a retired metric appears in the read."""

    def test_default_read_hides_retired(self, api_client: TrinityApiClient, created_agent: dict):
        """A retired number rendering as current is the §49.2 failure."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        for entry in response.json()["metrics"]:
            assert entry["status"] == "active"

    def test_include_retired_is_accepted(self, api_client: TrinityApiClient, created_agent: dict):
        """The flag is honoured and the body shape is unchanged."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics?include_retired=true")

        assert_status(response, 200)
        data = response.json()
        assert_has_fields(data, BODY_FIELDS)
        for entry in data["metrics"]:
            assert entry["status"] in ("active", "retired")


class TestMetricsFindings:
    """Compatibility findings ride the read (D-010 and friends)."""

    def test_findings_are_a_list_of_named_codes(
        self, api_client: TrinityApiClient, created_agent: dict
    ):
        """Findings are actionable sentences keyed by a code, never blanks."""
        agent_name = created_agent["name"]
        response = api_client.get(f"/api/agents/{agent_name}/metrics")

        assert_status(response, 200)
        data = response.json()

        assert isinstance(data["findings"], list)
        for finding in data["findings"]:
            assert_has_fields(finding, ["code"])
            assert finding["code"]
        if data["findings"]:
            assert data["findings_evaluated_at"], \
                "findings must carry the time they were evaluated"


class TestMetricsAccessControl:
    """Tests for metrics endpoint access control."""

    def test_shared_agent_metrics_accessible(self, api_client: TrinityApiClient, created_agent: dict):
        """Shared agents should have accessible metrics."""
        agent_name = created_agent["name"]

        # First share the agent (if we have another user context, we'd test that)
        # For now, just verify owner can access
        response = api_client.get(f"/api/agents/{agent_name}/metrics")
        assert_status(response, 200)

    def test_unowned_unshared_agent_denied(self, api_client: TrinityApiClient):
        """Access to unowned, unshared agent metrics should be denied."""
        # This would require a second user context to properly test
        # For now, just verify 404 for nonexistent agents
        response = api_client.get("/api/agents/other-users-private-agent/metrics")
        assert_status_in(response, [403, 404])
