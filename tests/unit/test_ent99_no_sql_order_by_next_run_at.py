"""Repo-wide guard: ``agent_schedules.next_run_at`` is never ordered in SQL.

``next_run_at`` is a **mixed-format** TEXT column with two writers that disagree
about what they store:

    # src/backend/db/schedules/crud.py — the BACKEND writer
    tz = pytz.timezone(timezone) if timezone else pytz.UTC
    next_time = croniter(cron, datetime.now(tz)).get_next(datetime)
    next_run_at_iso = next_run_at.isoformat()   # "2026-08-13T09:00:00+03:00"

    # src/scheduler/database.py — the SCHEDULER writer
    params.append(to_utc_iso(next_run_at))      # "2026-08-13T06:00:00Z"

Both strings name the SAME instant. Sorted **lexicographically** — which is what
SQL does to a TEXT column — they order by *local wall clock* instead: the Kyiv
row (09:00+03:00 = 06:00 UTC) sorts AFTER a UTC row at 08:00Z, though it fires
two hours earlier.

``architecture.md`` Invariant #16 records the column as safe "because
``next_run_at`` is only ever parse-compared in Python, never lexicographically in
SQL". That is a true statement about today's code and **nothing enforced it** —
it lived in prose, in one docstring, and in a test scoped to one accessor. The
natural way to write the next fleet-wide read ("what fires next?") is
``ORDER BY next_run_at ASC LIMIT n``, which is wrong in a way that is invisible
in review, invisible in a UTC-only test fixture, and invisible on a UTC-only
fleet. It surfaces only as "the dashboard names the wrong next fire", on
someone else's timezone, months later.

So the prose becomes a check. Cost: one grep. It is deliberately repo-wide and
**does not skip** when the backend venv is absent — it imports nothing but the
standard library, so it is a real CI gate everywhere.

**If this test fails**, do not add your file to the allowlist. Filter in SQL,
then order and slice in **Python** over parsed instants
(``utils/helpers.parse_iso_timestamp`` returns an aware UTC datetime for every
stored form, so the sort key is uniformly comparable). Note that a bare ``LIMIT``
without an ``ORDER BY`` is not the workaround either: it is arbitrary truncation
that can drop the true next fire.

The durable fix — normalizing ``next_run_at`` to UTC at both writers so the
column can be indexed and ordered in SQL — is a follow-up listed in the PR body,
not attempted here. When it lands, this guard is what gets deleted, on purpose.

Filed under ent#99, but it guards a latent platform bug that exists today,
independently of any tile.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_TREES = ("src/backend", "src/scheduler")

# Files consciously exempted, each with a reason. EMPTY, and it should stay that
# way: an entry here means somebody decided a lexicographic sort of a
# mixed-format timestamp column was acceptable, which needs an argument, not a
# line in a list.
_ALLOWLIST: dict[str, str] = {}

# `ORDER BY [alias.]next_run_at`, tolerating newlines, a leading table alias and
# extra sort keys — but tempered so the window cannot run past the end of one
# clause into an unrelated statement.
_SQL_ORDER_BY = re.compile(
    r"order\s+by\s+((?:(?!\bfrom\b|\bwhere\b|\bselect\b|;)[\s\S]){0,120}?)\bnext_run_at\b",
    re.IGNORECASE,
)

# SQLAlchemy: `order_by(AgentSchedules.next_run_at)`, `.order_by(text("next_run_at"))`, …
_SA_ORDER_BY = re.compile(r"order_by\s*\([^)]{0,200}next_run_at", re.IGNORECASE | re.DOTALL)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for tree in _SCANNED_TREES:
        base = _ROOT / tree
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
    return files


def _offenders() -> list[str]:
    out: list[str] = []
    for path in _python_files():
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "next_run_at" not in text:
            continue
        for pattern, label in ((_SQL_ORDER_BY, "SQL ORDER BY"), (_SA_ORDER_BY, "order_by()")):
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                out.append(f"{rel}:{line} ({label})")
    return sorted(set(out))


def test_scan_actually_reads_the_backend() -> None:
    """A guard that reads nothing certifies nothing."""
    files = _python_files()
    assert len(files) > 100, f"expected to scan the backend + scheduler, found {len(files)} files"
    assert any(
        "next_run_at" in p.read_text(encoding="utf-8", errors="replace") for p in files
    ), "no file mentions next_run_at at all — the scan is pointed at the wrong tree"


def test_detector_recognises_the_shapes_it_is_meant_to_catch() -> None:
    """The regexes are the whole guard, so they are asserted directly."""
    caught = [
        "ORDER BY next_run_at ASC LIMIT 5",
        "order by next_run_at",
        "ORDER  BY\n            next_run_at DESC",
        "ORDER BY s.next_run_at ASC",
        "ORDER BY enabled DESC, next_run_at ASC",
    ]
    for sql in caught:
        assert _SQL_ORDER_BY.search(sql), f"detector missed: {sql!r}"

    assert _SA_ORDER_BY.search("q.order_by(AgentSchedules.next_run_at.asc())")
    assert _SA_ORDER_BY.search('select(t).order_by(text("next_run_at"))')

    # Not offenders: reading, filtering or writing the column is fine — only
    # ORDERING by it is the bug.
    for ok in (
        "SELECT next_run_at FROM agent_schedules",
        "WHERE next_run_at IS NOT NULL",
        "ORDER BY started_at DESC",
        "UPDATE agent_schedules SET next_run_at = ?",
    ):
        assert not _SQL_ORDER_BY.search(ok), f"false positive on: {ok!r}"
        assert not _SA_ORDER_BY.search(ok), f"false positive on: {ok!r}"

    # And the tempering holds: an unrelated ORDER BY must not reach forward into
    # a later, separate statement that merely mentions the column.
    unrelated = (
        "ORDER BY started_at DESC LIMIT 5;\n"
        "        SELECT next_run_at FROM agent_schedules WHERE enabled = 1"
    )
    assert not _SQL_ORDER_BY.search(unrelated)


def test_no_sql_orders_by_next_run_at() -> None:
    offenders = _offenders()
    assert offenders == [], (
        "next_run_at is a mixed-format column (the scheduler writes 'Z', the backend writes "
        "the schedule's own UTC offset), so a lexicographic SQL sort orders by LOCAL WALL "
        "CLOCK — '09:00+03:00' sorts after '08:00Z' though it fires two hours earlier. "
        "Filter in SQL, then order in Python over parsed instants (Invariant #16). "
        f"Offenders: {offenders}"
    )
