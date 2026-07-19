"""Shared leaf helpers for the schedule operations package.

`_norm_ts` is the sole cross-slice module-global (imported by the
executions / analytics / stats slices). `_generate_id` is an MRO-shared
static on `ScheduleCommonMixin`, inherited by every schedule slice."""

import secrets
from typing import Optional


from utils.helpers import to_utc_iso, parse_iso_timestamp

def _norm_ts(value: Optional[str]) -> Optional[str]:
    """Normalize a stored ISO timestamp to UTC with an explicit 'Z' suffix (#1474).

    Summary/list readers return raw ``dict(row)`` values straight from the DB.
    A scheduler-written naive string (no offset) then serializes naive out of
    Pydantic, and JS ``new Date(naive)`` parses it as *local* time — shifting
    the row by the viewer's UTC offset. ``parse_iso_timestamp`` assumes UTC for
    naive strings (matching every Python read path), and ``to_utc_iso`` re-emits
    the 'Z' form the already-correct sibling readers produce. None passes through.
    """
    return to_utc_iso(parse_iso_timestamp(value)) if value else None

class ScheduleCommonMixin:
    """Base mixin: MRO-shared static helpers used across schedule slices."""

    @staticmethod
    def _generate_id() -> str:
        """Generate a unique ID."""
        return secrets.token_urlsafe(16)
