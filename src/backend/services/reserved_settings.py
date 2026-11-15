"""Settings keys a registered module owns, kept off the generic catch-all
(trinity-enterprise#1174).

`routers/settings/generic.py` addresses ANY `system_settings` key. A key a
module reads as an authorization fact — who a machine credential may act on —
must not be writable, deletable or readable through it by anything but its
own dedicated route, or the gate that route enforces is cosmetic. A module
registers the key here at startup; the generic routes then:

* refuse PUT and DELETE for every caller, with a 422 naming the dedicated route;
* hide the key from a non-interactive caller on GET (404, as if absent) and
  from its `GET /api/settings` listing.

OSS names no module's key: the registry is filled at runtime, so this file
carries the mechanism only.
"""
from __future__ import annotations

from typing import Dict, Optional

_RESERVED: Dict[str, str] = {}


def register_reserved_setting(key: str, managed_by: str) -> None:
    """Reserve `key`; `managed_by` names the route that writes it (shown in the 422)."""
    _RESERVED[key] = managed_by


def unregister_reserved_setting(key: str) -> None:
    _RESERVED.pop(key, None)


def managed_by(key: str) -> Optional[str]:
    """The dedicated route for a reserved key, or None when it is not reserved."""
    return _RESERVED.get(key)
