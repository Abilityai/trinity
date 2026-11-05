"""The avatar store: its one directory and the one barrier every avatar path
goes through (AVATAR-001, AVATAR-002).

Avatar file names are built from an agent name that arrives in a URL path (the
read routes are unauthenticated: avatars are public assets) or from the
database. Every construction under ``AVATAR_DIR`` goes through
``avatar_path_or_none`` so containment is decided in one place, in a shape
CodeQL's ``py/path-injection`` query recognises as a barrier:

* the base is normalised and given a trailing separator, so a sibling that
  shares its prefix (``/data/avatars2``) is outside it, and a root written as
  ``./x`` or ``a//b`` still matches the normalised joined path;
* the joined path is normalised and must start with that base;
* a NUL byte is refused by name: it survives both steps above.

Never add a ``".." in name`` check: ``a..b`` is a valid agent name.

``AVATAR_DIR`` is read through this module at call time. Callers that need the
directory itself (``mkdir``) use ``avatar_paths.AVATAR_DIR``, never a
from-import alias, so a test that patches the constant moves every caller.
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

AVATAR_DIR = Path("/data/avatars")


def avatar_path_or_none(agent_name: str, suffix: str) -> Optional[Path]:
    """``AVATAR_DIR/<agent_name><suffix>``, or ``None`` when it would leave the store."""
    filename = f"{agent_name}{suffix}"
    if "\x00" in filename:
        return None
    base = os.path.join(os.path.normpath(str(AVATAR_DIR)), "")
    fullpath = os.path.normpath(os.path.join(base, filename))
    if not fullpath.startswith(base):
        return None
    return Path(fullpath)


def avatar_path(agent_name: str, suffix: str, *, detail: str = "Agent not found") -> Path:
    """As ``avatar_path_or_none``, but a refusal is a 404 carrying ``detail``.

    The default text keeps the #186 uniform-404 contract of the owner routes; a
    read route passes its own not-found text, so a refused name answers exactly
    like a missing file and the refusal is not an oracle.
    """
    path = avatar_path_or_none(agent_name, suffix)
    if path is None:
        raise HTTPException(status_code=404, detail=detail)
    return path
