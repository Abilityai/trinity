# Learnings — durable bug-class ledger

Append-only. One entry per *class* of error/pattern worth knowing before the next
plan or review. `/autoplan` reads this before planning; write for that reader.

## 2026-06-23 — pitfall — Adding a column needs BOTH schema.py AND tables.py; parity test guards only one
**Context**: `agent_ownership.voice_name` (trinity-enterprise#28). `tests/unit/test_schema_parity.py` compares `db/schema.py` (raw DDL) against `db/migrations.py` (the SQLite runner) — it does **not** import `db/tables.py` (the SQLAlchemy Core MetaData that Alembic autogenerates from and that every `db/*.py` accessor binds columns against). So a change that adds a column to `schema.py` + `migrations.py` but forgets `tables.py` keeps the parity test green, yet `select(agent_ownership.c.<col>)` blows up at runtime on the first call.
**Lesson**: A schema change touches **four** files — `db/schema.py`, `db/tables.py`, a SQLite migration in `db/migrations.py`, and an Alembic revision under `db/../migrations/versions/`. Don't trust schema-parity to catch a missing `tables.py` Column. The real guard is a db-accessor unit test that executes a live `select(table.c.<new_col>)` against a migrated DB (db_harness) — it fails loudly if `tables.py` was missed.

## 2026-06-28 — pitfall — `@dataclass` on a fieldless str-Enum breaks member equality
**Context**: `TaskExecutionErrorCode(str, Enum)` is decorated `@dataclass` (`services/task_execution_service.py:75`). `@dataclass` synthesises an `__eq__` that compares the (zero) fields, so for enum members `AUTH == BILLING` — and even `TIMEOUT == AUTH` — all evaluate **True**. `apply_result`'s breaker line `envelope.error_code == TaskExecutionErrorCode.AUTH` therefore fired for *any* non-None code reaching it (504→TIMEOUT pre-existing; 429→BILLING after #1085 populated it), wrongly tripping the AUTH dispatch breaker. Found in the #1085 review; fixed by value-comparing (`error_code.value == "auth"`).
**Lesson**: Never put `@dataclass` on an `Enum` subclass. Compare enum codes by `.value` (or plain `is`/identity), never `==` against another member, until the stray `@dataclass` decorators are removed fleet-wide. Membership tests (`x in (A, B)`) are also broken by the same quirk.
