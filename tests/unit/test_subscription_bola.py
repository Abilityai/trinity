"""
Unit tests for subscription BOLA fix (Issue #182).

Verifies that PUT /api/subscriptions/agents/{name} and
DELETE /api/subscriptions/agents/{name} enforce owner/admin-only auth
(NOT any shared user).

INV-8 (#1310) moved the inline `can_user_share_agent`/`can_user_access_agent`
checks behind the shared imperative helpers in dependencies.py:
  * assert_agent_owner  wraps can_user_share_agent  (owner/admin only)
  * assert_agent_access wraps can_user_access_agent (owner/admin/shared)
so this guard now asserts the *helper* each endpoint uses — which is the exact
BOLA-relevant boundary — instead of the raw db-call it used to inline. The
behavioral side (a shared reader is denied at the owner endpoints) is covered by
tests/unit/test_1310_auth_consolidation.py::test_owner_sites_deny_shared_reader.

Issue: https://github.com/abilityai/trinity/issues/182
Module: src/backend/routers/subscriptions.py
"""

import os
import sys
import pytest
import ast

# Path to the router source
ROUTER_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'src', 'backend', 'routers', 'subscriptions.py'
)


class TestSubscriptionBOLA:
    """Issue #182: subscription mutation endpoints must use owner-only auth."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        """Load the router source once."""
        with open(ROUTER_PATH) as f:
            self.source = f.read()
        self.tree = ast.parse(self.source)

    def _get_function_source(self, func_name: str) -> str:
        """Extract the source of a specific function from the AST."""
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    return ast.get_source_segment(self.source, node)
        raise ValueError(f"Function {func_name} not found")

    # ---- PUT /api/subscriptions/agents/{name} ----

    def test_assign_uses_owner_gate(self):
        """assign_subscription_to_agent must gate on assert_agent_owner (owner/admin
        only), NOT assert_agent_access (which would admit any shared user)."""
        src = self._get_function_source("assign_subscription_to_agent")
        assert "assert_agent_owner" in src, (
            "assign_subscription_to_agent must use assert_agent_owner (owner/admin only)"
        )
        assert "assert_agent_access" not in src, (
            "assign_subscription_to_agent must NOT use assert_agent_access (allows shared users)"
        )

    def test_assign_owner_gate_message(self):
        """assign_subscription_to_agent owner-gate detail should mention owner/admin."""
        src = self._get_function_source("assign_subscription_to_agent")
        lower_src = src.lower()
        assert "owner" in lower_src or "admin" in lower_src, (
            "owner-gate detail should mention owner or admin requirement"
        )

    # ---- DELETE /api/subscriptions/agents/{name} ----

    def test_clear_uses_owner_gate(self):
        """clear_agent_subscription must gate on assert_agent_owner, not assert_agent_access."""
        src = self._get_function_source("clear_agent_subscription")
        assert "assert_agent_owner" in src, (
            "clear_agent_subscription must use assert_agent_owner (owner/admin only)"
        )
        assert "assert_agent_access" not in src, (
            "clear_agent_subscription must NOT use assert_agent_access (allows shared users)"
        )

    def test_clear_owner_gate_message(self):
        """clear_agent_subscription owner-gate detail should mention owner/admin."""
        src = self._get_function_source("clear_agent_subscription")
        lower_src = src.lower()
        assert "owner" in lower_src or "admin" in lower_src

    # ---- GET /api/subscriptions/agents/{name}/auth (read-only, should remain permissive) ----

    def test_auth_status_uses_access_gate(self):
        """get_agent_auth_status is read-only and should use assert_agent_access
        (owner/admin/shared), NOT the owner-only assert_agent_owner."""
        src = self._get_function_source("get_agent_auth_status")
        assert "assert_agent_access" in src, (
            "get_agent_auth_status is read-only and should allow shared users"
        )
        assert "assert_agent_owner" not in src, (
            "get_agent_auth_status should NOT restrict to owners only"
        )

    # ---- Global: no regression ----

    def test_no_access_gate_in_mutation_endpoints(self):
        """Ensure the permissive access gate (or the raw can_user_access_agent) is
        NOT used in the assign/clear mutation endpoints — that was the #182 BOLA."""
        for func_name in ["assign_subscription_to_agent", "clear_agent_subscription"]:
            src = self._get_function_source(func_name)
            assert "assert_agent_access" not in src and "can_user_access_agent" not in src, (
                f"{func_name} must not use the permissive access gate — "
                f"this was the BOLA vulnerability in Issue #182"
            )

    def test_mutation_endpoints_have_owner_gate(self):
        """Both mutation endpoints must carry the owner-only authorization gate."""
        for func_name in ["assign_subscription_to_agent", "clear_agent_subscription"]:
            src = self._get_function_source(func_name)
            assert "assert_agent_owner" in src, (
                f"{func_name} is missing the owner-only authorization gate"
            )
