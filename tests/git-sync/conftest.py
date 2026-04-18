"""
Local conftest for tests/git-sync/.

The top-level tests/conftest.py wires up HTTP fixtures and auto-logs in
against a live backend, which these unit tests don't need. We override the
backend-dependent fixtures with no-ops so pytest can collect and run the
S7 unit tests in complete isolation.
"""

import pytest


@pytest.fixture(scope="session")
def api_client():
    """Override the live-backend api_client fixture."""
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the autouse cleanup_after_test fixture that hits the backend."""
    yield
