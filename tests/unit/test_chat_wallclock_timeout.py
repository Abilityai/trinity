"""Regression tests for #313: chat mode wall-clock timeout (GUARD-003 follow-up).

Before this fix, `execute_claude_code` called `process.wait()` with no timeout
and `run_in_executor` without `asyncio.wait_for`. A stalled Claude CLI
subprocess (billing error, stuck stream, hook grandchild wedge not caught by
`--max-turns`) would hang the chat session until the container was killed
externally.

Fix mirrors the existing task-mode pattern in `execute_headless_task`:
- Read `execution_timeout_sec` from guardrails config with a 30-min default.
- Inner `process.wait(timeout=...)` bounds the subprocess thread.
- Outer `asyncio.wait_for(..., timeout=+60)` is a safety net.
- On timeout, kill the process group and raise HTTP 504.

These tests validate the fix is wired correctly without importing the full
`claude_code` module (its relative imports require the whole agent_server
package to be loadable). Source-level assertions catch accidental reverts.
"""
from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVICES = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "services"
)
_CLAUDE_CODE_PY = _AGENT_SERVICES / "claude_code.py"
# _DEFAULT_EXECUTION_TIMEOUT_SEC moved to _runtime_config.py per #122 module split.
_RUNTIME_CONFIG_PY = _AGENT_SERVICES / "_runtime_config.py"


@pytest.fixture(scope="module")
def source() -> str:
    assert _CLAUDE_CODE_PY.is_file(), f"missing {_CLAUDE_CODE_PY}"
    return _CLAUDE_CODE_PY.read_text()


@pytest.fixture(scope="module")
def tree(source: str) -> ast.Module:
    return ast.parse(source)


@pytest.fixture(scope="module")
def runtime_config_tree() -> ast.Module:
    assert _RUNTIME_CONFIG_PY.is_file(), f"missing {_RUNTIME_CONFIG_PY}"
    return ast.parse(_RUNTIME_CONFIG_PY.read_text())


def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _function_source(source: str, func: ast.AsyncFunctionDef) -> str:
    lines = source.splitlines()
    return "\n".join(lines[func.lineno - 1 : func.end_lineno])


# ---------------------------------------------------------------------------
# Module-level constant regression guards
# ---------------------------------------------------------------------------


def test_default_timeout_constant_is_30_minutes(runtime_config_tree: ast.Module) -> None:
    """The default wall-clock cap is 30 minutes (1800 s) per issue spec.

    Constant moved to services/_runtime_config.py per #122 module split.
    """
    for node in ast.walk(runtime_config_tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_DEFAULT_EXECUTION_TIMEOUT_SEC"
        ):
            assert isinstance(node.value, ast.Constant), "default must be literal"
            assert node.value.value == 1800, f"expected 1800, got {node.value.value!r}"
            return
    raise AssertionError("_DEFAULT_EXECUTION_TIMEOUT_SEC constant not defined in _runtime_config.py")


# ---------------------------------------------------------------------------
# execute_claude_code wiring (the subprocess body lives in _execute_claude_code_once since #2958)
# ---------------------------------------------------------------------------


def test_chat_reads_guardrails_execution_timeout(source: str, tree: ast.Module) -> None:
    """Chat mode must resolve timeout from the shared guardrails field."""
    body = _function_source(source, _find_function(tree, "_execute_claude_code_once"))
    assert 'guardrails.get("execution_timeout_sec")' in body, (
        "chat mode must read execution_timeout_sec from guardrails"
    )
    assert "_DEFAULT_EXECUTION_TIMEOUT_SEC" in body, (
        "chat mode must fall back to the default constant"
    )


def test_chat_has_inner_process_wait_timeout(source: str, tree: ast.Module) -> None:
    """Inner process.wait must be bounded by timeout_seconds."""
    body = _function_source(source, _find_function(tree, "_execute_claude_code_once"))
    assert "process.wait(timeout=timeout_seconds)" in body, (
        "inner subprocess wait must pass the timeout"
    )
    assert "subprocess.TimeoutExpired" in body, (
        "inner wait must handle TimeoutExpired"
    )
    assert "_terminate_process_group(process" in body, (
        "timeout handler must kill the process group"
    )


def test_chat_has_outer_asyncio_wait_for(source: str, tree: ast.Module) -> None:
    """Outer wait_for is the async safety net with a 60s grace buffer."""
    body = _function_source(source, _find_function(tree, "_execute_claude_code_once"))
    assert "asyncio.wait_for(" in body, (
        "chat mode must wrap the executor call in asyncio.wait_for"
    )
    assert "timeout_seconds + 60" in body, (
        "outer timeout must exceed inner by 60s for drain/cleanup"
    )
    assert "asyncio.TimeoutError" in body, (
        "outer handler must catch asyncio.TimeoutError"
    )


def test_chat_timeout_returns_http_504(source: str, tree: ast.Module) -> None:
    """Both timeout paths must raise HTTPException(504)."""
    body = _function_source(source, _find_function(tree, "_execute_claude_code_once"))
    # Count 504 raises — expect at least 2 (one per timeout path).
    count = body.count("status_code=504")
    assert count >= 2, f"expected ≥2 HTTP 504 raises in chat mode, found {count}"


# ---------------------------------------------------------------------------
# Behavioural check on the underlying subprocess primitive
# ---------------------------------------------------------------------------


def test_subprocess_wait_timeout_raises_timeoutexpired() -> None:
    """Sanity: `process.wait(timeout=X)` raises TimeoutExpired — the primitive
    the fix relies on. Guards against a platform-specific regression.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.3)
        elapsed = time.monotonic() - start
        # Primitive is cheap — timeout should fire well before the sleep ends.
        assert elapsed < 1.0, f"wait(timeout=0.3) took {elapsed:.2f}s"
    finally:
        proc.kill()
        proc.wait(timeout=2.0)
