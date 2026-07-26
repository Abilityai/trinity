"""The enterprise-load block must not call a real failure "normal" (#1653 / ent#196).

`main.py` used to wrap the enterprise import AND `register_enterprise(app)` in a
single `try` whose first arm was `except ImportError`. `ModuleNotFoundError`
subclasses `ImportError`, so a real bug raised *inside* module registration —
enterprise modules lazily import OSS seams in their `register()` — printed:

    Trinity Enterprise submodule not present — OSS-only build
    (this is normal; enterprise modules are an optional private submodule)

On a mounted install that is actively false. The diagnostic traceback never ran,
boot did not crash, and the failing module's routes could stay mounted while its
entitlement was never registered — 403-ing forever, with nothing in the logs
connecting the two.

The fix has two parts and BOTH are needed:

1. **Structural** — the `ImportError` arms now cover only the top-level import;
   anything raised by `register_enterprise(app)` reaches the diagnostic. This is
   what catches a module lazily importing a missing *sibling*, whose
   `ModuleNotFoundError.name` starts with `enterprise` and would look benign to a
   name check.
2. **Name check** — even on the top-level import, "not present" is only true when
   the missing module IS the enterprise package. A missing third-party dependency
   wears the same exception type and is a real failure.
"""
from __future__ import annotations

import re
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"


def _classify_top_level(exc: BaseException) -> str:
    """The predicate main.py applies to a failure of the TOP-LEVEL import."""
    if isinstance(exc, ModuleNotFoundError):
        return "benign" if (exc.name or "").split(".")[0] == "enterprise" else "loud"
    return "loud"


# --- the top-level import arm ------------------------------------------------

def test_absent_submodule_is_still_reported_as_normal():
    """An OSS clone has no `enterprise` package — that must stay calm."""
    assert _classify_top_level(
        ModuleNotFoundError("No module named 'enterprise'", name="enterprise")
    ) == "benign"
    assert _classify_top_level(
        ModuleNotFoundError("No module named 'enterprise.backend'", name="enterprise.backend")
    ) == "benign"


def test_missing_third_party_dependency_is_loud():
    """The package is present but needs something that isn't installed — a real
    failure wearing the same exception type as an absent submodule."""
    assert _classify_top_level(
        ModuleNotFoundError("No module named 'pyotp'", name="pyotp")
    ) == "loud"


def test_missing_name_in_the_top_level_import_is_loud():
    """A plain ImportError (package exists, NAME doesn't) is a bug."""
    exc = ImportError("cannot import name 'register_enterprise'")
    assert not isinstance(exc, ModuleNotFoundError)
    assert _classify_top_level(exc) == "loud"


# --- the structural guarantee (what a name check cannot provide) -------------

def test_registration_call_is_outside_the_importerror_arms():
    """`register_enterprise(app)` must sit in the `else` arm, so EVERY exception
    it raises reaches the diagnostic rather than an ImportError handler.

    This is the case a name check gets wrong: a module doing
    `from .some_missing_sibling import x` inside `register()` raises
    `ModuleNotFoundError(name='enterprise.backend.some_missing_sibling')`, whose
    first segment IS `enterprise` — indistinguishable from an absent submodule by
    name alone.

    Parsed with `ast`, not string search: the surrounding comments mention
    `register_enterprise(app)` in prose, and a substring check matches those.
    """
    import ast

    tree = ast.parse(MAIN_PY.read_text())

    def calls_register(nodes) -> bool:
        return any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "register_enterprise"
            for node in nodes
            for n in ast.walk(node)
        )

    guarded = [
        t for t in ast.walk(tree)
        if isinstance(t, ast.Try) and calls_register(t.orelse)
    ]
    assert guarded, (
        "register_enterprise(app) must be called from the `else` arm of the "
        "try/except that guards the top-level import (#1653)"
    )

    for t in guarded:
        # It must NOT also be reachable from the try body those handlers guard.
        assert not calls_register(t.body), (
            "register_enterprise(app) still shares the try that catches "
            "ImportError — a ModuleNotFoundError from inside register() would be "
            "reported as 'submodule not present'"
        )
        # The else arm must itself catch broadly and report.
        inner = [n for n in ast.walk(ast.Module(body=t.orelse, type_ignores=[]))
                 if isinstance(n, ast.Try)]
        assert inner, "the registration call needs its own try/except"
        handlers = [h for tr in inner for h in tr.handlers]
        assert any(getattr(h.type, "id", None) == "Exception" for h in handlers), (
            "registration must be guarded by `except Exception` so a "
            "ModuleNotFoundError from register() reaches the diagnostic"
        )


def test_a_missing_sibling_from_register_is_treated_as_a_bug():
    """Behavioural proof, with the real exception shape a lazy sibling import
    produces — the case that motivated the structural change."""
    missing_sibling = ModuleNotFoundError(
        "No module named 'enterprise.backend.gone'", name="enterprise.backend.gone")

    # A name check alone calls this benign — precisely the trap.
    assert _classify_top_level(missing_sibling) == "benign"

    # Raised from register_enterprise(app), which no longer sits under the
    # ImportError arms, it is handled as a bug. Mirror that placement:
    handled = None
    try:                                  # the top-level import
        pass
    except ModuleNotFoundError:           # pragma: no cover — not taken
        handled = "benign"
    else:
        try:
            raise missing_sibling
        except Exception:
            handled = "loud"
    assert handled == "loud"


def test_benign_message_is_gated_on_the_enterprise_package():
    src = MAIN_PY.read_text()
    start = src.index("from enterprise.backend import register_enterprise")
    block = src[start:start + 3000]
    assert 'split(".")[0] == "enterprise"' in block, (
        "the benign branch must verify the missing module IS the enterprise package"
    )


# --- CI string contract ------------------------------------------------------

def test_ci_grepped_strings_are_unchanged():
    """`.github/workflows/*` greps stdout for these exact strings — the
    `backend boots without enterprise submodule` job asserts the OSS-only one, so
    a reworded message would break CI silently."""
    src = MAIN_PY.read_text()
    assert '"Trinity Enterprise modules registered"' in src
    assert "Trinity Enterprise submodule not present — OSS-only build " in src
