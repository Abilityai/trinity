#!/usr/bin/env python3
"""Link-quality eval for the Ask Trinity docs assistant (trinity-enterprise#1285).

Runs a fixed question set against the public `ask-trinity` Cloud Function and
checks, per answer:

  * doc link   — a https://docs.ability.ai/... link is present (where expected)
  * video link — a YouTube link is present (where expected)
  * resolves   — every absolute URL in the answer answers HTTP 200 (after redirects)
  * no .md     — no relative / repo-path `.md` link (dead for every consumer)

Run it before and after a prompt/index change and compare. Stdlib only.

    python3 scripts/ask-trinity-link-eval.py                # table to stdout
    python3 scripts/ask-trinity-link-eval.py --json out.json  # also save raw answers
    ASK_TRINITY_ENDPOINT=... python3 scripts/ask-trinity-link-eval.py

Exit code is 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ENDPOINT = os.environ.get(
    "ASK_TRINITY_ENDPOINT",
    "https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity",
)

# (question, expect_doc_link, expect_video_link)
QUESTIONS: list[tuple[str, bool, bool]] = [
    ("How do I schedule an agent to run every morning?", True, False),
    ("How do I deploy Trinity on DigitalOcean?", True, True),
    ("How do I add credentials to an agent?", True, False),
    ("What roles and permissions does Trinity have?", True, False),
    ("How do I back up and restore my Trinity instance?", True, False),
    ("How do I upgrade Trinity to a new release?", True, False),
    ("Is there a video overview of what Trinity is?", False, True),
    ("Are there video tutorials on building a second brain for my agents?", False, True),
    ("How do I convert an existing Claude Code agent to run on Trinity?", True, True),
    ("How does agent-to-agent collaboration work in Trinity?", True, False),
]

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)[^)]*\)")
BARE_URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
DOC_RE = re.compile(r"^https://docs\.ability\.ai(/|$)")
VIDEO_RE = re.compile(r"^https://(www\.)?(youtube\.com|youtu\.be)/")
UA = "ask-trinity-link-eval/1.0"


def ask(question: str) -> str:
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"question": question}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode())
    return payload.get("answer") or payload.get("error") or ""


def extract_links(answer: str) -> tuple[list[str], list[str]]:
    """Return (absolute_urls, relative_md_targets)."""
    absolute: list[str] = []
    relative_md: list[str] = []
    for target in MD_LINK_RE.findall(answer):
        if target.startswith(("http://", "https://")):
            absolute.append(target)
        elif re.search(r"\.md(#|$)", target):
            relative_md.append(target)
    for url in BARE_URL_RE.findall(answer):
        absolute.append(url.rstrip(".,;:"))
    seen: set[str] = set()
    # A templated URL quoted from an install command (`.../<release-tag>/...`,
    # `${VERSION}`) is a placeholder, not a link the bot is offering — skip it.
    uniq = [
        u for u in absolute
        if not re.search(r"[<{$]", u) and not (u in seen or seen.add(u))
    ]
    return uniq, relative_md


def resolves(url: str, cache: dict[str, int | str]) -> int | str:
    if url not in cache:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                cache[url] = resp.status
        except urllib.error.HTTPError as e:
            cache[url] = e.code
        except Exception as e:  # noqa: BLE001 — report, don't crash the eval
            cache[url] = type(e).__name__
    return cache[url]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", help="write raw answers + per-question results here")
    args = ap.parse_args()

    status_cache: dict[str, int | str] = {}
    results = []
    all_ok = True
    for i, (q, want_doc, want_video) in enumerate(QUESTIONS, 1):
        try:
            answer = ask(q)
        except Exception as e:  # noqa: BLE001
            answer = f"<request failed: {e}>"
        urls, rel_md = extract_links(answer)
        statuses = {u: resolves(u, status_cache) for u in urls}
        has_doc = any(DOC_RE.match(u) for u in urls)
        has_video = any(VIDEO_RE.match(u) for u in urls)
        broken = [u for u, s in statuses.items() if s != 200]
        ok = (
            (has_doc or not want_doc)
            and (has_video or not want_video)
            and not broken
            and not rel_md
        )
        all_ok &= ok
        results.append({
            "n": i, "question": q, "answer": answer,
            "want_doc": want_doc, "want_video": want_video,
            "has_doc": has_doc, "has_video": has_video,
            "urls": statuses, "broken": broken, "relative_md": rel_md, "ok": ok,
        })
        mark = lambda want, have: ("ok" if have else "MISS") if want else ("+" if have else "-")
        print(
            f"{i:>2}. {'PASS' if ok else 'FAIL'}  doc:{mark(want_doc, has_doc):<4} "
            f"video:{mark(want_video, has_video):<4} urls:{len(urls):>2} "
            f"broken:{len(broken)} rel-md:{len(rel_md)}  {q}",
            flush=True,
        )
        for u in broken:
            print(f"      broken {statuses[u]}: {u}")
        for t in rel_md:
            print(f"      relative .md: {t}")

    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} passed  (endpoint: {ENDPOINT})")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"raw answers written to {args.json}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
