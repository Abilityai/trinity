// @vitest-environment jsdom
/**
 * trinity-enterprise#959 — rooms in the Inbox, MOUNTED.
 *
 *   Unread     a member room with new messages is a row; opening it renders
 *              the room's new messages in the pane and THEN reads it, through
 *              the shell's `markRead('room', id)` — once
 *   access     a room the viewer is not in (an admin's list holds every room)
 *              puts nothing on the page: no row, no id, no name, no count
 *   All        lists member rooms; the "Rooms aren't in the Inbox yet" note is gone
 *   bulk       Mark all read reads a room as a room
 *   the room   a message arriving in the room the person is looking at is read
 *              (the chat's rule), so the sidebar never badges the open room
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalInbox from '@/components/portal/PortalInbox.vue'
import PortalRoom from '@/components/portal/PortalRoom.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = (q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
})

const NOW = Date.now()
const iso = (minsAgo) => new Date(NOW - minsAgo * 60_000).toISOString()
const thread = (id, over = {}) => ({
  id, session_id: id, agent_name: 'scout', is_main: false, title: `Chat ${id}`,
  last_message_at: iso(30), unread: 0, ...over,
})
const room = (id, over = {}) => ({
  id, is_room: true, member: true, name: `Room ${id}`, title: `Room ${id}`,
  agent_names: ['ada', 'bo'], last_message_at: iso(5), unread: 0, ...over,
})
const roomDetail = (id, messages) => ({ id, name: `Room ${id}`, status: 'open', participants: [], messages, own_tags: {}, working: [] })
const msg = (id, seq, over = {}) => ({
  id, seq, kind: 'message', sender_kind: 'agent', sender_identity: 'ada', content: `say ${id}`,
  created_at: iso(10 - seq), ...over,
})

let store, router, wrapper
beforeEach(async () => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.asksAvailable = true
  store.asksLoaded = true
  store.multiAgentChatAvailable = true
  store.fetchHistory = vi.fn(async () => ({ messages: [] }))
  store.fetchSessionDeliverablesStrict = vi.fn(async () => [])
  store.fetchAsks = vi.fn(async () => store.asks)
  store.fetchRoom = vi.fn(async (id) => roomDetail(id, [
    msg('m1', 1, { kind: 'system', sender_kind: 'system', sender_identity: null, content: 'Room created.' }),
    msg('m2', 2, { sender_kind: 'workspace_user', sender_identity: 'me@example.com', content: 'over to you' }),
    msg('m3', 3, { content: 'Ada here: the numbers are in.' }),
    msg('m4', 4, { sender_identity: 'bo', content: 'Bo: and the chart.' }),
  ]))
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace/inbox', component: { template: '<div />' } },
      { path: '/workspace/c/:id', component: { template: '<div />' } },
      { path: '/workspace/r/:roomId', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers(); document.body.innerHTML = '' })

async function mountInbox(props = {}, query = {}) {
  await router.replace({ path: '/workspace/inbox', query })
  await flushPromises()
  wrapper = mount(PortalInbox, {
    props: { threads: [], previews: {}, threadsLoaded: true, threadsFailed: false, labels: { ada: 'Ada', bo: 'Bo' }, ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}
const has = (w, id) => w.find(`[data-testid="${id}"]`).exists()

describe('Unread: a room with new messages (AC 1)', () => {
  it('is one row, ordered with chats, naming its agents and its count', async () => {
    const threads = [thread('t1', { unread: 1, last_message_at: iso(30) }), room('r1', { unread: 2, last_message_at: iso(5) })]
    const w = await mountInbox({ threads }, { tab: 'unread' })
    expect(w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))).toEqual(['room:r1', 'thread:t1'])
    expect(w.find('[data-testid="inbox-row-agent-room:r1"]').text()).toBe('Ada, Bo')
    expect(w.find('[data-testid="inbox-row-room:r1"]').text()).toContain('Room r1')
    expect(w.find('[data-testid="inbox-row-new-room:r1"]').text()).toBe('2 new')
    expect(w.find('[data-testid="inbox-list-total"]').text()).toContain('2 chats · 3 new')
  })

  it('opening it renders the new messages in the pane, then reads it as a room — once', async () => {
    const threads = [thread('t1', { unread: 1, last_message_at: iso(1) }), room('r1', { unread: 2, last_message_at: iso(5) })]
    const previews = { 'room:r1': { latest: { kind: 'message', id: 'm4', at: iso(6), excerpt: 'Bo: and the chart.' }, first_unread_message_id: 'm3' } }
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads, previews, markRead }, { tab: 'unread' })
    expect(markRead).not.toHaveBeenCalled()          // the desktop preview of t1 is not a read

    await w.find('[data-testid="inbox-row-room:r1"]').trigger('click')
    await flushPromises()
    expect(store.fetchRoom).toHaveBeenCalledWith('r1', 0)
    const shown = w.findAll('[data-testid^="inbox-pane-message-"]').map((x) => x.attributes('data-testid'))
    expect(shown).toEqual(['inbox-pane-message-m3', 'inbox-pane-message-m4'])
    expect(w.find('[data-testid="inbox-pane-body"]').text()).toContain('Ada')
    expect(w.find('[data-testid="inbox-pane-body"]').text()).toContain('Bo')
    expect(markRead.mock.calls).toEqual([['room', 'r1']])

    // The shell zeroes it: the row stays, drawn read, while it is selected.
    await w.setProps({ threads: [threads[0], { ...threads[1], unread: 0 }] })
    expect(has(w, 'inbox-row-read-room:r1')).toBe(true)
    expect(markRead).toHaveBeenCalledTimes(1)
  })

  it('a long room is read to its end before it is marked read (Codex review)', async () => {
    // The room read returns at most 500 rows from `since` — oldest first — so a
    // room past that ended the pane on m500 while the new messages sat beyond
    // it, and the read then cleared what the pane never showed.
    const all = Array.from({ length: 502 }, (_, i) => msg(`m${i + 1}`, i + 1))
    store.fetchRoom = vi.fn(async (id, since = 0) => roomDetail(id, all.filter((m) => m.seq > since).slice(0, 500)))
    const previews = { 'room:r1': { latest: { kind: 'message', id: 'm502', at: iso(1), excerpt: 'x' }, first_unread_message_id: 'm501' } }
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [room('r1', { unread: 2 })], previews, markRead }, { tab: 'unread' })
    await w.find('[data-testid="inbox-row-room:r1"]').trigger('click')
    await flushPromises()
    const shown = w.findAll('[data-testid^="inbox-pane-message-"]').map((x) => x.attributes('data-testid'))
    expect(shown).toEqual(['inbox-pane-message-m501', 'inbox-pane-message-m502'])
    // Paged from `since`, to the first empty batch (the desktop preview read it
    // once already; the explicit open reloads it).
    expect(store.fetchRoom.mock.calls.slice(-3).map((c) => c[1])).toEqual([0, 500, 502])
    expect(markRead.mock.calls).toEqual([['room', 'r1']])
  })

  it('a room that fails to load is not read', async () => {
    store.fetchRoom = vi.fn(async () => { throw new Error('404') })
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [room('r1', { unread: 2 })], markRead }, { tab: 'unread' })
    await w.find('[data-testid="inbox-row-room:r1"]').trigger('click')
    await flushPromises()
    expect(has(w, 'inbox-pane-room-failed')).toBe(true)
    expect(markRead).not.toHaveBeenCalled()
  })

  it('"Open in chat" opens the room', async () => {
    const w = await mountInbox({ threads: [room('r1', { unread: 2 })] }, { tab: 'unread', item: 'room:r1' })
    await w.find('[data-testid="inbox-pane-open"]').trigger('click')
    expect(w.emitted('open-chat')[0][0]).toBe('/workspace/r/r1')
  })

  it('Mark all read reads the room as a room', async () => {
    const markRead = vi.fn(async () => true)
    const w = await mountInbox({ threads: [thread('t1', { unread: 1 }), room('r1', { unread: 2 })], markRead }, { tab: 'unread' })
    expect(w.find('[data-testid="inbox-mark-all-read"]').text()).toBe('Mark 2 chats read')
    await w.find('[data-testid="inbox-mark-all-read"]').trigger('click')
    await flushPromises()
    document.querySelector('[data-testid="confirm-dialog-confirm"]').click()   // two chats: it asks first
    await flushPromises()
    expect(markRead.mock.calls.slice().sort()).toEqual([['room', 'r1'], ['thread', 't1']])
  })
})

describe('a non-member never sees the room (AC 3)', () => {
  it('no row, id, name or count — on Unread, on All, and through a deep link', async () => {
    const secret = room('room_secret', { member: false, unread: 4, name: 'Board offsite', title: 'Board offsite' })
    for (const query of [{ tab: 'unread' }, { tab: 'all' }, { tab: 'all', item: 'room:room_secret' }]) {
      const w = await mountInbox({ threads: [thread('t1', { unread: 1 }), secret] }, query)
      const html = w.html()
      expect(html, JSON.stringify(query)).not.toContain('room_secret')
      expect(html, JSON.stringify(query)).not.toContain('Board offsite')
      expect(store.fetchRoom).not.toHaveBeenCalled()
      wrapper.unmount(); wrapper = null
    }
  })
})

describe('All (AC 2, AC 5)', () => {
  it('lists my rooms beside my chats, and no footer note about rooms', async () => {
    const w = await mountInbox({ threads: [thread('t1', { last_message_at: iso(30) }), room('r1', { last_message_at: iso(5) })] }, { tab: 'all' })
    expect(w.findAll('[data-inbox-row]').map((r) => r.attributes('data-inbox-row'))).toEqual(['room:r1', 'thread:t1'])
    expect(w.find('[data-testid="inbox-list-footer"]').text()).not.toContain('Rooms')
  })
})

describe('the open room reads what arrives in it', () => {
  it('emits read on open and when an agent message lands, never for nothing new', async () => {
    vi.useFakeTimers()
    let messages = [msg('m3', 3)]
    store.fetchRoom = vi.fn(async (id, since = 0) => roomDetail(id, messages.filter((m) => m.seq > since)))
    wrapper = mount(PortalRoom, {
      props: { roomId: 'r1', roster: [{ name: 'ada' }] },
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
    expect(wrapper.emitted('read')).toEqual([['r1']])        // opening is reading

    vi.advanceTimersByTime(3100)                              // a poll with nothing new
    await flushPromises()
    expect(wrapper.emitted('read')).toHaveLength(1)

    messages = [...messages, msg('m4', 4, { sender_identity: 'bo' })]
    vi.advanceTimersByTime(3100)                              // an agent replies
    await flushPromises()
    expect(wrapper.emitted('read')).toEqual([['r1'], ['r1']])
  })
})
