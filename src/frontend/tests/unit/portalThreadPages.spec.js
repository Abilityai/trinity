/**
 * trinity-enterprise#594 — a long Workspace thread reads past its window.
 *
 * The rules a "Show earlier" page and a same-thread reload follow, as pure
 * functions over the rows the transcript holds (`historyRow`-mapped):
 *
 *   prependEarlier(existing, older) — older rows go first, nothing twice.
 *   mergeWindow(existing, fresh)    — a reload of the NEWEST window keeps the
 *                                     earlier pages above it, or says it could
 *                                     not (no overlap → reset).
 *
 * Both restore turn pairing (#3166) across the page seam, and a voice call cut
 * by a page boundary folds into one block once both halves are on screen.
 * Plus `holdPosition` in the stick-to-bottom composable: the reader stays on
 * the row they were reading while rows are prepended above it.
 */
import { describe, it, expect } from 'vitest'
import { markRaw, nextTick, ref } from 'vue'
import { prependEarlier, mergeWindow } from '@/components/portal/portalThreadPages'
import { groupVoiceBlocks } from '@/components/portal/portalVoiceMode'
import { useStickToBottom } from '@/composables/useStickToBottom'

const row = (id, over = {}) => ({ id, role: 'user', content: id, ...over })
const ids = (rows) => rows.map((r) => r.id)

describe('prependEarlier', () => {
  it('puts the older page first and keeps every newer row', () => {
    const out = prependEarlier([row('c'), row('d')], [row('a'), row('b')])
    expect(ids(out)).toEqual(['a', 'b', 'c', 'd'])
  })

  it('never shows a row twice when the pages overlap', () => {
    const out = prependEarlier([row('b'), row('c')], [row('a'), row('b')])
    expect(ids(out)).toEqual(['a', 'b', 'c'])
  })

  it('keeps a local unsent row (no id) where it is', () => {
    const local = { role: 'user', content: 'sending…', local: true }
    const out = prependEarlier([row('c'), local], [row('a')])
    expect(out.map((r) => r.id || r.content)).toEqual(['a', 'c', 'sending…'])
  })

  it('pairs a reply with a question that arrived on the older page (#3166)', () => {
    // Overlapping turns store Q_A, Q_B, R_A, R_B; the boundary fell between Q_A
    // and Q_B, so the newer page alone could not pair R_A.
    const newer = [
      row('qb', { executionId: 'B' }),
      row('ra', { role: 'assistant', executionId: 'A' }),
      row('rb', { role: 'assistant', executionId: 'B' }),
    ]
    const out = prependEarlier(newer, [row('qa', { executionId: 'A' })])
    expect(ids(out)).toEqual(['qa', 'ra', 'qb', 'rb'])
  })

  it('folds a voice call split across the page boundary into one block', () => {
    const spoken = (id, i) => row(id, { role: i % 2 ? 'assistant' : 'user', source: 'voice', voiceCallId: 'vs_1' })
    const older = [row('t0'), spoken('s0', 0), spoken('s1', 1)]
    const newer = [spoken('s2', 2), row('lbl', { role: 'system', source: 'voice', voiceCallId: 'vs_1', content: 'Voice call · 3 min' }), row('t1')]
    // Alone, the newer page shows half the call.
    expect(groupVoiceBlocks(newer).filter((i) => i.kind === 'voice-call')[0].turns).toHaveLength(1)
    const items = groupVoiceBlocks(prependEarlier(newer, older))
    const blocks = items.filter((i) => i.kind === 'voice-call')
    expect(blocks).toHaveLength(1)
    expect(blocks[0].turns.map((t) => t.id)).toEqual(['s0', 's1', 's2'])
    expect(blocks[0].label).toBe('Voice call · 3 min')
    expect(items.map((i) => i.kind)).toEqual(['message', 'voice-call', 'message'])
  })
})

