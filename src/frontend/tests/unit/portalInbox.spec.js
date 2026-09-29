/**
 * trinity-enterprise#610 — the Workspace Inbox's pure rules.
 *
 * The Inbox reads data the shell already holds (plan D6), so what can go wrong
 * is the arithmetic: which rows a window shows, their order, and whether the
 * two counts on the pinned row agree with the sidebar. The central claim (AC 1,
 * D13) is "the agent rows and the Inbox never disagree", pinned below as a
 * property over generated threads rather than as a handful of examples.
 */
import { describe, it, expect } from 'vitest'

import {
  inboxLandingTarget, actionItems, unreadItems, allItems, inboxCounts,
  itemKey, parseItemKey, sidebarThreadsOf, inSidebar, defaultInboxTab,
  normalizeInboxTab, newLabel,
} from '@/components/portal/portalInbox'
import {
  totalUnread, unreadByAgent, WORKSPACE_INBOX, WORKSPACE_ROOT, shouldEscapeStage,
  agentRowTitle, unreadBadgeTitle,
} from '@/components/portal/portalUtils'

const NOW = Date.parse('2026-09-28T12:00:00Z')
const iso = (msAgo) => new Date(NOW - msAgo).toISOString()
const H = 3600 * 1000
const D = 24 * H

const thread = (id, over = {}) => ({
  id, agent_name: 'scout', title: `t-${id}`, is_main: false,
  last_message_at: iso(H), unread: 0, ...over,
})
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', title: `ask ${id}`, question: 'q?',
  created_at: iso(H), status: 'pending', ...over,
})

describe('the landing (D9)', () => {
  it('bare /workspace lands on the Inbox', () => {
    expect(inboxLandingTarget({ path: '/workspace', params: {}, query: {} })).toBe(WORKSPACE_INBOX)
    expect(inboxLandingTarget({ path: '/workspace/', params: {}, query: {} })).toBe(WORKSPACE_INBOX)
  })

  it('an unrelated query key does not block the landing', () => {
    expect(inboxLandingTarget({ path: '/workspace', query: { utm: 'x' } })).toBe(WORKSPACE_INBOX)
  })

  it.each([
    ['/workspace/c/s1', { sessionId: 's1' }, {}],
    ['/workspace/r/r1', { roomId: 'r1' }, {}],
    ['/workspace/a/scout', { agentName: 'scout' }, {}],
    ['/workspace/inbox', {}, {}],
    ['/workspace', {}, { agent: 'scout' }],
    ['/workspace', {}, { new: '1' }],
    ['/workspace', {}, { new: '' }],
    ['/workspace', {}, { voice: '1' }],
    ['/workspace', { sessionId: 's1' }, {}],
  ])('an explicit target wins: %s %j %j', (path, params, query) => {
    expect(inboxLandingTarget({ path, params, query })).toBeNull()
  })

  it('WORKSPACE_ROOT is unchanged and the Inbox is a stage the escape rule leaves', () => {
    expect(WORKSPACE_ROOT).toBe('/workspace')
    expect(WORKSPACE_INBOX).toBe('/workspace/inbox')
    expect(shouldEscapeStage(WORKSPACE_INBOX, {})).toBe(true)
  })
})

describe('item keys', () => {
  it('round-trips, ids with colons included', () => {
    expect(parseItemKey(itemKey('ask', 'a1'))).toEqual({ type: 'ask', id: 'a1' })
    expect(parseItemKey(itemKey('thread', 'x:y'))).toEqual({ type: 'thread', id: 'x:y' })
  })
  it.each([null, '', 'ask:', ':a', 'room:r1', 'nokey', 42])('refuses %j', (k) => {
    expect(parseItemKey(k)).toBeNull()
  })
  it('tabs', () => {
    expect(normalizeInboxTab('unread')).toBe('unread')
    expect(normalizeInboxTab('bogus')).toBeNull()
    expect(defaultInboxTab({ needs: 1, came: 3 })).toBe('action')
    expect(defaultInboxTab({ needs: 0, came: 3 })).toBe('unread')
    expect(defaultInboxTab({ needs: 0, came: 0 })).toBe('all')
  })
})

