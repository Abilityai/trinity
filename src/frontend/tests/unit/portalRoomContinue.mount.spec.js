// @vitest-environment jsdom
/**
 * trinity#4302 — a paused mention chain offers Continue in the Workspace room.
 *
 * Mounts the real PortalRoom over a stubbed store (the #3706 harness): the
 * control is present iff the payload's `paused_chain` stands on an open room,
 * a double click asks once, a coded refusal is said, a network failure and the
 * "moved on" refusals are not, and the control goes when the offer does.
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

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
}))

const PAUSE_TEXT = 'The agents paused after 8 hand-offs and are waiting for a person. scout was next.'
const room = (over = {}) => ({
  id: 'room_1', name: 'Pricing review', status: 'open',
  participants: [{ kind: 'user', identity: 'alice', role: 'moderator' },
    { kind: 'agent', identity: 'scout' }, { kind: 'agent', identity: 'critic' }],
  you: { kind: 'user', identity: 'alice' },
  own_tags: {}, working: [],
  messages: [
    { seq: 1, sender_kind: 'user', sender_identity: 'alice', kind: 'message', content: '@scout go' },
    { seq: 2, sender_kind: 'agent', sender_identity: 'critic', kind: 'message', content: '@scout again' },
    { seq: 3, sender_kind: 'system', sender_identity: 'system', kind: 'chain_paused', content: PAUSE_TEXT, mentions: ['scout'] },
  ],
  paused_chain: { seq: 3, next: ['scout'] },
  ...over,
})
const refusal = (status, code, message) => ({ response: { status, data: { detail: { code, message } } } })

let store, wrapper
async function mountWith(payload) {
  store.fetchRoom = vi.fn(async () => JSON.parse(JSON.stringify(payload)))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/workspace/r/:roomId', component: { template: '<div />' } }],
  })
  await router.push('/workspace/r/room_1')
  await router.isReady()
  wrapper = mount(PortalRoom, {
    props: { roomId: 'room_1', roster: [{ name: 'scout' }, { name: 'critic' }] },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.multiAgentChatAvailable = true
  store.continueRoomChain = vi.fn(async () => ({ room_id: 'room_1', seq: 4, woke: ['scout'] }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

const button = () => wrapper.find('[data-testid="room-continue"]')
const offer = () => wrapper.find('[data-testid="room-continue-offer"]')

describe('the Continue control is the payload offer (#4302)', () => {
  it('renders under an open room with a standing offer, naming who was next', async () => {
    await mountWith(room())
    expect(button().exists()).toBe(true)
    expect(offer().text()).toContain('Continue lets scout carry on, or send a message.')
  })

  it('names two agents in plain words', async () => {
    await mountWith(room({ paused_chain: { seq: 3, next: ['scout', 'critic'] } }))
    expect(offer().text()).toContain('Continue lets scout and critic carry on')
  })

  it('is absent when there is no offer', async () => {
    await mountWith(room({ paused_chain: null }))
    expect(offer().exists()).toBe(false)
  })

  it('is absent when the room is closed, even with a stale offer', async () => {
    await mountWith(room({ status: 'closed', stop_reason: 'message_budget' }))
    expect(offer().exists()).toBe(false)
  })

  it('is absent when the room is gone for the reader', async () => {
    store.fetchRoom = vi.fn(async () => { throw refusal(404, 'room_not_found', 'Not found') })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)', component: { template: '<div />' } }] })
    await router.push('/workspace/r/room_1'); await router.isReady()
    wrapper = mount(PortalRoom, { props: { roomId: 'room_1', roster: [] }, global: { plugins: [router] }, attachTo: document.body })
    await flushPromises()
    expect(wrapper.find('[data-testid="room-gone"]').exists()).toBe(true)
    expect(offer().exists()).toBe(false)
  })

  it('renders the pause row as a centred system line, not a bubble', async () => {
    await mountWith(room())
    const line = wrapper.findAll('p').find((p) => p.text() === PAUSE_TEXT)
    expect(line, 'pause row not rendered as a system line').toBeTruthy()
    expect(line.classes()).toContain('text-center')
    // Not attributed: no sender label "system" above an agent bubble.
    expect(wrapper.text()).not.toMatch(/^system$/m)
  })
})

describe('pressing Continue (#4302)', () => {
  it('asks once for the offer seq, even on a double click', async () => {
    let release
    store.continueRoomChain = vi.fn(() => new Promise((r) => { release = r }))
    await mountWith(room())
    await button().trigger('click')
    await button().trigger('click')
    expect(store.continueRoomChain).toHaveBeenCalledTimes(1)
    expect(store.continueRoomChain).toHaveBeenCalledWith('room_1', 3)
    expect(button().attributes('disabled')).toBeDefined()
    release({ room_id: 'room_1', seq: 4, woke: ['scout'] })
    await flushPromises()
  })

  it('goes when the reload says the offer is consumed', async () => {
    await mountWith(room())
    const after = room({ paused_chain: null })
    after.messages.push({ seq: 4, sender_kind: 'system', sender_identity: 'system', kind: 'chain_continued', content: 'alice asked scout to carry on.' })
    store.fetchRoom = vi.fn(async () => JSON.parse(JSON.stringify({ ...after, messages: after.messages.slice(3) })))
    await button().trigger('click')
    await flushPromises()
    expect(store.fetchRoom).toHaveBeenCalled()
    expect(offer().exists()).toBe(false)
    expect(wrapper.text()).toContain('alice asked scout to carry on.')
  })

  it('a coded refusal that leaves the offer standing is said, and the person may retry', async () => {
    store.continueRoomChain = vi.fn(async () => {
      throw refusal(403, 'agent_not_accessible', 'You no longer have access to scout')
    })
    await mountWith(room())
    await button().trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="room-continue-error"]').text()).toContain('You no longer have access to scout')
    expect(button().attributes('disabled')).toBeUndefined()
  })

  it('a budget refusal says why', async () => {
    store.continueRoomChain = vi.fn(async () => {
      throw refusal(410, 'room_closed', 'This room reached its message budget')
    })
    await mountWith(room())
    await button().trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="room-continue-error"]').text()).toContain('message budget')
  })

  it('a network failure shows no error — the reload tells the truth', async () => {
    store.continueRoomChain = vi.fn(async () => { throw new Error('Network Error') })
    await mountWith(room())
    await button().trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="room-continue-error"]').exists()).toBe(false)
  })

  it.each(['room_moved_on', 'request_in_progress'])('a %s refusal is silent', async (code) => {
    store.continueRoomChain = vi.fn(async () => { throw refusal(409, code, 'moved on') })
    await mountWith(room())
    await button().trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="room-continue-error"]').exists()).toBe(false)
  })
})

describe('the store action (#4302)', () => {
  it('posts {seq} to the room continue route with no Idempotency-Key', async () => {
    const { portalHttp } = await import('@/stores/clientPortal')
    setActivePinia(createPinia())
    const real = useClientPortalStore()   // the real action, not the beforeEach stub
    real.portalToken = 'tok'
    real.multiAgentChatAvailable = true
    portalHttp.post.mockResolvedValueOnce({ data: { room_id: 'r 1', seq: 9, woke: [] } })
    expect(await real.continueRoomChain('r 1', 8)).toEqual({ room_id: 'r 1', seq: 9, woke: [] })
    const [url, body, cfg] = portalHttp.post.mock.calls.at(-1)
    expect(url).toBe('/api/rooms/r%201/continue')
    expect(body).toEqual({ seq: 8 })
    expect(cfg?.headers?.['Idempotency-Key']).toBeUndefined()
  })

  it('a coded refusal reaches the caller and leaves the rooms capability up', async () => {
    const { portalHttp } = await import('@/stores/clientPortal')
    setActivePinia(createPinia())
    const real = useClientPortalStore()
    real.portalToken = 'tok'
    real.multiAgentChatAvailable = true
    portalHttp.post.mockRejectedValueOnce(refusal(403, 'not_a_person', 'Only a person can continue'))
    await expect(real.continueRoomChain('room_1', 3)).rejects.toMatchObject({ response: { status: 403 } })
    expect(real.multiAgentChatAvailable).toBe(true)
  })
})
