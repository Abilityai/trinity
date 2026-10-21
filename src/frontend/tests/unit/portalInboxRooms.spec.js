/**
 * trinity-enterprise#959 — rooms in the Workspace Inbox: the pure rules.
 *
 * A room ("a chat with more than one agent") is a row of its own in Unread when
 * it has new messages for the viewer, and in All always — but ONLY a room the
 * viewer is a member of. The rooms list marks that (`member`, set by the server
 * from the participant rows); an admin's list holds every room, so a room
 * without `member === true` must never become a row.
 *
 * The counts come from `/chat-state`, which counts a room only for its members;
 * the Inbox reads the same `sidebarThreads` the sidebar sums, so Σ "N new" ==
 * the pinned row's "came" holds with rooms in it.
 */
import { describe, it, expect } from 'vitest'

import {
  unreadItems, allItems, allFooterNotes, inboxCounts, parseItemKey, resolveItem, stableRows,
  emptyVisit, listHeadLabel, listHeadExact, openInChatTarget, inboxSelectedAgent, isChatItem,
  roomAgentsLabel, roomPaneMessages, paneRuns, markAllConfirm,
} from '@/components/portal/portalInbox'
import { totalUnread } from '@/components/portal/portalUtils'

const NOW = Date.parse('2026-10-21T12:00:00Z')
const iso = (msAgo) => new Date(NOW - msAgo).toISOString()
const H = 3600 * 1000
const D = 24 * H

const thread = (id, over = {}) => ({
  id, agent_name: 'scout', title: `t-${id}`, is_main: false, last_message_at: iso(H), unread: 0, ...over,
})
const room = (id, over = {}) => ({
  id, is_room: true, member: true, name: `Room ${id}`, title: `Room ${id}`,
  agent_names: ['ada', 'bo'], last_message_at: iso(H), unread: 0, ...over,
})

describe('Unread lists a room with new messages (AC 1)', () => {
  it('one row per room, ordered with chats by latest arrival', () => {
    const threads = [
      thread('t-old', { unread: 1, last_message_at: iso(5 * H) }),
      room('r1', { unread: 3, last_message_at: iso(2 * H) }),
      thread('t-new', { unread: 2, last_message_at: iso(H) }),
    ]
    const previews = {
      'room:r1': { latest: { kind: 'message', id: 'm9', at: iso(30 * 60 * 1000), excerpt: 'done' }, first_unread_message_id: 'm7' },
    }
    const rows = unreadItems(threads, previews)
    expect(rows.map((r) => r.key)).toEqual(['room:r1', 'thread:t-new', 'thread:t-old'])
    expect(rows[0]).toMatchObject({
      type: 'room', id: 'r1', n: 3, title: 'Room r1', first_unread_message_id: 'm7',
      agent_names: ['ada', 'bo'], latest: { excerpt: 'done' },
    })
  })

  it('a read room is not in Unread', () => {
    expect(unreadItems([room('r1', { unread: 0 })], {})).toEqual([])
  })

  it('a room the viewer is not a member of never becomes a row, whatever it carries', () => {
    const threads = [room('r-admin', { member: false, unread: 2 }), room('r-unknown', { member: undefined, unread: 1 })]
    expect(unreadItems(threads, {})).toEqual([])
  })
})

describe('All lists my rooms alongside my chats (AC 2)', () => {
  it('member rooms, read or not; non-member rooms never', () => {
    const items = allItems(
      [thread('t1', { last_message_at: iso(3 * H) }), room('r1', { last_message_at: iso(H) }),
        room('r2', { member: false, last_message_at: iso(2 * H) })],
      [], {}, NOW,
    )
    expect(items.map((r) => r.key)).toEqual(['room:r1', 'thread:t1'])
  })

  it('the footer no longer says rooms are missing (AC 5)', () => {
    const notes = allFooterNotes({ hasRooms: true, askCount: 0 })
    expect(notes.join(' ')).not.toMatch(/Rooms/)
    expect(notes).toEqual(['Answered, expired and cancelled asks drop off after 7 days.'])
  })
})

describe('the counts still agree (AC 4)', () => {
  it('Σ "N new" over Unread rows == the pinned row\'s came, with rooms in it', () => {
    const threads = [thread('t1', { unread: 2 }), room('r1', { unread: 3 }), room('r2', { unread: 0 })]
    const rows = unreadItems(threads, {})
    expect(rows.reduce((a, r) => a + r.n, 0)).toBe(5)
    expect(inboxCounts(threads, []).came).toBe(5)
    expect(totalUnread(threads)).toBe(5)
  })

  it('the head line counts a room as a chat', () => {
    const rows = unreadItems([thread('t1', { unread: 2 }), room('r1', { unread: 3 })], {})
    expect(listHeadLabel('unread', rows)).toBe('2 chats · 5 new')
    const many = unreadItems([room('r1', { unread: 150 })], {})
    expect(listHeadExact('unread', many)).toBe('1 chat · 150 new')
    expect(listHeadLabel('all', allItems([room('r1')], [], {}, NOW))).toBe('1 chat')
  })

  it('Mark all read counts the rooms it reads', () => {
    const rows = unreadItems([thread('t1', { unread: 2 }), room('r1', { unread: 3 })], {})
    expect(markAllConfirm({ chats: rows.length, messages: 5 }).confirm).toBe('Mark 2 chats read')
  })
})

