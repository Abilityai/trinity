import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  viewState,
  staleBannerMessage,
  listFrom,
  decideAutoExpand,
} from '@/utils/loadingState'

/**
 * #1927 — the decision surface behind "background refresh is invisible"
 * (design-system p13/p14/p15).
 *
 * The bug class: a template gates its loading UI on a fetch-in-flight flag, so
 * every background poll swaps rendered data for a spinner. The fix is one rule —
 * loading means "no data yet" — and this module is the ONLY home of that rule.
 * `vitest.config.js` pins `environment: 'node'` (no component mounting), so the
 * four surfaces are thin callers and what is asserted here is what they do.
 */

const here = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(here, '../../src', rel), 'utf8')

describe('viewState — loading means "no data yet", never "fetch in flight"', () => {
  it('is import-pure: loads in node without touching window', () => {
    expect(typeof viewState).toBe('function')
  })

  it('no data + no error ⇒ loading, whether or not a fetch is in flight', () => {
    expect(viewState({ loading: true, hasLoaded: false, error: '' })).toEqual({ state: 'loading', stale: false })
    // The pre-fetch frame (mount, before the first request leaves) is also
    // "no data yet" — never the empty copy.
    expect(viewState({ loading: false, hasLoaded: false, error: '' })).toEqual({ state: 'loading', stale: false })
  })

  it('no data + error ⇒ failed (not the empty copy — the #1926 lie)', () => {
    expect(viewState({ loading: false, hasLoaded: false, error: 'boom' })).toEqual({ state: 'failed', stale: false })
    // A retry in flight with no data is still the failed state until it lands.
    expect(viewState({ loading: true, hasLoaded: false, error: 'boom' })).toEqual({ state: 'failed', stale: false })
  })

  it('a SUCCEEDED fetch that returned zero ⇒ empty', () => {
    expect(viewState({ loading: false, hasLoaded: true, error: '', count: 0 })).toEqual({ state: 'empty', stale: false })
  })

  it('data on screen ⇒ ready, even while a background poll is in flight (p13)', () => {
    expect(viewState({ loading: true, hasLoaded: true, error: '', count: 3 })).toEqual({ state: 'ready', stale: false })
  })

  it('data on screen + a failed refresh ⇒ ready but STALE — never a spinner, never the failed block', () => {
    expect(viewState({ loading: false, hasLoaded: true, error: 'poll died', count: 3 })).toEqual({ state: 'ready', stale: true })
    // …and a retry in flight keeps the data up (retrying ≠ loading).
    expect(viewState({ loading: true, hasLoaded: true, error: 'poll died', count: 3 })).toEqual({ state: 'ready', stale: true })
    // An empty list that then failed to refresh is empty-but-stale.
    expect(viewState({ loading: false, hasLoaded: true, error: 'poll died', count: 0 })).toEqual({ state: 'empty', stale: true })
  })

  it('count is optional: a non-list surface (one object) is ready once loaded', () => {
    expect(viewState({ loading: false, hasLoaded: true, error: '' })).toEqual({ state: 'ready', stale: false })
  })

  it('treats null/undefined error as "no error" and truthy strings/objects as error', () => {
    expect(viewState({ loading: false, hasLoaded: true, error: null, count: 1 }).stale).toBe(false)
    expect(viewState({ loading: false, hasLoaded: true, error: undefined, count: 1 }).stale).toBe(false)
    expect(viewState({ loading: false, hasLoaded: true, error: new Error('x'), count: 1 }).stale).toBe(true)
  })
})

