"""Answer-link sanitiser for the Ask Trinity docs assistant (trinity-enterprise#1285).

The answer model is told to use only URLs that appear verbatim in its sources,
but when the retrieved chunk is a repo markdown page it still invents a docs URL
by putting the docs domain in front of the repo path
(`https://docs.ability.ai/automation/scheduling`, `.../digitalocean.md`). Those
404 for every reader. This module checks each markdown link in an answer against
the docs site's own catalogue (llms.txt) and:

  * keeps a link the catalogue lists;
  * repairs a docs-domain or relative `.md` link to the one catalogue page whose
    path ends the same way (or whose flattened path matches the flattened name
    of the indexed file), when exactly one page matches best;
  * otherwise unlinks it (keeps the link text), as it does for a YouTube link
    whose video ID the catalogue does not list;
  * leaves any other absolute link (GitHub, marketplaces) alone;
  * removes a `**Read more:**` / `**Watch:**` line left with no link.

Stdlib only and pure, so it is unit-tested without the Google SDK
(tests/unit/test_ent1285_ask_trinity_links.py).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

DOCS_HOST = "docs.ability.ai"
DOCS_ORIGIN = f"https://{DOCS_HOST}"
_YOUTUBE_HOSTS = {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"}

# [text](target "optional title") — not preceded by `!` (images are left alone)
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_CATALOGUE_URL_RE = re.compile(r"\]\((https?://[^)\s]+)\)")
_LABEL_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?\*\*(Read more|Watch):?\*\*", re.I)
_EMPTY_LABEL_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(Read more|Watch):?(?:\*\*)?:?[\s,;·|/-]*$", re.I)
# a bare URL not already the target of a markdown link `](...)`
_BARE_URL_RE = re.compile(r"(^|[\s(<])(?<!\]\()(https?://[^\s)\]>\"'`]+)")


def _youtube_id(url: str) -> str | None:
    p = urlparse(url)
    host = p.netloc.lower()
    if host not in _YOUTUBE_HOSTS:
        return None
    if host == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if p.path == "/watch":
        return (parse_qs(p.query).get("v") or [None])[0]
    m = re.match(r"^/(?:embed|shorts|live)/([^/?#]+)", p.path)
    return m.group(1) if m else None


def parse_catalogue(text: str) -> set[str]:
    """Every absolute URL linked from an llms.txt catalogue."""
    return {u.rstrip("/") for u in _CATALOGUE_URL_RE.findall(text or "")}


def _segments(path: str) -> list[str]:
    path = path.split("#")[0].split("?")[0]
    path = re.sub(r"\.(md|mdx|txt|html)$", "", path.strip().rstrip("/"))
    return [s for s in path.split("/") if s and s not in (".", "..")]


def _repair(target: str, doc_paths: dict[str, list[str]]) -> str | None:
    """The one catalogue docs URL `target` most plausibly meant, or None."""
    segs = _segments(urlparse(target).path if "://" in target else target)
    if not segs:
        return None
    # 1. longest common path suffix, unique
    best, best_len = [], 0
    for url, real in doc_paths.items():
        n = 0
        while n < min(len(segs), len(real)) and segs[-1 - n] == real[-1 - n]:
            n += 1
        if n > best_len:
            best, best_len = [url], n
        elif n == best_len and n:
            best.append(url)
    if best_len and len(best) == 1:
        return best[0]
    if best_len:
        return None  # ambiguous — refuse to guess
    # 2. flattened file name (`user-docs-faq-x` / `faq-x`) ending in a flattened real path
    flat = "-".join(segs)
    hits = [
        (len(f), url)
        for url, real in doc_paths.items()
        if real and (f := "-".join(real)) and (flat == f or flat.endswith("-" + f))
    ]
    if hits:
        hits.sort(reverse=True)
        if len(hits) == 1 or hits[0][0] > hits[1][0]:
            return hits[0][1]
    return None


def sanitize_links(answer: str, known_urls: set[str]) -> str:
    """Repair or unlink every link in `answer` that the catalogue does not serve."""
    known = {u.rstrip("/") for u in known_urls}
    doc_paths = {
        u: _segments(urlparse(u).path) for u in known if urlparse(u).netloc.lower() == DOCS_HOST
    }
    video_ids = {vid for u in known if (vid := _youtube_id(u))}
    have_catalogue = bool(known)

    def replace(m: re.Match) -> str:
        text, target = m.group(1), m.group(2)
        # `[https://x](https://x)`: the visible text IS the URL, so it must
        # follow a repair, and an unfixable one leaves nothing worth showing
        text_is_url = text.strip().rstrip("/") == target.rstrip("/")

        def link(url: str | None) -> str:
            if url is None:
                return "" if text_is_url else text
            return f"[{url if text_is_url else text}]({url})"

        parsed = urlparse(target)
        if parsed.scheme in ("http", "https"):
            host = parsed.netloc.lower()
            if not have_catalogue:
                return m.group(0)
            if host == DOCS_HOST:
                if target.rstrip("/") in known:
                    return m.group(0)
                return link(_repair(target, doc_paths))
            if host in _YOUTUBE_HOSTS:
                vid = _youtube_id(target)
                return m.group(0) if vid and vid in video_ids else link(None)
            return m.group(0)
        if parsed.scheme or target.startswith("#"):
            return m.group(0)  # mailto:, in-page anchors
        if not re.search(r"\.mdx?(#|$)", target):
            return m.group(0)
        return link(_repair(target, doc_paths) if have_catalogue else None)

    def replace_bare(m: re.Match) -> str:
        url = m.group(2).rstrip(".,;:!?")
        trail = m.group(2)[len(url):]
        if not have_catalogue:
            return m.group(0)
        host = urlparse(url).netloc.lower()
        if host == DOCS_HOST:
            if url.rstrip("/") in known:
                return m.group(0)
            fixed = _repair(url, doc_paths)
        elif host in _YOUTUBE_HOSTS:
            vid = _youtube_id(url)
            fixed = url if vid and vid in video_ids else None
        else:
            return m.group(0)
        # unfixable: drop the URL together with the space before it
        return f"{m.group(1)}{fixed}{trail}" if fixed else trail

    def fix_prose(segment: str) -> str:
        segment = _LINK_RE.sub(replace, segment)
        return _BARE_URL_RE.sub(replace_bare, segment)

    lines: list[str] = []
    in_fence = False
    for line in answer.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        # inline code spans (odd parts) are quoted commands, never rewritten
        parts = re.split(r"(`[^`\n]*`)", line)
        fixed = "".join(p if i % 2 else fix_prose(p) for i, p in enumerate(parts))
        # A Read more / Watch line exists to carry links; once every one of
        # them was unlinked, the leftover titles point at nothing — drop it.
        if _LABEL_LINE_RE.match(line) and _LINK_RE.search(line) and not _LINK_RE.search(fixed):
            continue
        if line.strip() and re.fullmatch(r"\s*(?:[-*+]|\d+\.)?\s*", fixed):
            continue  # a line (or list item) that was only an unfixable URL
        lines.append(fixed)

    # a bare label line ("Read more:") is kept only when a URL follows it
    kept: list[str] = []
    for i, line in enumerate(lines):
        if _EMPTY_LABEL_LINE_RE.match(line):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not re.search(r"https?://", nxt):
                continue
        kept.append(line)
    return "\n".join(kept).rstrip()
