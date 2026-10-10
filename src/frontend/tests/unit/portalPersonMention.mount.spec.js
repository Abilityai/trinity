// @vitest-environment jsdom
/**
 * trinity-enterprise#631 — tagging a person, MOUNTED (design contract #2918:
 * a predicate that gates a store write or a keyboard contract is proven by a
 * mount, not a regex).
 *
 *   the picker      `@` + a name offers people who exist (from the server, once
 *                   there is a query); Tab picks; the send carries the address
 *                   beside the text; an unknown name is refused BY NAME
 *   the marks       the tagger's own message says delivered / read
 *   the card        a reader who cannot see the room is told so and who can let
 *                   them in — no content; a reader who can sees the message
 *   the Inbox       an unread tag sits in Unread; opening it marks it read once
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
import PortalRoom from '@/components/portal/PortalRoom.vue'
import PortalMentionCard from '@/components/portal/PortalMentionCard.vue'
import PortalInbox from '@/components/portal/PortalInbox.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
}))

const BOB = { email: 'bob@example.com', label: 'Bob Baker' }
const ROOM = {
  id: 'room_1', name: 'Pricing review', status: 'open',
  participants: [{ kind: 'user', identity: 'alice', role: 'moderator' }, { kind: 'agent', identity: 'scout' }],
  messages: [
    { id: 'msg-1', seq: 2, kind: 'message', sender_kind: 'user', sender_identity: 'alice', content: '@Bob Baker look', tags: [] },
  ],
  own_tags: { 'msg-1': [{ label: 'Bob Baker', state: 'read', read_at: '2026-10-10T10:05:00Z' }] },
  working: [],
}

let store, wrapper, router
beforeEach(async () => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace/inbox', component: { template: '<div />' } },
      { path: '/workspace/r/:roomId', component: { template: '<div />' } },
      { path: '/workspace/c/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/workspace/inbox')
  await router.isReady()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers(); document.body.innerHTML = '' })

describe('the room composer tags a person', () => {
  async function mountRoom() {
    store.fetchRoom = vi.fn(async () => JSON.parse(JSON.stringify(ROOM)))
    store.fetchRoomPeople = vi.fn(async (_room, q) => (String(q).toLowerCase().startsWith('bo') ? [BOB] : []))
    store.postRoomMessage = vi.fn(async () => ({ seq: 3, tags: [{ label: 'Bob Baker', state: 'delivered' }] }))
    wrapper = mount(PortalRoom, {
      props: { roomId: 'room_1', roster: [{ name: 'scout' }] },
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
    return wrapper
  }

  async function typeInto(ta, value) {
    ta.element.value = value
    ta.element.setSelectionRange(value.length, value.length)
    await ta.trigger('input')
    vi.advanceTimersByTime(400)
    await flushPromises()
  }

  it('offers a person who exists, Tab picks them, and the send carries their address', async () => {
    vi.useFakeTimers()
    const w = await mountRoom()
    const ta = w.find('textarea')
    await typeInto(ta, '@bo')
    expect(store.fetchRoomPeople).toHaveBeenCalledWith('room_1', 'bo')
    const options = w.findAll('[role="option"]')
    expect(options.map((o) => o.text())).toEqual([expect.stringContaining('Bob Baker')])
    expect(w.text()).toContain('Agents and people')

    await ta.trigger('keydown', { key: 'Tab' })
    await flushPromises()
    expect(ta.element.value).toBe('@Bob Baker ')

    await ta.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(store.postRoomMessage).toHaveBeenCalledTimes(1)
    expect(store.postRoomMessage).toHaveBeenCalledWith('room_1', '@Bob Baker', { tags: ['bob@example.com'] })
  })

  it('a message that no longer names the picked person tags no one', async () => {
    vi.useFakeTimers()
    const w = await mountRoom()
    const ta = w.find('textarea')
    await typeInto(ta, '@bo')
    await ta.trigger('keydown', { key: 'Tab' })
    await flushPromises()
    ta.element.value = 'never mind'
    await ta.trigger('input')
    await ta.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(store.postRoomMessage).toHaveBeenCalledWith('room_1', 'never mind', { tags: [] })
  })

  it('refuses an unknown name by name, with the reason', async () => {
    vi.useFakeTimers()
    const w = await mountRoom()
    await typeInto(w.find('textarea'), '@zed')
    expect(w.text()).toContain('No one called “zed” can be tagged here — you can tag people who work with scout.')
    expect(w.findAll('[role="option"]')).toHaveLength(0)
  })

  it("the tagger's own message says the tag was read", async () => {
    const w = await mountRoom()
    const marks = w.find('[data-testid="portal-room-tags-2"]')
    expect(marks.exists()).toBe(true)
    expect(marks.text()).toContain('Bob Baker · read')
  })

  it('a refused tag keeps the message in the composer and names the reason', async () => {
    vi.useFakeTimers()
    const w = await mountRoom()
    store.postRoomMessage = vi.fn(async () => {
      const err = new Error('422')
      err.response = { status: 422, data: { detail: { code: 'unknown_person', message: 'bob@example.com can\'t be tagged here', name: 'bob@example.com' } } }
      throw err
    })
    const ta = w.find('textarea')
    await typeInto(ta, '@bo')
    await ta.trigger('keydown', { key: 'Tab' })
    await ta.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(ta.element.value).toBe('@Bob Baker')
    expect(w.text()).toContain("bob@example.com can't be tagged here")
  })
})

describe('the tag, opened', () => {
  const item = { key: 'mention:m1', type: 'mention', id: 'm1', agent_name: 'scout',
    mention: { id: 'm1', tagged_by: 'Alice Archer', conversation: { kind: 'room', label: 'Pricing review' } } }

  it("a reader who cannot see the room is told so and who can let them in — and shown nothing of it", async () => {
    store.openMention = vi.fn(async () => ({
      id: 'm1', agent_name: 'scout', state: 'unread', created_at: '2026-10-10T10:00:00Z', tagged_by: 'Alice Archer',
      conversation: { kind: 'room', id: null, label: 'Pricing review' },
      can_see: false, message: null, context: [], can_let_you_in: ['Alice Archer'],
    }))
    wrapper = mount(PortalMentionCard, { props: { item }, global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-mention-cant-see"]').text()).toContain('Alice Archer runs this room and can let you in')
    expect(wrapper.find('[data-testid="inbox-mention-context"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="inbox-mention-open"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="inbox-mention-reach"]').text()).toContain('no email is sent')
    expect(wrapper.emitted('rendered')).toEqual([['mention:m1']])
  })

  it('a reader who can see the room gets the tagged message, marked, and a way in', async () => {
    store.openMention = vi.fn(async () => ({
      id: 'm1', agent_name: 'scout', state: 'unread', created_at: '2026-10-10T10:00:00Z', tagged_by: 'Alice Archer',
      conversation: { kind: 'room', id: 'room_1', label: 'Pricing review' },
      can_see: true, can_let_you_in: [],
      message: { id: 'x2', sender_kind: 'person', sender_label: 'Alice Archer', content: 'look at margin', tagged: true },
      context: [
        { id: 'x1', sender_kind: 'agent', sender_label: 'scout', content: 'margin is 42%', tagged: false },
        { id: 'x2', sender_kind: 'person', sender_label: 'Alice Archer', content: 'look at margin', tagged: true },
      ],
    }))
    wrapper = mount(PortalMentionCard, { props: { item }, global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-mention-tagged"]').text()).toContain('look at margin')
    expect(wrapper.find('[data-testid="inbox-mention-tagged"]').text()).toContain('tagged you here')
    await wrapper.find('[data-testid="inbox-mention-open"]').trigger('click')
    expect(wrapper.emitted('open-chat')).toEqual([['/workspace/r/room_1']])
  })

  it('a failed read is LoadFailed, never "can\'t see", and never rendered', async () => {
    store.openMention = vi.fn(async () => { throw new Error('boom') })
    wrapper = mount(PortalMentionCard, { props: { item }, global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-mention-failed"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="inbox-mention-cant-see"]').exists()).toBe(false)
    expect(wrapper.emitted('rendered')).toBeUndefined()
  })
})

describe('the Inbox', () => {
  const tag = { id: 'm1', agent_name: 'scout', state: 'unread', created_at: new Date().toISOString(), read_at: null,
    tagged_by: 'Alice Archer', conversation: { kind: 'room', label: 'Pricing review' } }

  it('an unread tag sits in Unread, and opening it marks it read exactly once', async () => {
    store.asksLoaded = true
    store.mentions = [tag]
    store.fetchAsks = vi.fn(async () => [])
    store.openMention = vi.fn(async () => ({ ...tag, conversation: { ...tag.conversation, id: null },
      can_see: false, message: null, context: [], can_let_you_in: ['Alice Archer'] }))
    store.markMentionRead = vi.fn(async (id) => {
      store.mentions = store.mentions.map((m) => (m.id === id ? { ...m, state: 'read' } : m))
      return true
    })
    await router.replace({ path: '/workspace/inbox', query: { tab: 'unread' } })
    wrapper = mount(PortalInbox, {
      props: { threads: [], previews: {}, threadsLoaded: true, threadsFailed: false, labels: {} },
      global: { plugins: [router] },
      attachTo: document.body,
    })
    await flushPromises()
    const row = wrapper.find('[data-testid="inbox-row-mention:m1"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Alice Archer')
    expect(row.text()).toContain('Mentioned you in the room “Pricing review”')
    expect(wrapper.find('[data-testid="inbox-row-new-mention:m1"]').exists()).toBe(true)
    // The desktop preview is not a read.
    expect(store.markMentionRead).not.toHaveBeenCalled()

    await row.trigger('click')
    await flushPromises()
    expect(store.markMentionRead).toHaveBeenCalledTimes(1)
    expect(store.markMentionRead).toHaveBeenCalledWith('m1')
    // Kept in place, drawn read.
    expect(wrapper.find('[data-testid="inbox-row-read-mention:m1"]').exists()).toBe(true)
  })
})