describe('mergeWindow', () => {
  it('keeps the earlier pages above a reloaded newest window', () => {
    const existing = [row('a'), row('b'), row('c'), row('d')]
    const { rows, reset } = mergeWindow(existing, [row('c'), row('d'), row('e')])
    expect(reset).toBe(false)
    expect(ids(rows)).toEqual(['a', 'b', 'c', 'd', 'e'])
  })

  it('takes the fresh copy of every row it covers (a rating, a server id)', () => {
    const existing = [row('a'), row('b', { myRating: null })]
    const { rows } = mergeWindow(existing, [row('b', { myRating: 'up' })])
    expect(rows[1].myRating).toBe('up')
  })

  it('replaces the local rows the window now covers', () => {
    const local = { role: 'user', content: 'q', local: true }
    const { rows } = mergeWindow([row('a'), row('b'), local], [row('b'), row('q')])
    expect(ids(rows)).toEqual(['a', 'b', 'q'])
  })

  it('reports a reset when the window shares nothing with what is on screen', () => {
    const { rows, reset } = mergeWindow([row('a'), row('b')], [row('x'), row('y')])
    expect(reset).toBe(true)
    expect(ids(rows)).toEqual(['x', 'y'])
  })
})

describe('holdPosition keeps the reader on the row they were reading', () => {
  function withFakeObserver(fn) {
    const instances = []
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb) { this.cb = cb; instances.push(this) }
      observe() {}
      disconnect() {}
      fire() { this.cb([]) }
    }
    try { return fn(instances) } finally { globalThis.ResizeObserver = original }
  }

  it('restores the distance from the bottom after a prepend', async () => {
    const node = markRaw({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0 })
    const s = useStickToBottom(ref(node))
    s.onScroll()                                    // at the top: detached
    await s.holdPosition(async () => { node.scrollHeight = 1800 })
    // 600px of transcript was below the viewport; it still is.
    expect(node.scrollTop).toBe(800)
  })

  it('re-applies the held distance on late growth, then lets go on a real scroll', async () => {
    await withFakeObserver(async (instances) => {
      const node = markRaw({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0, firstElementChild: markRaw({}) })
      const s = useStickToBottom(ref(node))
      await nextTick()
      s.onScroll()
      await s.holdPosition(async () => { node.scrollHeight = 1800 })
      expect(node.scrollTop).toBe(800)

      node.scrollHeight = 2000                      // an older bubble's markdown settles
      instances[0].fire()
      expect(node.scrollTop).toBe(1000)

      node.scrollTop = 300                          // the reader scrolls up
      s.onScroll()
      node.scrollHeight = 2200
      instances[0].fire()
      expect(node.scrollTop).toBe(300)              // the hold is gone
    })
  })

  it('a pin to the bottom ends the hold', async () => {
    await withFakeObserver(async (instances) => {
      const node = markRaw({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0, firstElementChild: markRaw({}) })
      const s = useStickToBottom(ref(node))
      await nextTick()
      s.onScroll()
      await s.holdPosition(async () => { node.scrollHeight = 1800 })
      await s.pinToBottom()
      expect(node.scrollTop).toBe(1800)
      node.scrollHeight = 2000
      instances[0].fire()
      expect(node.scrollTop).toBe(2000)             // following again, not holding
    })
  })
})

describe('holdPosition does not drag the reader for an arrival below them', () => {
  it('an arrival ends the hold', async () => {
    const instances = []
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb) { this.cb = cb; instances.push(this) }
      observe() {}
      disconnect() {}
    }
    try {
      const node = markRaw({ scrollHeight: 1000, clientHeight: 400, scrollTop: 0, firstElementChild: markRaw({}) })
      const s = useStickToBottom(ref(node))
      await nextTick()
      s.onScroll()
      await s.holdPosition(async () => { node.scrollHeight = 1800 })
      expect(node.scrollTop).toBe(800)
      node.scrollHeight = 2000                      // a reply lands at the bottom
      await s.onArrive(1)
      instances[0].cb([])
      expect(node.scrollTop).toBe(800)              // the reader stays put
      expect(s.unread.value).toBe(1)
    } finally { globalThis.ResizeObserver = original }
  })
})
