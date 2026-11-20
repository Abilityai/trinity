/**
 * #2536 — `utils/viewModes.js`, the ONE home of the Dashboard mode list.
 *
 * The order is load-bearing three ways: it is the switcher's visual order, the
 * `v` cycle order (Timeline → Grid → List → Timeline), and index 0 is the
 * default a stale/unknown persisted mode degrades to. Before #2536 the list
 * lived twice in DIFFERENT orders (`['grid','timeline','list']` in the store,
 * `['timeline','grid','list']` in the template), and a cycle built on the
 * store copy would have run against the buttons. Pure module — no DOM.
 */
import { describe, it, expect } from 'vitest'
import {
  ALL_VIEW_MODES, DEFAULT_VIEW_MODE, GATED_VIEW_MODES, VIEW_MODES, nextViewMode, viewModeLabel, viewModesFor,
} from '../../src/utils/viewModes.js'

describe('#2536 viewModes — the one home of the mode list', () => {
  it('lists timeline, grid, list in visual order; timeline is the default; the list is frozen', () => {
    expect([...VIEW_MODES]).toEqual(['timeline', 'grid', 'list'])
    expect(DEFAULT_VIEW_MODE).toBe('timeline')
    expect(Object.isFrozen(VIEW_MODES)).toBe(true)
  })

  it('nextViewMode advances Timeline → Grid → List → Timeline', () => {
    expect(nextViewMode('timeline')).toBe('grid')
    expect(nextViewMode('grid')).toBe('list')
    expect(nextViewMode('list')).toBe('timeline')
  })

  it('an unknown mode wraps to the default (mirrors the store degrade guard)', () => {
    for (const bad of ['graph', '', undefined, null, 'GRID', 0, {}]) {
      expect(nextViewMode(bad), `nextViewMode(${JSON.stringify(bad)})`).toBe(DEFAULT_VIEW_MODE)
    }
  })

  it('three applications return to the start from every mode (cycle property)', () => {
    for (const start of VIEW_MODES) {
      let m = start
      const seen = new Set()
      for (let i = 0; i < VIEW_MODES.length; i++) {
        seen.add(m)
        m = nextViewMode(m)
      }
      expect(m, `cycle from ${start}`).toBe(start)
      // Every mode is visited exactly once per lap — no mode is unreachable.
      expect(seen.size).toBe(VIEW_MODES.length)
    }
  })
})

describe('entitled modes (trinity-enterprise#1307)', () => {
  it('a build without the entitlement has exactly the three base modes', () => {
    expect(viewModesFor([])).toEqual([...VIEW_MODES])
    expect(viewModesFor(['audit'])).toEqual([...VIEW_MODES])
  })

  it('the Flight Deck joins the end of the list when entitled', () => {
    expect(GATED_VIEW_MODES.deck).toBe('flight_deck')
    expect(viewModesFor(['flight_deck'])).toEqual(['timeline', 'grid', 'list', 'deck'])
    expect([...ALL_VIEW_MODES]).toEqual(['timeline', 'grid', 'list', 'deck'])
    expect(viewModeLabel('deck')).toBe('Flight Deck')
    expect(viewModeLabel('grid')).toBe('grid')
  })

  it('the cycle visits the deck only on the list that has it', () => {
    const entitled = viewModesFor(['flight_deck'])
    expect(nextViewMode('list', entitled)).toBe('deck')
    expect(nextViewMode('deck', entitled)).toBe('timeline')
    expect(nextViewMode('list')).toBe('timeline')
    // a saved deck on a build without it degrades to the default
    expect(nextViewMode('deck')).toBe(DEFAULT_VIEW_MODE)
  })
})
