#!/usr/bin/env python3
"""PreToolUse hook: inject the owning architecture area file when a session
first edits a path that area owns (#2306).

Why this exists
---------------
Splitting `architecture.md` into on-demand area files only pays if the right
file actually gets opened. It does not, reliably: an agent that half-remembers
a subsystem answers instead of reading, and strengthening the "always check"
prose in the index makes it assert having checked rather than check. So the
index carries a *consequence* per area (a reason to spend the tool call) and
this hook carries the *determinism* — the load is driven by the file being
edited, not by the agent choosing to look.

Source of truth
---------------
The path map is parsed out of the "Architecture Map" table in
`docs/memory/architecture.md`. There is deliberately no sidecar map: a second
copy is a second thing to drift, and the table is already the reviewed artifact.

Contract
--------
Advisory only. Emits `additionalContext` and never blocks a tool call. Injects
each area at most once per session. Exits 0 with no output on ANY error, so a
malformed table, a moved doc, or an unreadable state dir can never wedge
editing — the failure mode is "back to today's behaviour", never "cannot edit".
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

DOC_REL = "docs/memory/architecture.md"
AREA_REL = "docs/memory/architecture"
MAP_HEADING = "## Architecture Map"
STATE_PREFIX = "trinity-arch-hook"

# `| [`file.md`](architecture/file.md) | `glob`<br>`glob` | consequence |`
ROW = re.compile(r"^\|\s*\[`([^`]+)`\]\([^)]+\)\s*\|(.+?)\|(.+?)\|\s*$")
GLOB = re.compile(r"`([^`]+)`")


def repo_root(cwd: str) -> Path | None:
    p = Path(cwd or ".").resolve()
    for cand in (p, *p.parents):
        if (cand / DOC_REL).is_file():
            return cand
    return None


def parse_map(doc: Path) -> list[tuple[str, list[str], str]]:
    """-> [(area_filename, [owned globs], consequence)] from the core's own table."""
    text = doc.read_text(encoding="utf-8")
    start = text.index(MAP_HEADING)
    section = text[start:]
    nxt = section.find("\n## ", 1)
    if nxt != -1:
        section = section[:nxt]
    out = []
    for line in section.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        fname, owns_cell, why = m.group(1), m.group(2), m.group(3)
        globs = GLOB.findall(owns_cell)
        if fname.endswith(".md") and globs:
            out.append((fname, globs, why.strip()))
    return out


def match_score(rel: str, pattern: str) -> int:
    """0 = no match, else the pattern's specificity (its literal prefix length).

    Most-specific-wins matters: `backend.md` owns `src/backend/services/**` as a
    catalog, but `cleanup_service.py` is documented in `reliability.md`. Emitting
    both would point at the catalog on every single service edit, which is the
    noise that gets a hook switched off.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-2]
        ok = rel.startswith(prefix) or rel == pattern[:-3]
    else:
        ok = fnmatch.fnmatch(rel, pattern)
    if not ok:
        return 0
    return len(pattern.split("*")[0])


def state_path(session_id: str) -> Path:
    digest = hashlib.sha256((session_id or "nosession").encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"{STATE_PREFIX}-{digest}.json"


def load_seen(p: Path) -> set[str]:
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(p: Path, seen: set[str]) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    os.replace(tmp, p)


def main() -> None:
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input") or {}
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        return

    root = repo_root(data.get("cwd") or os.getcwd())
    if root is None:
        return
    try:
        rel = str(Path(file_path).resolve().relative_to(root))
    except ValueError:
        return  # edit outside this repo

    scored = []
    for fname, globs, why in parse_map(root / DOC_REL):
        best = max((match_score(rel, g) for g in globs), default=0)
        if best:
            scored.append((best, fname, why))
    if not scored:
        return
    top = max(s for s, _, _ in scored)
    hits = [(f, why) for s, f, why in scored if s == top]

    sp = state_path(data.get("session_id", ""))
    seen = load_seen(sp)
    fresh = [(f, why) for f, why in hits if f not in seen]
    if not fresh:
        return

    blocks = []
    for fname, why in fresh:
        area = root / AREA_REL / fname
        if not area.is_file():
            continue
        seen.add(fname)
        blocks.append(
            f"`{rel}` is owned by the architecture area **{AREA_REL}/{fname}**, "
            f"which is not auto-loaded.\n\nWhy it matters here: {why}\n\n"
            f"Read `{AREA_REL}/{fname}` before changing behaviour in this file. "
            f"If this edit changes what that area documents, update the area file "
            f"in the same change (core editorial rule 4)."
        )
    if not blocks:
        return
    save_seen(sp, seen)

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "\n\n---\n\n".join(blocks),
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # advisory: never block an edit
    sys.exit(0)