describe('Action (D8)', () => {
  it('pending only, newest first', () => {
    const rows = actionItems([
      ask('old', { created_at: iso(5 * H) }),
      ask('done', { status: 'answered', ended_at: iso(0) }),
      ask('new', { created_at: iso(H) }),
      ask('exp', { status: 'expired' }),
    ])
    expect(rows.map((r) => r.key)).toEqual(['ask:new', 'ask:old'])
    expect(rows[0]).toMatchObject({ type: 'ask', id: 'new', agent_name: 'scout', status: 'pending' })
    expect(rows[0].ask.id).toBe('new')
  })
  it('tolerates junk', () => {
    expect(actionItems(null)).toEqual([])
    expect(actionItems([null, {}])).toEqual([])
  })
})

describe('Unread (D1)', () => {
  it('chats with arrivals; archived INCLUDED; rooms excluded; latest.at orders', () => {
    const threads = [
      thread('read', { unread: 0 }),
      thread('arch', { unread: 1, archived_at: iso(D), last_message_at: iso(3 * H) }),
      thread('late', { unread: 2, last_message_at: iso(5 * H) }),
      { id: 'room1', is_room: true, unread: 4, agent_names: ['a', 'b'], last_message_at: iso(0) },
    ]
    const previews = {
      'thread:late': { latest: { kind: 'deliverable', id: 'r9', at: iso(10 * 60 * 1000), excerpt: 'x', outcome: null },
        first_unread_message_id: 'm1' },
    }
    const rows = unreadItems(threads, previews)
    expect(rows.map((r) => r.id)).toEqual(['late', 'arch'])
    expect(rows[0]).toMatchObject({ n: 2, first_unread_message_id: 'm1', archived: false })
    expect(rows[0].latest.kind).toBe('deliverable')
    expect(rows[1]).toMatchObject({ n: 1, archived: true, latest: null })
  })
  it('falls back to last_message_at when there is no preview', () => {
    const rows = unreadItems([
      thread('a', { unread: 1, last_message_at: iso(2 * H) }),
      thread('b', { unread: 1, last_message_at: iso(H) }),
    ], {})
    expect(rows.map((r) => r.id)).toEqual(['b', 'a'])
  })
})

describe('All — literally all (§3g D-4a)', () => {
  it('recency is the later of last_message_at and latest.at', () => {
    const threads = [
      thread('msg', { last_message_at: iso(2 * H) }),
      thread('dlv', { last_message_at: iso(5 * H), unread: 1 }),
    ]
    const previews = { 'thread:dlv': { latest: { kind: 'deliverable', id: 'r', at: iso(H) } } }
    expect(allItems(threads, [], previews, NOW).map((r) => r.id)).toEqual(['dlv', 'msg'])
  })

  it('every chat of any age; ended asks for 7 days; pending asks always', () => {
    const items = allItems(
      [thread('fresh', { last_message_at: iso(29 * D) }), thread('old', { last_message_at: iso(400 * D) })],
      [ask('p-old', { created_at: iso(40 * D) }),
        ask('e-in', { status: 'expired', ended_at: iso(6 * D), created_at: iso(20 * D) }),
        ask('e-out', { status: 'answered', ended_at: iso(8 * D) })],
      {}, NOW,
    )
    expect(items.map((r) => r.key).sort()).toEqual(['ask:e-in', 'ask:p-old', 'thread:fresh', 'thread:old'])
  })

  it('an empty non-Main chat is ordered by when it was created', () => {
    const items = allItems(
      [thread('a', { last_message_at: iso(3 * H) }), thread('empty', { last_message_at: null, created_at: iso(H) })],
      [], {}, NOW,
    )
    expect(items.map((r) => r.id)).toEqual(['empty', 'a'])
  })

  it('rooms are not listed (PR C)', () => {
    expect(allItems([{ id: 'r', is_room: true, last_message_at: iso(H) }], [], {}, NOW)).toEqual([])
  })

  it('is not bounded: the list pages instead (pageWindow)', () => {
    const many = Array.from({ length: 70 }, (_, i) => thread(`t${i}`, { last_message_at: iso(i * H) }))
    const items = allItems(many, [], {}, NOW)
    expect(items).toHaveLength(70)
    expect(items[0].id).toBe('t0')
  })

  it('property: Unread ⊆ All, over generated fleets', () => {
    let seed = 11
    const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648 }
    for (let run = 0; run < 300; run++) {
      const threads = Array.from({ length: 1 + Math.floor(rnd() * 12) }, (_, i) => thread(`t${i}`, {
        unread: rnd() < 0.5 ? Math.floor(rnd() * 9) : 0,
        last_message_at: rnd() < 0.1 ? null : iso(Math.floor(rnd() * 400) * D),
        created_at: iso(Math.floor(rnd() * 500) * D),
        archived_at: rnd() < 0.2 ? iso(D) : null,
      })).filter(inSidebar)
      const all = new Set(allItems(threads, [], {}, NOW).map((r) => r.key))
      for (const u of unreadItems(threads, {})) expect(all.has(u.key)).toBe(true)
    }
  })
})

