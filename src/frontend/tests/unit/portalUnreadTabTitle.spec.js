/**
 * ent#557 — the browser tab says how many replies are waiting.
 *
 * The sidebar badge cannot reach the case this feature is about: a Workspace tab
 * sitting behind other tabs. `document.title` is the only channel a background
 * tab has, and before this nothing but the router ever wrote it.
 *
 * The decision is a pure function (`formatTabTitle`) and is executed here. The
 * two-writer problem it exists to solve is asserted through the module's own
 * API — set a base, set a count, set a base again — because that ordering is
 * the bug: with two direct writers of `document.title` a navigation would drop
 * the count and a count update would drop the label.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

import {
  formatTabTitle,
  setBaseTitle,
  setUnreadCount,
  clearUnreadCount,
  TAB_UNREAD_CAP,
  _resetTabTitleForTest,
} from '../../src/utils/tabTitle.js'

const BASE = 'Trinity — Workspace'

beforeEach(() => {
  _resetTabTitleForTest()
  vi.unstubAllGlobals()
  vi.stubGlobal('document', { title: '' })
})

describe('ent#557 — how the marker reads', () => {
  it('prefixes the count onto the label', () => {
    expect(formatTabTitle(BASE, 3)).toBe(`(3) ${BASE}`)
  })

  it('is a PREFIX, so a truncated tab keeps it', () => {
    // Browsers truncate a tab from the right. A suffix would be the first
    // thing to disappear on exactly the tab that most needs the marker.
    expect(formatTabTitle(BASE, 3).startsWith('(3)')).toBe(true)
  })

  it('composes with the label rather than replacing it', () => {
    // ent#556 changes what the label SAYS. Nothing here needs to know.
    expect(formatTabTitle('Trinity — Something Else', 2)).toBe('(2) Trinity — Something Else')
  })

  it('renders nothing at zero, not a (0)', () => {
    // A badge announcing that there is nothing to announce.
    expect(formatTabTitle(BASE, 0)).toBe(BASE)
  })

  it('caps where the sidebar caps', () => {
    expect(formatTabTitle(BASE, TAB_UNREAD_CAP)).toBe(`(${TAB_UNREAD_CAP}) ${BASE}`)
    expect(formatTabTitle(BASE, TAB_UNREAD_CAP + 1)).toBe(`(${TAB_UNREAD_CAP}+) ${BASE}`)
    expect(formatTabTitle(BASE, 5000)).toBe(`(${TAB_UNREAD_CAP}+) ${BASE}`)
  })

  it('treats anything that is not a positive count as nothing', () => {
    for (const bad of [null, undefined, -1, 0, NaN, Infinity, 'three', {}]) {
      expect(formatTabTitle(BASE, bad), String(bad)).toBe(BASE)
    }
  })

  it('floors a fractional count rather than printing it', () => {
    expect(formatTabTitle(BASE, 2.7)).toBe(`(2) ${BASE}`)
  })

  it('survives an empty base without emitting a stray separator', () => {
    expect(formatTabTitle('', 2)).toBe('(2)')
    expect(formatTabTitle('', 0)).toBe('')
  })
})

describe('ent#557 — the two writers do not erase each other', () => {
  it('a navigation keeps the count', () => {
    setUnreadCount(3)
    setBaseTitle(BASE)
    expect(document.title).toBe(`(3) ${BASE}`)
    setBaseTitle('Trinity — Settings')
    expect(document.title).toBe('(3) Trinity — Settings')
  })

  it('a count change keeps the label', () => {
    setBaseTitle(BASE)
    setUnreadCount(1)
    expect(document.title).toBe(`(1) ${BASE}`)
    setUnreadCount(4)
    expect(document.title).toBe(`(4) ${BASE}`)
  })

  it('reading everything returns the plain title', () => {
    setBaseTitle(BASE)
    setUnreadCount(2)
    setUnreadCount(0)
    expect(document.title).toBe(BASE)
  })

  it('leaving the Workspace clears the marker', () => {
    // The count would otherwise outlive the only surface that can explain it —
    // a tab reading `(3)` on a page with nothing to click.
    setBaseTitle(BASE)
    setUnreadCount(3)
    clearUnreadCount()
    expect(document.title).toBe(BASE)
  })

  it('does not touch document.title when the count has not changed', () => {
    setBaseTitle(BASE)
    setUnreadCount(2)
    document.title = 'someone else wrote this'
    setUnreadCount(2)
    expect(document.title).toBe('someone else wrote this')
  })

  it('does not throw where there is no document', () => {
    // The module is imported by the ROUTER, which is loaded in contexts that
    // have no DOM at all.
    vi.stubGlobal('document', undefined)
    expect(() => { setBaseTitle(BASE); setUnreadCount(2) }).not.toThrow()
  })
})
