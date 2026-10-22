"""Create-time runtime validation (#1187 review).

``create_agent_internal`` calls ``validate_runtime(config.runtime)`` after the
runtime is finalized (request value, possibly overridden by the template). A
known runtime (or the unset default) passes; a typo'd one raises HTTP 400 here
instead of letting the agent container crash-loop on boot when the agent-side
``get_runtime()`` can't resolve ``AGENT_RUNTIME``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

# Cross-file sys.modules hygiene (#1187). Sibling unit modules
# (test_inject_assigned_credentials, test_start_agent_skip_inject,
# test_subscription_auto_switch_no_cred_import) replace
# ``services.agent_service[.helpers]`` with Mocks in ``sys.modules`` at
# module-collection time and never restore them; the unit conftest's
# baseline-restore covers neither key (not in ``_SYS_MODULES_BASELINE_VALUES``
# nor ``_POP_PREFIXES``). Under pytest-randomly that Mock can be resident when
# THIS module is imported, making the ``from``-import below bind
# ``KNOWN_RUNTIME_NAMES``/``validate_runtime`` to Mock attributes ("'Mock'
# object is not iterable"). Evict the stubbed subtree first so we always bind
# the real module from disk, independent of collection order.
_STUBBED_MODULE_NAMES = [
    "services.agent_service.helpers",
    "services.agent_service.read_only",
    "services.agent_service.file_sharing",
    "services.agent_service",
]

# Only a STAND-IN is evicted. Evicting the genuine modules too would re-import
# them as second copies, and every module that already bound a name from the
# first copy (e.g. a router's `get_accessible_agents`, bound when another file
# imported `main` at collection) would then be patched through the wrong copy.
_REAL_AGENT_SERVICE_DIR = str(
    Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "agent_service")


def _is_stand_in(module) -> bool:
    path = getattr(module, "__file__", None)
    return not (path and str(Path(path).resolve()).startswith(_REAL_AGENT_SERVICE_DIR))


for _stub in _STUBBED_MODULE_NAMES:  # import-time eviction (monkeypatch can't reach)
    if _stub in sys.modules and _is_stand_in(sys.modules[_stub]):
        sys.modules.pop(_stub, None)

from services.agent_service.helpers import (  # noqa: E402  (must follow the stub eviction above)
    KNOWN_RUNTIME_NAMES,
    validate_runtime,
)


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot/restore the stubbed subtree around each test.

    Pairs with ``_STUBBED_MODULE_NAMES`` to form the sanctioned snapshot/restore
    escape hatch the sys.modules lint recognizes (precedent:
    tests/unit/test_telegram_webhook_backfill.py), and prevents this module's
    import-time eviction from leaking the unstubbed real modules into the
    collection state other files depend on.
    """
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def test_known_runtimes_pass():
    for runtime in KNOWN_RUNTIME_NAMES:
        validate_runtime(runtime)  # must not raise
    # case-insensitive — template/env casing varies
    validate_runtime("Codex")
    validate_runtime("Claude-Code")


def test_unset_or_empty_is_valid_default():
    """Back-compat: unset/empty is the Claude default, not an error."""
    validate_runtime(None)
    validate_runtime("")


def test_unknown_runtime_raises_400():
    with pytest.raises(HTTPException) as exc:
        validate_runtime("codez")  # typo
    assert exc.value.status_code == 400
    assert "codez" in exc.value.detail


def test_codex_is_a_known_runtime():
    """Guard against the backend list drifting from the agent-side
    ``runtime_adapter.KNOWN_RUNTIMES`` — codex must be accepted."""
    assert "codex" in KNOWN_RUNTIME_NAMES
    validate_runtime("codex")


def test_opencode_is_a_known_runtime():
    """#2981: OpenCode must be accepted at create, any casing."""
    assert "opencode" in KNOWN_RUNTIME_NAMES
    validate_runtime("opencode")
    validate_runtime("OpenCode")
