/**
 * trinity-enterprise#707 — the agent card's sync numbers.
 *
 * `utils/syncSummary.js` formats what the backend measured — ahead / behind on
 * the agent's own branch, the dirty-file count, the age of the last successful
 * push — into one line, and picks the chip kind and text colour from the
 * backend's `state` (via `classifySyncHealth`). It decides no state: the same
 * numbers under a different `state` give a different colour, never the
 * reverse.
 *
 * It reads both entry shapes: the per-agent reads (`ahead_working` /
 * `behind_working`, the dashboard batch and `/git/sync-state`) and the fleet
 * block (`ahead` / `behind`).
 */
import { describe, it, expect } from 'vitest'
import {
  formatSyncSummary,
  syncChip,
  syncTextClass,
  formatAge,
} from '../../src/utils/syncSummary.js'

const NOW = Date.parse('2026-09-27T12:00:00Z')
const PUSHED_3H = '2026-09-27T09:00:00.000000Z'

const entry = (over = {}) => ({
  state: 'red',
  reason: 'diverged 0 behind / 7 ahead for 26h',
  recommendation: 'enable auto-sync',
  ahead_working: 7,
  behind_working: 0,
  dirty_files: 12,
  last_successful_push_at: PUSHED_3H,
  ...over,
})

describe('formatSyncSummary', () => {
  it('ahead, behind, dirty and the push age on one line', () => {
    expect(formatSyncSummary(entry(), NOW)).toBe('↑7 ↓0 · 12 dirty · pushed 3h ago')
  })

  it('reads the fleet block shape too', () => {
    const fleet = { state: 'yellow', ahead: 0, behind: 31, dirty_files: 0,
      last_successful_push_at: PUSHED_3H }
    expect(formatSyncSummary(fleet, NOW)).toBe('↑0 ↓31 · pushed 3h ago')
  })

  it('a clean tree drops the dirty part; no push says so', () => {
    expect(formatSyncSummary(entry({ dirty_files: 0, last_successful_push_at: null }), NOW))
      .toBe('↑7 ↓0 · never pushed')
  })

  it('an unknown count is a question mark, never a zero', () => {
    expect(formatSyncSummary(entry({ ahead_working: null, behind_working: undefined }), NOW))
      .toBe('↑? ↓? · 12 dirty · pushed 3h ago')
  })

  it('nothing for an agent the poller has not observed', () => {
    expect(formatSyncSummary({ state: 'unknown', ahead_working: 0 }, NOW)).toBeNull()
    expect(formatSyncSummary(null, NOW)).toBeNull()
  })

  it('a naive timestamp is UTC, like the backend writes it', () => {
    expect(formatSyncSummary(entry({ last_successful_push_at: '2026-09-27T09:00:00' }), NOW))
      .toBe('↑7 ↓0 · 12 dirty · pushed 3h ago')
  })
})

describe('formatAge', () => {
  it.each([
    [30 * 1000, 'just now'],
    [45 * 60 * 1000, '45m ago'],
    [26 * 3600 * 1000, '26h ago'],
    [9 * 86400 * 1000, '9d ago'],
    [-5 * 60 * 1000, 'just now'], // a clock a little ahead of ours
  ])('%i ms → %s', (ms, text) => {
    expect(formatAge(ms)).toBe(text)
  })
})

describe('syncChip — the tile chip', () => {
  it.each([
    ['red', 'crit'],
    ['yellow', 'warn'],
    ['green', 'calm'],
  ])('state %s → chip kind %s', (state, kind) => {
    expect(syncChip(entry({ state }), NOW).kind).toBe(kind)
  })

  it('text is the summary; the title is reason — recommendation plus the absolute push time', () => {
    const chip = syncChip(entry(), NOW)
    expect(chip.text).toBe('↑7 ↓0 · 12 dirty · pushed 3h ago')
    expect(chip.title.split('\n')[0]).toBe('diverged 0 behind / 7 ahead for 26h — enable auto-sync')
    expect(chip.title).toMatch(/^Last successful push: .*2026/m)
  })

  it('a frozen agent says its schedules are paused (both entry shapes)', () => {
    expect(syncChip(entry({ freeze: true }), NOW).title).toMatch(/^Scheduled runs are paused until it syncs$/m)
    expect(syncChip(entry({ frozen: true }), NOW).title).toMatch(/paused/)
    expect(syncChip(entry({ freeze: false }), NOW).title).not.toMatch(/paused/)
  })

  it('no chip for unknown', () => {
    expect(syncChip({ state: 'unknown' }, NOW)).toBeNull()
    expect(syncChip(undefined, NOW)).toBeNull()
  })

  it('the kind follows state, not the numbers', () => {
    // A big divergence the backend calls yellow (a deployment) stays warn.
    expect(syncChip(entry({ state: 'yellow', behind_working: 400 }), NOW).kind).toBe('warn')
    // A zero-everything row the backend calls red (a failed push) stays crit.
    expect(syncChip(entry({ state: 'red', ahead_working: 0, dirty_files: 0 }), NOW).kind)
      .toBe('crit')
  })
})

describe('syncTextClass — status text on a chrome fill (800 light / 300 dark)', () => {
  it.each([
    ['red', 'text-status-danger-800 dark:text-status-danger-300'],
    ['yellow', 'text-status-warning-800 dark:text-status-warning-300'],
    ['green', 'text-status-success-800 dark:text-status-success-300'],
    ['unknown', ''],
  ])('%s → %s', (state, cls) => {
    expect(syncTextClass({ state })).toBe(cls)
  })
})
