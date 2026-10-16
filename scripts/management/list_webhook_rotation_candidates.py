#!/usr/bin/env python3
"""List the schedule webhooks an operator should rotate (trinity-enterprise#875).

Before ent#875 anyone an agent was shared with could mint, rotate, read and
revoke the webhook of ANY schedule on it, including a schedule the owner made.
A webhook URL is a bearer credential, and nothing records who minted a token,
so history cannot tell an owner-minted token from one a share holder minted.
The safe set is every schedule that has a webhook token (or a signing secret)
on an agent that is shared with someone today:

    python3 scripts/management/list_webhook_rotation_candidates.py
    python3 scripts/management/list_webhook_rotation_candidates.py --all

`--all` lists every schedule with a webhook token or signing secret, which also
covers an agent that was shared once and unshared later (the share row is gone,
so the default run cannot see it). READ-ONLY: it changes nothing. Rotate each
listed schedule from the Schedules tab (Rotate URL, then Rotate secret) or with
`POST /api/agents/{agent}/schedules/{schedule_id}/webhook`.

Database: `DATABASE_URL` (PostgreSQL) when set, else the SQLite file at
`--db`, else `$TRINITY_DB_PATH`, else `~/trinity-data/trinity.db`. Exit codes:
0 the list was produced (even when empty, said in words), 2 the database could
not be read (it fails loudly rather than printing nothing).
"""
import argparse
import os
import sys
from pathlib import Path

SQL = """
SELECT s.id            AS schedule_id,
       s.agent_name    AS agent,
       s.name          AS schedule,
       COALESCE(u.username, '(unknown, owner_id=' || COALESCE(CAST(s.owner_id AS TEXT), 'NULL') || ')') AS creator,
       CASE WHEN s.webhook_token IS NOT NULL AND s.webhook_token <> '' THEN 'yes' ELSE 'no' END AS has_token,
       CASE WHEN s.webhook_secret_encrypted IS NOT NULL AND s.webhook_secret_encrypted <> '' THEN 'yes' ELSE 'no' END AS has_secret
  FROM agent_schedules s
  LEFT JOIN users u ON u.id = s.owner_id
 WHERE ((s.webhook_token IS NOT NULL AND s.webhook_token <> '')
        OR (s.webhook_secret_encrypted IS NOT NULL AND s.webhook_secret_encrypted <> ''))
{shared_clause}
 ORDER BY s.agent_name, s.name
"""
SHARED_CLAUSE = "   AND s.agent_name IN (SELECT DISTINCT agent_name FROM agent_sharing)"
SHARES_SQL = "SELECT agent_name, shared_with_email FROM agent_sharing ORDER BY agent_name, shared_with_email"


def _engine(db_arg):
    from sqlalchemy import create_engine
    url = os.environ.get("DATABASE_URL")
    if url:
        return create_engine(url), "PostgreSQL (DATABASE_URL)"
    path = Path(db_arg or os.environ.get("TRINITY_DB_PATH") or Path.home() / "trinity-data" / "trinity.db")
    if not path.is_file():
        raise FileNotFoundError(f"no SQLite database at {path} (pass --db, or set DATABASE_URL)")
    return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true"), f"SQLite {path} (read-only)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="SQLite database file (default: $TRINITY_DB_PATH or ~/trinity-data/trinity.db)")
    ap.add_argument("--all", action="store_true",
                    help="every schedule with a webhook token or secret, not only those on a shared agent")
    args = ap.parse_args(argv)

    try:
        from sqlalchemy import text
        engine, where = _engine(args.db)
        with engine.connect() as conn:
            rows = conn.execute(text(SQL.format(shared_clause="" if args.all else SHARED_CLAUSE))).mappings().all()
            total = conn.execute(text(
                "SELECT COUNT(*) FROM agent_schedules WHERE (webhook_token IS NOT NULL AND webhook_token <> '')"
                " OR (webhook_secret_encrypted IS NOT NULL AND webhook_secret_encrypted <> '')"
            )).scalar()
            shares = {}
            for r in conn.execute(text(SHARES_SQL)).mappings():
                shares.setdefault(r["agent_name"], []).append(r["shared_with_email"])
    except Exception as exc:  # noqa: BLE001 - any failure must be loud, never an empty list
        print(f"ERROR: could not read the database: {exc}", file=sys.stderr)
        return 2

    print(f"Database: {where}")
    print(f"Schedules with a webhook token or signing secret: {total}")
    scope = "every schedule with a webhook" if args.all else "those on an agent that is shared today"
    print(f"Rotation candidates ({scope}): {len(rows)}\n")
    if not rows:
        print("None. No schedule with a webhook sits on an agent that is shared with anyone."
              + ("" if args.all else " (Re-run with --all to include agents that were shared once and unshared.)"))
        return 0
    for r in rows:
        who = ", ".join(shares.get(r["agent"], [])) or "(not shared today)"
        print(f"- agent={r['agent']}  schedule={r['schedule']!r}  id={r['schedule_id']}")
        # Only the fact that a token / secret exists is printed, as constant text:
        # no value from a credential column reaches the output.
        has_token = "yes" if r["has_token"] == "yes" else "no"
        has_secret = "yes" if r["has_secret"] == "yes" else "no"
        print(f"    creator: {r['creator']}   token: {has_token}   signing secret: {has_secret}")
        print(f"    shared with: {who}")
    print("\nRotate each URL (and its signing secret, if one is set): Schedules tab -> Webhook -> Rotate URL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
