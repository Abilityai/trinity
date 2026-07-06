"""HMAC-SHA256 signature helper for schedule webhooks (trinity-enterprise#77).

When a schedule webhook has signature auth enabled, the caller must send an
``X-Trinity-Signature: sha256=<hexdigest>`` header where the digest is
``HMAC-SHA256(secret, raw_request_body)``. The secret is minted once, shown to
the operator a single time, and stored only as an AES-256-GCM envelope
(Invariant #12).

This binds the signature to the exact bytes sent — a leaked URL token alone is
no longer sufficient to trigger the schedule (matches the GitHub/Stripe/Twilio
webhook convention). An empty body is signed as the empty byte string, so a
context-less trigger still authenticates.

Pure + dependency-free so it unit-tests without Redis/DB/HTTP.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

SIGNATURE_HEADER = "X-Trinity-Signature"
# Key under which the plaintext secret is stored inside the AES-256-GCM envelope
# (the encryption service serializes a {key: value} dict). Shared by the mint
# path (db/schedules.py) and the verify path (routers/webhooks.py) so the two
# never drift.
SECRET_ENVELOPE_KEY = "webhook_secret"
_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    """Return the canonical ``sha256=<hexdigest>`` signature for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body or b"", hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, provided: Optional[str]) -> bool:
    """Constant-time check that ``provided`` matches the expected signature.

    Returns False (never raises) for a missing/empty/malformed header or a
    mismatch, so the caller can uniformly reject with 401. Accepts the header
    with or without the ``sha256=`` prefix for client leniency, but always
    compares the full canonical form via ``hmac.compare_digest``.
    """
    if not secret or not provided:
        return False
    candidate = provided.strip()
    if not candidate.startswith(_PREFIX):
        candidate = f"{_PREFIX}{candidate}"
    expected = compute_signature(secret, body)
    return hmac.compare_digest(candidate, expected)
