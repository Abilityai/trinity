/**
 * #2792 — a room inlines only ITS OWN live work.
 *
 * The bug: `PortalRoom.vue` picked live cards from the Work feed by agent name,
 * so while a participant was marked working, that agent's schedule run, loop
 * turn, 1:1 thread, other room or MCP task all rendered as cards in this room.
 * The feed is deliberately agent-scoped in a room (the rail's Work tab shows
 * everything the participants are doing), so the join has to happen here — on
 * the chat id the room turn now stamps on its execution row.
 *
 * Two halves, the house shape (`portalWork.spec.js`, `portalVoiceLayoutMotion.spec.js`):
 *
 *   1. the pure rule `liveItemsForRoom` — the AC pin (one room row + one foreign
 *      row on the SAME working agent → exactly one card), the falsy room, the
 *      stale row, the masked agent, and the server `working` intersection;
 *   2. the component's real selection line, sliced out of the SFC and EXECUTED
 *      against a stub feed — a text pin alone would go green on a filter that
 *      still renders the foreign row, which is the bug.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { liveItemsForRoom } from '../../src/components/portal/portalWork.js'

const ROOM = 'room-1'
const AGENT = 'researcher'

function row(over = {}) {
  return { id: 'x', agent_name: AGENT, status: 'running', stale: false, chat_id: ROOM, ...over }
}

// ---------------------------------------------------------------------------
// 1. The rule
// ---------------------------------------------------------------------------

describe('#2792 — liveItemsForRoom', () => {
  it('AC: one row for this room and one foreign row on the SAME working agent → exactly one card', () => {
    const feed = [
      row({ id: 'ours' }),
      row({ id: 'schedule', chat_id: null }),          // a schedule run on the same agent
      row({ id: 'thread', chat_id: 'sess-9' }),        // the agent's 1:1 thread
      row({ id: 'other-room', chat_id: 'room-2' }),    // another room
    ]
    const out = liveItemsForRoom(feed, ROOM, [AGENT])
    expect(out.map((it) => it.id)).toEqual(['ours'])
  })

  it('a live row for this room on a SECOND working participant is also a card', () => {
    const feed = [row({ id: 'a' }), row({ id: 'b', agent_name: 'writer' })]
    expect(liveItemsForRoom(feed, ROOM, [AGENT, 'writer']).map((it) => it.id)).toEqual(['a', 'b'])
  })

  it('no room id → nothing (the fallback line renders, never unrelated cards)', () => {
    expect(liveItemsForRoom([row()], null)).toEqual([])
    expect(liveItemsForRoom([row()], '')).toEqual([])
    expect(liveItemsForRoom([row()], undefined)).toEqual([])
  })

  it('a ghost row is not live even when it names this room', () => {
    expect(liveItemsForRoom([row({ stale: true })], ROOM, [AGENT])).toEqual([])
    expect(liveItemsForRoom([row({ status: 'success' })], ROOM, [AGENT])).toEqual([])
  })

  it('a masked (off-roster) agent draws no card — there is no name for the avatar', () => {
    expect(liveItemsForRoom([row({ agent_name: null })], ROOM, [AGENT])).toEqual([])
  })

  it('the server `working` list is an intersection when given, and no filter when absent', () => {
    // The room polls at 3 s and the feed at 12 s: a row whose agent the server
    // no longer marks working is a card under an already-posted reply.
    expect(liveItemsForRoom([row()], ROOM, [])).toEqual([])
    expect(liveItemsForRoom([row()], ROOM, ['writer'])).toEqual([])
    expect(liveItemsForRoom([row()], ROOM, [AGENT]).length).toBe(1)
    expect(liveItemsForRoom([row()], ROOM).length).toBe(1)
    expect(liveItemsForRoom([row()], ROOM, null).length).toBe(1)
  })

  it('tolerates a non-array feed', () => {
    expect(liveItemsForRoom(null, ROOM, [AGENT])).toEqual([])
    expect(liveItemsForRoom({ live: [] }, ROOM, [AGENT])).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 2. The component's selection line, executed
// ---------------------------------------------------------------------------

const ROOM_SFC = readFileSync(
  fileURLToPath(new URL('../../src/components/portal/PortalRoom.vue', import.meta.url)), 'utf8',
)

/** Slice the real `roomLiveItems` computed out of the SFC and run it against stubs. */
function roomLiveItemsFromSource({ feed, roomId, working }) {
  const start = ROOM_SFC.indexOf('const roomLiveItems = computed(')
  expect(start, 'roomLiveItems computed not found in PortalRoom.vue').toBeGreaterThan(-1)
  const line = ROOM_SFC.slice(start, ROOM_SFC.indexOf('\n', start))
  const factory = new Function(
    'computed', 'liveItemsForRoom', 'workStore', 'props', 'workingAgents',
    `${line}\n return roomLiveItems`,
  )
  // `computed` is identity here: the body is what is under test, not reactivity.
  return factory((fn) => fn(), liveItemsForRoom, { live: feed }, { roomId }, { value: working })
}

describe('#2792 — PortalRoom selects its cards by the room id', () => {
  it('the reported bug: a foreign live row on a working participant is NOT a card in this room', () => {
    const out = roomLiveItemsFromSource({
      feed: [row({ id: 'ours' }), row({ id: 'schedule', chat_id: null })],
      roomId: ROOM,
      working: [AGENT],
    })
    expect(out.map((it) => it.id)).toEqual(['ours'])
  })

  it('no row for this room yet (reload mid-turn, feed lag) → no cards, so the fallback line renders', () => {
    const out = roomLiveItemsFromSource({
      feed: [row({ id: 'schedule', chat_id: null })],
      roomId: ROOM,
      working: [AGENT],
    })
    expect(out).toEqual([])
  })

  it('the fallback line is still there, after the card branch', () => {
    const cards = ROOM_SFC.indexOf('data-testid="portal-room-work"')
    const fallback = ROOM_SFC.indexOf('v-else-if="workingAgents.length"')
    expect(cards).toBeGreaterThan(-1)
    expect(fallback).toBeGreaterThan(cards)
  })
})
