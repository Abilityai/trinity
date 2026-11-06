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
* a NUL byte is refused by name: it survives both steps above;
* a path component longer than ``NAME_MAX`` bytes is refused too. The
  filesystem would refuse it anyway, but by raising ``ENAMETOOLONG`` from
  ``exists()``/``stat()``, which is a 500 on the public read routes. No agent
  with avatar files can have such a name, so a refusal hides nothing: the read
  routes answer it exactly as they answer any agent with no avatar.

Never add a ``".." in name`` check: ``a..b`` is a valid agent name.

``AVATAR_DIR`` must be an absolute path. With a root of ``"."`` or ``""`` the
base normalises to ``"./"`` while a joined name normalises to ``"x"``, so every
name would be refused.

``AVATAR_DIR`` is read through this module at call time. Callers that need the
directory itself (``mkdir``) use ``avatar_paths.AVATAR_DIR``, never a
from-import alias, so a test that patches the constant moves every caller.
"""

import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

AVATAR_DIR = Path("/data/avatars")

#: The longest single path component, in bytes, that Linux filesystems store
#: (ext4, xfs, overlayfs, tmpfs). Bytes, not characters: a 128-character name
#: of two-byte letters is already over it.
NAME_MAX = 255


def _component_too_long(relative: str) -> bool:
    # surrogatepass: never raises, and only ever over-counts a lone surrogate.
    return any(
        len(part.encode("utf-8", "surrogatepass")) > NAME_MAX
        for part in relative.split(os.sep)
    )


def avatar_path_or_none(agent_name: str, suffix: str) -> Optional[Path]:
    """``AVATAR_DIR/<agent_name><suffix>``, or ``None`` when it would leave the store."""
    filename = f"{agent_name}{suffix}"
    if "\x00" in filename:
        return None
    base = os.path.join(os.path.normpath(str(AVATAR_DIR)), "")
    fullpath = os.path.normpath(os.path.join(base, filename))
    if not fullpath.startswith(base):
        return None
    if _component_too_long(fullpath[len(base):]):
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
