/**
 * trinity-enterprise#527 rider — the readiness stamp on the agents list and the
 * fleet grid. The shared predicate both surfaces render from.
 */
import { describe, it, expect } from 'vitest'
import { readinessBadge } from '../../src/utils/readinessBadge.js'

describe('readinessBadge', () => {
  it('no stamp is no badge — never a guessed calibrating', () => {
    expect(readinessBadge(null)).toBeNull()
    expect(readinessBadge(undefined)).toBeNull()
    expect(readinessBadge({})).toBeNull()
  })

  it('an unknown state is not rendered as either badge', () => {
    expect(readinessBadge({ status: 'maybe' })).toBeNull()
  })

  it('ready by the owner', () => {
    expect(readinessBadge({ status: 'ready', changed_at: '2026-09-28T10:00:00Z', source: 'owner' })).toEqual({
      label: 'ready',
      variant: 'success',
      title: 'Ready — marked ready by its owner since 2026-09-28 (UTC)',
    })
  })

  it('ready from the ent#689 rollout says so', () => {
    expect(readinessBadge({ status: 'ready', changed_at: '2026-09-24T08:00:00Z', source: 'rollout' }).title)
      .toBe('Ready — carried over when the readiness gate shipped since 2026-09-24 (UTC)')
  })

  it('calibrating names what it holds back', () => {
    const b = readinessBadge({ status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' })
    expect(b.variant).toBe('warning')
    expect(b.title).toBe('Calibrating since 2026-09-28 (UTC) — its scheduled brief is paused until its owner marks it ready')
  })

  it('a missing or malformed date is left out, not printed', () => {
    expect(readinessBadge({ status: 'ready', changed_at: null }).title).toBe('Ready — marked ready by its owner')
    expect(readinessBadge({ status: 'ready', changed_at: 'yesterday' }).title).toBe('Ready — marked ready by its owner')
  })
})