describe('pageWindow — 50 rows, then Show more (§3g SM / C4)', () => {
  const rows = (n) => Array.from({ length: n }, (_, i) => ({ key: `thread:t${i}` }))
  it('shows the first `limit` rows and says the total', () => {
    const w = pageWindow(rows(120), PAGE_SIZE)
    expect(PAGE_SIZE).toBe(50)
    expect(w.shown).toHaveLength(50)
    expect(w.total).toBe(120)
    expect(w.hidden).toBe(70)
  })
  it('expands to include a selected row past the window', () => {
    const w = pageWindow(rows(120), 50, 'thread:t71')
    expect(w.shown).toHaveLength(72)
    expect(w.shown.at(-1).key).toBe('thread:t71')
    expect(w.hidden).toBe(48)
  })
  it('a selection inside the window, or not in the list, changes nothing', () => {
    expect(pageWindow(rows(120), 50, 'thread:t3').shown).toHaveLength(50)
    expect(pageWindow(rows(120), 50, 'thread:gone').shown).toHaveLength(50)
  })
  it('everything when it fits; junk tolerated', () => {
    expect(pageWindow(rows(12), 50)).toMatchObject({ total: 12, hidden: 0 })
    expect(pageWindow(null, 50)).toMatchObject({ shown: [], total: 0, hidden: 0 })
  })
})

describe('inboxLayout — split or stacked by the CONTAINER, not the viewport (§3g A4)', () => {
  const L = (width, o = {}) => inboxLayout({ width, ...o })
  it.each([
    // [width, allowance, prev, mode, wide]
    [1280, 0, null, 'split', true],
    [1100, 0, null, 'split', true],
    [1099, 0, null, 'split', false],
    [720, 0, null, 'split', false],
    [719, 0, null, 'stacked', false],
    [719, 0, 'split', 'split', false],     // hysteresis: stays split down to 704
    [704, 0, 'split', 'split', false],
    [703, 0, 'split', 'stacked', false],
    [735, 0, 'stacked', 'split', false],   // …and a stacked one splits again at 720
    [719, 0, 'stacked', 'stacked', false],
    [608, 0, null, 'stacked', false],      // 1280 with the rail open
    [1100, 384, null, 'stacked', false],   // the rail about to arrive is counted
    [1200, 48, null, 'split', true],
  ])('%ipx (allowance %i, was %s) → %s', (width, allowance, prev, mode, wide) => {
    expect(L(width, { allowance, prev })).toEqual({ mode, wide })
  })
  it('an unmeasured container falls back to the viewport', () => {
    expect(L(0, { phoneViewport: true })).toEqual({ mode: 'stacked', wide: false })
    expect(L(0, { phoneViewport: false })).toEqual({ mode: 'split', wide: false })
    expect(L(-1, {})).toEqual({ mode: 'split', wide: false })
  })
  it('a phone viewport is stacked whatever the container says', () => {
    expect(L(900, { phoneViewport: true })).toEqual({ mode: 'stacked', wide: false })
  })
})

describe('wording: arrivals are "new", not "replies" (D3)', () => {
  it('the row title and the badge title say new', () => {
    expect(agentRowTitle({ name: 'scribe', unread: 2 })).toBe('scribe — 2 new')
    expect(unreadBadgeTitle(3)).toBe("3 new you haven't read")
    expect(unreadBadgeTitle(0)).toBe('')
    expect(newLabel(1)).toBe('1 new')
    expect(newLabel(0)).toBe('')
  })
})

