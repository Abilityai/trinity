"""Parity guard for the credential-path policy (#11, Invariant #5).

The agent server is a separate image and cannot import ``src/backend``, so the
curated policy is vendored into the base image. Both copies MUST stay
byte-identical — otherwise the backend gate and the agent-server second layer
could diverge and one would enforce a different allowlist than the other.

Edit the canonical file (``src/backend/services/credential_paths.py``) and copy
it over the agent-server vendored file to keep this green.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CANON = _ROOT / "src/backend/services/credential_paths.py"
_VENDORED = _ROOT / "docker/base-image/agent_server/credential_paths.py"


def test_policy_copies_are_byte_identical():
    assert _CANON.exists() and _VENDORED.exists()
    assert _CANON.read_bytes() == _VENDORED.read_bytes(), (
        "credential_paths.py drifted between backend and agent-server — "
        "re-copy the canonical file over the vendored one."
    )


def test_canonical_policy_behaves():
    from services.credential_paths import is_allowed_credential_path as ok
    assert ok(".config/gcloud/sa.json") and ok("client.pem") and ok(".ssh/id_rsa")
    assert not ok(".bashrc") and not ok("../x") and not ok(".ssh/authorized_keys")
