// @vitest-environment jsdom
/**
 * #3140 — the conversation reports a thread it cannot open.
 *
 * When the shell could not trust its thread list (the list read failed), it
 * lets the conversation try the URL's chat id. If that history read 404s, the
 * id is someone else's or gone: the conversation emits `thread-missing` so the
 * shell can say "This chat isn't available" instead of leaving an empty chat
 * with a live composer under that id. Any other failure still starts empty.
 *
 * Mounted (#2918): the real PortalConversation (shallow), store stubbed at the
 * store seam — the harness of `portalReplyRefused.mount.spec.js`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function open(status) {
  store.fetchHistory = vi.fn(async () => { throw { response: { status } } })
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 'not-mine' },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}

describe('#3140 — PortalConversation and a thread it cannot open', () => {
  it('a 404 on the history read emits thread-missing with the id', async () => {
    const w = await open(404)
    expect(store.fetchHistory).toHaveBeenCalledWith('scout', 'not-mine')
    expect(w.emitted('thread-missing')).toEqual([['not-mine']])
  })

  it.each([[500], [503], [undefined]])('a %s is not "missing" — the thread starts empty as before', async (status) => {
    const w = await open(status)
    expect(w.emitted('thread-missing')).toBeUndefined()
  })
})