// A seeded PRNG so a failure is reproducible from the printed seed.
function rng(seed) {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6D2B79F5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function genThreads(r) {
  const agents = ['a', 'b', 'c', 'd']
  const n = Math.floor(r() * 25)
  const out = []
  for (let i = 0; i < n; i++) {
    const kind = r()
    if (kind < 0.1) {
      // Rooms report unread 0 until PR C (the backend counts threads only).
      out.push({ id: `room${i}`, is_room: true, unread: 0, agent_names: agents.slice(0, 2), last_message_at: iso(r() * 40 * D) })
      continue
    }
    const isMain = r() < 0.4
    const unread = r() < 0.5 ? Math.floor(r() * 6) : 0
    // D4's touch: a Main that received an arrival has `last_message_at` set.
    // A report-only Main is no exception — the stamp touches it.
    const neverUsed = isMain && unread === 0 && r() < 0.5
    out.push({
      id: `t${i}`, agent_name: agents[Math.floor(r() * agents.length)], is_main: isMain,
      last_message_at: neverUsed ? null : iso(r() * 40 * D),
      archived_at: r() < 0.2 ? iso(r() * 10 * D) : null,
      unread,
    })
  }
  return out
}

describe('property: the Inbox and the sidebar never disagree (AC 1, D13)', () => {
  it('holds over 500 generated fleets', () => {
    for (let seed = 1; seed <= 500; seed++) {
      const r = rng(seed)
      const raw = genThreads(r)
      const threads = sidebarThreadsOf(raw)            // what the shell passes both consumers
      const asks = Array.from({ length: Math.floor(r() * 5) }, (_, i) => ask(`k${i}`))
      const { came, needs } = inboxCounts(threads, asks)
      const byAgent = Object.values(unreadByAgent(threads)).reduce((a, b) => a + b, 0)
      const rowsSum = unreadItems(threads, {}).reduce((a, it) => a + it.n, 0)

      const ctx = `seed=${seed}`
      expect(came, ctx).toBe(totalUnread(threads))
      expect(came, ctx).toBe(byAgent)
      expect(rowsSum, ctx).toBe(came)
      expect(needs, ctx).toBe(asks.length)
      // Every thread with an arrival survives the sidebar's projection, so the
      // count the sidebar shows is the whole count.
      for (const t of raw) {
        if ((Number(t.unread) || 0) > 0) expect(inSidebar(t), `${ctx} ${t.id}`).toBe(true)
      }
      expect(totalUnread(threads), ctx).toBe(totalUnread(raw))
    }
  })
})

// ---- The shell's and the pane's seams (F3) ------------------------------------
import {
  isInboxPath, inboxBranchVisible, inboxSelectedAgent, stableRows, emptyVisit, isGhost, resolveItem,
  paneWindow, openInChatTarget, agentLabels, PANE_TAIL, listHeadLabel, inboxRowLabel, pageWindow, PAGE_SIZE,
  inboxLayout, paneRuns, inboxCanvasCount,
} from '@/components/portal/portalInbox'

describe('shell seams', () => {
  it('isInboxPath matches the Inbox route only', () => {
    expect(isInboxPath('/workspace/inbox')).toBe(true)
    expect(isInboxPath('/workspace/inbox/')).toBe(true)
    expect(isInboxPath('/workspace')).toBe(false)
    expect(isInboxPath('/workspace/c/inbox')).toBe(false)
  })

  it('the Inbox branch renders only on a READY stage (ent#253)', () => {
    expect(inboxBranchVisible({ isInboxRoute: true, stageState: 'ready' })).toBe(true)
    for (const s of ['loading', 'failed', 'empty']) {
      expect(inboxBranchVisible({ isInboxRoute: true, stageState: s })).toBe(false)
    }
    expect(inboxBranchVisible({ isInboxRoute: false, stageState: 'ready' })).toBe(false)
  })

  it('the selected agent comes from the item, and only from the roster', () => {
    const agents = [{ name: 'scout' }, { name: 'sage' }]
    const threads = [{ id: 't1', agent_name: 'scout' }]
    const asks = [{ id: 'a1', agent_name: 'sage' }, { id: 'a2', agent_name: 'gone' }]
    expect(inboxSelectedAgent({ item: 'thread:t1', threads, asks, agents })).toEqual({ name: 'scout' })
    expect(inboxSelectedAgent({ item: 'ask:a1', threads, asks, agents })).toEqual({ name: 'sage' })
    // Never the agents[0] fallback, and never an off-roster name.
    expect(inboxSelectedAgent({ item: null, threads, asks, agents })).toBeNull()
    expect(inboxSelectedAgent({ item: 'ask:a2', threads, asks, agents })).toBeNull()
    expect(inboxSelectedAgent({ item: 'thread:nope', threads, asks, agents })).toBeNull()
  })

  it('agentLabels uses the trimmed display label, else the slug', () => {
    expect(agentLabels([{ name: 'a', display_label: ' Alpha ' }, { name: 'b', display_label: '  ' }, { name: 'c' }]))
      .toEqual({ a: 'Alpha', b: 'b', c: 'c' })
  })
})

describe('stableRows — rows keep their place for one tab visit (§3g S1: A1, A7, A12)', () => {
  const T = (id, over = {}) => ({ key: `thread:${id}`, type: 'thread', id, n: 1, ...over })
  const A = (id, over = {}) => ({ key: `ask:${id}`, type: 'ask', id, status: 'pending', ...over })
  const keys = (r) => r.rows.map((i) => i.key)
  const liveAll = { thread: (id) => ({ id, unread: 0 }), ask: (id) => ({ id, status: 'answered' }) }

  it('the first render is the fresh order', () => {
    const r = stableRows([T('a'), T('b'), T('c')], emptyVisit(), liveAll)
    expect(keys(r)).toEqual(['thread:a', 'thread:b', 'thread:c'])
    expect(r.rows.every((i) => !isGhost(i))).toBe(true)
  })

  it('a row that leaves stays where it was, as a ghost drawn read (A12)', () => {
    const v1 = stableRows([T('a'), T('b'), T('c')], emptyVisit(), liveAll).visit
    const r = stableRows([T('a'), T('c')], v1, liveAll)
    expect(keys(r)).toEqual(['thread:a', 'thread:b', 'thread:c'])
    expect(r.rows[1]).toMatchObject({ readInPlace: true, n: 0 })
    expect(isGhost(r.rows[1])).toBe(true)
  })

  it('an ask that leaves Action stays in place drawn as it ended', () => {
    const v1 = stableRows([A('x'), A('y')], emptyVisit(), liveAll).visit
    const r = stableRows([A('y')], v1, { ask: (id) => ({ id, status: 'expired' }) })
    expect(keys(r)).toEqual(['ask:x', 'ask:y'])
    expect(r.rows[0]).toMatchObject({ status: 'expired', endedInPlace: true })
    // Gone from the asks list entirely: still drawn, ended.
    const r2 = stableRows([A('y')], v1, { ask: () => null })
    expect(r2.rows[0]).toMatchObject({ endedInPlace: true })
  })

  it('a re-sort in the fresh list does not move a kept row (A7: an answered ask on All)', () => {
    const v1 = stableRows([A('a1'), T('t1'), A('a2')], emptyVisit(), liveAll).visit
    // a2 was answered: its ended_at makes it the newest, so All re-sorts it first.
    const r = stableRows([A('a2', { status: 'answered' }), A('a1'), T('t1')], v1, liveAll)
    expect(keys(r)).toEqual(['ask:a1', 'thread:t1', 'ask:a2'])
    expect(r.rows[2].status).toBe('answered') // drawn as it is now
  })

  it('a newer arrival on a kept row does not re-sort it; a new key goes in before its nearest fresh neighbour', () => {
    const v1 = stableRows([T('a'), T('b'), T('c')], emptyVisit(), liveAll).visit
    const r = stableRows([T('c', { n: 4 }), T('new'), T('a'), T('b')], v1, liveAll)
    // `new` is followed by `a` in fresh order → it goes in before `a`.
    expect(keys(r)).toEqual(['thread:new', 'thread:a', 'thread:b', 'thread:c'])
    expect(r.rows[3].n).toBe(4)
    const tail = stableRows([T('a'), T('b'), T('c'), T('z')], v1, liveAll)
    expect(keys(tail)).toEqual(['thread:a', 'thread:b', 'thread:c', 'thread:z'])
  })

  it('a ghost with a new arrival lights up in place (T3)', () => {
    const v1 = stableRows([T('a'), T('b')], emptyVisit(), liveAll).visit
    const v2 = stableRows([T('b')], v1, liveAll).visit
    const r = stableRows([T('b'), T('a', { n: 2 })], v2, liveAll)
    expect(keys(r)).toEqual(['thread:a', 'thread:b'])
    expect(isGhost(r.rows[0])).toBe(false)
    expect(r.rows[0].n).toBe(2)
  })

  it('a deleted thread is dropped, not ghosted', () => {
    const v1 = stableRows([T('a'), T('b')], emptyVisit(), liveAll).visit
    const r = stableRows([T('b')], v1, { thread: () => null })
    expect(keys(r)).toEqual(['thread:b'])
  })

  it('a kept chat whose refresh carries no preview keeps what was new (All, after the read)', () => {
    const opened = T('t1', { n: 2, first_unread_message_id: 'm3', latest: { kind: 'message', id: 'm4', at: 'x', excerpt: 'hi' } })
    const v1 = stableRows([opened], emptyVisit(), liveAll).visit
    const r = stableRows([T('t1', { n: 0, first_unread_message_id: null, latest: null })], v1, liveAll)
    expect(r.rows[0]).toMatchObject({ n: 0, first_unread_message_id: 'm3', latest: opened.latest })
    // A fresh preview wins over the kept one.
    const r2 = stableRows([T('t1', { n: 1, first_unread_message_id: 'm9', latest: null })], v1, liveAll)
    expect(r2.rows[0].first_unread_message_id).toBe('m9')
  })

  it('a new visit forgets the ghosts', () => {
    const v1 = stableRows([T('a'), T('b')], emptyVisit(), liveAll).visit
    expect(keys(stableRows([T('b')], emptyVisit(), liveAll))).toEqual(['thread:b'])
    expect(keys(stableRows([T('b')], v1, liveAll))).toEqual(['thread:a', 'thread:b'])
  })

  it('is idempotent: re-applying the same fresh list changes nothing', () => {
    const v1 = stableRows([T('a'), T('b'), A('c')], emptyVisit(), liveAll).visit
    const once = stableRows([A('c'), T('b')], v1, liveAll)
    const twice = stableRows([A('c'), T('b')], once.visit, liveAll)
    expect(keys(twice)).toEqual(keys(once))
    expect(twice.visit.order).toEqual(once.visit.order)
  })

  it('tolerates junk', () => {
    expect(stableRows(null, null).rows).toEqual([])
    expect(stableRows([T('a')], undefined).rows.map((i) => i.key)).toEqual(['thread:a'])
  })

  it('property: every fresh key is shown exactly once, and kept rows keep their relative order', () => {
    let seed = 7
    const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648 }
    for (let run = 0; run < 300; run++) {
      const pool = Array.from({ length: 12 }, (_, i) => T(`k${i}`))
      const pick = () => pool.filter(() => rnd() < 0.6).sort(() => rnd() - 0.5)
      let visit = emptyVisit()
      let prev = []
      for (let step = 0; step < 5; step++) {
        const fresh = pick()
        const r = stableRows(fresh, visit, liveAll)
        const shown = keys(r)
        expect(new Set(shown).size).toBe(shown.length)
        for (const it of fresh) expect(shown).toContain(it.key)
        const kept = prev.filter((k) => shown.includes(k))
        expect(shown.filter((k) => kept.includes(k))).toEqual(kept)
        visit = r.visit
        prev = shown
      }
    }
  })
})

