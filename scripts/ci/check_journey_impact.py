#!/usr/bin/env python3
"""CI driver for the Journey Impact gate (#2350, Gate G1).

Thin: it gathers inputs (PR body, changed files, any referenced epic) and hands
them to `journey_impact.decide`, which holds the decision and is unit-tested.
Keeping the judgement out of a workflow's shell body is the point — a rule that
only runs in CI is a rule nobody can test.

Reads from the environment so no attacker-authored text ever reaches a shell.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from journey_impact import Declaration, decide, parse_declaration  # noqa: E402

# `Fixes #N` / `Related to #N` / `Related to owner/repo#N`.
_ISSUE_REF_RE = re.compile(
    r"(?:closes|close|fixes|fix|resolves|resolve|related to|refs?|part of)\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>\d+)",
    re.IGNORECASE,
)
MAX_DIFF_FILES = 400          # a huge PR should not turn the gate into a crawl
MAX_FILE_BYTES = 200_000


def changed_files(base: str, head: str):
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError as e:
        print(f"::warning::could not diff {base}...{head} ({e}); "
              f"skeleton detection will see no files")
        return []
    paths = [p for p in out.splitlines() if p.strip()][:MAX_DIFF_FILES]
    files = []
    for p in paths:
        try:
            blob = subprocess.run(["git", "show", f"{head}:{p}"],
                                  capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            blob = ""          # deleted in this PR
        files.append((p, blob[:MAX_FILE_BYTES]))
    return files


def epic_declarations(body: str, repo: str):
    """Declarations on issues this PR references that are labelled `type-epic`.

    Best-effort: a lookup failure is reported and does not fail the gate. The
    obligation this gate enforces has to be legible to the author, and "the
    issues API was briefly unavailable" is not something they can act on.
    """
    out = []
    seen = set()
    for m in _ISSUE_REF_RE.finditer(body or ""):
        target_repo = m.group("repo") or repo
        num = m.group("num")
        key = (target_repo, num)
        if key in seen:
            continue
        seen.add(key)
        try:
            raw = subprocess.run(
                ["gh", "issue", "view", num, "--repo", target_repo,
                 "--json", "body,labels"],
                capture_output=True, text=True, check=True,
            ).stdout
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            print(f"::warning::could not read {target_repo}#{num} ({type(e).__name__}); "
                  f"not treating it as an epic")
            continue
        labels = {l.get("name") for l in (data.get("labels") or [])}
        if "type-epic" not in labels:
            continue
        out.append((f"{target_repo}#{num}", parse_declaration(data.get("body"))))
    return out


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    repo = os.environ.get("REPO", "")
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "HEAD")

    pr_decl = parse_declaration(body)
    epics = epic_declarations(body, repo) if repo else []
    # Only pay for the diff when something might oblige a skeleton.
    needs_files = pr_decl.kind == "new" or any(
        d.kind == "new" for _, d in epics
    )
    files = changed_files(base, head) if (needs_files and base) else []

    verdict = decide(pr_declaration=pr_decl, epic_declarations=epics,
                     changed_files=files)

    for line in verdict.lines:
        print(line)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Journey Impact\n\n")
            for line in verdict.lines:
                fh.write(f"{line}\n\n")

    if not verdict.ok:
        print("::error title=Journey Impact::" + verdict.lines[-1].splitlines()[0])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
