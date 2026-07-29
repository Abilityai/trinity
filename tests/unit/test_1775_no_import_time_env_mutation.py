"""Static guard: no import-time Redis env mutation under tests/integration (#1775).

pytest imports every test module during **collection**, before any test runs. A
module-scope ``os.environ["REDIS_URL"] = ...`` therefore rewrites the Redis
target for the whole session — including for modules that were collected
earlier and for the harness that set it deliberately. That is exactly what
``test_monitoring_service.py`` did: the directory-collected integration suite
ran 6 of 83 tests, 57 of them skipping on an unreachable Redis, while pytest
still exited 0 and the run read green.

Guarding the write (``if "REDIS_URL" not in os.environ:``) is not sufficient and
is not what this guard asks for:

* a module that ERRORs later in its import has already executed the write, and
* the root conftest's ``setdefault`` makes ``"REDIS_URL" in os.environ``
  unconditionally true, so the guard is a no-op that silently pins the dummy.

The Redis target is resolved exactly once, in ``tests/integration/conftest.py``,
at a scope that runs before any test module is imported.

Two properties this guard must have, both learned the hard way:

1. **AST, not regex.** A textual "no occurrence anywhere" check would fail on
   correct existing code — ``tests/integration/test_postgres_backend.py`` has a
   legitimate ``os.environ.setdefault("REDIS_URL", ...)`` inside a *fixture*,
   which runs at test time against an explicitly-chosen backend and is not the
   bug class.
2. **Module scope only.** Function and fixture bodies are skipped; top-level
   ``if`` / ``try`` / ``for`` / ``with`` bodies and class bodies are NOT, because
   those still execute at import.

Deliberately does NOT assert anything about ``sys.modules``: ``tests/lint_sys_modules.py``
(#762) already bans that repo-wide, at every scope, with its own baseline. One
linter per invariant.

Lives in ``tests/unit/`` (not ``tests/``) so it runs as a pure static check
without the backend-connection autouse fixtures the integration suite carries —
same rationale as ``tests/unit/test_agent_auth_header_guard.py``. ``/verify-local``'s
unit stage runs ``tests/unit``, so this gate is enforced before every push.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from integration.redis_target import mask_redis_url

_TESTS = Path(__file__).resolve().parents[1]
_INTEGRATION = _TESTS / "integration"

# Mutating any of these at import time re-points the whole session's Redis.
WATCHED_KEYS = frozenset({"REDIS_URL", "REDIS_PASSWORD", "REDIS_BACKEND_PASSWORD"})

# conftest.py IS the resolver — it is the one place allowed to write these.
EXEMPT_FILENAMES = frozenset({"conftest.py"})

# Statements whose bodies do NOT run at import time.
_DEFERRED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _is_environ_subscript(node: ast.expr) -> str | None:
    """Return the watched key if `node` is `<x>.environ["KEY"]` / `environ["KEY"]`."""
    if not isinstance(node, ast.Subscript):
        return None
    target = node.value
    is_environ = (isinstance(target, ast.Attribute) and target.attr == "environ") or (
        isinstance(target, ast.Name) and target.id == "environ"
    )
    if not is_environ:
        return None
    key = node.slice
    if isinstance(key, ast.Constant) and key.value in WATCHED_KEYS:
        return str(key.value)
    return None


def _is_environ_method(node: ast.expr, methods: frozenset[str]) -> str | None:
    """Return the watched key if `node` is `<x>.environ.<method>("KEY", ...)`."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in methods:
        return None
    owner = node.func.value
    is_environ = (isinstance(owner, ast.Attribute) and owner.attr == "environ") or (
        isinstance(owner, ast.Name) and owner.id == "environ"
    )
    if not is_environ or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and first.value in WATCHED_KEYS:
        return str(first.value)
    return None


_MUTATING_METHODS = frozenset({"setdefault", "pop"})


def find_import_time_env_mutations(
    source: str, filename: str = "<planted>"
) -> list[str]:
    """Return one description per module-scope mutation of a watched Redis key.

    Walks module-level statements, descending through control flow and class
    bodies (both execute at import) but never into a function, coroutine or
    lambda body (those run later, under a fixture or a test).
    """
    findings: list[str] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, _DEFERRED_SCOPES):
            return  # runs at call time, not import time — out of scope

        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                key = _is_environ_subscript(target)
                if key:
                    findings.append(
                        f"{filename}:{node.lineno}: module-scope assignment to "
                        f"os.environ[{key!r}]"
                    )
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                key = _is_environ_subscript(target)
                if key:
                    findings.append(
                        f"{filename}:{node.lineno}: module-scope del of "
                        f"os.environ[{key!r}]"
                    )
        elif isinstance(node, ast.Call):
            key = _is_environ_method(node, _MUTATING_METHODS)
            if key:
                findings.append(
                    f"{filename}:{node.lineno}: module-scope "
                    f"os.environ.{node.func.attr}({key!r}, ...)"  # type: ignore[union-attr]
                )

        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source, filename))
    return findings


def _integration_modules() -> list[Path]:
    return sorted(
        p for p in _INTEGRATION.glob("*.py") if p.name not in EXEMPT_FILENAMES
    )


# ── the gate ────────────────────────────────────────────────────────────────


def test_integration_suite_has_no_import_time_redis_env_mutation():
    """AC1: only tests/integration/conftest.py may resolve the Redis target."""
    assert _integration_modules(), "no integration modules found — bad path?"

    findings: list[str] = []
    for path in _integration_modules():
        findings += find_import_time_env_mutations(
            path.read_text(), str(path.relative_to(_TESTS.parent))
        )

    assert not findings, (
        "Import-time Redis env mutation found. pytest imports these modules "
        "during collection, so this re-points REDIS_URL for the whole session "
        "and silently turns other modules' Redis tests into skips (#1775). "
        "Resolve the target in tests/integration/conftest.py, or read it inside "
        "a fixture.\n  " + "\n  ".join(findings)
    )