describe('resolveItem — the one fallback for a selection outside the list', () => {
  it('builds an old chat and an ask from the key, whatever the tab', () => {
    const t = thread('old', { last_message_at: iso(40 * D), unread: 0 })
    expect(resolveItem('thread:old', { threads: [t] })).toMatchObject({ key: 'thread:old', type: 'thread', id: 'old' })
    const a = ask('a9', { status: 'answered', ended_at: iso(60 * D) })
    expect(resolveItem('ask:a9', { asks: [a] })).toMatchObject({ key: 'ask:a9', type: 'ask', status: 'answered' })
  })
  it('carries the chat preview when there is one', () => {
    const t = thread('t1', { unread: 2 })
    const previews = { 'thread:t1': { latest: { kind: 'message', id: 'm2', at: iso(1), excerpt: 'x' }, first_unread_message_id: 'm1' } }
    expect(resolveItem('thread:t1', { threads: [t], previews }).first_unread_message_id).toBe('m1')
  })
  it('null for an unknown or malformed key', () => {
    expect(resolveItem('thread:gone', { threads: [] })).toBeNull()
    expect(resolveItem('ask:gone', { asks: [] })).toBeNull()
    expect(resolveItem('nonsense', {})).toBeNull()
    expect(resolveItem(null, {})).toBeNull()
  })
})

