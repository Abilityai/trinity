# Retention defaults — upgrade safety (#1638)

**Date**: 2026-07-15
**Affects**: instances that tracked the `dev` branch between 2026-07-14 and this fix.
**Action required**: none for released versions. Read "Were you affected?" below if you track `dev`.

---

## TL;DR

A change on `dev` (#1065, commit `43411375`) lowered five retention defaults to
5 days. Because those defaults are read at *prune* time as the fallback for any
install with no explicit setting — which is the default state, since nothing
writes those rows — every such install silently inherited the new 5-day window
and hard-deleted everything outside it **seconds after its next boot**.

**No tagged release ever contained this.** `43411375` is not an ancestor of
`v0.8.0` (the tag predates it by six days) and is contained in no tag. If you
run a release, you were never exposed and need do nothing.

This change fixes it before it could ship, and makes the whole class of bug
impossible to reintroduce silently.

## Were you affected?

Only if you deployed the `dev` branch on or after 2026-07-14 **and** restarted
the backend. Check:

```bash
# Did you ever run the affected code?
git merge-base --is-ancestor 43411375 HEAD && echo "exposed" || echo "not exposed"

# What is the oldest execution you still have?
sqlite3 ~/trinity-data/trinity.db \
  "SELECT MIN(started_at), COUNT(*) FROM schedule_executions;"
```

If `MIN(started_at)` is almost exactly 5 days old and you never configured
retention, the sweep ran.

## If you lost data

**There is no in-product recovery.** The prune is a hard `DELETE`; the rows are
gone. Restore from a backup taken before your first boot on the affected code:

```bash
./scripts/deploy/stop.sh
cp ~/trinity-data/trinity.db ~/trinity-data/trinity.db.pre-restore   # keep the current state
cp /path/to/backup/trinity.db ~/trinity-data/trinity.db
./scripts/deploy/start.sh
```

Take a snapshot **before** any upgrade — `scripts/deploy/backup-database.sh`.

### What could have been destroyed

Beyond execution history, note the blast radius was wider than row counts
suggest. `agent_soft_delete_retention_days` dropping 180 → 5 meant any agent
soft-deleted more than 5 days earlier was **hard-purged**, which removes its
`agent-<name>-{workspace,public,shared}` Docker volumes (#1581) — the durable
home volume holding any declared `data_paths` runtime data (#1169). That is not
recoverable from the database backup alone; you would need the volume backup.
This is why that key is now exempt from the floor entirely (below).

## What changed

The community retention floor (#1039) is unchanged as a *product* decision:
**new installs still get 5-day windows.** What changed is *how* it is applied.

| | Before (#1065) | After (#1638) |
|---|---|---|
| How the floor is applied | `OPS_SETTINGS_DEFAULTS = 5`, read at prune time | Explicit rows seeded into a **fresh** DB only |
| Install with no setting | Inherits the floor → **deletes existing data** | Falls back to the wide default → **keeps data** |
| `agent_soft_delete_retention_days` | 5 days (destroys agent volumes) | **180 — exempt from the floor in every edition** |
| A future default change | Retroactively prunes every un-configured install | Affects nothing that already exists |

Code defaults are back to their historical values — execution log 30, execution
rows 90, health checks 7, agent soft-delete 180, schedule soft-delete 30 — and
are now a **safety floor, not a policy knob**. The `system_settings` row is the
only thing that narrows a window, and only an operator (or the fresh-install
seed) writes one.

### Why invert rather than migrate existing installs forward

Every hole in the old mechanism failed *destructive*: a missed migration, a
deleted row, or a future default edit all resolved to "delete more data." With
the defaults wide, every one of those holes fails *safe* — the worst case is an
install keeps more history than intended. That property is worth more than the
disk it costs.

## Also fixed

- **`POST /api/settings/ops/reset` no longer touches retention windows.** It
  deleted every OPS row it knew about, so "reset to defaults" would have
  stranded a retention setting — re-arming mass deletion under the old defaults,
  and silently widening a fresh install's floor under the new ones. Retention now
  changes only through an explicit retention call. The response reports the
  skipped keys.
- **`GET /api/settings/retention` no longer advertises an escape hatch that does
  not exist.** It reported `precedence: "enterprise → env → community-default"`,
  but no env var is read for any of the five windows (grep
  `EXECUTION_ROW_RETENTION_DAYS` — zero hits). The one documented mitigation an
  operator could have reached for did nothing. It now reports the real
  precedence, plus a per-key `source` (`db-row` vs `code-default`).
- **The admin UI no longer lies about defaults.** `OPS_SETTINGS_DESCRIPTIONS`
  still said "default: 90" while the code deleted at 5; a test now pins the two
  together.
- **Mass deletion is visible.** A prune that hits the per-cycle cap logs at
  WARNING (a routine trickle stays INFO — alarm fatigue would defeat the point),
  agent purges always log at WARNING, and the effective window **and its source**
  are logged at boot *before* the first sweep runs.

## For operators who want the 5-day floor on an existing install

It was always available and still is — the floor is not enforced, and never was:

```bash
curl -X PUT http://localhost:8000/api/settings/ops/config \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"settings": {"execution_row_retention_days": "5"}}'
```

Verify what is actually in effect, and where each value came from:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/settings/retention | jq '{precedence, sources, windows}'
```
