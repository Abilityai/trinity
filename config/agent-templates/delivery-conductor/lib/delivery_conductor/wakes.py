"""Closed normalization for delivery-conductor wake identities."""

from __future__ import annotations

import hashlib

from .contracts import Wake


_SOURCES = frozenset({"direct", "schedule", "reminder", "worker-completion"})


class WakeNormalizationError(ValueError):
    """Raised when untrusted wake metadata is outside the closed source schema."""


def normalize_wake(
    source: str,
    source_event_id: str,
    payload_sha256: str,
) -> Wake:
    """Return one stable wake without retaining its raw source payload."""
    if not isinstance(source, str):
        raise WakeNormalizationError("wake source kind is not supported")
    if source not in _SOURCES:
        raise WakeNormalizationError("wake source kind is not supported")
    if not isinstance(source_event_id, str):
        raise WakeNormalizationError("wake source event identifier is invalid")
    wake_id = hashlib.sha256((source + source_event_id).encode("utf-8")).hexdigest()
    return Wake(wake_id, source, source_event_id, payload_sha256)
