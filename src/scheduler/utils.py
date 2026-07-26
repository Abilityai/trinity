"""
Timezone-aware timestamp helpers for the standalone scheduler (#1474).

CANONICAL SOURCE: src/backend/utils/helpers.py. The scheduler is a *separate*
package (own pyproject.toml / Dockerfile that copies only src/scheduler) and
cannot import src/backend at runtime, so ``utc_now_iso`` / ``to_utc_iso`` are
vendored here. This is a **behavioral** mirror, not a byte copy: it must AGREE
ON OUTPUT with the backend (same Z-suffixed ISO format), and ``to_utc_iso`` is
functionally identical but written with an early return rather than the
backend's if/else — so "regenerate and diff" cannot mechanically verify it.
Edit the backend copy and keep the *outputs* in sync; the contract is enforced
by ``tests/unit/test_1713_scheduler_utils_parity.py`` (#1713). (Contrast
``failure_classifier.py``, whose two copies ARE genuinely byte-identical and are
guarded by a byte-diff test — ``test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity``.)

IMPORTANT: All timestamps the scheduler writes MUST be stored as UTC with the
'Z' suffix so JavaScript (`new Date(...)`) interprets them as UTC — otherwise a
naive string renders shifted by the viewer's timezone offset (#1474).

``parse_scheduler_ts`` is scheduler-specific: it returns **naive UTC** so the
existing row models (which are naive) and the duration math
(``datetime.utcnow() - started_at``, also naive) keep working unchanged once
writes start emitting 'Z' (a tz-aware read minus a naive utcnow would raise).
"""

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string with 'Z' suffix.

    Mirror of ``src/backend/utils/helpers.py::utc_now_iso``.
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def to_utc_iso(dt: datetime) -> str:
    """Convert a datetime to a UTC ISO 8601 string with 'Z' suffix.

    Naive input is assumed to already be UTC; aware input is converted.
    Mirror of ``src/backend/utils/helpers.py::to_utc_iso``.
    """
    if dt.tzinfo is None:
        return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def parse_scheduler_ts(timestamp: str) -> datetime:
    """Parse a stored ISO timestamp to a **naive UTC** datetime.

    Tolerant of every shape the column now holds during/after rollout:
    - "…Z"        (new scheduler + backend writes)
    - "…+03:00"   (backend-written next_run_at in the schedule's own zone)
    - "…"         (legacy naive rows already on disk — assumed UTC)

    The 'Z' is stripped to '+00:00' *before* ``fromisoformat`` so the parse is
    valid on the declared ``requires-python`` (bare-'Z' support in
    ``fromisoformat`` only landed in 3.11). An aware result is converted to UTC
    and its tzinfo dropped, preserving the *instant* while keeping the naive
    model type — so downstream ``aware − naive`` subtraction never happens.
    """
    if timestamp.endswith('Z'):
        timestamp = timestamp[:-1] + '+00:00'

    dt = datetime.fromisoformat(timestamp)

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt
