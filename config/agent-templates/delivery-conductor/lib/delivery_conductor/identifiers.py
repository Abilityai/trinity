"""One identifier grammar for runtime input, durable state, and projection."""

from __future__ import annotations

import re


MAX_IDENTIFIER_LENGTH = 128
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_safe_identifier(value: object) -> bool:
    """Return whether ``value`` is safe in every conductor persistence surface."""
    return (
        isinstance(value, str)
        and _IDENTIFIER.fullmatch(value) is not None
        and ".." not in value
    )


def require_safe_identifier(name: str, value: object) -> str:
    """Return a validated identifier or raise without echoing untrusted input."""
    if not is_safe_identifier(value):
        raise ValueError(f"{name} must be a sanitized identifier")
    return value