describe('units and caps (§3g A8 / D-1 / B6b)', () => {
  const live = (n, type, extra = {}) => Array.from({ length: n }, (_, i) => ({ key: `${type}:${i}`, type, n: 0, ...extra }))
  it('the list head names its unit on every tab', () => {
    expect(listHeadLabel('action', live(21, 'ask'))).toBe('21 asks')
    expect(listHeadLabel('action', live(1, 'ask'))).toBe('1 ask')
    expect(listHeadLabel('unread', [...live(14, 'thread', { n: 5 }), { key: 'thread:x', type: 'thread', n: 0 }])).toBe('15 chats · 70 new')
    expect(listHeadLabel('unread', live(1, 'thread', { n: 1 }))).toBe('1 chat · 1 new')
    expect(listHeadLabel('all', [...live(3, 'thread'), ...live(2, 'ask')])).toBe('3 chats · 2 asks')
    expect(listHeadLabel('all', live(1, 'thread'))).toBe('1 chat')
    expect(listHeadLabel('all', live(1, 'ask'))).toBe('1 ask')
  })
  it('with no live rows it says so rather than "0"', () => {
    for (const t of ['action', 'unread', 'all']) expect(listHeadLabel(t, [])).toBe('All caught up')
  })
  it('the "new" sum caps like every other count', () => {
    expect(listHeadLabel('unread', live(2, 'thread', { n: 80 }))).toBe('2 chats · 99+ new')
  })
  it('the pinned row is named once, with both counts in words', () => {
    expect(inboxRowLabel({ needs: 0, came: 0 })).toBe('Inbox')
    expect(inboxRowLabel({ needs: 2, came: 0 })).toBe('Inbox, 2 asks are waiting on your answer')
    expect(inboxRowLabel({ needs: 1, came: 157 })).toBe("Inbox, 1 ask is waiting on your answer, 157 new you haven't read")
  })
})

