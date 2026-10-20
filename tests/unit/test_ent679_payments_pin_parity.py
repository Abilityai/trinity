"""payments-py pin parity + import smoke (abilityai/trinity-enterprise#679).

The #1891 shape, applied to a Python dependency instead of a Python version.
``tests/requirements-test.txt`` carried a FLOOR (``payments-py>=1.0.0``) while
``docker/backend/Dockerfile`` pinned ``1.2.1``, so CI exercised 1.18.0 against a
1.2.1 image — a divergence that, by construction, cannot catch an incompatible
SDK call (trinity-enterprise#763). Both are now exact and equal, and this file
is what keeps them that way.

The import smoke is the second half and is not decoration:
``payments_py.payments`` imports the a2a package at module load, so ONE missing
transitive dependency flips ``NEVERMINED_AVAILABLE`` to False and both payment
doors answer 501 — with a green build and no error anywhere except a WARNING
log. That is why the 1.18.0 runtime set is pinned explicitly in the Dockerfile
(T8) and why those pins are compared against what is actually importable here.
"""
from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "docker" / "backend" / "Dockerfile"
REQUIREMENTS = REPO / "tests" / "requirements-test.txt"

#: The 1.18.0 runtime dependency set pinned explicitly in the backend image.
#: Each name must be pinned exactly in the Dockerfile AND resolve to that same
#: version in the environment running this test.
TRANSITIVE_PINS = (
    "a2a-sdk",
    "mcp",
    "python-socketio",
    "pyjwt",
    "jsonschema",
    "websocket-client",
    "helicone-helpers",
    "black",
    "mkdocs",
    "mkdocs-material",
    "mkdocstrings",
    "mike",
    "pytest-asyncio",
)

#: Already floating in the backend image BEFORE this bump — `docker`, `twilio`
#: and `google-genai` each pull `requests`, so payments-py does not newly expose
#: it. Listed with its reason so a later reader can tell reviewed from
#: overlooked; it is not a licence to add more.
PRE_EXISTING_FLOATERS = frozenset({"requests"})


