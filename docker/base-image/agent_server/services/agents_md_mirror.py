"""Keep ``AGENTS.md`` a refreshed copy of ``CLAUDE.md`` (Abilityai/lilu#627).

Codex and OpenCode read their persistent identity from ``AGENTS.md``, not
``CLAUDE.md``. Upstream mirrors it with a one-time ``cp`` at boot, which froze
the identity at the first boot (every later edit to ``CLAUDE.md`` was lost to
those runtimes). Abilityai/lilu#257 replaced the copy with a symlink, but
ent#819 forbids the platform creating ANY link in an agent home: download and
preview refuse links for every caller.

So the mirror is a plain file, refreshed at boot (``startup.sh``) and again at
the start of every Codex and OpenCode turn. A regular ``AGENTS.md`` is
ambiguous — it is either our copy or an AGENTS.md the template author wrote —
so a provenance record (the sha256 of the last copy WE wrote) decides:

* absent, or a leftover lilu#257 symlink        -> write the copy
* regular file whose sha256 matches the record  -> our stale copy, refresh it
* any other regular file                        -> authored, never overwritten

The record lives in the agent's scratch dir (``$AGENT_TMPDIR``), which the
canonical gitignore already excludes, so it never dirties auto-sync.
``startup.sh`` writes the same record in the same format.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_AGENT_HOME = "/home/developer"
RECORD_NAME = "agents-md.sha256"


def _default_record_path() -> str:
    tmpdir = (
        os.environ.get("AGENT_TMPDIR")
        or os.environ.get("TMPDIR")
        or os.path.join(_AGENT_HOME, ".tmp")
    )
    return os.path.join(tmpdir, RECORD_NAME)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(record_path: str) -> Optional[str]:
    try:
        with open(record_path, "r", encoding="ascii") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def _write_record(record_path: str, digest: str) -> None:
    os.makedirs(os.path.dirname(record_path) or ".", exist_ok=True)
    with open(record_path, "w", encoding="ascii") as fh:
        fh.write(digest + "\n")


def refresh_agents_md(home: Optional[str] = None, record_path: Optional[str] = None) -> str:
    """Refresh ``AGENTS.md`` from ``CLAUDE.md`` if, and only if, it is ours.

    Returns one of ``no-source``, ``current``, ``refreshed``, ``authored`` or
    ``error``. Never raises: a turn must not fail because its identity mirror
    could not be refreshed.
    """
    home = home or _AGENT_HOME
    record_path = record_path or _default_record_path()
    source = os.path.join(home, "CLAUDE.md")
    target = os.path.join(home, "AGENTS.md")
    try:
        if os.path.islink(source) or not os.path.isfile(source):
            return "no-source"
        with open(source, "rb") as fh:
            want = fh.read()
        digest = _sha256(want)

        if os.path.islink(target):
            pass  # the lilu#257-era link: ours, replaced by a copy below
        elif os.path.exists(target):
            with open(target, "rb") as fh:
                have = fh.read()
            if have == want:
                if _read_record(record_path) != digest:
                    _write_record(record_path, digest)
                return "current"
            if _read_record(record_path) != _sha256(have):
                return "authored"

        tmp = os.path.join(home, ".AGENTS.md.trinity-tmp")
        with open(tmp, "wb") as fh:
            fh.write(want)
        os.replace(tmp, target)  # replaces a leftover link itself, never its target
        _write_record(record_path, digest)
        return "refreshed"
    except OSError as exc:
        logger.warning("[AGENTS.md] could not refresh the CLAUDE.md mirror: %s", exc)
        return "error"