describe('the pane window (D11)', () => {
  const msgs = [
    { id: 'm1', role: 'user' }, { id: 'm2', role: 'assistant' },
    { id: 'm3', role: 'user' }, { id: 'm4', role: 'assistant' }, { id: 'm5', role: 'assistant' },
  ]
  it('renders from the first unread message, inclusive', () => {
    expect(paneWindow(msgs, 'm4', 2).shown.map((m) => m.id)).toEqual(['m4', 'm5'])
    expect(paneWindow(msgs, 'm4', 2).earlier).toBe(0)
  })
  it('an id outside the window is said, never silently under-claimed', () => {
    const w = paneWindow(msgs, 'm0-gone', 9)
    expect(w.shown.every((m) => m.role === 'assistant')).toBe(true)
    expect(w.earlier).toBe(9 - w.shown.length)
    expect(w.earlier).toBeGreaterThan(0)
  })
  it('caps a long run of arrivals at the tail, and says how many are earlier (§3g S2 / A2)', () => {
    const twelve = Array.from({ length: 12 }, (_, i) => ({ id: `m${i + 1}`, role: 'assistant' }))
    const w = paneWindow(twelve, 'm1', 12)
    expect(w.shown.map((m) => m.id)).toEqual(['m8', 'm9', 'm10', 'm11', 'm12'])
    expect(w.earlier).toBe(7)
  })
  it('the earlier count is arrivals only: a hidden user message is not one', () => {
    const mixed = [
      { id: 'm1', role: 'assistant' }, { id: 'm2', role: 'user' }, { id: 'm3', role: 'assistant' },
      { id: 'm4', role: 'assistant' }, { id: 'm5', role: 'assistant' }, { id: 'm6', role: 'assistant' },
      { id: 'm7', role: 'assistant' },
    ]
    const w = paneWindow(mixed, 'm1', 6)
    expect(w.shown.map((m) => m.id)).toEqual(['m3', 'm4', 'm5', 'm6', 'm7'])
    expect(w.earlier).toBe(1)
  })
  it('a read chat shows its tail', () => {
    expect(paneWindow(msgs, null).shown).toHaveLength(Math.min(PANE_TAIL, msgs.length))
  })
})

