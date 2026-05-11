"""
Issue #759 — session in-flight sentinel + dynamic lock TTL.

Test strategy: AST + source-only checks, matching test_session_persistence_flag.py
and test_database_facade_delegation.py. The tests/.venv runs Python 3.9 but
src/backend uses PEP-604 syntax (`str | None`) introduced in 3.10, so the
import chain through routers/__init__.py → routers/auth.py → database.py →
db/migrations.py blows up at collection time. The AST-only pattern keeps the
tests runnable in any Python with pytest installed and is the established
convention in this codebase for guard-style structural tests.

Three guards in this file:

1. **Lock-key + sentinel-key helpers are defined and shaped correctly** —
   string literals are read straight out of the AST. A typo here would
   silently split-brain producer vs. probe (the original autoplan draft
   had `trinity:session:lock:` instead of `session_lock:`).

2. **`_ResumeLock.__init__` uses `_session_lock_key`** — not a literal
   string. This is the single guard against a future edit re-introducing
   the typo class.

3. **`send_session_message` wraps the turn body in `_InflightSentinel`**
   AND passes `ttl_seconds` to `_ResumeLock`. Without the sentinel wrap,
   `turn_in_progress=true` never fires for cold turns (the resume lock
   skips them by design); without the dynamic TTL, turns >5 min drop
   the lock before completion and allow concurrent JSONL writes.

4. **`_serialize_session` accepts `turn_in_progress` and includes it
   in the dict** — pins the API contract the frontend reads.

5. **`_resolve_lock_ttl` formula** — direct exec of the function on a
   stub so the cap + fallback behaviour is verified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS_ROUTER_PY = _PROJECT_ROOT / "src" / "backend" / "routers" / "sessions.py"


def _read() -> str:
    assert _SESSIONS_ROUTER_PY.is_file(), f"missing {_SESSIONS_ROUTER_PY}"
    return _SESSIONS_ROUTER_PY.read_text()


def _tree() -> ast.Module:
    return ast.parse(_read())


def _find_func(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    return None


def _find_class(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


# ---------------------------------------------------------------------------
# 1. Helper functions exist and their key strings are pinned
# ---------------------------------------------------------------------------


def test_session_lock_key_helper_returns_canonical_prefix():
    """Pin `session_lock:{agent_name}:{claude_session_id}`. Frontend
    debug scripts and ops dashboards may grep this prefix; renames break
    them silently."""
    tree = _tree()
    fn = _find_func(tree, "_session_lock_key")
    assert fn is not None, "_session_lock_key helper missing"

    # Body should be a single Return with an f-string whose literal portion
    # is exactly `session_lock:` followed by the two formatted vars.
    src = ast.unparse(fn)
    assert "session_lock:" in src, (
        "_session_lock_key must use the literal prefix 'session_lock:'. "
        "Don't rename without also updating debug tooling and Redis ops "
        "docs that grep this prefix."
    )


def test_session_inflight_key_helper_returns_canonical_prefix():
    """Sentinel namespace `session_inflight:` distinct from the lock."""
    tree = _tree()
    fn = _find_func(tree, "_session_inflight_key")
    assert fn is not None, "_session_inflight_key helper missing"
    src = ast.unparse(fn)
    assert "session_inflight:" in src, (
        "_session_inflight_key must use the literal prefix 'session_inflight:'."
    )


def test_inflight_namespace_is_distinct_from_lock_namespace():
    """The two primitives serve different purposes — if their prefixes
    collide we'd serialise cold-turn JSONL writes by accident. Pin the
    separation."""
    src = _read()
    # Both prefixes must appear as standalone literals.
    assert re.search(r'"session_lock:', src) is not None
    assert re.search(r'"session_inflight:', src) is not None
    # And they must not be the SAME literal.
    assert '"session_lock:"' != '"session_inflight:"'


# ---------------------------------------------------------------------------
# 2. _ResumeLock constructs its key via the helper, not a literal
# ---------------------------------------------------------------------------


def test_resume_lock_uses_lock_key_helper_in_init():
    """Regression guard against the autoplan-flagged typo class:
    if `_ResumeLock.__init__` re-introduces a literal `session_lock:`
    string instead of calling the helper, a future rename of the helper
    leaves them split-brained. The fix is one shared call site."""
    tree = _tree()
    cls = _find_class(tree, "_ResumeLock")
    assert cls is not None, "_ResumeLock class missing"

    init = None
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            init = node
            break
    assert init is not None, "_ResumeLock.__init__ missing"

    src = ast.unparse(init)
    assert "_session_lock_key" in src, (
        "_ResumeLock.__init__ must construct its key via _session_lock_key(); "
        "do not duplicate the string literal in this class."
    )


def test_resume_lock_init_accepts_ttl_seconds_kwarg():
    """Dynamic TTL is essential for long turns — without it, the static
    300s lock TTL drops mid-turn and allows concurrent JSONL writes."""
    tree = _tree()
    cls = _find_class(tree, "_ResumeLock")
    init = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    arg_names = [a.arg for a in init.args.args + init.args.kwonlyargs]
    assert "ttl_seconds" in arg_names, (
        "_ResumeLock.__init__ must accept ttl_seconds= so the turn handler "
        "can pass the per-agent execution timeout (#759)."
    )


# ---------------------------------------------------------------------------
# 3. send_session_message wires up the sentinel + dynamic TTL
# ---------------------------------------------------------------------------


def test_send_session_message_wraps_turn_in_inflight_sentinel():
    """The handler must bracket its turn body in `_InflightSentinel(...)`.
    Without this, GET sessions/{id} returns `turn_in_progress=false`
    even while the backend is in the middle of running a turn — the
    cold-turn arm of #759 stays broken."""
    tree = _tree()
    handler = _find_func(tree, "send_session_message")
    assert handler is not None, "send_session_message missing"

    found = False
    for child in ast.walk(handler):
        if isinstance(child, ast.AsyncWith):
            for item in child.items:
                call = item.context_expr
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", None) or getattr(
                        call.func, "attr", None
                    )
                    if name == "_InflightSentinel":
                        found = True
                        break
            if found:
                break
    assert found, (
        "send_session_message must use `async with _InflightSentinel(...)` "
        "to bracket the turn body — that's what drives turn_in_progress=true "
        "during a turn (including cold turns)."
    )


