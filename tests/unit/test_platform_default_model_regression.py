"""
Regression tests for #831 — platform default model.

The original bug: headless_executor.py hardcoded ``"sonnet"`` (a bare alias,
not a valid Anthropic API model ID) as the fallback when ``model=None``. This
caused Sonnet quota exhaustion on 44 production schedules and confusion because
the Settings UI showed Opus 4.5 as "Default — most capable".

Fix: ``task_execution_service.execute_task()`` resolves ``model=None`` →
``settings_service.get_platform_default_model()`` before forwarding to the
agent. The agent-side fallback in ``headless_executor.py`` and
``claude_code.py`` is now a safety-net for direct agent-server calls only and
uses ``"claude-sonnet-4-6"`` rather than the bare ``"sonnet"`` alias.

These tests are source/AST-level (no backend required) and will catch an
accidental revert or inadvertent re-introduction of the ``"sonnet"`` bare
alias fallback.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_SETTINGS_SERVICE = (
    _PROJECT_ROOT / "src" / "backend" / "services" / "settings_service.py"
)
_TASK_EXEC_SERVICE = (
    _PROJECT_ROOT / "src" / "backend" / "services" / "task_execution_service.py"
)
_HEADLESS_EXECUTOR = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "services" / "headless_executor.py"
)
_CLAUDE_CODE = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "services" / "claude_code.py"
)


def _src(path: Path) -> str:
    assert path.is_file(), f"missing source file: {path}"
    return path.read_text()


def _tree(path: Path) -> ast.Module:
    return ast.parse(_src(path))


def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function '{name}' not found in AST")


def _func_source(full_src: str, func_node) -> str:
    lines = full_src.splitlines()
    return "\n".join(lines[func_node.lineno - 1 : func_node.end_lineno])


# ---------------------------------------------------------------------------
# 1. settings_service.py — constant pinning
# ---------------------------------------------------------------------------


class TestSettingsServiceConstants:
    """PLATFORM_DEFAULT_MODEL_VALUE must equal 'claude-sonnet-4-6'."""

    def test_platform_default_model_value_constant(self):
        """PLATFORM_DEFAULT_MODEL_VALUE must be the full Sonnet 4.6 model ID."""
        tree = _tree(_SETTINGS_SERVICE)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "PLATFORM_DEFAULT_MODEL_VALUE"
            ):
                assert isinstance(node.value, ast.Constant), (
                    "PLATFORM_DEFAULT_MODEL_VALUE must be a string literal"
                )
                assert node.value.value == "claude-sonnet-4-6", (
                    f"Expected 'claude-sonnet-4-6', got {node.value.value!r}. "
                    "Changing this default is a breaking change for all null-model schedules."
                )
                return
        raise AssertionError(
            "PLATFORM_DEFAULT_MODEL_VALUE not found in settings_service.py — "
            "was the constant removed or renamed?"
        )

    def test_platform_default_model_key_constant(self):
        """PLATFORM_DEFAULT_MODEL_KEY must be 'platform_default_model' (DB key)."""
        tree = _tree(_SETTINGS_SERVICE)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "PLATFORM_DEFAULT_MODEL_KEY"
            ):
                assert isinstance(node.value, ast.Constant)
                assert node.value.value == "platform_default_model", (
                    "PLATFORM_DEFAULT_MODEL_KEY must match the system_settings DB key"
                )
                return
        raise AssertionError("PLATFORM_DEFAULT_MODEL_KEY not found in settings_service.py")

    def test_get_platform_default_model_method_exists(self):
        """SettingsService must expose get_platform_default_model()."""
        tree = _tree(_SETTINGS_SERVICE)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "SettingsService"
            ):
                methods = [n.name for n in ast.walk(node)
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                assert "get_platform_default_model" in methods, (
                    "SettingsService.get_platform_default_model() is missing — "
                    "task_execution_service depends on it for null-model resolution"
                )
                return
        raise AssertionError("SettingsService class not found in settings_service.py")


# ---------------------------------------------------------------------------
# 2. task_execution_service.py — null-model resolution wiring
# ---------------------------------------------------------------------------


class TestTaskExecutionServiceModelResolution:
    """execute_task() must resolve model=None via settings_service, not a hardcode."""

    def test_execute_task_calls_get_platform_default_model(self):
        """execute_task body must call settings_service.get_platform_default_model()."""
        src = _src(_TASK_EXEC_SERVICE)
        tree = ast.parse(src)
        func = _find_function(tree, "execute_task")
        body = _func_source(src, func)
        assert "get_platform_default_model()" in body, (
            "execute_task() must call settings_service.get_platform_default_model() "
            "to resolve null model. Removing this call re-introduces the #831 bug."
        )

    def test_execute_task_does_not_hardcode_bare_sonnet(self):
        """execute_task must not assign the bare 'sonnet' alias as a model fallback."""
        src = _src(_TASK_EXEC_SERVICE)
        tree = ast.parse(src)
        func = _find_function(tree, "execute_task")
        body = _func_source(src, func)
        # String search for bare "sonnet" assignment (the original #831 bug pattern).
        # "claude-sonnet-4-6" is fine; only the bare alias is forbidden.
        bare_pattern = re.compile(r'''\bmodel\s*=\s*["']sonnet["']''', re.IGNORECASE)
        matches = bare_pattern.findall(body)
        assert not matches, (
            "execute_task() contains bare 'sonnet' model assignment — "
            "this is the #831 regression. Use 'claude-sonnet-4-6' or "
            "settings_service.get_platform_default_model() instead."
        )

    def test_execute_task_imports_settings_service(self):
        """settings_service must be importable at the top of the module."""
        src = _src(_TASK_EXEC_SERVICE)
        assert "settings_service" in src, (
            "task_execution_service.py must import settings_service for null-model resolution"
        )


# ---------------------------------------------------------------------------
# 3. headless_executor.py — safety-net fallback must use full model ID
# ---------------------------------------------------------------------------


class TestHeadlessExecutorFallback:
    """The safety-net fallback in _setup_headless_command must be 'claude-sonnet-4-6'."""

    def test_no_bare_sonnet_string_in_setup_command(self):
        """_setup_headless_command must not assign the bare 'sonnet' alias."""
        src = _src(_HEADLESS_EXECUTOR)
        tree = ast.parse(src)
        func = _find_function(tree, "_setup_headless_command")
        body = _func_source(src, func)
        for node in ast.walk(ast.parse(body)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.strip().lower() == "sonnet"
            ):
                raise AssertionError(
                    "_setup_headless_command() assigns bare 'sonnet' string — "
                    "the original #831 bug. The safety-net must use 'claude-sonnet-4-6'."
                )

    def test_safety_net_uses_full_model_id(self):
        """The model=None branch must set 'claude-sonnet-4-6', not a bare alias."""
        src = _src(_HEADLESS_EXECUTOR)
        tree = ast.parse(src)
        func = _find_function(tree, "_setup_headless_command")
        body = _func_source(src, func)
        assert "claude-sonnet-4-6" in body, (
            "_setup_headless_command safety-net must assign 'claude-sonnet-4-6'. "
            "It appears the fix was reverted."
        )


# ---------------------------------------------------------------------------
# 4. claude_code.py — get_default_model and chat fallback
# ---------------------------------------------------------------------------


class TestClaudeCodeDefaults:
    """claude_code.py must return 'claude-sonnet-4-6', not 'sonnet'."""

    def test_get_default_model_returns_full_id(self):
        """AgentState.get_default_model() must return 'claude-sonnet-4-6', not a bare alias."""
        src = _src(_CLAUDE_CODE)
        tree = ast.parse(src)
        func = _find_function(tree, "get_default_model")
        body = _func_source(src, func)
        # Must contain the full model ID
        assert "claude-sonnet-4-6" in body, (
            "get_default_model() must return 'claude-sonnet-4-6' (full Anthropic API ID). "
            "The bare 'sonnet' alias is not a valid production model ID."
        )
        # Must NOT return the bare alias
        bare_return = re.compile(r'''return\s+["']sonnet["']''', re.IGNORECASE)
        assert not bare_return.search(body), (
            "get_default_model() returns bare 'sonnet' alias — re-introduces #831 bug."
        )

    def test_execute_claude_code_fallback_uses_full_model_id(self):
        """The model=None branch in execute_claude_code must use 'claude-sonnet-4-6'."""
        src = _src(_CLAUDE_CODE)
        tree = ast.parse(src)
        func = _find_function(tree, "execute_claude_code")
        body = _func_source(src, func)
        # If there's a bare "sonnet" assignment as fallback, that's a regression
        for node in ast.walk(ast.parse(body)):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and node.value.value.strip().lower() == "sonnet"
            ):
                raise AssertionError(
                    "execute_claude_code() assigns bare 'sonnet' as model fallback — "
                    "re-introduces #831 bug. Use 'claude-sonnet-4-6'."
                )


# ---------------------------------------------------------------------------
# 5. No file contains bare "= 'sonnet'" or "= \"sonnet\"" as a model default
# ---------------------------------------------------------------------------


class TestNoBareSonnetAlias:
    """Neither the backend nor agent_server should use the bare 'sonnet' alias as a model default."""

    @pytest.mark.parametrize("path", [
        _TASK_EXEC_SERVICE,
        _HEADLESS_EXECUTOR,
        _CLAUDE_CODE,
        _SETTINGS_SERVICE,
    ])
    def test_no_model_assignment_with_bare_sonnet(self, path: Path):
        """Source file must not contain model = 'sonnet' (bare alias assignment)."""
        src = _src(path)
        # Pattern: assignment of bare 'sonnet' or "sonnet" (case-insensitive, strip whitespace)
        # This catches `model = "sonnet"` but not `"claude-sonnet-4-6"`.
        bare_alias_pattern = re.compile(
            r'''\bmodel\s*=\s*["']sonnet["']''',
            re.IGNORECASE,
        )
        matches = bare_alias_pattern.findall(src)
        assert not matches, (
            f"{path.name} contains bare 'sonnet' model assignment: {matches}\n"
            "This is the #831 regression pattern. Use 'claude-sonnet-4-6' or "
            "settings_service.get_platform_default_model() instead."
        )