describe('paneRuns — one header per run of one sender (§3g A13)', () => {
  const at = (min) => new Date(NOW + min * 60_000).toISOString()
  const m = (id, role, min) => ({ id, role, created_at: at(min), content: id })
  it('consecutive messages from one sender are one run', () => {
    const runs = paneRuns([m('a1', 'assistant', 0), m('a2', 'assistant', 1), m('a3', 'assistant', 2)])
    expect(runs).toHaveLength(1)
    expect(runs[0]).toMatchObject({ role: 'assistant', at: at(0) })
    expect(runs[0].messages.map((x) => x.id)).toEqual(['a1', 'a2', 'a3'])
  })
  it('a change of sender, a system line, or a gap over 10 minutes starts a new run', () => {
    const runs = paneRuns([
      m('u1', 'user', 0), m('a1', 'assistant', 1), m('s1', 'system', 2), m('a2', 'assistant', 3),
      m('a3', 'assistant', 13), m('a4', 'assistant', 23.5),
    ])
    expect(runs.map((r) => r.messages.map((x) => x.id))).toEqual([['u1'], ['a1'], ['s1'], ['a2', 'a3'], ['a4']])
    expect(runs.map((r) => r.role)).toEqual(['user', 'assistant', 'system', 'assistant', 'assistant'])
  })
  it('two system lines are two runs; junk tolerated', () => {
    expect(paneRuns([m('s1', 'system', 0), m('s2', 'system', 0)])).toHaveLength(2)
    expect(paneRuns(null)).toEqual([])
  })
})

describe('inboxCanvasCount — Open canvas only when there is one to open (§3g C10)', () => {
  const tabs = [{ id: 'work' }, { id: 'canvas' }]
  it('the agent\'s canvases, when the Canvas tab is one this session has', () => {
    expect(inboxCanvasCount({ tabs, canvases: { scout: [{ id: 'c1' }, { id: 'c2' }] }, agent: 'scout' })).toBe(2)
  })
  it('0 without the tab, the agent, or any canvas', () => {
    expect(inboxCanvasCount({ tabs: [{ id: 'work' }], canvases: { scout: [{ id: 'c1' }] }, agent: 'scout' })).toBe(0)
    expect(inboxCanvasCount({ tabs, canvases: { scout: [{ id: 'c1' }] }, agent: null })).toBe(0)
    expect(inboxCanvasCount({ tabs, canvases: {}, agent: 'scout' })).toBe(0)
    expect(inboxCanvasCount({})).toBe(0)
  })
})

describe('Open in chat (D11)', () => {
  it('anchors at the first unread message, else the latest deliverable, else the bottom', () => {
    expect(openInChatTarget({ type: 'thread', id: 's1', first_unread_message_id: 'm9' }))
      .toBe('/workspace/c/s1?anchor=m%3Am9')
    expect(openInChatTarget({ type: 'thread', id: 's1', latest: { kind: 'deliverable', id: 'r7' } }))
      .toBe('/workspace/c/s1?anchor=d%3Ar7')
    expect(openInChatTarget({ type: 'thread', id: 's1', latest: { kind: 'message', id: 'm1' } })).toBe('/workspace/c/s1')
    expect(openInChatTarget({ type: 'ask', id: 'a1' })).toBeNull()
  })
})

import { inboxRailAllowance } from '@/components/portal/portalInbox'

describe('inboxRailAllowance — the rail width the Inbox has not lost YET (§3g A4, round 3)', () => {
  // The rail column ENTERS from width 0 (a 300ms width transition). Treating
  // "the column exists" as "the rail's width is gone" let a 1440 Inbox measure
  // ~1147 mid-animation, choose the 384 list, then fall to 1099 and choose 320
  // again: a 320 → 384 → 320 flip on every load (CLS 0.053, round-3 benchmark).
  // The allowance is the target minus what the column has actually grown to,
  // so the width the Inbox decides on (container − allowance) is constant
  // through the whole animation.
  it('no column: the whole target is counted in advance', () => {
    expect(inboxRailAllowance({ target: 384, present: false, measured: 0 })).toBe(384)
    expect(inboxRailAllowance({ target: 48, present: false, measured: 0 })).toBe(48)
  })
  it('a column mid-enter: only what it has not grown into yet', () => {
    expect(inboxRailAllowance({ target: 384, present: true, measured: 0 })).toBe(384)
    expect(inboxRailAllowance({ target: 48, present: true, measured: 12 })).toBe(36)
    expect(inboxRailAllowance({ target: 384, present: true, measured: 384 })).toBe(0)
  })
  it('the width the Inbox decides on is constant through the enter animation', () => {
    const row = 1147 // the Inbox + rail share at a 1440 viewport, sidebar aside
    for (const grown of [0, 7, 22, 40, 48]) {
      const container = row - grown
      expect(container - inboxRailAllowance({ target: 48, present: true, measured: grown })).toBe(row - 48)
    }
  })
  it('never negative (a column wider than its target, mid-drag)', () => {
    expect(inboxRailAllowance({ target: 384, present: true, measured: 400 })).toBe(0)
  })
})
