"""subscription_headroom_history — durable probe snapshots for utilization trends (ent#433)

One row per #471 headroom probe. #471 keeps a single last-known-good Redis
snapshot per subscription, overwritten on every probe, so utilization TRENDS
were unanswerable; this is the durable half. The SQLite track creates the same
shape via db/schema.py + db/migrations.py.

Two deliberate divergences from the SQLite DDL, both required and both matching
the 0033_agent_evaluations precedent:

- **No FOREIGN KEY.** The SQLite DDL carries one for documentation, but
  `PRAGMA foreign_keys` is off platform-wide AND `schema._PG_TABLE_SUBS`
  regex-strips every `FOREIGN KEY ... REFERENCES ...` clause before the DDL
  reaches PostgreSQL — so this schema has ZERO enforced FKs on either backend.
  Writing a real constraint here would create the platform's first one and make
  the backends behave differently: an in-flight probe landing its INSERT just
  after `delete_subscription` commits (a real race — `get_headroom(wait=False)`
  spawns a background probe that can run for up to 15s) would succeed on SQLite
  and raise ForeignKeyViolation on PostgreSQL. Cascade is performed explicitly
  inside `delete_subscription`'s transaction instead.
- **`SERIAL` / `DOUBLE PRECISION`.** PostgreSQL has no
  `INTEGER PRIMARY KEY AUTOINCREMENT` and no `REAL` in the SQLite sense; the
  sqlite→PG DDL translator makes exactly these substitutions for the fresh-install
  path, and this revision states them directly.

Revision ID: 0041_subscription_headroom_history
Revises: 0040_rl_events_failure_kind
Create Date: 2026-08-20
"""
from alembic import op

revision = "0041_subscription_headroom_history"
down_revision = "0040_rl_events_failure_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_headroom_history (
            id SERIAL PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            status TEXT NOT NULL,
            five_hour_utilization_pct DOUBLE PRECISION,
            five_hour_resets_at TEXT,
            five_hour_status TEXT,
            seven_day_utilization_pct DOUBLE PRECISION,
            seven_day_resets_at TEXT,
            seven_day_status TEXT,
            representative_claim TEXT,
            overage_status TEXT,
            unified_status TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_headroom_history_sub_fetched "
        "ON subscription_headroom_history(subscription_id, fetched_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_headroom_history_fetched "
        "ON subscription_headroom_history(fetched_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS subscription_headroom_history")
