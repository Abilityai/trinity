// @vitest-environment jsdom
/**
 * #3454 — the Retention tab shows the automatic-backup status that
 * `GET /api/settings/retention` already returns under `backup`.
 *
 * Mounted, because the point is what an operator can read: a healthy backup, a
 * failed one, one that has never run, a backend that reports nothing, and the
 * frame before the response arrives must each say what they are.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import BackupStatusPanel from '../../src/components/settings/BackupStatusPanel.vue'

const NOW = new Date('2026-10-09T17:45:00Z')

// The shape `build_backup_status_block` returns.
const healthy = () => ({
  enabled: true,
  schedule_utc: '03:30',
  backup_dir: '/data/backups',
  scope: 'same-disk',
  retention_days: 14,
  min_keep: 3,
  last_status: 'ok',
  last_success_at: '2026-10-09T03:30:00.257126Z',
  last_success_age_days: 0.58,
  stale: false,
  last_error: null,
  last_path: '/data/backups/trinity-backup-20261009.db',
  last_size_bytes: 86802432,
  last_duration_ms: 137,
  last_trigger: 'scheduled',
  artifacts: {
    count: 13,
    total_bytes: 1146257408,
    newest: 'trinity-backup-20261009.db',
    newest_age_seconds: 50400,
  },
})

const panel = (props) => mount(BackupStatusPanel, { props: { hasLoaded: true, ...props } })
const status = (w) => w.get('[data-testid="backup-status"]').text()

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => vi.useRealTimers())

describe('BackupStatusPanel (#3454)', () => {
  it('a healthy backup shows when it last ran, the recovery points and where they live', () => {
    const w = panel({ backup: healthy() })
    expect(status(w)).toBe('Healthy')

    const last = w.get('[data-testid="backup-last-success"]')
    expect(last.text()).toContain('14 hours ago')
    // Absolute time on hover, parsed as UTC rather than as local time.
    expect(last.get('time').attributes('datetime')).toBe('2026-10-09T03:30:00.257Z')
    expect(last.get('time').attributes('title')).toBeTruthy()

    const points = w.get('[data-testid="backup-recovery-points"]').text()
    expect(points).toContain('13')
    expect(points).toContain('1.07 GB')
    expect(points).toContain('trinity-backup-20261009.db')

    expect(w.get('[data-testid="backup-location"]').text()).toContain('/data/backups')
    // The same-disk boundary is stated, not left for the operator to infer.
    expect(w.get('[data-testid="backup-location"]').text()).toMatch(/same disk/i)
    expect(w.get('[data-testid="backup-schedule"]').text()).toContain('03:30 UTC')
    expect(w.get('[data-testid="backup-kept"]').text()).toContain('14 days')
    expect(w.find('[data-testid="backup-problem"]').exists()).toBe(false)
  })

  it('a failed backup reads as failed and carries the error', () => {
    const w = panel({
      backup: {
        ...healthy(),
        last_status: 'failed',
        last_error: 'OperationalError: database disk image is malformed',
        stale: true,
        last_success_at: '2026-10-01T03:30:00Z',
        last_success_age_days: 8.58,
      },
    })
    expect(status(w)).toBe('Last backup failed')
    expect(status(w)).not.toMatch(/healthy/i)
    const problem = w.get('[data-testid="backup-problem"]').text()
    expect(problem).toContain('database disk image is malformed')
    // The last GOOD backup is still named, as the last good one.
    expect(w.get('[data-testid="backup-last-success"]').text()).not.toMatch(/never/i)
  })

  it('a backup skipped for lack of disk space is not healthy either', () => {
    const w = panel({ backup: { ...healthy(), last_status: 'skipped_no_space', last_error: 'need 104 MB free' } })
    expect(status(w)).toMatch(/not enough disk space/i)
    expect(w.get('[data-testid="backup-problem"]').text()).toContain('need 104 MB free')
  })

  it('a last success older than the alarm threshold reads as stale, not healthy', () => {
    const w = panel({ backup: { ...healthy(), stale: true, last_success_age_days: 5.2 } })
    expect(status(w)).toBe('Stale')
  })

  it('a backup that has never run says so', () => {
    const w = panel({
      backup: {
        ...healthy(),
        last_status: null,
        last_success_at: null,
        last_success_age_days: null,
        last_path: null,
        last_size_bytes: null,
        last_duration_ms: null,
        last_trigger: null,
        artifacts: { count: 0, total_bytes: 0, newest: null, newest_age_seconds: null },
      },
    })
    expect(status(w)).toBe('No backup yet')
    expect(w.get('[data-testid="backup-last-success"]').text()).toMatch(/never/i)
    expect(w.get('[data-testid="backup-recovery-points"]').text()).toMatch(/none/i)
  })

  it('disabled backups read as off even with an old success on record', () => {
    const w = panel({ backup: { ...healthy(), enabled: false } })
    expect(status(w)).toBe('Off')
  })

  it('a response with no backup key says the status is not reported', () => {
    const w = panel({ backup: undefined })
    expect(w.find('[data-testid="backup-status"]').exists()).toBe(false)
    expect(w.get('[data-testid="backup-not-reported"]').text()).toMatch(/does not report/i)
    expect(w.text()).not.toMatch(/healthy|never|failed/i)
  })

  it('a backup block the server could not build says unavailable, never healthy', () => {
    const w = panel({ backup: { error: 'unavailable' } })
    expect(w.find('[data-testid="backup-status"]').exists()).toBe(false)
    expect(w.get('[data-testid="backup-unavailable"]').text()).toMatch(/unavailable/i)
  })

  it('before the response arrives it holds its footprint and claims nothing', () => {
    const w = panel({ hasLoaded: false, backup: undefined })
    expect(w.get('[data-testid="backup-loading"]').attributes('aria-busy')).toBe('true')
    expect(w.find('[data-testid="backup-status"]').exists()).toBe(false)
    expect(w.find('[data-testid="backup-not-reported"]').exists()).toBe(false)
    // The same rows are reserved that the loaded state fills.
    const rows = (x) => x.findAll('[data-testid="backup-facts"] dt').map((d) => d.text())
    expect(rows(w)).toEqual(rows(panel({ backup: healthy() })))
  })

  it('a failed load is a failure, and offers the retry', async () => {
    const w = panel({ hasLoaded: false, backup: undefined, error: 'Failed to load retention settings.' })
    expect(w.find('[data-testid="backup-loading"]').exists()).toBe(false)
    expect(w.find('[data-testid="backup-not-reported"]').exists()).toBe(false)
    await w.get('[data-testid="load-failed"] button').trigger('click')
    expect(w.emitted('retry')).toHaveLength(1)
  })
})
