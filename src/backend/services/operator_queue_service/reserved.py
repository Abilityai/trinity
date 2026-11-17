"""Runtime id-prefix reservation (trinity-enterprise#1111, T4).

`_oq._RESERVED_ID_PREFIXES` is the OSS literal of platform-reserved
operator-queue id prefixes. A module the platform registers at RUNTIME —
today the enterprise `platform_alerts` emitters, which live outside this
repo — cannot add a line to that literal, so it reserves its prefix here.

The literal stays IMMUTABLE for two reasons. `db.migrations`'s frozen
`PLATFORM_ALERT_HEAL_PREFIXES` is pinned against it member-for-member
(`tests/unit/test_2372_platform_alert_ack.py`), so a tuple that grows at
import time would make that one-time heal's list look incomplete; and a paid
feature's name does not belong in an OSS literal. Every reader therefore
calls `reserved_id_prefixes()`, never the literal — a reservation only some
seams honour is not a reservation.

Its own module (#3785, G1): the registry is mutable state behind two public
functions, and `__init__` sits at the 800-line cap. `__init__` re-exports
both; inside the package every reader names this module.
"""
import logging
import re

from services import operator_queue_service as _oq

logger = logging.getLogger("services.operator_queue_service")

_EXTRA_RESERVED_ID_PREFIXES: tuple = ()

# A reservable prefix: id-shaped (the `_ID_RE` alphabet), already lower-case and
# unpadded — every reader normalizes the CANDIDATE that way before the
# `startswith`, so an upper-case reservation would silently never match — and
# ending in the family separator with at least one character before it (a bare
# `-` would reserve every id that starts with one).
_RESERVABLE_PREFIX_RE = re.compile(r"^[a-z0-9._:-]*[a-z0-9][a-z0-9._:-]*[-_]$")


def reserve_id_prefix(prefix: str) -> None:
    """Reserve an operator-queue id prefix so no agent can author a row under it.

    An unreserved prefix is one an agent CAN author, which means it can
    pre-create the deterministic id of the platform alert about itself and
    swallow it through `create_item`'s ON CONFLICT DO NOTHING (#1632, #2744).
    A module that raises platform rows — `platform_alerts.register(Kind)`
    verifies it — calls this at registration time.

    Idempotent: re-reserving, or reserving a prefix the literal already
    carries, is a no-op. A malformed prefix raises `ValueError` rather than
    being normalized: a reservation that does not match the ids it was meant to
    cover is worse than no reservation, because it reads as protection.
    """
    global _EXTRA_RESERVED_ID_PREFIXES
    if not isinstance(prefix, str) or not _RESERVABLE_PREFIX_RE.match(prefix):
        raise ValueError(
            f"reserved id prefix must be lower-case, id-shaped and end in '-' or "
            f"'_': {prefix!r}")
    if prefix in _oq._RESERVED_ID_PREFIXES or prefix in _EXTRA_RESERVED_ID_PREFIXES:
        return
    _EXTRA_RESERVED_ID_PREFIXES = _EXTRA_RESERVED_ID_PREFIXES + (prefix,)


def reserved_id_prefixes() -> tuple:
    """Every platform-reserved id prefix: the literal plus what was reserved.

    The single read surface for the ~11 reader seams (both ingest hijack
    guards, `is_platform_minted`, `raise_ask`'s refusal, and the cap /
    exclusion reads). Returns a tuple so it can be passed straight to
    `str.startswith`.
    """
    return _oq._RESERVED_ID_PREFIXES + _EXTRA_RESERVED_ID_PREFIXES