describe('staleBannerMessage — the style guide treatment: "showing data from HH:MM"', () => {
  const fmt = (d) => `T${d.getUTCHours()}:${String(d.getUTCMinutes()).padStart(2, '0')}`

  it('names the subject and the time the data was loaded', () => {
    const at = new Date(Date.UTC(2026, 7, 21, 10, 42))
    const msg = staleBannerMessage('the queue', at, { formatTime: fmt })
    expect(msg).toBe("Couldn't refresh the queue — showing data from T10:42.")
  })

  it('accepts an epoch number as well as a Date', () => {
    const at = Date.UTC(2026, 7, 21, 9, 5)
    expect(staleBannerMessage('agents', at, { formatTime: fmt })).toContain('T9:05')
  })

  it('falls back honestly when no load time is known — never prints "undefined"', () => {
    const msg = staleBannerMessage('notifications', null)
    expect(msg).toBe("Couldn't refresh notifications — showing the last data that loaded.")
    expect(msg).not.toMatch(/undefined|null|NaN/)
  })

  it('uses a locale time by default (no injected formatter) and still says what it shows', () => {
    const msg = staleBannerMessage('executions', new Date())
    expect(msg).toMatch(/^Couldn't refresh executions — showing data from .+\.$/)
    expect(msg).not.toMatch(/undefined|NaN/)
  })
})

describe('listFrom — response-shape normalizer for the /m fetchers (pre-existing TypeErrors)', () => {
  it('passes a bare array through', () => {
    expect(listFrom([1, 2], 'items')).toEqual([1, 2])
  })
  it('unwraps the named key: {items:[…]} (operator-queue) and {agents:[…]} (execution-stats)', () => {
    expect(listFrom({ items: [{ id: 1 }], count: 1 }, 'items')).toEqual([{ id: 1 }])
    expect(listFrom({ agents: [{ name: 'a' }] }, 'agents')).toEqual([{ name: 'a' }])
  })
  it('never returns a non-array: null / undefined / wrong key / non-array value ⇒ []', () => {
    expect(listFrom(null, 'items')).toEqual([])
    expect(listFrom(undefined, 'items')).toEqual([])
    expect(listFrom({ count: 0 }, 'items')).toEqual([])
    expect(listFrom({ items: 'nope' }, 'items')).toEqual([])
    expect(listFrom('nope', 'items')).toEqual([])
  })
})

describe('decideAutoExpand — landing expands the first open item; a human in control is never overridden (p5)', () => {
  it('armed + open items + nothing expanded ⇒ expand the first open item', () => {
    expect(decideAutoExpand({ armed: true, openIds: ['a', 'b'], expandedId: null })).toBe('a')
  })
  it('armed but something OPEN is already expanded ⇒ nothing', () => {
    expect(decideAutoExpand({ armed: true, openIds: ['a', 'b'], expandedId: 'b' })).toBe(null)
  })
  it('a STALE expandedId (resolved while away, not in the open set) does not block forever', () => {
    expect(decideAutoExpand({ armed: true, openIds: ['a', 'b'], expandedId: 'gone' })).toBe('a')
  })
  it('not armed (the user toggled something) ⇒ never, whatever the delta', () => {
    expect(decideAutoExpand({ armed: false, openIds: ['a', 'b'], expandedId: null })).toBe(null)
    expect(decideAutoExpand({ armed: false, openIds: ['a', 'b', 'c'], expandedId: 'gone' })).toBe(null)
  })
  it('no open items ⇒ nothing', () => {
    expect(decideAutoExpand({ armed: true, openIds: [], expandedId: null })).toBe(null)
  })
  it('tolerates a missing/non-array openIds', () => {
    expect(decideAutoExpand({ armed: true, openIds: undefined, expandedId: null })).toBe(null)
  })
})

describe('wiring — the four surfaces and the store actually call this module', () => {
  // A helper nobody calls tests nothing (learnings: a guard must run the real
  // code). These source assertions fail until each surface is wired.
  const consumers = [
    ['views/MobileAdmin.vue', /from\s+['"](?:\.\.|@)\/utils\/loadingState['"]/],
    ['components/SchedulesPanel.vue', /from\s+['"](?:\.\.|@)\/utils\/loadingState['"]/],
    ['components/InfoPanel.vue', /from\s+['"](?:\.\.|@)\/utils\/loadingState['"]/],
    ['stores/operatorQueue.js', /from\s+['"](?:\.\.|@)\/utils\/loadingState['"]/],
  ]
  for (const [file, re] of consumers) {
    it(`${file} imports from utils/loadingState`, () => {
      expect(read(file)).toMatch(re)
    })
  }

  it('Operations.vue no longer auto-expands from a bare length watcher (the rule lives in the store)', () => {
    const src = read('views/Operations.vue')
    expect(src).not.toMatch(/toggleExpand\(\s*operatorQueueStore\.openItems\[0\]\.id\s*\)/)
    expect(src).toMatch(/maybeAutoExpand/)
  })
})
