#!/usr/bin/env python3
"""Absolute unit-suite failure gate for pushes to `dev`/`main` (#2247).

`backend-unit-test.yml` compares BASE against HEAD. That answers "did this PR make
things worse", which is the right question for a PR and the wrong one for a suite
that has rotted: when both sides fail identically the diff is empty and the check
is green. And on a push there is no base/head at all, so both the `test` and
`diff` jobs skip — nothing evaluates the result.

`test_ent96_timeline_split.py` fell straight through that gap. Its fixtures pinned
an absolute hour against a relative 24h window, so it passed the day it landed
(2026-08-14) and failed every day after. Five tests, permanently red on `dev`, and
not one red check anywhere: invisible on PRs because base and head both failed,
invisible on pushes because the diff job does not run there.

This closes it with the cheapest thing that cannot rot: run the suite and compare
the failing set against a committed baseline, in ABSOLUTE terms.

Why the baseline ships EMPTY, and why that is the point
------------------------------------------------------
Downloading the JUnit artifacts of a real CI run showed the honest number: the
unit suite on `dev` fails **exactly 8** tests under all three seeds — the 5 ent#96
plus the 3 stale strict-xfails — and the #2243 fix takes it to **0**. The workflow
comment claiming "34 failures + 17 errors documented in #660" is years of drift.

So the baseline is empty, which makes this gate "no failures at all". A ratcheted
list of tolerated failures is the standard move (see
`tests/lint_sys_modules_baseline.txt`), and it is worth having the mechanism —
but a tolerated-failure list is a place for debt to hide, so it starts empty and
every entry has to be argued for in review.

Exit codes: 0 clean, 1 unexpected failures (or an unusable XML — see below).

Fail-closed on unusable input, per #715's lesson: a run that produced no JUnit, or
XML with zero testcases, is an infrastructure failure. Treating that as "nothing
failed" is exactly how a broken gate looks identical to a passing one.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Same identity as scripts/ci/diff-pytest-failures.py: (classname, name, kind).
# Kind is tracked because "known failure flips to collection error" is a real
# regression class, and the two scripts MUST agree on identity or a test could be
# baselined under one spelling and reported under another.
TestId = tuple[str, str, str]


@dataclass
class Summary:
    path: Path
    failures: set[TestId] = field(default_factory=set)
    testcase_count: int = 0
    parse_error: str | None = None


def format_test_id(test_id: TestId) -> str:
    classname, name, kind = test_id
    full = f"{classname}::{name}" if classname else name
    return f"{full} [{kind}]" if kind == "error" else full


def parse_baseline(path: Path | None) -> set[str]:
    """One test id per line. `#` comments and blanks ignored."""
    if path is None or not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def parse_xml(path: Path) -> Summary:
    summary = Summary(path=path)
    try:
        root = ET.parse(path).getroot()
    except Exception as exc:  # noqa: BLE001 — any XML problem is an infra failure
        summary.parse_error = f"{type(exc).__name__}: {exc}"
        return summary
    for tc in root.iter("testcase"):
        summary.testcase_count += 1
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        if tc.find("failure") is not None:
            summary.failures.add((classname, name, "failure"))
        elif tc.find("error") is not None:
            summary.failures.add((classname, name, "error"))
    return summary


def check(xml_paths: list[Path], baseline_path: Path | None, out=sys.stdout) -> int:
    baseline = parse_baseline(baseline_path)
    summaries = [parse_xml(p) for p in xml_paths]

    unusable = [s for s in summaries if s.parse_error or s.testcase_count == 0]
    if not summaries or unusable:
        print("## Unit-suite absolute check: UNUSABLE INPUT\n", file=out)
        if not summaries:
            print("No JUnit XML was given — the suite did not run.", file=out)
        for s in unusable:
            reason = s.parse_error or "0 testcases (suite collected nothing)"
            print(f"- `{s.path.name}`: {reason}", file=out)
        print(
            "\nFailing closed: an infrastructure failure that reports green is the "
            "failure mode this gate exists to prevent (#715, #2247).",
            file=out,
        )
        return 1

    # Union across seeds: a failure that appears under ANY ordering is a failure.
    observed = set().union(*(s.failures for s in summaries))
    observed_str = {format_test_id(t) for t in observed}

    unexpected = sorted(observed_str - baseline)
    fixed = sorted(baseline - observed_str)
    total = sum(s.testcase_count for s in summaries)

    print("## Unit-suite absolute check\n", file=out)
    print(
        f"- runs: {len(summaries)} · testcases seen: {total} · "
        f"failing (union): {len(observed_str)} · baselined: {len(baseline)}",
        file=out,
    )

    if fixed:
        # Never a failure: passing MORE tests must not break the build. But say it
        # loudly, because a baseline nobody prunes is how the tolerated set grows.
        print(
            f"\n### {len(fixed)} baselined test(s) now PASS — prune them from "
            f"`{baseline_path.name if baseline_path else 'the baseline'}`\n",
            file=out,
        )
        for t in fixed:
            print(f"- `{t}`", file=out)

    if unexpected:
        print(f"\n### FAIL — {len(unexpected)} unexpected failing test(s)\n", file=out)
        for t in unexpected:
            print(f"- `{t}`", file=out)
        print(
            "\nEither fix them, or add the id verbatim to the baseline with a comment "
            "saying why it is tolerated. A test can rot into permanent red without a "
            "single red check; this gate is the thing that notices (#2247).",
            file=out,
        )
        return 1

    print("\nPASS — no failures outside the baseline.", file=out)
    return 0


# ---------------------------------------------------------------------------
# Self-test (same convention as diff-pytest-failures.py --self-test, which the
# workflow runs before trusting the script).
# ---------------------------------------------------------------------------

def _write(path: Path, testcases: str) -> None:
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{testcases}</testsuite></testsuites>')


def _run_self_test() -> int:
    import io
    import tempfile

    ok = True

    def case(label: str, got: int, want: int, text: str = "", must_contain: str = "") -> None:
        nonlocal ok
        passed = got == want and (not must_contain or must_contain in text)
        ok = ok and passed
        print(f"  {'ok  ' if passed else 'FAIL'} {label} (exit {got}, want {want})")
        if not passed and must_contain and must_contain not in text:
            print(f"       missing from output: {must_contain!r}")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        clean = d / "clean.xml"
        _write(clean, '<testcase classname="t" name="ok"/>')
        one_fail = d / "one.xml"
        _write(
            one_fail,
            '<testcase classname="t" name="ok"/>'
            '<testcase classname="t" name="bad"><failure/></testcase>',
        )
        err = d / "err.xml"
        _write(
            err,
            '<testcase classname="t" name="ok"/>'
            '<testcase classname="t" name="boom"><error/></testcase>',
        )
        empty = d / "empty.xml"
        _write(empty, "")
        broken = d / "broken.xml"
        broken.write_text("<not-xml")

        buf = io.StringIO()
        case("clean suite, empty baseline", check([clean], None, buf), 0)

        buf = io.StringIO()
        case(
            "one failure, empty baseline -> FAIL naming it",
            check([one_fail], None, buf),
            1,
            buf.getvalue(),
            "t::bad",
        )

        bl = d / "baseline.txt"
        bl.write_text("# tolerated\nt::bad\n")
        buf = io.StringIO()
        case("baselined failure tolerated", check([one_fail], bl, buf), 0)

        buf = io.StringIO()
        case(
            "baselined test now passing -> PASS but reported",
            check([clean], bl, buf),
            0,
            buf.getvalue(),
            "now PASS",
        )

        bl_err = d / "baseline_err.txt"
        bl_err.write_text("t::boom\n")
        buf = io.StringIO()
        case(
            "an error is not the same identity as a failure",
            check([err], bl_err, buf),
            1,
            buf.getvalue(),
            "t::boom [error]",
        )

        buf = io.StringIO()
        case(
            "zero testcases -> fail closed",
            check([empty], None, buf),
            1,
            buf.getvalue(),
            "UNUSABLE",
        )

        buf = io.StringIO()
        case("unparseable xml -> fail closed", check([broken], None, buf), 1, buf.getvalue(), "UNUSABLE")

        buf = io.StringIO()
        case("no xml at all -> fail closed", check([], None, buf), 1, buf.getvalue(), "did not run")

        buf = io.StringIO()
        case(
            "union across seeds: failure in one run counts",
            check([clean, one_fail], None, buf),
            1,
            buf.getvalue(),
            "t::bad",
        )

    print("self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", nargs="*", type=Path, default=[], help="JUnit XML file(s)")
    ap.add_argument("--baseline", type=Path, default=None, help="tolerated-failure id list")
    ap.add_argument("--out", type=Path, default=None, help="also append the report here")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _run_self_test()

    import io

    buf = io.StringIO()
    rc = check(list(args.xml), args.baseline, buf)
    report = buf.getvalue()
    print(report, end="")
    if args.out:
        with args.out.open("a") as fh:
            fh.write(report)
    return rc


if __name__ == "__main__":
    sys.exit(main())
