import { describe, it, expect } from 'vitest'
import {
  RETENTION_FIELDS,
  visibleRetentionFields,
  retentionFieldValue,
  retentionFieldSource,
  isEnvSourced,
  retentionFormFromStatus,
  retentionSaveBody,
} from '../../src/utils/retentionFields.js'

// trinity-enterprise#671 — the Settings → Retention field model. These rules
// decide what the panel's Save WRITES, which is why they live in a util the
// view calls rather than inline in Settings.vue (too large to mount).

const SIBLINGS = [
  'log_retention_days',
  'execution_log_retention_days',
  'execution_row_retention_days',
  'health_check_retention_days',
  'agent_soft_delete_retention_days',
  'schedule_soft_delete_retention_days',
]

function status({ edition = 'enterprise', sources = {}, quotaSource = 'code-default', windows = {}, quota = 100000 } = {}) {
  return {
    edition,
    community_floor_days: 5,
    windows: {
      log_retention_days: 5,
      execution_log_retention_days: 30,
      execution_row_retention_days: 90,
      health_check_retention_days: 7,
      agent_soft_delete_retention_days: 180,
      schedule_soft_delete_retention_days: 30,
      metrics_retention_days: 365,
      audit_log_retention_days: 365,
      ...windows,
    },
    sources: { metrics_retention_days: 'db-row', ...sources },
    quotas: { metrics_daily_point_cap: { value: quota, source: quotaSource } },
  }
}

const field = (key) => RETENTION_FIELDS.find((f) => f.key === key)

describe('visibleRetentionFields', () => {
  it('shows the two metric rows beside the siblings when the managed endpoint exists', () => {
    expect(visibleRetentionFields(status()).map((f) => f.key)).toEqual([
      ...SIBLINGS,
      'metrics_retention_days',
      'metrics_daily_point_cap',
    ])
  })

  it('leaves the metric rows out in Community (AC3) — the siblings still render', () => {
    expect(visibleRetentionFields(status({ edition: 'community' })).map((f) => f.key)).toEqual(SIBLINGS)
  })

  it('renders nothing enterprise-only before the response has arrived', () => {
    expect(visibleRetentionFields(null).map((f) => f.key)).toEqual(SIBLINGS)
  })
})

describe('value and source', () => {
  it('reads the window from `windows`/`sources` and the quota from `quotas`', () => {
    const r = status({ quota: 42, quotaSource: 'env', sources: { metrics_retention_days: 'env' } })
    expect(retentionFieldValue(r, field('metrics_retention_days'))).toBe(365)
    expect(retentionFieldSource(r, field('metrics_retention_days'))).toBe('env')
    expect(retentionFieldValue(r, field('metrics_daily_point_cap'))).toBe(42)
    expect(retentionFieldSource(r, field('metrics_daily_point_cap'))).toBe('env')
  })

  it('treats only an env-backed field whose source is `env` as env-sourced', () => {
    const r = status({ quotaSource: 'env', sources: { metrics_retention_days: 'db-row' } })
    expect(isEnvSourced(r, field('metrics_daily_point_cap'))).toBe(true)
    expect(isEnvSourced(r, field('metrics_retention_days'))).toBe(false)
    // A sibling window has no env tier, whatever a payload claims.
    expect(isEnvSourced({ ...r, sources: { health_check_retention_days: 'env' } }, field('health_check_retention_days'))).toBe(false)
  })

  it('pre-fills the form from the response for every visible field', () => {
    const values = retentionFormFromStatus(status({ quota: 0 }))
    expect(values.metrics_daily_point_cap).toBe(0)
    expect(values.metrics_retention_days).toBe(365)
    expect(values.health_check_retention_days).toBe(7)
    expect('metrics_daily_point_cap' in retentionFormFromStatus(status({ edition: 'community' }))).toBe(false)
  })
})

describe('retentionSaveBody', () => {
  const r = status()
  const fields = visibleRetentionFields(r)
  const loaded = retentionFormFromStatus(r)

  it('sends only the field that changed', () => {
    const form = { ...loaded, metrics_daily_point_cap: 5000 }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({ metrics_daily_point_cap: 5000 })
  })

  it('sends nothing when nothing changed — an untouched code default never becomes a stored row', () => {
    expect(retentionSaveBody(fields, { ...loaded }, loaded, r)).toEqual({})
  })

  it('sends 0 when the operator lifts the quota (0 = unlimited is a real value, not "unset")', () => {
    const form = { ...loaded, metrics_daily_point_cap: 0 }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({ metrics_daily_point_cap: 0 })
  })

  it('never sends an env-sourced field, even if the form holds a different value', () => {
    const envR = status({ quotaSource: 'env' })
    const envLoaded = retentionFormFromStatus(envR)
    const form = { ...envLoaded, metrics_daily_point_cap: 7, health_check_retention_days: 14 }
    expect(retentionSaveBody(visibleRetentionFields(envR), form, envLoaded, envR)).toEqual({
      health_check_retention_days: 14,
    })
  })

  it('skips a cleared or unparseable input', () => {
    const form = { ...loaded, health_check_retention_days: '', metrics_retention_days: 'abc' }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({})
  })

  it('passes an out-of-bounds value through for the server to reject by name', () => {
    const form = { ...loaded, metrics_daily_point_cap: 10000001 }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({ metrics_daily_point_cap: 10000001 })
  })

  it('sends a huge entry as itself — never truncated to its leading digit', () => {
    // v-model.number turns 22 typed digits into 1e22; parseInt(1e22) === 1.
    const form = { ...loaded, metrics_daily_point_cap: 1e22 }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({ metrics_daily_point_cap: 1e22 })
  })

  it('sends a fractional entry as itself for the server to reject, not silently truncated', () => {
    const form = { ...loaded, health_check_retention_days: 12.5 }
    expect(retentionSaveBody(fields, form, loaded, r)).toEqual({ health_check_retention_days: 12.5 })
  })

  it('does not send a Community-hidden field', () => {
    const c = status({ edition: 'community' })
    const form = { ...retentionFormFromStatus(c), metrics_daily_point_cap: 1 }
    expect(retentionSaveBody(visibleRetentionFields(c), form, retentionFormFromStatus(c), c)).toEqual({})
  })
})

describe('field definitions', () => {
  it('bounds each row by its own maximum, not the window cap', () => {
    expect(field('metrics_daily_point_cap').max).toBe(10000000)
    expect(field('metrics_retention_days').max).toBe(3650)
    expect(field('metrics_daily_point_cap').hint).toBe('0 = unlimited')
  })

  it('names the variable and the revert in the env explanation (decided copy)', () => {
    expect(field('metrics_retention_days').envTitle).toBe(
      'Set by METRICS_RETENTION_DAYS — unset the variable and restart to edit it here; it then starts from the 365-day default.'
    )
    expect(field('metrics_daily_point_cap').envTitle).toBe(
      'Set by METRICS_DAILY_POINT_CAP — unset the variable and restart to edit it here; it then starts from the 100,000 default.'
    )
  })
})