describe('a room item, end to end', () => {
  it('is a chat item; its key round-trips; it resolves from the thread list', () => {
    const rows = unreadItems([room('r1', { unread: 1 })], {})
    expect(isChatItem(rows[0])).toBe(true)
    expect(isChatItem({ type: 'thread' })).toBe(true)
    expect(isChatItem({ type: 'ask' })).toBe(false)
    expect(parseItemKey('room:r1')).toEqual({ type: 'room', id: 'r1' })
    expect(resolveItem('room:r1', { threads: [room('r1', { unread: 1 })] })).toMatchObject({ type: 'room', id: 'r1', n: 1 })
    // A room the viewer is not in does not resolve — a deep link cannot draw it.
    expect(resolveItem('room:r2', { threads: [room('r2', { member: false })] })).toBeNull()
    // A room id is never looked up as a thread, nor a thread id as a room.
    expect(resolveItem('thread:r1', { threads: [room('r1')] })).toBeNull()
    expect(resolveItem('room:t1', { threads: [thread('t1')] })).toBeNull()
  })

  it('a room read in place keeps its row, drawn read, for the visit (principle 5)', () => {
    const before = unreadItems([room('r1', { unread: 2 })], {})
    const first = stableRows(before, emptyVisit(), {})
    const read = room('r1', { unread: 0 })
    const after = stableRows(unreadItems([read], {}), first.visit, { room: (id) => (id === 'r1' ? read : null) })
    expect(after.rows).toHaveLength(1)
    expect(after.rows[0]).toMatchObject({ key: 'room:r1', n: 0, readInPlace: true, ghost: true })
    // Gone from the list (deleted, or no longer a member) → dropped, never ghosted.
    expect(stableRows([], first.visit, { room: () => null }).rows).toEqual([])
  })

  it('opens the room itself, and follows no single agent', () => {
    const [it] = unreadItems([room('r 1', { unread: 1 })], {})
    expect(openInChatTarget(it)).toBe('/workspace/r/r%201')
    expect(inboxSelectedAgent({ item: 'room:r 1', threads: [room('r 1')], agents: [{ name: 'ada' }] })).toBeNull()
  })

  it('names its agents by label, bounded', () => {
    const labels = { ada: 'Ada', bo: 'Bo', cy: 'Cy', di: 'Di' }
    expect(roomAgentsLabel({ agent_names: ['ada', 'bo'] }, labels)).toBe('Ada, Bo')
    expect(roomAgentsLabel({ agent_names: ['ada', 'bo', 'cy', 'di'] }, labels)).toBe('Ada, Bo, Cy +1')
    expect(roomAgentsLabel({ agent_names: ['zed'] }, labels)).toBe('zed')
    expect(roomAgentsLabel({ agent_names: [] }, labels)).toBe('Agents')
  })
})

describe("the pane's room transcript", () => {
  it('maps room messages to the pane shape, with their sender', () => {
    const out = roomPaneMessages([
      { id: 'a', seq: 1, kind: 'system', sender_kind: 'system', sender_identity: null, content: 'Room created.', created_at: iso(3 * H) },
      { id: 'b', seq: 2, kind: 'message', sender_kind: 'workspace_user', sender_identity: 'me@x.test', content: 'hi', created_at: iso(2 * H) },
      { id: 'c', seq: 3, kind: 'message', sender_kind: 'agent', sender_identity: 'ada', content: 'yo', created_at: iso(H) },
    ])
    expect(out.map((m) => [m.id, m.role, m.sender])).toEqual([
      ['a', 'system', null], ['b', 'user', null], ['c', 'assistant', 'ada'],
    ])
  })

  it('two agents in a row are two runs, each with its own header', () => {
    const at = iso(H)
    const runs = paneRuns(roomPaneMessages([
      { id: 'c', kind: 'message', sender_kind: 'agent', sender_identity: 'ada', content: '1', created_at: at },
      { id: 'd', kind: 'message', sender_kind: 'agent', sender_identity: 'ada', content: '2', created_at: at },
      { id: 'e', kind: 'message', sender_kind: 'agent', sender_identity: 'bo', content: '3', created_at: at },
    ]))
    expect(runs.map((r) => [r.sender, r.messages.length])).toEqual([['ada', 2], ['bo', 1]])
  })

  it('a chat (no sender) still runs by role, unchanged', () => {
    const at = iso(H)
    const runs = paneRuns([
      { id: '1', role: 'assistant', created_at: at }, { id: '2', role: 'assistant', created_at: at },
    ])
    expect(runs).toHaveLength(1)
  })
})

// ---- property: rooms in the fleet, the counts still agree ---------------------
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

describe('property: Unread == came and ⊆ All, with rooms (AC 4)', () => {
  it('holds over 300 generated fleets', () => {
    for (let seed = 1; seed <= 300; seed++) {
      const r = rng(seed)
      const threads = []
      const n = Math.floor(r() * 20)
      for (let i = 0; i < n; i++) {
        if (r() < 0.4) {
          // The server counts a room only for its members, so a non-member
          // room always arrives with unread 0.
          const member = r() < 0.7
          threads.push(room(`r${i}`, { member, unread: member && r() < 0.6 ? 1 + Math.floor(r() * 5) : 0, last_message_at: iso(r() * 30 * D) }))
        } else {
          threads.push(thread(`t${i}`, { unread: r() < 0.5 ? Math.floor(r() * 5) : 0, last_message_at: iso(r() * 30 * D) }))
        }
      }
      const ctx = `seed=${seed}`
      const rows = unreadItems(threads, {})
      expect(rows.reduce((a, it) => a + it.n, 0), ctx).toBe(inboxCounts(threads, []).came)
      const all = new Set(allItems(threads, [], {}, NOW).map((it) => it.key))
      for (const it of rows) expect(all.has(it.key), `${ctx} ${it.key}`).toBe(true)
      for (const t of threads.filter((x) => x.is_room && !x.member)) {
        expect(all.has(`room:${t.id}`), `${ctx} ${t.id}`).toBe(false)
      }
    }
  })
})
