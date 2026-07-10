"""Parity enforcement for the per-agent Redis keyspace registry (Issue #1560).

``services/agent_runtime_state.py`` promises to be the single enumeration point
for every Redis keyspace keyed by **agent name**, so that the next contributor to
add one cannot silently forget to clear it on delete / rename / purge / recreate.
This is that check — the Redis-side twin of
``tests/unit/test_agent_cleanup_parity.py``, which does the same job for the SQL
``AGENT_REFS`` registry.

The failure mode it guards is exactly the one that produced #1560: a keyspace
(``agent:circuit:{name}``) that outlived the agent it described, so a fresh,
healthy container inheriting the name was fast-failed as "unhealthy" by its
predecessor's verdict.

The check is bidirectional:

  forward   — every ``"agent:<segment>"`` string literal in ``src/backend`` is
              either cleared (``CLEARED_KEYSPACES``) or consciously exempted
              (``EXEMPT_KEYSPACES``, which carries the reason).
  backward  — every registered prefix still appears somewhere in the backend
              (catches a stale entry after a keyspace is renamed or removed).

``agent_runtime_state.py`` keeps its service imports function-local precisely so
this test can load it with the stdlib alone — no redis, no fastapi, no docker.
It is therefore a real CI gate and does NOT skip when the backend venv is absent.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _load(mod_name: str, rel_path: str):
    """Load a backend module by file path, bypassing ``services/__init__.py``
    (which imports the Docker SDK transitively)."""
    spec = importlib.util.spec_from_file_location(mod_name, str(_BACKEND / rel_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_ars = _load("trinity_agent_runtime_state", "services/agent_runtime_state.py")

# Matches the opening of a per-agent Redis key *string literal*, e.g.
#   "agent:circuit:"            -> circuit
#   f"agent:slot:{name}:{eid}"  -> slot
#   f"agent:heartbeat:seen:{n}" -> heartbeat
# Comments (`# agent:circuit:{name} HASH`) are deliberately not matched — only
# literals actually build keys.
_KEY_LITERAL = re.compile(r"""["']agent:([a-z_]+)""")


def _segment(prefix: str) -> str:
    """'agent:circuit:' -> 'circuit'"""
    return prefix.split(":")[1]


def _registered_segments() -> dict:
    cleared = {_segment(p): p for p in _ars.CLEARED_KEYSPACES}
    exempt = {_segment(p): p for p in _ars.EXEMPT_KEYSPACES}
    overlap = set(cleared) & set(exempt)
    assert not overlap, f"keyspace both cleared and exempt: {sorted(overlap)}"
    return {**cleared, **exempt}


# `src/backend/enterprise` is an optional private submodule — absent on OSS
# clones, present on core-team ones. Scanning it would make this OSS test's
# result depend on whether the submodule happens to be mounted. Enterprise owns
# its own keyspaces and lifecycle (architecture.md, Invariant #3), so it is
# excluded here rather than half-covered.
_EXCLUDED_DIRS = {"enterprise"}


def _segments_in_backend() -> dict:
    """segment -> sorted list of files that build a key with it."""
    found: dict = {}
    for path in sorted(_BACKEND.rglob("*.py")):
        if _EXCLUDED_DIRS & set(path.relative_to(_BACKEND).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        for seg in _KEY_LITERAL.findall(text):
            found.setdefault(seg, []).append(str(path.relative_to(_BACKEND)))
    return found


def test_every_per_agent_keyspace_is_registered_or_exempt():
    """Forward parity: a new `agent:*` keyspace must be registered before it ships.

    If this fails you added a per-agent Redis key. Either clear it in
    `services/agent_runtime_state.py` (add the prefix to `CLEARED_KEYSPACES` and
    wire the delete into `clear_agent_breakers`/`clear_agent_runtime_state`), or
    add it to `EXEMPT_KEYSPACES` **with the reason it must survive**.
    """
    registered = _registered_segments()
    found = _segments_in_backend()

    unregistered = {seg: files for seg, files in found.items() if seg not in registered}
    assert not unregistered, (
        "Unregistered per-agent Redis keyspace(s) found in src/backend — a key that "
        "outlives its agent is the #1560 bug class:\n"
        + "\n".join(
            f"  agent:{seg}:  built in {', '.join(sorted(set(files)))}"
            for seg, files in sorted(unregistered.items())
        )
    )


def test_every_registered_keyspace_still_exists_in_the_backend():
    """Backward parity: catches a stale registry entry after a rename/removal."""
    registered = _registered_segments()
    found = _segments_in_backend()

    stale = sorted(seg for seg in registered if seg not in found)
    assert not stale, (
        "Registry names keyspace(s) that no longer appear in src/backend — "
        f"remove them from agent_runtime_state.py: {[registered[s] for s in stale]}"
    )


def test_exemptions_document_why_they_are_not_cleared():
    """An exemption without a reason is an oversight wearing a disguise."""
    for prefix, reason in _ars.EXEMPT_KEYSPACES.items():
        assert reason and len(reason.strip()) > 40, (
            f"EXEMPT_KEYSPACES[{prefix!r}] must explain why the key must survive"
        )


def test_transport_circuit_is_cleared_not_exempt():
    """The key that caused #1560 must never be quietly moved to the exempt list.

    `agent:circuit:` is read unconditionally on the execution path
    (`task_execution_service` -> `transport_open = not circuit.allow_request()`),
    unlike `agent:dispatch:`, which is gated behind the default-off
    `circuit_breaker_enabled` flag. Exempting it would re-open the bug.
    """
    assert "agent:circuit:" in _ars.CLEARED_KEYSPACES
    assert "agent:circuit:" not in _ars.EXEMPT_KEYSPACES
