"""Small markdown helpers for the docs guards (#2339).

Deliberately a sibling of `_invariant_catalog.py` rather than an import from
`test_2306_architecture_split.py`: a test module is not an import surface, and
extending a guard that is already green invites drift into it. The slug rule
mirrors GitHub's heading anchors closely enough for link checking.

Underscore-prefixed so pytest never collects it as a test module.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Iterator, List, NamedTuple, Set

_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*$")
# `[text](target)` and `[text](target "title")`; images share the syntax.
_LINK_RE = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def slug(heading: str) -> str:
    """GitHub's heading-anchor slug, closely enough for link checking."""
    s = heading.strip().lower()
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U)
    return s.strip().replace(" ", "-")


def prose_lines(path: Path) -> Iterator[tuple[int, str]]:
    """(line_no, line) for every line outside a fenced code block."""
    fence = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            yield n, line


def headings(path: Path) -> List[str]:
    """Heading slugs, in document order, code fences skipped."""
    out: List[str] = []
    for _, line in prose_lines(path):
        m = _HEADING_RE.match(line)
        if m:
            out.append(slug(m.group(1)))
    return out


class Link(NamedTuple):
    line: int
    target: str      # path part, may be ""
    anchor: str      # fragment without "#", may be ""


def relative_links(path: Path) -> List[Link]:
    """Every `](target)` link outside code fences that is neither absolute
    (`http`, `mailto`, leading `/`) nor a bare in-page anchor."""
    out: List[Link] = []
    for n, line in prose_lines(path):
        for m in _LINK_RE.finditer(line):
            raw = m.group(1)
            if raw.startswith(("http://", "https://", "mailto:", "/")):
                continue
            target, _, anchor = raw.partition("#")
            if not target and not anchor:
                continue
            out.append(Link(n, target, anchor))
    return out


def resolve(source: Path, link: Link) -> Path:
    """The filesystem path a relative link points at (anchor ignored)."""
    return (source.parent / link.target).resolve() if link.target else source.resolve()


def anchor_exists(target: Path, anchor: str) -> bool:
    if target.suffix.lower() != ".md":
        return True  # cannot check anchors in non-markdown targets
    return anchor in set(headings(target))


def normalised_prose(path: Path, min_chars: int = 20) -> List[str]:
    """Lines that can carry copied prose: stripped, no blanks, no fences, no
    table separators, no short fragments."""
    out: List[str] = []
    for _, line in prose_lines(path):
        s = line.strip()
        if len(s) < min_chars:
            continue
        if re.fullmatch(r"\|?[\s:|-]+\|?", s):
            continue
        out.append(s)
    return out


def shingles(lines: List[str], n: int) -> Set[tuple]:
    return {tuple(lines[i:i + n]) for i in range(0, max(0, len(lines) - n + 1))}
