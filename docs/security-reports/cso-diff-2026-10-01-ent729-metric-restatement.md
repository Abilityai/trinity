# /cso --diff — ent#729 metric restatement (2026-10-01)

**Scope**: branch `feature/729-metric-restatement` vs `dev` · daily mode (8/10 gate) · cso v1.2
**Result**: **0 findings** (0 critical / 0 high / 0 medium).

## Surface changed
- `POST /api/agents/{name}/metrics/points` — a different value at an existing `(metric, ts, dims)` now restates the stored row (`revision` +1, `recorded_at`, `execution_id` follow the write) instead of deduplicating; the 201 gains `corrected`.
- MCP `record_metrics` — static description text and one receipt count.
- `metric_points` — `revision BIGINT NOT NULL DEFAULT 0`, `recorded_at TEXT` (SQLite `metric_points_restatement` + Alembic `0086`).

No new endpoint, tool, principal, dependency, workflow, image or compose change.

## Verified clean
| Check | Evidence |
|---|---|
| Auth boundary (Invariant #8) | unchanged — `AuthorizedAgent` + the self-gate at `routers/metric_points.py:180` before any read; #2996 census, #186, #1310, #293 guards green |
| Cross-agent restatement | impossible — an agent key records only as itself, and the store stamps `agent_name` from the auth-resolved name |
| Write-side columns | `revision` / `recorded_at` stamped by the store; a smuggled value in a row dict is ignored (tested) |
| Injection (A05) | SQLAlchemy Core upsert, bound parameters; SET/WHERE reference columns and `excluded.*` only |
| Failure direction (A10) | the store's duplicate-identity refusal is stated; unreachable from the route (`duplicate_in_batch` refuses first, `metric_points_service.py:376`) |
| Migration | additive; NOT NULL only with a constant default; no backfill; a pre-ent#729 INSERT still succeeds on both dialects |
| Logs | one INFO line per correcting batch: agent + count, never a value or dimension (tested) |
| Agent-loaded text | tool description static; 0 concealed-Unicode / injection-string hits across changed files |
| Secrets / enterprise tokens | 0 hits in added lines |

## Accepted (by design, ruled at the plan gate)
- A shared viewer who may record may now also restate a past point. The route already admitted shared viewers, who could already set the latest value; decision #9 of the ent#729 plan.
- Restatement is lossy and last-write-wins (requirements §48.9).
- An identical earlier batch replays over a later correction (`A → B → A` under one key/turn ends on `B`) — documented and pinned by a route test.

## Coverage gaps
- Enterprise submodule not mounted: a private caller of `db.insert_metric_points` (2-tuple → 3-field NamedTuple) was not inspected; org code search returned no hits.
