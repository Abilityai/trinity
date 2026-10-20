/**
 * trinity-enterprise#706 — the dashboard dot renders the BACKEND's sync state.
 *
 * `utils/syncHealth.js` used to own the thresholds (24 h yellow, 7 d red,
 * behind_working > 0 red) and computed a colour from raw fields. The backend
 * now serves `state` / `reason` / `recommendation` from one policy module
 * (services/sync_freeze_policy.py), so this file maps state → colour and
 * shows the reason — it must not second-guess the state from timestamps.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  classifySyncHealth,
  syncHealthColor,
  syncHealthLabel,
} from '../../src/utils/syncHealth.js'

const MONTH_AGO = new Date(Date.now() - 30 * 24 * 3600 * 1000).toISOString()
const NOW = new Date().toISOString()

describe('classifySyncHealth renders the backend state', () => {
  it.each([
    ['green', 'green'],
    ['yellow', 'yellow'],
    ['red', 'red'],
    ['unknown', 'gray'],
  ])('state %s → %s', (state, colour) => {
    expect(classifySyncHealth({ state })).toBe(colour)
  })

  it('a missing entry or a missing state is gray', () => {
    expect(classifySyncHealth(null)).toBe('gray')
    expect(classifySyncHealth({ last_sync_status: 'success', last_sync_at: NOW })).toBe('gray')
    expect(classifySyncHealth({ state: 'purple' })).toBe('gray')
  })

  it('never re-derives the state from timestamps or counters', () => {
    // A month-old heartbeat the backend calls green stays green…
    expect(classifySyncHealth({ state: 'green', last_sync_at: MONTH_AGO })).toBe('green')
    // …and the 15-ahead auto-sync-off agent the issue describes is what the
    // backend says, not "never synced → gray".
    expect(classifySyncHealth({
      state: 'red', last_sync_status: 'never', ahead_working: 15, behind_working: 0,
    })).toBe('red')
    // behind_working > 0 is no longer an automatic red.
    expect(classifySyncHealth({ state: 'yellow', behind_working: 31 })).toBe('yellow')
  })
})

describe('syncHealthColor', () => {
  it.each([
    ['green', 'bg-status-success-500'],
    ['yellow', 'bg-status-warning-500'],
    ['red', 'bg-status-danger-500'],
    ['unknown', 'bg-gray-400'],
  ])('state %s → %s', (state, cls) => {
    expect(syncHealthColor({ state })).toBe(cls)
  })
})

describe('syncHealthLabel is the backend reason', () => {
  it('shows the reason, and the recommendation when there is one', () => {
    expect(syncHealthLabel({
      state: 'red',
      reason: 'diverged 31 behind / 0 ahead for 26h',
      recommendation: 'pull via git_pull',
    })).toBe('diverged 31 behind / 0 ahead for 26h — pull via git_pull')
    expect(syncHealthLabel({ state: 'green', reason: 'in sync', recommendation: null }))
      .toBe('in sync')
  })

  it('has a fallback for no entry or no reason', () => {
    expect(syncHealthLabel(null)).toBe('Sync status unknown')
    expect(syncHealthLabel({ state: 'unknown' })).toBe('Sync status unknown')
  })
})

describe('no thresholds live in the frontend', () => {
  // @source-text-pin: a util module, not an SFC — the pin is that the
  // threshold constants and the clock are gone from the file entirely.
  const src = readFileSync(resolve(__dirname, '../../src/utils/syncHealth.js'), 'utf8')
  it('has no day/week constants and never reads the clock', () => {
    expect(src).not.toMatch(/DAY_MS|WEEK_MS/)
    expect(src).not.toMatch(/Date\.now\(\)/)
    expect(src).not.toMatch(/behind_working/)
  })
})
