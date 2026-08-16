"""Tests for tests/lint_root_test_placement.py (Issue #1895).

Mirrors tests/unit/test_lint_sys_modules.py: import the AST-only guard and drive
its pure helpers with synthetic source, no filesystem/backend needed. Lives under
tests/unit/ so the per-PR unit job collects it — a guard must run where the bug it
prevents would regress.
"""

from __future__ import annotations

import ast
from pathlib import Path

from lint_root_test_placement import (
    ALLOW_COMMENT,
    TESTS_ROOT,
    _collect_unmarked_async,
    _pytestmark_has_asyncio,
    _requests_live_fixture,
    _root_file_ok,
)


def _tree(src: str) -> ast.Module:
    return ast.parse(src)


# ---------------------------------------------------------------------------
# Part 1 — root placement
# ---------------------------------------------------------------------------


class TestPart1RootPlacement:
    def test_self_contained_file_is_flagged(self):
        # A pure-logic test taking no live fixture is self-contained → not "ok".
        src = "def test_pure():\n    assert 1 + 1 == 2\n"
        assert _requests_live_fixture(_tree(src)) is False
        path = TESTS_ROOT / "test_new_selfcontained.py"
        assert _root_file_ok(path, src, baseline=set()) is False

    def test_live_fixture_param_passes(self):
        src = "def test_live(api_client):\n    assert api_client\n"
        assert _requests_live_fixture(_tree(src)) is True
        path = TESTS_ROOT / "test_new_live.py"
        assert _root_file_ok(path, src, baseline=set()) is True

    def test_live_fixture_in_a_fixture_counts(self):
        # A file whose *fixture* (not a test) requests a live fixture still needs
        # the backend — matches the real test_whatsapp_integration shape.
        src = (
            "import pytest\n"
            "@pytest.fixture\n"
            "def binding(created_agent):\n"
            "    return created_agent\n"
            "def test_uses_binding(binding):\n"
            "    assert binding\n"
        )
        assert _requests_live_fixture(_tree(src)) is True

    def test_locally_redefined_fixture_is_not_a_live_signal(self):
        # canary/idempotency define `def api_client(): yield None` to neuter the
        # root harness — requesting that name resolves to the LOCAL override.
        src = (
            "def api_client():\n"
            "    yield None\n"
            "def test_x(api_client):\n"
            "    assert api_client is None\n"
        )
        assert _requests_live_fixture(_tree(src)) is False

    def test_allow_comment_exempts(self):
        src = (
            "# allow-root-live-test: raw-httpx smoke against a live backend\n"
            "import httpx\n"
            "def test_smoke():\n"
            "    httpx.get('http://localhost:8000/health')\n"
        )
        assert ALLOW_COMMENT in src
        path = TESTS_ROOT / "test_new_smoke.py"
        assert _root_file_ok(path, src, baseline=set()) is True

    def test_baselined_file_passes(self):
        src = "def test_pure():\n    assert True\n"
        path = TESTS_ROOT / "test_grandfathered.py"
        assert (
            _root_file_ok(path, src, baseline={"tests/test_grandfathered.py"}) is True
        )
        # ...and is NOT ok once removed from the baseline.
        assert _root_file_ok(path, src, baseline=set()) is False


# ---------------------------------------------------------------------------
# Part 2 — async markers under tests/unit/
# ---------------------------------------------------------------------------


def _async_findings(src: str) -> list:
    tree = _tree(src)
    return _collect_unmarked_async(
        tree.body,
        Path("sample.py"),
        module_marked=_pytestmark_has_asyncio(tree.body),
        class_marked=False,
    )


class TestPart2AsyncMarkers:
    def test_unmarked_async_is_flagged(self):
        src = "async def test_a():\n    pass\n"
        findings = _async_findings(src)
        assert len(findings) == 1
        assert findings[0].test_name == "test_a"

    def test_function_decorator_marker_passes(self):
        src = (
            "import pytest\n"
            "@pytest.mark.asyncio\n"
            "async def test_a():\n"
            "    pass\n"
        )
        assert _async_findings(src) == []

    def test_class_decorator_marker_passes(self):
        # @pytest.mark.asyncio on the class marks all its methods (test_backlog shape).
        src = (
            "import pytest\n"
            "@pytest.mark.asyncio\n"
            "class TestX:\n"
            "    async def test_a(self):\n"
            "        pass\n"
            "    async def test_b(self):\n"
            "        pass\n"
        )
        assert _async_findings(src) == []

    def test_class_pytestmark_passes(self):
        # class-level `pytestmark = pytest.mark.asyncio` (test_idempotency shape),
        # including the list form `[pytest.mark.asyncio, pytest.mark.skip(...)]`.
        src = (
            "import pytest\n"
            "class TestX:\n"
            "    pytestmark = [pytest.mark.asyncio, pytest.mark.skip(reason='x')]\n"
            "    async def test_a(self):\n"
            "        pass\n"
        )
        assert _async_findings(src) == []

    def test_module_pytestmark_passes(self):
        src = (
            "import pytest\n"
            "pytestmark = pytest.mark.asyncio\n"
            "async def test_a():\n"
            "    pass\n"
        )
        assert _async_findings(src) == []

    def test_sync_test_never_flagged(self):
        src = "def test_sync():\n    pass\n"
        assert _async_findings(src) == []


def test_guard_passes_on_the_real_tree():
    """The live guard must be green on the committed tree (both parts)."""
    import lint_root_test_placement as guard

    baseline = guard.load_baseline()
    assert guard.collect_root_findings(baseline) == []
    assert guard.collect_async_findings() == []