def _dockerfile_pin(package: str) -> str | None:
    """The exact version ``package`` is pinned to in the backend Dockerfile.

    Tolerates the extras form (``mkdocstrings[python]==0.29.1``) and the quoting
    the file uses for any requirement containing a bracket.
    """
    pattern = re.compile(
        r'^\s*"?' + re.escape(package) + r'(?:\[[^\]]+\])?==([0-9][^"\s\\]*)"?\s*\\?\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(DOCKERFILE.read_text())
    return match.group(1) if match else None


def _dockerfile_declares(package: str) -> bool:
    """Is ``package`` constrained in the image at all (exactly OR as a range)?

    Weaker than :func:`_dockerfile_pin` on purpose: the "nothing floats"
    coverage check cares only that pip is not free to pick, while the
    per-package parity check below demands an exact pin.
    """
    pattern = re.compile(
        r'^\s*"?' + re.escape(package) + r'(?:\[[^\]]+\])?\s*[=<>!]',
        re.IGNORECASE | re.MULTILINE,
    )
    return pattern.search(DOCKERFILE.read_text()) is not None


def _requirements_pin(package: str) -> str | None:
    pattern = re.compile(
        r"^" + re.escape(package) + r"(?:\[[^\]]+\])?==([0-9][^\s;]*)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(REQUIREMENTS.read_text())
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# 1. The two pins agree, and both are exact
# ---------------------------------------------------------------------------

def test_payments_py_pin_is_exact_in_both_files():
    image = _dockerfile_pin("payments-py")
    tests = _requirements_pin("payments-py")
    assert image is not None, "payments-py is not pinned exactly in docker/backend/Dockerfile"
    assert tests is not None, (
        "payments-py is not pinned exactly in tests/requirements-test.txt — a floor "
        "(>=) is what let CI run a different SDK than the image (#763)"
    )
    assert image == tests, (
        f"payments-py pin divergence: image {image} vs tests {tests}. CI must exercise "
        "the version production ships."
    )


def test_payments_py_installed_version_matches_the_pin():
    """The environment running the suite IS the pinned version."""
    assert md.version("payments-py") == _dockerfile_pin("payments-py")


def test_payments_py_is_at_least_the_inband_release():
    """1.18.0 is the floor the in-band metadata flow needs (ruling 3).

    Below it, ``payments_py.a2a.inband`` does not exist and the A2A x402 rail is
    header-only.
    """
    pin = _dockerfile_pin("payments-py")
    major, minor = (int(part) for part in pin.split(".")[:2])
    assert (major, minor) >= (1, 18), f"payments-py {pin} predates the in-band A2A flow"


# ---------------------------------------------------------------------------
# 2. The transitive runtime set is pinned, and pinned to what is importable
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("package", TRANSITIVE_PINS)
def test_transitive_runtime_dependency_is_pinned_to_the_installed_version(package):
    pinned = _dockerfile_pin(package)
    assert pinned is not None, (
        f"{package} is a RUNTIME dependency of payments-py but is not pinned in "
        "docker/backend/Dockerfile — the image would float it (F6)"
    )
    assert pinned == md.version(package), (
        f"{package}: image pins {pinned}, this environment resolved "
        f"{md.version(package)}. Image and CI must agree."
    )


def test_pinned_set_covers_every_unconditional_runtime_requirement():
    """A future payments-py gaining a dependency must not float it silently.

    Only unconditional requirements are in scope — an ``extra ==`` marker is
    opt-in and Trinity installs no extras.
    """
    unpinned = []
    for raw in md.requires("payments-py") or []:
        if "extra ==" in raw:
            continue
        name = re.split(r"[\s(\[<>=!;]", raw.strip(), maxsplit=1)[0]
        if not _dockerfile_declares(name) and name.lower() not in PRE_EXISTING_FLOATERS:
            unpinned.append(name)
    assert unpinned == [], (
        f"payments-py runtime dependencies are unconstrained in the backend image: "
        f"{unpinned}. Pin them (T8) — an import failure in any one of them turns both "
        "payment doors into a silent 501."
    )


# ---------------------------------------------------------------------------
# 3. Import smoke — the SDK actually loads under this pin set
# ---------------------------------------------------------------------------

def test_payments_py_imports_and_nevermined_is_available():
    """The 501-with-a-green-build failure mode, caught at its own layer.

    ``NEVERMINED_AVAILABLE`` is computed by a try/except ImportError at module
    import, so this executes the exact expression production depends on.
    """
    from services.nevermined_payment_service import NEVERMINED_AVAILABLE

    assert NEVERMINED_AVAILABLE is True, (
        "payments_py failed to import under the pinned dependency set — both payment "
        "doors would answer 501. Check the Dockerfile transitive pins."
    )


def test_inband_and_x402_modules_are_importable():
    """The 1.18.0 surfaces the A2A gate is built on (ruling 3)."""
    from payments_py.a2a.inband import extract_inband_token
    from payments_py.x402.token import encode_access_token
    from payments_py.x402.helpers import build_payment_required

    assert callable(extract_inband_token)
    assert callable(encode_access_token)
    assert callable(build_payment_required)


def test_facilitator_call_signatures_are_positional_compatible():
    """1.18.0 keeps the argument ORDER the 1.2.1 call sites pass positionally.

    ``settle_permissions`` is called with four positional arguments
    (``payment_required, access_token, max_amount, agent_request_id``) in
    ``nevermined_payment_service``; a reordered signature upstream would be a
    silent mis-binding, not an error.
    """
    import inspect

    from payments_py.x402.facilitator_api import FacilitatorAPI

    verify = list(inspect.signature(FacilitatorAPI.verify_permissions).parameters)
    settle = list(inspect.signature(FacilitatorAPI.settle_permissions).parameters)
    assert verify[:3] == ["self", "payment_required", "x402_access_token"]
    assert settle[:5] == [
        "self",
        "payment_required",
        "x402_access_token",
        "max_amount",
        "agent_request_id",
    ]
