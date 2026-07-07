"""Unit tests for the webhook HMAC signature helper (trinity-enterprise#77).

`services/webhook_signature.py` is the net-new security primitive: when a
schedule webhook has signature auth enabled, the caller must sign the RAW body
with the per-webhook secret (HMAC-SHA256) in the X-Trinity-Signature header, and
the public trigger fails closed on a missing/invalid signature.

Pure + dependency-free — loaded directly, no Redis/DB/services import.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_spec = importlib.util.spec_from_file_location(
    "webhook_signature_under_test",
    str(_BACKEND / "services" / "webhook_signature.py"),
)
sig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sig)

SECRET = "whsec_" + "a" * 32
BODY = b'{"context":"do the thing"}'


class TestComputeSignature:
    pytestmark = pytest.mark.unit

    def test_canonical_prefix_and_hexdigest(self):
        out = sig.compute_signature(SECRET, BODY)
        assert out.startswith("sha256=")
        assert len(out) == len("sha256=") + 64  # sha256 hex

    def test_deterministic(self):
        assert sig.compute_signature(SECRET, BODY) == sig.compute_signature(SECRET, BODY)

    def test_body_bound(self):
        assert sig.compute_signature(SECRET, BODY) != sig.compute_signature(SECRET, b"other")

    def test_secret_bound(self):
        assert sig.compute_signature(SECRET, BODY) != sig.compute_signature("whsec_other", BODY)


class TestVerifySignature:
    pytestmark = pytest.mark.unit

    def test_valid(self):
        assert sig.verify_signature(SECRET, BODY, sig.compute_signature(SECRET, BODY)) is True

    def test_valid_without_prefix(self):
        raw = sig.compute_signature(SECRET, BODY).split("=", 1)[1]
        assert sig.verify_signature(SECRET, BODY, raw) is True

    def test_tampered_body_rejected(self):
        good = sig.compute_signature(SECRET, BODY)
        assert sig.verify_signature(SECRET, b"tampered", good) is False

    def test_wrong_secret_rejected(self):
        good = sig.compute_signature(SECRET, BODY)
        assert sig.verify_signature("whsec_wrong", BODY, good) is False

    def test_missing_header_rejected(self):
        assert sig.verify_signature(SECRET, BODY, None) is False
        assert sig.verify_signature(SECRET, BODY, "") is False

    def test_empty_secret_rejected(self):
        assert sig.verify_signature("", BODY, sig.compute_signature(SECRET, BODY)) is False

    def test_empty_body_signs_and_verifies(self):
        # A context-less trigger sends no body — it must still authenticate.
        assert sig.verify_signature(SECRET, b"", sig.compute_signature(SECRET, b"")) is True

    def test_garbage_header_rejected(self):
        assert sig.verify_signature(SECRET, BODY, "sha256=not-a-real-digest") is False

    def test_header_and_envelope_constants(self):
        assert sig.SIGNATURE_HEADER == "X-Trinity-Signature"
        assert sig.SECRET_ENVELOPE_KEY == "webhook_secret"
