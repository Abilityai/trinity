// @vitest-environment jsdom
/**
 * trinity-enterprise#1066 — a secret pasted into the composer is refused by the
 * server before anything is stored (422 `secret_in_message`). The composer's
 * half: the text is not kept anywhere on this side either. No failed bubble
 * carrying it (a Retry would only re-send it), not handed back to the
 * composer, no draft in storage — and the server's pointer is shown instead.
 *
 * Mounted (#2918): the real PortalConversation (shallow), the store's network
 * calls stubbed at the store seam, the real drafts store over jsdom storage.
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
import { usePortalDraftsStore } from '@/stores/portalDrafts'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Built at run time, like the backend guard's fixtures: no token-shaped literal in the tree.
const TOKEN = 'ghp_' + 'b'.repeat(36)
const SECRET = `here it is: ${TOKEN}`
const POINTER = 'That looks like a secret. Nothing was stored. Ask the agent for the credential and use the secret field it opens.'

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.clientEmail = 'person@example.com'
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.sendPortalChat = vi.fn()
  store.startPortalChat = vi.fn().mockRejectedValue({
    response: { status: 422, data: { detail: { code: 'secret_in_message', shape: 'github_token', message: POINTER } } },
  })
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function pasteAndSend(text = SECRET) {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    global: { renderStubDefaultSlot: true, stubs: { InlineError: false } },
    attachTo: document.body,
  })
  await flushPromises()
  await wrapper.find('textarea').setValue(text)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}

describe('a secret pasted as a plain message', () => {
  it('shows the server’s pointer and keeps the text nowhere on this side', async () => {
    await pasteAndSend()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="portal-secret-refused"]').text()).toContain(POINTER)
    // Not in the thread (no failed bubble), not in the composer, no Retry.
    expect(wrapper.html()).not.toContain(TOKEN)
    expect(wrapper.find('textarea').element.value).toBe('')
    expect(wrapper.findAll('button').some((b) => b.text().includes('Retry'))).toBe(false)
    // No draft, in memory or in storage.
    const drafts = usePortalDraftsStore()
    expect(Object.values(drafts.drafts).some((d) => d.text.includes('ghp_'))).toBe(false)
    expect(JSON.stringify({ ...localStorage })).not.toContain('ghp_')
  })

  it('the same text left as a draft in ANOTHER thread is cleared too', async () => {
    // The person pasted the message into another conversation first and left
    // it there, then pasted it here and sent it. The composer watch only
    // clears THIS thread's draft; "keeps no copy" covers the other one, which
    // sits in local storage.
    const drafts = usePortalDraftsStore()
    drafts.set('scout::other-thread', SECRET)
    drafts.set('scout::unrelated', 'a draft about something else')
    expect(JSON.stringify({ ...localStorage })).toContain('ghp_')
    await pasteAndSend()
    expect(drafts.get('scout::other-thread')).toBeFalsy()
    expect(drafts.get('scout::unrelated')).toBe('a draft about something else')
    expect(JSON.stringify({ ...localStorage })).not.toContain('ghp_')
  })

  it('the synchronous fallback’s refusal is handled the same way', async () => {
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 404 } })
    store.sendPortalChat = vi.fn().mockRejectedValue({
      response: { status: 422, data: { detail: { code: 'secret_in_message', shape: 'github_token', message: POINTER } } },
    })
    await pasteAndSend()
    expect(store.sendPortalChat).toHaveBeenCalledTimes(1)
    expect(wrapper.html()).not.toContain(TOKEN)
    expect(wrapper.find('[data-testid="portal-secret-refused"]').text()).toContain(POINTER)
  })

  it('the next send clears the pointer', async () => {
    await pasteAndSend()
    store.startPortalChat = vi.fn(async () => ({ execution_id: 'e2', session_id: 's1' }))
    await wrapper.find('textarea').setValue('ok, I will use the field')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="portal-secret-refused"]').exists()).toBe(false)
  })

  it('any other 422 is unchanged: the words go back to the composer to edit', async () => {
    store.startPortalChat = vi.fn().mockRejectedValue({
      response: { status: 422, data: { detail: [{ loc: ['body', 'message'], type: 'string_too_long', ctx: { max_length: 10 } }] } },
    })
    await pasteAndSend('a long message')
    expect(wrapper.find('textarea').element.value).toBe('a long message')
    expect(wrapper.find('[data-testid="portal-secret-refused"]').exists()).toBe(false)
  })
})
