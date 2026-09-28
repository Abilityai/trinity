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
  normalizeInboxTab, newLabel, totalLabel, ALL_LIMIT,
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

describe('All', () => {
  it('recency is the later of last_message_at and latest.at', () => {
    const threads = [
      thread('msg', { last_message_at: iso(2 * H) }),
      thread('dlv', { last_message_at: iso(5 * H), unread: 1 }),
    ]
    const previews = { 'thread:dlv': { latest: { kind: 'deliverable', id: 'r', at: iso(H) } } }
    const { items } = allItems(threads, [], previews, NOW)
    expect(items.map((r) => r.id)).toEqual(['dlv', 'msg'])
  })

  it('30-day chat window; ended asks for 7 days; pending asks always', () => {
    const { items, total } = allItems(
      [thread('fresh', { last_message_at: iso(29 * D) }), thread('stale', { last_message_at: iso(31 * D) }),
        thread('never', { last_message_at: null })],
      [ask('p-old', { created_at: iso(40 * D) }),
        ask('e-in', { status: 'expired', ended_at: iso(6 * D), created_at: iso(20 * D) }),
        ask('e-out', { status: 'answered', ended_at: iso(8 * D) })],
      {}, NOW,
    )
    expect(items.map((r) => r.key).sort()).toEqual(['ask:e-in', 'ask:p-old', 'thread:fresh'])
    expect(total).toBe(3)
  })

  it('rooms are not listed (PR C)', () => {
    const { items } = allItems([{ id: 'r', is_room: true, last_message_at: iso(H) }], [], {}, NOW)
    expect(items).toEqual([])
  })

  it('bounded to 50 with the total stated', () => {
    const many = Array.from({ length: 70 }, (_, i) => thread(`t${i}`, { last_message_at: iso(i * H) }))
    const { items, total } = allItems(many, [], {}, NOW)
    expect(items).toHaveLength(ALL_LIMIT)
    expect(total).toBe(70)
    expect(items[0].id).toBe('t0')
    expect(totalLabel(total, items.length)).toBe('70 · latest 50 shown')
    expect(totalLabel(12, 12)).toBe('12')
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
  isInboxPath, inboxBranchVisible, inboxSelectedAgent, holdSelected,
  paneWindow, openInChatTarget, agentLabels, PANE_TAIL,
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

describe('holdSelected — the opened row stays in place (principle 5)', () => {
  const t1 = { key: 'thread:t1', type: 'thread', id: 't1', n: 3 }
  const t2 = { key: 'thread:t2', type: 'thread', id: 't2', n: 1 }
  it('re-inserts a read chat at its old index, drawn read', () => {
    const out = holdSelected([t2], { item: t1, index: 0 }, 'thread:t1', { thread: () => ({ unread: 0 }) })
    expect(out.map((i) => i.key)).toEqual(['thread:t1', 'thread:t2'])
    expect(out[0]).toMatchObject({ n: 0, readInPlace: true })
  })
  it('draws an ended ask as it is now', () => {
    const a = { key: 'ask:a1', type: 'ask', id: 'a1', status: 'pending' }
    const out = holdSelected([], { item: a, index: 3 }, 'ask:a1', { ask: () => ({ id: 'a1', status: 'expired' }) })
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ status: 'expired', endedInPlace: true })
  })
  it('does nothing once the selection has moved, or while the row is still a member', () => {
    expect(holdSelected([t2], { item: t1, index: 0 }, 'thread:t2')).toEqual([t2])
    expect(holdSelected([t1, t2], { item: t1, index: 0 }, 'thread:t1')).toEqual([t1, t2])
    expect(holdSelected([t2], null, 'thread:t1')).toEqual([t2])
  })

  it('a still-listed selected chat keeps the snapshot it was opened with (All, after the read)', () => {
    const opened = { key: 'thread:t1', type: 'thread', id: 't1', n: 2, first_unread_message_id: 'm3',
      latest: { kind: 'message', id: 'm4', at: 'x', excerpt: 'hi' } }
    const rebuilt = { ...opened, n: 0, first_unread_message_id: null, latest: null }
    const out = holdSelected([rebuilt], { item: opened, index: 0 }, 'thread:t1')
    expect(out[0].first_unread_message_id).toBe('m3')
    expect(out[0].latest).toEqual(opened.latest)
    expect(out[0].n).toBe(0)
    // Another selection releases the hold: the live item is drawn as it is.
    expect(holdSelected([rebuilt], { item: opened, index: 0 }, 'thread:t2')).toEqual([rebuilt])
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
  it('a read chat shows its tail', () => {
    expect(paneWindow(msgs, null).shown).toHaveLength(Math.min(PANE_TAIL, msgs.length))
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
