# Learnings — durable bug-class ledger

Append-only. One entry per *class* of error/pattern worth knowing before the next
plan or review. `/autoplan` reads this before planning; write for that reader.

## 2026-06-23 — pitfall — Adding a column needs BOTH schema.py AND tables.py; parity test guards only one
**Context**: `agent_ownership.voice_name` (trinity-enterprise#28). `tests/unit/test_schema_parity.py` compares `db/schema.py` (raw DDL) against `db/migrations.py` (the SQLite runner) — it does **not** import `db/tables.py` (the SQLAlchemy Core MetaData that Alembic autogenerates from and that every `db/*.py` accessor binds columns against). So a change that adds a column to `schema.py` + `migrations.py` but forgets `tables.py` keeps the parity test green, yet `select(agent_ownership.c.<col>)` blows up at runtime on the first call.
**Lesson**: A schema change touches **four** files — `db/schema.py`, `db/tables.py`, a SQLite migration in `db/migrations.py`, and an Alembic revision under `db/../migrations/versions/`. Don't trust schema-parity to catch a missing `tables.py` Column. The real guard is a db-accessor unit test that executes a live `select(table.c.<new_col>)` against a migrated DB (db_harness) — it fails loudly if `tables.py` was missed.
