/**
 * Settings → Retention: the editable fields and the rules that decide what the
 * panel renders and what its Save writes (#1039; trinity-enterprise#671).
 *
 * The rules live here rather than inline in `Settings.vue` because that view is
 * too large to mount in a unit test — a rule kept in the SFC is a rule only a
 * browser can reach (the `opsSettings.js` precedent, #2411). Three of them
 * guard a WRITE, which is why they matter:
 *
 * - **Only changed fields are sent.** The Save used to PUT every field, so
 *   saving one window turned every other knob's code default into a stored row
 *   and re-clamped values nobody had touched. For an env-backed knob that row
 *   silently ends the environment variable's authority.
 * - **An env-sourced field is never sent.** Its row is read-only; a value in
 *   the body would create a row that overrides the variable without anyone
 *   having chosen to.
 * - **Enterprise-only fields render only when the same response says the
 *   managed endpoint exists** (`edition === 'enterprise'`), so nothing pops in
 *   after a second async load, and Community never shows a row it cannot save.
 *
 * The managed endpoint must accept every `key` below — the enterprise suite
 * parses this file (`key: '...'`) and fails when one is missing, because a key
 * the endpoint does not know is a save that cannot succeed.
 */

export const RETENTION_FIELDS = [
  { key: 'log_retention_days', label: 'Log archival', unit: 'days', max: 3650, from: 'windows' },
  { key: 'execution_log_retention_days', label: 'Execution logs', unit: 'days', max: 3650, from: 'windows' },
  { key: 'execution_row_retention_days', label: 'Execution rows', unit: 'days', max: 3650, from: 'windows' },
  { key: 'health_check_retention_days', label: 'Health checks', unit: 'days', max: 3650, from: 'windows' },
  { key: 'agent_soft_delete_retention_days', label: 'Soft-deleted agents', unit: 'days', max: 3650, from: 'windows' },
  { key: 'schedule_soft_delete_retention_days', label: 'Soft-deleted schedules', unit: 'days', max: 3650, from: 'windows' },
  // trinity-enterprise#671 — the two ent#478 metric knobs. Both are env-backed
  // (row → env → default), so each names its variable and what unsetting it
  // does: the boot seeder then writes the default as a row, which for the
  // window can be narrower than the value the variable held.
  {
    key: 'metrics_retention_days',
    label: 'Metric points',
    unit: 'days',
    max: 3650,
    from: 'windows',
    enterpriseOnly: true,
    envVar: 'METRICS_RETENTION_DAYS',
    envTitle: 'Set by METRICS_RETENTION_DAYS — unset the variable and restart to edit it here; it then starts from the 365-day default.',
  },
  {
    // Not a window: a per-agent per-UTC-day write budget. 0 is UNLIMITED and
    // there is no floor, so it carries its own hint instead of the footer's.
    key: 'metrics_daily_point_cap',
    label: 'Metric point quota',
    unit: 'points / agent / day',
    max: 10000000,
    from: 'quotas',
    hint: '0 = unlimited',
    enterpriseOnly: true,
    envVar: 'METRICS_DAILY_POINT_CAP',
    envTitle: 'Set by METRICS_DAILY_POINT_CAP — unset the variable and restart to edit it here; it then starts from the 100,000 default.',
  },
]

/** The fields to render for one `GET /api/settings/retention` response. */
export function visibleRetentionFields(retention) {
  const enterprise = retention?.edition === 'enterprise'
  return RETENTION_FIELDS.filter((f) => !f.enterpriseOnly || enterprise)
}

/** The value the response reports for a field (windows map or quotas block). */
export function retentionFieldValue(retention, field) {
  if (field.from === 'quotas') return retention?.quotas?.[field.key]?.value
  return retention?.windows?.[field.key]
}

/** Where the value came from: `db-row` / `env` / `code-default` (or undefined). */
export function retentionFieldSource(retention, field) {
  if (field.from === 'quotas') return retention?.quotas?.[field.key]?.source
  return retention?.sources?.[field.key]
}

/** True when the environment supplies the value — the row is then read-only. */
export function isEnvSourced(retention, field) {
  return Boolean(field.envVar) && retentionFieldSource(retention, field) === 'env'
}

/** `{ key: value }` for every visible field — the form's starting point AND the
 *  snapshot Save diffs against. */
export function retentionFormFromStatus(retention) {
  const values = {}
  for (const f of visibleRetentionFields(retention)) {
    values[f.key] = retentionFieldValue(retention, f)
  }
  return values
}

/**
 * The PUT body: only fields the operator changed, never an env-sourced one,
 * never an empty or non-numeric one. An empty object means nothing to save.
 *
 * The number is sent as typed, never through `parseInt`: `v-model.number`
 * hands a 22-digit entry over as `1e22`, and `parseInt(1e22)` is `1` — a save
 * that would set the quota to one point a day. Out-of-range and fractional
 * values go to the server as themselves, which rejects them by name.
 */
export function retentionSaveBody(fields, form, loaded, retention) {
  const body = {}
  for (const f of fields) {
    if (isEnvSourced(retention, f)) continue
    const raw = form?.[f.key]
    if (raw === '' || raw === null || raw === undefined) continue
    const n = Number(raw)
    if (!Number.isFinite(n)) continue
    if (n === loaded?.[f.key]) continue
    body[f.key] = n
  }
  return body
}