def test_send_session_message_passes_dynamic_ttl_to_resume_lock():
    """`_ResumeLock` must be constructed with `ttl_seconds=lock_ttl`
    (sourced from `_resolve_lock_ttl(name)`) — not the old static
    constant."""
    tree = _tree()
    handler = _find_func(tree, "send_session_message")
    src = ast.unparse(handler)
    assert "_resolve_lock_ttl" in src, (
        "send_session_message must resolve a dynamic TTL via _resolve_lock_ttl(name)"
    )
    assert "ttl_seconds=lock_ttl" in src or "ttl_seconds=" in src, (
        "send_session_message must pass ttl_seconds= to _ResumeLock; the "
        "static 300s constant has been removed."
    )


# ---------------------------------------------------------------------------
# 4. _serialize_session contract
# ---------------------------------------------------------------------------


def test_serialize_session_accepts_turn_in_progress_kwarg_default_false():
    """Callers that don't pass the kwarg (list endpoint, write-path
    responses) must get a safe False default — they shouldn't accidentally
    surface True."""
    tree = _tree()
    fn = _find_func(tree, "_serialize_session")
    assert fn is not None

    # Inspect the function signature for a `turn_in_progress` arg with
    # default `False`.
    arg_names = [a.arg for a in fn.args.args]
    defaults = fn.args.defaults
    assert "turn_in_progress" in arg_names, (
        "_serialize_session must accept a turn_in_progress kwarg."
    )

    # Default value should be the literal False.
    idx_in_args = arg_names.index("turn_in_progress")
    # `defaults` aligns to the tail of `args`.
    default_offset = idx_in_args - (len(arg_names) - len(defaults))
    assert default_offset >= 0, (
        "turn_in_progress should have a default; existing callers must keep working."
    )
    default_node = defaults[default_offset]
    assert isinstance(default_node, ast.Constant) and default_node.value is False, (
        "turn_in_progress default must be False."
    )


