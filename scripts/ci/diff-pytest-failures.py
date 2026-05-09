"""JUnit XML regression diff for the unit-suite CI gate (issue #715).

Compares the union of failing tests in --base XMLs against the union in --head
XMLs. Exits 1 if --head introduces any new failing test ID OR if any input XML
is missing/unparseable/empty (fail-closed on infrastructure failure).

Test identity is (classname, name, kind) where kind is "failure" (assertion)
or "error" (collection / fixture crash). Tracking kind separately matters
because the unit suite has 17 collection errors today and "known failure
flips to collection error" is a real regression class.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

TestId = tuple[str, str, str]  # (classname, name, kind)


@dataclass
class XmlSummary:
    path: Path
    failures: set[TestId] = field(default_factory=set)
    pass_count: int = 0
    fail_count: int = 0
    error_count: int = 0
    skip_count: int = 0
    total: int = 0
    parse_error: str | None = None


def _parse_one(path: Path) -> XmlSummary:
    """Parse a single JUnit XML. Returns a summary with failures set populated.

    A summary with parse_error set OR total == 0 is treated as infrastructure
    failure by the caller.
    """
    summary = XmlSummary(path=path)
    if not path.exists():
        summary.parse_error = f"file does not exist: {path}"
        return summary
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        summary.parse_error = f"unparseable XML: {exc}"
        return summary

    root = tree.getroot()
    testcases = list(root.iter("testcase"))
    summary.total = len(testcases)

    for tc in testcases:
        classname = tc.get("classname", "")
        name = tc.get("name", "")
        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None:
            summary.failures.add((classname, name, "failure"))
            summary.fail_count += 1
        elif error is not None:
            summary.failures.add((classname, name, "error"))
            summary.error_count += 1
        elif skipped is not None:
            summary.skip_count += 1
        else:
            summary.pass_count += 1
    return summary


def _format_test_id(test_id: TestId) -> str:
    classname, name, kind = test_id
    sigil = "F" if kind == "failure" else "E"
    full = f"{classname}::{name}" if classname else name
    return f"[{sigil}] {full}"


def _render_summary(
    base_summaries: list[XmlSummary],
    head_summaries: list[XmlSummary],
    regressions: set[TestId],
    fixes: set[TestId],
    infra_failures: list[str],
) -> str:
    lines = ["# Backend unit-suite regression diff", ""]

    if infra_failures:
        lines.append("## ⚠️ Infrastructure failures")
        lines.append("")
        for msg in infra_failures:
            lines.append(f"- {msg}")
        lines.append("")

    lines.append("## Per-XML totals")
    lines.append("")
    lines.append("| Side | Path | Total | Pass | Fail | Error | Skip |")
    lines.append("|---|---|---:|---:|---:|---:|---:|")
    for label, summaries in (("base", base_summaries), ("head", head_summaries)):
        for s in summaries:
            lines.append(
                f"| {label} | `{s.path.name}` | {s.total} | {s.pass_count} | "
                f"{s.fail_count} | {s.error_count} | {s.skip_count} |"
            )
    lines.append("")

    if regressions:
        lines.append(f"## ❌ New failures introduced by HEAD ({len(regressions)})")
        lines.append("")
        lines.append("Tests failing under HEAD that did not fail under BASE in any seed:")
        lines.append("")
        for test_id in sorted(regressions):
            lines.append(f"- {_format_test_id(test_id)}")
        lines.append("")
    else:
        lines.append("## ✅ No new failures")
        lines.append("")

    if fixes:
        lines.append(f"## 🟢 Failures fixed by HEAD ({len(fixes)})")
        lines.append("")
        for test_id in sorted(fixes):
            lines.append(f"- {_format_test_id(test_id)}")
        lines.append("")

    lines.append(
        "_Legend: [F] = assertion failure, [E] = collection or fixture error._"
    )
    lines.append(
        "_Identity = (classname, name, kind); union taken across all input XMLs._"
    )
    return "\n".join(lines)


def diff(
    base_paths: Iterable[Path],
    head_paths: Iterable[Path],
) -> tuple[str, int]:
    """Returns (summary_markdown, exit_code)."""
    base_paths = list(base_paths)
    head_paths = list(head_paths)
    base_summaries = [_parse_one(p) for p in base_paths]
    head_summaries = [_parse_one(p) for p in head_paths]

    infra_failures: list[str] = []
    for s in (*base_summaries, *head_summaries):
        if s.parse_error is not None:
            infra_failures.append(f"`{s.path}`: {s.parse_error}")
        elif s.total == 0:
            infra_failures.append(f"`{s.path}`: zero testcases — pytest likely crashed before collection")

    base_known: set[TestId] = set().union(*(s.failures for s in base_summaries)) if base_summaries else set()
    head_known: set[TestId] = set().union(*(s.failures for s in head_summaries)) if head_summaries else set()

    regressions = head_known - base_known
    fixes = base_known - head_known

    summary_md = _render_summary(
        base_summaries, head_summaries, regressions, fixes, infra_failures
    )

    if infra_failures or regressions:
        return summary_md, 1
    return summary_md, 0


def _write_xml(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip())


def _build_xml(testcases: str) -> str:
    return f'<?xml version="1.0" encoding="utf-8"?><testsuite>{testcases}</testsuite>'


def _run_self_test() -> int:
    """In-process tests covering the load-bearing edge cases."""
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Case 1: clean diff (identical sets) → exit 0
        passing = _build_xml('<testcase classname="t" name="ok"/>')
        with_failure = _build_xml(
            '<testcase classname="t" name="bad"><failure/></testcase>'
            '<testcase classname="t" name="ok"/>'
        )
        (tmp_path / "base1.xml").write_text(with_failure)
        (tmp_path / "head1.xml").write_text(with_failure)
        _, code = diff([tmp_path / "base1.xml"], [tmp_path / "head1.xml"])
        if code != 0:
            failures.append(f"case 1 (identical): expected 0, got {code}")

        # Case 2: head introduces a new failure → exit 1
        head2 = _build_xml(
            '<testcase classname="t" name="bad"><failure/></testcase>'
            '<testcase classname="t" name="new_bad"><failure/></testcase>'
        )
        (tmp_path / "head2.xml").write_text(head2)
        md, code = diff([tmp_path / "base1.xml"], [tmp_path / "head2.xml"])
        if code != 1:
            failures.append(f"case 2 (new failure): expected 1, got {code}")
        if "new_bad" not in md:
            failures.append("case 2: regression list missing new_bad")

        # Case 3: head fixes a failure → exit 0
        _, code = diff([tmp_path / "head2.xml"], [tmp_path / "base1.xml"])
        if code != 0:
            failures.append(f"case 3 (fix only): expected 0, got {code}")

        # Case 4: failure flips to error in head → exit 1 (different kind = different identity)
        head4 = _build_xml(
            '<testcase classname="t" name="bad"><error/></testcase>'
            '<testcase classname="t" name="ok"/>'
        )
        (tmp_path / "head4.xml").write_text(head4)
        md, code = diff([tmp_path / "base1.xml"], [tmp_path / "head4.xml"])
        if code != 1:
            failures.append(f"case 4 (failure→error): expected 1, got {code}")
        if "[E] t::bad" not in md:
            failures.append("case 4: regression list missing [E] t::bad")

        # Case 5: missing XML → infra failure → exit 1
        _, code = diff([tmp_path / "does-not-exist.xml"], [tmp_path / "base1.xml"])
        if code != 1:
            failures.append(f"case 5 (missing): expected 1, got {code}")

        # Case 6: unparseable XML → infra failure → exit 1
        (tmp_path / "broken.xml").write_text("this is not xml <<<")
        _, code = diff([tmp_path / "broken.xml"], [tmp_path / "base1.xml"])
        if code != 1:
            failures.append(f"case 6 (unparseable): expected 1, got {code}")

        # Case 7: zero testcases (pytest crashed before collection) → infra failure → exit 1
        (tmp_path / "empty.xml").write_text(_build_xml(""))
        _, code = diff([tmp_path / "empty.xml"], [tmp_path / "base1.xml"])
        if code != 1:
            failures.append(f"case 7 (zero testcases): expected 1, got {code}")

        # Case 8: union de-noising — failure under base seed B is "known" → not a regression
        base8a = _build_xml('<testcase classname="t" name="flake"><failure/></testcase>')
        base8b = _build_xml('<testcase classname="t" name="flake"/>')
        head8 = _build_xml('<testcase classname="t" name="flake"><failure/></testcase>')
        (tmp_path / "base8a.xml").write_text(base8a)
        (tmp_path / "base8b.xml").write_text(base8b)
        (tmp_path / "head8.xml").write_text(head8)
        _, code = diff(
            [tmp_path / "base8a.xml", tmp_path / "base8b.xml"], [tmp_path / "head8.xml"]
        )
        if code != 0:
            failures.append(f"case 8 (union de-noise): expected 0, got {code}")

    if failures:
        print("SELF-TEST FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("self-test OK (8/8 cases pass)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", nargs="*", type=Path, default=[], help="Baseline JUnit XML files"
    )
    parser.add_argument(
        "--head", nargs="*", type=Path, default=[], help="Head (PR) JUnit XML files"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="Write Markdown summary to this path"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run in-process correctness tests on synthetic XMLs and exit",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_test()

    if not args.base or not args.head:
        parser.error("--base and --head are required (unless --self-test)")

    summary_md, exit_code = diff(args.base, args.head)
    print(summary_md)
    if args.out is not None:
        args.out.write_text(summary_md + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
