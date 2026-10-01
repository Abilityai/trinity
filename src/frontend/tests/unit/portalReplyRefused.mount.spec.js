// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A — #3054 review item 2: a refused reply has a way out.
 *
 * The server refuses a reply target it cannot prove (422 "Remove the reply and
 * send again"). The composer's chip has already gone with the text by then, and
 * the failed message kept its `replyTo`, so Retry re-sent the same id and got
 * the same 422 every time. The failed message's chip is now removable: the
 * person drops the reply and Retry sends the message as an ordinary turn.
 *
 * Mounted (#2918): the real PortalConversation (shallow) with the real
 * PortalReplyChip, the store's network calls stubbed at the store seam.
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
import PortalReplyChip from '@/components/portal/PortalReplyChip.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const REFUSED = "That message can't be replied to here. Remove the reply and send again."

let wrapper
let store
let replied
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  replied = false
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1',
    messages: replied ? [{ id: 'r1', role: 'assistant', content: 'Done.', source: null }] : [],
  }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn()
    .mockRejectedValueOnce({ response: { status: 422, data: { detail: REFUSED } } })
    .mockImplementationOnce(async () => {
      replied = true
      return { execution_id: 'e2', session_id: 's1' }
    })
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function sendReplyThatIsRefused() {
  wrapper = shallowMount(PortalConversation, {
    props: {
      agent: { name: 'scout', playbooks: [] }, sessionId: 's1',
      replyTarget: { sessionId: 's1', messageId: 'm9', excerpt: 'Heads up: 429 twice.' },
      // The shell's half: `reply-done` clears the target it passed in.
      'onReply-done': () => wrapper.setProps({ replyTarget: null }),
    },
    global: { renderStubDefaultSlot: true, stubs: { PortalReplyChip: false } },
    attachTo: document.body,
  })
  await flushPromises()
  await wrapper.find('textarea').setValue('what does that mean?')
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  expect(store.startPortalChat).toHaveBeenCalledTimes(1)
  expect(store.startPortalChat.mock.calls[0][3].replyToMessageId).toBe('m9')
}

const retryButton = () => wrapper.findAll('button').find((b) => b.text().includes('Retry'))

describe('a refused reply', () => {
  it('can drop the reply on the failed message, and Retry then sends without it', async () => {
    await sendReplyThatIsRefused()
    expect(wrapper.text()).toContain(REFUSED)

    // The failed message still shows what it replied to — and can let go of it.
    const chips = wrapper.findAllComponents(PortalReplyChip)
    expect(chips).toHaveLength(1)
    await chips[0].find('[data-testid="portal-reply-chip-remove"]').trigger('click')
    expect(wrapper.findAllComponents(PortalReplyChip)).toHaveLength(0)

    await retryButton().trigger('click')
    await flushPromises()

    expect(store.startPortalChat).toHaveBeenCalledTimes(2)
    const [, text, , opts] = store.startPortalChat.mock.calls[1]
    expect(text).toBe('what does that mean?')
    expect(opts.replyToMessageId).toBeNull()
    // The agent's reply landed (its bubble is a shallow stub carrying the text),
    // and the message is no longer failed.
    expect(wrapper.html()).toContain('content="Done."')
    expect(wrapper.text()).not.toContain(REFUSED)
    expect(retryButton()).toBeUndefined()
  })

  it('a message that was delivered keeps its reply chip fixed', async () => {
    store.startPortalChat = vi.fn(async () => { replied = true; return { execution_id: 'e1', session_id: 's1' } })
    wrapper = shallowMount(PortalConversation, {
      props: {
        agent: { name: 'scout', playbooks: [] }, sessionId: 's1',
        replyTarget: { sessionId: 's1', messageId: 'm9', excerpt: 'Heads up' },
        'onReply-done': () => wrapper.setProps({ replyTarget: null }),
      },
      global: { renderStubDefaultSlot: true, stubs: { PortalReplyChip: false } },
      attachTo: document.body,
    })
    await flushPromises()
    await wrapper.find('textarea').setValue('ok')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    const chips = wrapper.findAllComponents(PortalReplyChip)
    expect(chips).toHaveLength(1)
    expect(chips[0].find('[data-testid="portal-reply-chip-remove"]').exists()).toBe(false)
  })
})
