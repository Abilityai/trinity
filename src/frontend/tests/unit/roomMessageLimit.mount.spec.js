// @vitest-environment jsdom
/**
 * #3706 — the room composer states the message limit before sending, and a
 * server refusal of an over-long message is said in words.
 *
 * A 422 carries a Pydantic list in `detail`; the room names the limit from it.
 * The 1:1 composer follows the same rule (#3460).
 *
 * The one read of a backend file below is a PIN: the client limit is a copy of
 * `RoomMessageCreate.content`'s max_length, and nothing else notices drift.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { readFileSync } from 'fs'
import { resolve } from 'path'

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
import { ROOM_MESSAGE_MAX_CHARS, messageRefusalReason } from '@/components/portal/portalMessageLimit'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({
  matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
}))

const MAX = ROOM_MESSAGE_MAX_CHARS
const fmt = (n) => n.toLocaleString('en-US')
const LIMIT = '[data-testid="portal-message-limit"]'
const ROOM = {
  id: 'room_1', name: 'Pricing review', status: 'open',
  participants: [{ kind: 'user', identity: 'alice', role: 'moderator' }, { kind: 'agent', identity: 'scout' }],
  messages: [], own_tags: {}, working: [],
}
const tooLong422 = (max = MAX) => ({ response: { status: 422, data: { detail: [{
  type: 'string_too_long', loc: ['body', 'content'],
  msg: `String should have at most ${max} characters`, input: '…', ctx: { max_length: max },
}] } } })

let store, wrapper
beforeEach(async () => {
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoom = vi.fn(async () => JSON.parse(JSON.stringify(ROOM)))
  store.postRoomMessage = vi.fn(async () => ({ seq: 1, tags: [] }))
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/workspace/r/:roomId', component: { template: '<div />' } }],
  })
  await router.push('/workspace/r/room_1')
  await router.isReady()
  wrapper = mount(PortalRoom, {
    props: { roomId: 'room_1', roster: [{ name: 'scout' }] },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

const textarea = () => wrapper.find('textarea')
const sendButton = () => wrapper.find('button[type="submit"]')

describe('the room limit is the server limit (#3706)', () => {
  it('equals RoomMessageCreate.content max_length, read from the backend model', () => {
    const models = readFileSync(resolve(__dirname, '../../../backend/shared_sessions/models.py'), 'utf8')
    const cls = models.split(/^class /m).find((block) => block.startsWith('RoomMessageCreate('))
    expect(cls, 'RoomMessageCreate not found in shared_sessions/models.py').toBeTruthy()
    const field = cls.match(/^\s+content:\s*str\s*=\s*Field\(([^)]*)\)/m)
    expect(field, 'RoomMessageCreate.content is no longer a bounded Field').toBeTruthy()
    const max = field[1].match(/max_length\s*=\s*([\d_]+)/)
    expect(max, 'RoomMessageCreate.content lost its max_length').toBeTruthy()
    expect(MAX).toBe(Number(max[1].replace(/_/g, '')))
  })
})

describe('the room composer (#3706)', () => {
  it('sends the reported 13,661-character paste', async () => {
    const draft = 'x'.repeat(13_661)
    await textarea().setValue(draft)
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.postRoomMessage).toHaveBeenCalledTimes(1)
    expect(store.postRoomMessage.mock.calls[0][1]).toBe(draft)
  })

  it('a draft AT the limit sends, with the count shown', async () => {
    await textarea().setValue('x'.repeat(MAX))
    expect(wrapper.find(LIMIT).text()).toBe(`${fmt(MAX)} of ${fmt(MAX)} characters`)
    expect(sendButton().attributes('disabled')).toBeUndefined()
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.postRoomMessage).toHaveBeenCalledTimes(1)
  })

  it('one character over cannot be sent, and the limit is named', async () => {
    const draft = 'x'.repeat(MAX + 1)
    await textarea().setValue(draft)
    const notice = wrapper.find(LIMIT)
    expect(notice.text()).toContain(`${fmt(MAX)} characters`)
    expect(notice.text()).toContain(fmt(MAX + 1))
    expect(notice.attributes('role')).toBe('alert')
    expect(sendButton().attributes('disabled')).toBeDefined()

    await wrapper.find('form').trigger('submit')
    await textarea().trigger('keydown', { key: 'Enter' })
    await flushPromises()
    expect(store.postRoomMessage).not.toHaveBeenCalled()
    expect(textarea().element.value).toBe(draft)
  })

  it('a server refusal names the limit and hands the draft back', async () => {
    store.postRoomMessage = vi.fn(async () => { throw tooLong422(4000) })
    await textarea().setValue('hello there')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('at most 4,000 characters')
    expect(wrapper.text()).not.toContain('That message was not delivered')
    expect(textarea().element.value).toBe('hello there')
  })
})

describe('messageRefusalReason reads the room field too (#3706)', () => {
  it('names the limit for an over-long `content`', () => {
    expect(messageRefusalReason(tooLong422(MAX))).toContain(`${fmt(MAX)} characters`)
  })
})