# ── detector proofs: positives ──────────────────────────────────────────────


def test_detector_flags_unconditional_module_scope_write():
    """The literal #1775 bug: test_monitoring_service.py's old line 85."""
    src = (
        'import os\nos.environ["REDIS_URL"] = f"redis://backend:{pw}@localhost:6379"\n'
    )
    assert find_import_time_env_mutations(src)


def test_detector_flags_write_guarded_by_a_top_level_if():
    """A top-level `if` body still runs at import — the guarded four were
    harmless only by luck, and the guard itself is defeated by the root
    conftest's setdefault."""
    src = (
        "import os\n"
        'if "REDIS_URL" not in os.environ:\n'
        '    os.environ["REDIS_URL"] = "redis://backend:pw@localhost:6379"\n'
    )
    assert find_import_time_env_mutations(src)


def test_detector_flags_module_scope_setdefault():
    src = 'import os\nos.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")\n'
    assert find_import_time_env_mutations(src)


def test_detector_flags_aliased_os_import():
    """tests/conftest.py imports `os as _os_589`; an alias must not evade."""
    src = 'import os as _o\n_o.environ["REDIS_PASSWORD"] = "test"\n'
    assert find_import_time_env_mutations(src)


def test_detector_flags_write_inside_a_module_scope_try():
    src = (
        "import os\n"
        "try:\n"
        '    os.environ["REDIS_URL"] = "redis://x:y@localhost:6379"\n'
        "except Exception:\n"
        "    pass\n"
    )
    assert find_import_time_env_mutations(src)


# ── detector proofs: negatives (the ones a regex would get wrong) ───────────


def test_detector_ignores_fixture_time_setdefault():
    """AC6: a setdefault inside a fixture runs at test time, not collection."""
    src = (
        "import os\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def manager():\n"
        '    os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")\n'
        '    os.environ["REDIS_PASSWORD"] = "test"\n'
        "    yield\n"
    )
    assert find_import_time_env_mutations(src) == []


def test_real_postgres_backend_module_is_not_flagged():
    """AC6, against the actual file — this is the case v1's regex guard broke on."""
    path = _INTEGRATION / "test_postgres_backend.py"
    assert path.exists(), "expected tests/integration/test_postgres_backend.py"
    source = path.read_text()
    assert 'os.environ.setdefault("REDIS_URL"' in source, (
        "fixture-time setdefault disappeared — this negative test no longer "
        "proves the module-scope restriction; pick another live negative case"
    )
    assert find_import_time_env_mutations(source, path.name) == []


def test_detector_ignores_unrelated_env_keys():
    src = 'import os\nos.environ["DATABASE_URL"] = "postgres://x"\n'
    assert find_import_time_env_mutations(src) == []


def test_detector_ignores_reads():
    src = 'import os\nurl = os.environ["REDIS_URL"]\nother = os.environ.get("REDIS_URL")\n'
    assert find_import_time_env_mutations(src) == []


def test_integration_conftest_is_the_exempt_resolver():
    """The exemption must point at the file that actually owns resolution.

    The conftest's own write lives inside ``_apply_resolved_target`` — a
    function, so the detector would not flag it even without the exemption. The
    exemption is there for the *scope* the resolver is allowed to occupy: if it
    ever inlines the write at module level, that is legitimate. This test keeps
    the exemption honest by proving it names the file that does the writing.
    """
    conftest = _INTEGRATION / "conftest.py"
    assert conftest.name in EXEMPT_FILENAMES
    source = conftest.read_text()
    assert 'os.environ["REDIS_URL"] = url' in source, (
        "tests/integration/conftest.py no longer resolves the Redis target — if "
        "the resolver moved, move the exemption with it (and update this test)"
    )


# ── credential masking (AC7) ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "redis://backend:s3cr3t@localhost:6379",
            "redis://backend:***@localhost:6379",
        ),
        (
            "redis://backend:s3cr3t@127.0.0.1:6390/2",
            "redis://backend:***@127.0.0.1:6390/2",
        ),
        ("redis://localhost:6379", "redis://localhost:6379"),  # no credentials
        (None, "<unset>"),
        ("", "<unset>"),
    ],
)
def test_mask_redis_url(url, expected):
    assert mask_redis_url(url) == expected


def test_mask_never_leaks_the_password():
    masked = mask_redis_url("redis://backend:hunter2-hunter2@localhost:6379")
    assert "hunter2" not in masked
    # host:port and user survive — they are what identifies a misconfiguration.
    assert "localhost:6379" in masked and "backend" in masked


@pytest.mark.parametrize(
    "malformed",
    [
        "redis://backend:hunter2@localhost:notaport",  # urlsplit is lazy; .port raises
        "redis://backend:hunter2@[oops:6379",  # unterminated IPv6 literal
    ],
)
def test_mask_survives_a_malformed_url_without_leaking(malformed):
    """A masker must not throw on the input it is most needed for.

    `urlsplit` accepts these and only raises when `.port`/`.hostname` is read.
    With that read outside the guard, `mask_redis_url` raised — and since every
    caller passes the raw URL as an argument, the escaping ValueError made
    pytest render the caller's frame and print the password verbatim.
    """
    masked = mask_redis_url(malformed)  # must not raise
    assert "hunter2" not in masked
    assert masked == "<unparseable REDIS_URL>"
