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

``AVATAR_DIR`` is read through this module at call time, so a test that
patches the constant moves every caller. A caller that needs the directory to
exist calls ``ensure_avatar_store()``, never ``AVATAR_DIR.mkdir()``: the store
is the normalised directory the barrier checks against, and a root whose raw
spelling resolves elsewhere (a ``..`` after a symlink) would otherwise get one
directory created while every write goes to the other.
"""

import os
from pathlib import Path
from typing import Optional

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


def _store_base() -> str:
    """The normalised store directory with a trailing separator: the prefix
    every joined path must start with."""
    return os.path.join(os.path.normpath(str(AVATAR_DIR)), "")


def ensure_avatar_store() -> None:
    """Create the store directory the barrier checks against, if missing."""
    os.makedirs(_store_base(), exist_ok=True)


def avatar_path_or_none(agent_name: str, suffix: str) -> Optional[Path]:
    """``AVATAR_DIR/<agent_name><suffix>``, or ``None`` when it would leave the store."""
    filename = f"{agent_name}{suffix}"
    if "\x00" in filename:
        return None
    base = _store_base()
    fullpath = os.path.normpath(os.path.join(base, filename))
    if not fullpath.startswith(base):
        return None
    if _component_too_long(fullpath[len(base):]):
        return None
    return Path(fullpath)