def test_serialize_session_includes_turn_in_progress_in_dict():
    """The returned dict must include the field so it lands on the GET
    JSON response and the frontend can read it."""
    src = _read()
    # Restrict the regex to the _serialize_session function body. The
    # AST gives us start_lineno / end_lineno; slice the file accordingly.
    tree = _tree()
    fn = _find_func(tree, "_serialize_session")
    lines = src.splitlines()[fn.lineno - 1 : fn.end_lineno]
    body = "\n".join(lines)
    assert '"turn_in_progress"' in body, (
        "_serialize_session must emit `turn_in_progress` into the returned dict."
    )


# ---------------------------------------------------------------------------
# 5. _resolve_lock_ttl formula
# ---------------------------------------------------------------------------


def _exec_resolve_lock_ttl(timeout_value=None, raises=False):
    """Execute the source of _resolve_lock_ttl in an isolated namespace
    with a stubbed ``db`` and the real module-level constant.

    Avoids the heavy import chain — only the function body matters here,
    and the function body only depends on ``db.get_execution_timeout``,
    ``logger``, and the constant.
    """
    tree = _tree()
    fn = _find_func(tree, "_resolve_lock_ttl")
    assert fn is not None, "_resolve_lock_ttl missing"

    # Extract the constant from the module source so the test is pinned
    # to the actual value, not a duplicated literal.
    src = _read()
    match = re.search(r"_LOCK_TTL_FALLBACK\s*=\s*(\d+)", src)
    assert match is not None, "_LOCK_TTL_FALLBACK constant missing"
    fallback = int(match.group(1))

    # Build a minimal namespace for exec.
    class _DB:
        def get_execution_timeout(self, _name):
            if raises:
                raise RuntimeError("simulated DB outage")
            return timeout_value

    class _Logger:
        def warning(self, *a, **kw):
            pass

    ns = {
        "_LOCK_TTL_FALLBACK": fallback,
        "db": _DB(),
        "logger": _Logger(),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<ast>", "exec"), ns)
    return ns["_resolve_lock_ttl"]("any-agent"), fallback


def test_resolve_lock_ttl_adds_30s_buffer():
    """Default per-agent timeout is 900s; TTL = 930s."""
    ttl, fallback = _exec_resolve_lock_ttl(timeout_value=900)
    assert ttl == 930


def test_resolve_lock_ttl_caps_at_fallback():
    """Even with absurd timeouts we never exceed _LOCK_TTL_FALLBACK so
    zombie-lock cleanup remains bounded."""
    ttl, fallback = _exec_resolve_lock_ttl(timeout_value=60_000)
    assert ttl == fallback


def test_resolve_lock_ttl_falls_back_on_exception():
    """DB lookup failure must not raise from the turn handler. Over-TTL
    is safer than under-TTL since the key auto-expires."""
    ttl, fallback = _exec_resolve_lock_ttl(raises=True)
    assert ttl == fallback


# ---------------------------------------------------------------------------
# 6. The old static constant is gone (regression guard)
# ---------------------------------------------------------------------------


def test_static_lock_ttl_constant_removed():
    """The old `_LOCK_TTL_SECONDS = 300` constant must not survive — its
    presence would indicate the dynamic TTL change didn't fully land."""
    src = _read()
    assert "_LOCK_TTL_SECONDS = 300" not in src, (
        "Static 300s lock TTL was removed; turns >5 min now keep their lock. "
        "If you need to re-introduce a hard cap, raise _LOCK_TTL_FALLBACK instead."
    )
