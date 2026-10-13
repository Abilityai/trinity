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
      title: 'Ready since 2026-09-28 (UTC) — marked ready by its owner',
    })
  })

  it('ready from the ent#689 rollout says so', () => {
    expect(readinessBadge({ status: 'ready', changed_at: '2026-09-24T08:00:00Z', source: 'rollout' }).title)
      .toBe('Ready since 2026-09-24 (UTC) — carried over when the readiness gate shipped')
  })

  it('calibrating names what it holds back — only when a brief is actually held', () => {
    const cal = { status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' }
    const held = readinessBadge(cal, true)
    expect(held.variant).toBe('warning')
    expect(held.title).toBe('Calibrating since 2026-09-28 (UTC) — its scheduled brief is paused until its owner marks it ready')
  })

  it('calibrating with no held brief claims no pause (the role card says none either)', () => {
    // No seat-delivery schedule, or autonomy off: "marks it ready" would start nothing.
    const cal = { status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' }
    for (const b of [readinessBadge(cal), readinessBadge(cal, false), readinessBadge(cal, undefined)]) {
      expect(b.label).toBe('calibrating')
      expect(b.title).toBe('Calibrating since 2026-09-28 (UTC) — not yet marked ready by its owner')
      expect(b.title).not.toContain('paused')
    }
  })

  it('a missing or malformed date is left out, not printed', () => {
    expect(readinessBadge({ status: 'ready', changed_at: null }).title).toBe('Ready — marked ready by its owner')
    expect(readinessBadge({ status: 'ready', changed_at: 'yesterday' }).title).toBe('Ready — marked ready by its owner')
  })
})
