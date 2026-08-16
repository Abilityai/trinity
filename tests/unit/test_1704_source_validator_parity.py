"""#1704 — cross-boundary parity for the marketplace `source` validator.

A marketplace `source` is the ONE dangerous plugin argument — it decides where
`claude plugin marketplace add <source>` fetches from, and it flows into that
subprocess as an arg. It is validated by TWO different implementations, one on
each side of the backend↔agent boundary:

  * backend  `services/template_plugins._validate_source` — validates before the
             committed `~/.trinity/plugins.yaml` manifest is written;
  * agent    `agent_server/plugins_reinstall._is_source` — the SOLE gate on the
             untrusted `template.yaml` fallback a source-mode agent's clone
             carries, re-validated at boot BEFORE any `marketplace add`.

Unlike the byte-identical vendored pairs (`credential_paths.py`, `safe_yaml.py`,
Invariant #5), these are two DIFFERENT implementations by design, so no
byte-for-byte parity test guards them — and they silently diverged: the agent
copy accepted any `scheme://` (`ftp://`, `file://`, `ssh://`, `data://`) while
the backend accepted only `https://`, and the LOOSER copy was the one gating
untrusted input (learnings 2026-08-16). "Same charset" in a docstring is not a
test.

This pins agreement with a SHARED accept/reject table run against BOTH copies,
and encodes the safety invariant the learnings entry prescribes: the copy that
gates untrusted input (the agent) must be at least as strict as the other, i.e.
there is NO input the agent accepts but the backend rejects
(`agent_accepts ⊆ backend_accepts`). The reverse asymmetry is allowed and real —
the backend tolerates a leading-dash owner (`-evil/repo`) that the agent (the
exec gate) correctly drops as flag injection — which is exactly the safe
direction.

Both modules are loaded by FILE PATH under unique standalone names (the
`test_2007_mcp_template_render.py` idiom — the agent server ships in its own
image and cannot import `src/backend`). Both source trees ship a
`utils/credential_sanitizer.py`, so `utils` resolves order-dependently; `src`
backend is pinned at the FRONT of `sys.path` so the backend copy's
`utils.credential_sanitizer` import resolves from the backend tree, and the two
`redact_url_userinfo` copies agree on the userinfo boolean the validator reads
regardless, so the collision cannot flip a verdict here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"

pytestmark = pytest.mark.unit


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Pin src/backend at the FRONT so the backend copy's `utils` resolves from the
# backend tree; the agent dir provides top-level `safe_yaml` for the agent copy.
for _p in (str(_AGENT_SERVER), str(_BACKEND)):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)

_backend = _load(
    "_template_plugins_parity_1704",
    _BACKEND / "services" / "template_plugins.py",
)
_agent = _load(
    "_plugins_reinstall_parity_1704",
    _AGENT_SERVER / "plugins_reinstall.py",
)


def _backend_ok(source: str) -> bool:
    normalized, _reason = _backend._validate_source(source)
    return normalized is not None


def _agent_ok(source: str) -> bool:
    return _agent._is_source(source)


# Must be REJECTED by BOTH copies — the security contract. Every entry is an
# argument that would otherwise reach `claude plugin marketplace add <source>`.
SECURITY_REJECT = [
    "https://user:tok@github.com/o/r",  # userinfo credential leak
    "http://github.com/o/r",  # non-https scheme
    "ftp://evil.com/x",  # non-https scheme — the #1704 divergence
    "file:///etc/passwd",  # non-https scheme
    "ssh://host/path",  # non-https scheme
    "data://x",  # non-https scheme
    "git@github.com:o/r",  # ssh/userinfo remote
    "../../etc",  # traversal
    "/abs/path",  # absolute local path
    "not a repo; rm -rf",  # shell metacharacters
    "https://github.com/o/r$(x)",  # metacharacter inside the URL charset
    "--flag",  # flag injection
    "a/b/c",  # not owner/repo
    "",  # empty
]

# Must be ACCEPTED by BOTH copies — the legitimate declaration forms.
ACCEPT = [
    "abilityai/abilities",  # owner/repo shorthand
    "owner/repo",
    "o-w/r-p",  # internal dash is legitimate
    "https://github.com/o/r",  # bare https URL
]


@pytest.mark.parametrize("source", SECURITY_REJECT)
def test_both_copies_reject(source):
    """The security-critical set — both the writer and the untrusted-input gate
    must refuse it. Pins the #1704 non-https-scheme fix on BOTH sides at once."""
    assert _backend_ok(source) is False, f"backend must reject {source!r}"
    assert _agent_ok(source) is False, f"agent must reject {source!r}"


@pytest.mark.parametrize("source", ACCEPT)
def test_both_copies_accept(source):
    assert _backend_ok(source) is True, f"backend must accept {source!r}"
    assert _agent_ok(source) is True, f"agent must accept {source!r}"


def test_agent_gate_is_never_more_permissive_than_backend():
    """The safety invariant (learnings 2026-08-16): the agent copy gates the
    untrusted `template.yaml` fallback and runs the actual `marketplace add`, so
    it must be at least as strict as the backend writer — there is NO input the
    agent accepts but the backend rejects. The reverse asymmetry (backend laxer)
    is allowed and covered below.
    """
    probe = (
        SECURITY_REJECT
        + ACCEPT
        + [
            "-evil/repo",  # leading-dash owner — the one real divergence
            "-evil",
            "https://github.com/o/r/../x",  # traversal inside a URL
            "UPPER/Case",
            "a.b/c.d",
            "https://sub.github.com/o/r",
            "ok/ok",
        ]
    )
    for source in probe:
        if _agent_ok(source):
            assert _backend_ok(source), (
                f"agent accepts {source!r} but backend rejects it — the "
                f"untrusted-input gate is MORE permissive than the writer, the "
                f"unsafe divergence direction"
            )


def test_agent_rejects_leading_dash_owner():
    """The `owner/repo` form with a leading-dash owner is flag injection into
    `marketplace add`; the agent (the exec gate) drops it. The backend tolerates
    it (its `_NAME_RE` admits a leading `-`) — a divergence in the SAFE direction
    only, because the agent gate stops it before any subprocess. This documents
    the known asymmetry and pins the agent's rejection, which its own test file
    (`test_1704_plugins_reinstall.py`) covers for the bare name but not the
    `owner/repo` form.
    """
    assert _agent_ok("-evil/repo") is False
    # Backend laxity here is intentionally NOT asserted as a value to avoid a
    # characterization lock — if the backend is later tightened to match, only
    # the comment above needs updating, not this test.
