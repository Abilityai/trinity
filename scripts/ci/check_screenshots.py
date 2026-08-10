#!/usr/bin/env python3
"""Screenshot store guard — integrity (blocking) + staleness (advisory).

Trinity keeps every UI screenshot in ONE place, `docs/screenshots/`, with one
`MANIFEST.yaml` entry each. Before this guard the repo had three stores that drifted
apart: the capture tooling wrote to the directory no page embedded, so the freshest
set was the one nobody rendered while the rendered ones aged three months.

Two classes of check, deliberately weighted differently:

INTEGRITY (blocking) — these are always someone's mistake, never a side effect of
normal work:
  * a manifest entry whose file is missing, or a file with no manifest entry
    (the second is what lets an unowned, undocumented screenshot accumulate)
  * a broken `![](…)` / `<img src>` path in docs — silent on GitHub until a reader hits it
  * an image embedded from outside the canonical store (the drift this guard exists to stop)
  * a `sources:` path that no longer exists — critical, because freshness over a
    nonexistent path yields no commits, i.e. the entry silently becomes immortally "fresh".
    A guard whose own inputs can rot without complaint is worse than no guard.

STALENESS (advisory unless --strict) — an entry whose `sources` changed after its
`captured_at`. NOT blocking on PRs: a UI change legitimately makes a screenshot stale
in the very commit that changes the view, so blocking would force a recapture into every
frontend PR and get itself bypassed within a month. It surfaces as a warning + summary,
and `--strict` is for the scheduled/manual sweep that turns the list into recapture work.

Usage:
    python3 scripts/ci/check_screenshots.py [--strict] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment problem, must be loud
    sys.stderr.write(
        "check_screenshots.py needs PyYAML (pip install pyyaml).\n"
        "Refusing to skip: a guard that silently no-ops is worse than no guard.\n"
    )
    raise SystemExit(2)

REPO = Path(__file__).resolve().parents[2]
STORE = REPO / "docs" / "screenshots"
MANIFEST = STORE / "MANIFEST.yaml"

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Markdown trees scanned for image references.
DOC_ROOTS = [REPO / "docs"]
DOC_FILES = [REPO / "README.md", REPO / "CONTRIBUTING.md"]

# Where a doc image is allowed to live. Everything else is store drift.
#   docs/screenshots/**                      the canonical store
#   docs/assets/screenshots/whats-new/**     frozen release snapshots (never refreshed)
#   docs/assets/*                            brand assets (hero, explainer gif, logos)
#   docs/diagrams/**                         generated illustrations, not UI captures
ALLOWED_IMAGE_PREFIXES = (
    "docs/screenshots/",
    "docs/assets/screenshots/whats-new/",
    "docs/assets/",
    "docs/diagrams/",
)

# Historical records are not forward-maintained; a stale path there is not news.
REFERENCE_SCAN_EXCLUDES = ("docs/archive/", "docs/releases/", "docs/security-reports/")

REQUIRED_KEYS = ("file", "title", "route", "shows", "sources", "captured_at")

MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
HTML_IMAGE = re.compile(r"""<img[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
HTML_POSTER = re.compile(r"""\bposter\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


class Findings:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []  # (file, message)
        self.warnings: list[tuple[str, str]] = []

    def error(self, where: str, message: str) -> None:
        self.errors.append((where, message))

    def warn(self, where: str, message: str) -> None:
        self.warnings.append((where, message))


def annotate(kind: str, where: str, message: str) -> None:
    """Emit a GitHub annotation when running in Actions, plain text otherwise."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind} file={where}::{message}")
    else:
        print(f"{kind.upper():7} {where}: {message}")


def load_manifest(f: Findings) -> list[dict]:
    if not MANIFEST.is_file():
        f.error(rel(MANIFEST), "manifest is missing")
        return []
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        f.error(rel(MANIFEST), f"manifest does not parse: {exc}")
        return []
    if not isinstance(data, dict) or not isinstance(data.get("screenshots"), list):
        f.error(rel(MANIFEST), "manifest must be a mapping with a `screenshots:` list")
        return []
    entries = [e for e in data["screenshots"] if isinstance(e, dict)]
    if len(entries) != len(data["screenshots"]):
        f.error(rel(MANIFEST), "every `screenshots:` item must be a mapping")
    return entries


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def check_entries(entries: list[dict], f: Findings) -> dict[str, dict]:
    """Validate each manifest entry. Returns {filename: entry} for entries that parsed."""
    by_file: dict[str, dict] = {}
    where = rel(MANIFEST)

    for index, entry in enumerate(entries):
        label = entry.get("file") or f"screenshots[{index}]"

        missing = [k for k in REQUIRED_KEYS if k not in entry]
        if missing:
            f.error(where, f"{label}: missing required key(s): {', '.join(missing)}")
            continue

        name = entry["file"]
        if not isinstance(name, str) or "/" in name or name != Path(name).name:
            f.error(where, f"{label}: `file` must be a bare filename (the store is flat)")
            continue
        if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
            f.error(where, f"{name}: unexpected image suffix")
        if name in by_file:
            f.error(where, f"{name}: duplicate manifest entry")
            continue
        by_file[name] = entry

        if not (STORE / name).is_file():
            f.error(where, f"{name}: manifest entry has no file in docs/screenshots/")

        try:
            dt.date.fromisoformat(str(entry["captured_at"]))
        except (TypeError, ValueError):
            f.error(where, f"{name}: `captured_at` must be an ISO date (YYYY-MM-DD)")

        sources = entry["sources"]
        if not isinstance(sources, list):
            f.error(where, f"{name}: `sources` must be a list")
            continue

        external = bool(entry.get("external"))
        if not sources and not external:
            f.error(
                where,
                f"{name}: empty `sources` with no `external: true` — the entry could never "
                f"be reported stale, which is how a screenshot silently rots",
            )
        for source in sources:
            if not (REPO / str(source)).exists():
                f.error(
                    where,
                    f"{name}: source path does not exist: {source} — freshness over a "
                    f"missing path is always 'fresh', so this entry is now unguarded",
                )

    return by_file


def check_store_files(by_file: dict[str, dict], f: Findings) -> None:
    """Every image in the store must be claimed by the manifest."""
    if not STORE.is_dir():
        f.error(rel(STORE), "canonical screenshot store is missing")
        return
    for path in sorted(STORE.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.name not in by_file:
            f.error(
                rel(path),
                "image in the store has no MANIFEST.yaml entry — add one (file/title/route/"
                "shows/sources/captured_at) or delete the image",
            )


def iter_markdown() -> list[Path]:
    files = [p for p in DOC_FILES if p.is_file()]
    for root in DOC_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            if any(rel(path).startswith(x) for x in REFERENCE_SCAN_EXCLUDES):
                continue
            files.append(path)
    return files


def check_references(f: Findings) -> int:
    """Every doc image reference must resolve, and must live in an allowed location."""
    checked = 0
    for doc in iter_markdown():
        text = doc.read_text(encoding="utf-8", errors="replace")
        targets = (
            MD_IMAGE.findall(text) + HTML_IMAGE.findall(text) + HTML_POSTER.findall(text)
        )
        for target in targets:
            if target.startswith(("http://", "https://", "data:", "#", "mailto:")):
                continue
            if Path(target).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            checked += 1
            resolved = (doc.parent / target).resolve()
            location = rel(doc)
            if not resolved.is_file():
                f.error(location, f"broken image reference: {target}")
                continue
            posix = rel(resolved).replace(os.sep, "/")
            if not posix.startswith(ALLOWED_IMAGE_PREFIXES):
                f.error(
                    location,
                    f"image lives outside the canonical store: {posix} — move it to "
                    f"docs/screenshots/ and add a MANIFEST.yaml entry",
                )
    return checked


def git_history_is_shallow() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "true"


def newest_source_date(sources: list[str]) -> str | None:
    """Committer date (YYYY-MM-DD) of the newest commit touching any source path."""
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cs", "--"] + [str(s) for s in sources],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def check_freshness(by_file: dict[str, dict], f: Findings, strict: bool) -> list[dict]:
    stale: list[dict] = []
    if git_history_is_shallow():
        f.warn(
            rel(MANIFEST),
            "repository is a shallow clone — staleness not evaluated "
            "(set actions/checkout fetch-depth: 0)",
        )
        return stale

    report = f.error if strict else f.warn
    for name, entry in sorted(by_file.items()):
        if entry.get("external") or not entry.get("sources"):
            continue
        captured = str(entry["captured_at"])
        newest = newest_source_date(list(entry["sources"]))
        if newest is None:
            f.warn(rel(MANIFEST), f"{name}: no commits found over `sources` — cannot judge")
            continue
        if newest > captured:
            stale.append({"file": name, "captured_at": captured, "sources_changed": newest})
            report(
                rel(MANIFEST),
                f"{name}: stale — captured {captured}, but its UI changed {newest}. "
                f"Recapture with /capture-screenshots {Path(name).stem}",
            )
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat staleness as a failure (for the scheduled sweep, not PRs)",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="write the JSON summary to PATH, leaving stdout for annotations "
        "(CI uses this instead of a pipe, so the exit code is never swallowed)",
    )
    args = parser.parse_args()

    f = Findings()
    entries = load_manifest(f)
    by_file = check_entries(entries, f)
    check_store_files(by_file, f)
    references = check_references(f)
    stale = check_freshness(by_file, f, args.strict)

    for where, message in f.errors:
        annotate("error", where, message)
    for where, message in f.warnings:
        annotate("warning", where, message)

    summary = {
        "screenshots": len(by_file),
        "references_checked": references,
        "errors": [{"file": w, "message": m} for w, m in f.errors],
        "warnings": [{"file": w, "message": m} for w, m in f.warnings],
        "stale": stale,
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"\n{len(by_file)} screenshots in docs/screenshots/, "
            f"{references} doc image references checked, "
            f"{len(stale)} stale, {len(f.errors)} error(s)."
        )
        if stale and not args.strict:
            print("Staleness is advisory here — recapture as a follow-up, not a merge blocker.")

    if f.errors:
        print("\nFAIL: screenshot store integrity check failed.")
        return 1
    print("OK: screenshot store is consistent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
