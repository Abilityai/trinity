"""Count the SQL statements a block of code runs (#4671, #4672).

A list endpoint whose query count grows with the rows or agents it returns is
invisible to a functional test: the answer is right at every size, only slower.
This helper makes the count an assertion, so a new list endpoint can pin its
own:

    from query_counter import count_queries

    def test_list_does_not_scale_with_rows(...):
        seed(rows=3)
        with count_queries() as small:
            list_things()
        seed(rows=30)
        with count_queries() as large:
            list_things()
        assert large.count == small.count, large.report()

It listens on the active SQLAlchemy engine (`db.engine.get_engine()`), so it
counts whatever the code under test actually sends — SQLite or PostgreSQL —
and needs nothing from the code itself. Import-light like `db_harness`: no
`database` / `services` / `config`.

`by_table()` and `report()` exist for the failure message: "37 != 4" names no
culprit, a per-table tally does.
"""
from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from typing import Iterator, List

from sqlalchemy import event

# Transaction control and SQLite housekeeping are not lookups; counting them
# would make the number depend on the backend rather than on the code.
_NOISE = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|PRAGMA)\b", re.IGNORECASE)
_TABLE = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.IGNORECASE)


class QueryLog:
    """The statements one `count_queries()` block ran, in order."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def by_table(self) -> Counter:
        """Statements per table touched — a JOIN counts once for each table."""
        tally: Counter = Counter()
        for statement in self.statements:
            for table in dict.fromkeys(t.lower() for t in _TABLE.findall(statement)):
                tally[table] += 1
        return tally

    def report(self) -> str:
        """A one-line tally for an assertion message."""
        tables = ", ".join(f"{t}={n}" for t, n in self.by_table().most_common())
        return f"{self.count} queries ({tables})"


@contextmanager
def count_queries(engine=None) -> Iterator[QueryLog]:
    """Record every statement `engine` (default: the active one) executes
    inside the block. The listener is removed on exit, even on an exception."""
    if engine is None:
        from db.engine import get_engine

        engine = get_engine()
    log = QueryLog()

    def _record(conn, cursor, statement, parameters, context, executemany):
        if not _NOISE.match(statement):
            log.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        yield log
    finally:
        event.remove(engine, "before_cursor_execute", _record)
