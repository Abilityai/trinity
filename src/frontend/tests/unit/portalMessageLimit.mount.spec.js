// @vitest-environment jsdom
/**
 * #3460 — the composer names the message limit BEFORE sending, and a server
 * refusal is said in words.
 *
 * The server bounds a chat message (`PortalChatRequest.message`, max_length) and
 * the composer enforced nothing: 8001 characters went out, came back 422, and
 * the bubble read "The message wasn't delivered (error 422)" — a status code,
 * with the words already gone from the composer.
 *
 * Mounted (#2918): the real PortalConversation (shallow), the store's network
 * calls stubbed at the store seam — the harness portalReplyRefused.mount.spec.js
 * uses.
 *
 * The one read of a backend file below is a PIN, and its live consumer is the
 * cross-layer contract itself: the client limit is a second copy of a number
 * the server owns, and nothing else can notice the two drifting apart. It reads
 * no component source, so the source-text ratchet does not apply to it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { readFileSync } from 'fs'
import { resolve } from 'path'

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
import {
  PORTAL_MESSAGE_MAX_CHARS, MESSAGE_LIMIT_WARN_WITHIN,
  messageLength, messageLimitState, messageRefusalReason,
} from '@/components/portal/portalMessageLimit'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const MAX = PORTAL_MESSAGE_MAX_CHARS
const LIMIT = '[data-testid="portal-message-limit"]'
// What FastAPI actually answers for an over-long `message` (Pydantic v2).
const tooLong422 = (max = MAX) => ({ response: { status: 422, data: { detail: [{
  type: 'string_too_long', loc: ['body', 'message'],
  msg: `String should have at most ${max} characters`, input: '…', ctx: { max_length: max },
}] } } })

let wrapper
let store
beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 's1' }))
})
afterEach(() => { wrapper?.unmount(); wrapper = null })

async function mountChat() {
  wrapper = shallowMount(PortalConversation, {
    props: { agent: { name: 'scout', playbooks: [] }, sessionId: 's1' },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
  return wrapper
}
const textarea = () => wrapper.find('textarea')
const sendButton = () => wrapper.find('button[type="submit"]')
const retryButton = () => wrapper.findAll('button').find((b) => b.text().includes('Retry'))

describe('the client limit is the server limit (#3460)', () => {
  it('equals PortalChatRequest.message max_length, read from the backend model', () => {
    const models = readFileSync(resolve(__dirname, '../../../backend/client_portal/models.py'), 'utf8')
    const cls = models.split(/^class /m).find((block) => block.startsWith('PortalChatRequest('))
    expect(cls, 'PortalChatRequest not found in client_portal/models.py').toBeTruthy()
    const field = cls.match(/^\s+message:\s*str\s*=\s*Field\(([^)]*)\)/m)
    expect(field, 'PortalChatRequest.message is no longer a bounded Field').toBeTruthy()
    const max = field[1].match(/max_length\s*=\s*([\d_]+)/)
    expect(max, 'PortalChatRequest.message lost its max_length').toBeTruthy()
    expect(PORTAL_MESSAGE_MAX_CHARS).toBe(Number(max[1].replace(/_/g, '')))
  })

  it('counts what the server counts — code points, not UTF-16 units', () => {
    expect(messageLength('abc')).toBe(3)
    // One emoji is one character to Python's len() and two to String.length.
    expect(messageLength('😀'.repeat(10))).toBe(10)
    expect(messageLimitState('😀'.repeat(MAX)).over).toBe(false)
    expect(messageLimitState('😀'.repeat(MAX + 1)).over).toBe(true)
  })

  it('is quiet far from the limit, counts near it, and refuses past it', () => {
    expect(messageLimitState('hello')).toMatchObject({ near: false, over: false, message: '' })
    const near = messageLimitState('x'.repeat(MAX - MESSAGE_LIMIT_WARN_WITHIN))
    expect(near).toMatchObject({ near: true, over: false })
    expect(near.message).toBe('7,500 of 8,000 characters')
    expect(messageLimitState('x'.repeat(MAX - MESSAGE_LIMIT_WARN_WITHIN - 1)).near).toBe(false)
    expect(messageLimitState('x'.repeat(MAX))).toMatchObject({ near: true, over: false })
    const over = messageLimitState('x'.repeat(MAX + 1))
    expect(over.over).toBe(true)
    expect(over.message).toContain('8,000 characters')
    expect(over.message).toContain('8,001')
  })
})

describe('a server refusal in words (#3460)', () => {
  it('names the limit the server stated, never the status code', () => {
    const said = messageRefusalReason(tooLong422(8000))
    expect(said).toContain('8,000 characters')
    expect(said).not.toMatch(/422|error \d/)
    // The server's number wins over ours: it is the one that refused.
    expect(messageRefusalReason(tooLong422(4000))).toContain('4,000 characters')
  })

  it('says what the server said for any other validation refusal', () => {
    const said = messageRefusalReason({ response: { status: 422, data: { detail: [
      { type: 'string_too_short', loc: ['body', 'message'], msg: 'String should have at least 1 character' },
    ] } } })
    expect(said).toContain('String should have at least 1 character')
    expect(said).not.toMatch(/422|\[object/)
  })

  it('leaves every other failure to the existing reasons', () => {
    // A handler-raised 422 already carries its own sentence (#3054).
    expect(messageRefusalReason({ response: { status: 422, data: { detail: 'Remove the reply and send again.' } } })).toBeNull()
    expect(messageRefusalReason({ response: { status: 503, data: { detail: [] } } })).toBeNull()
    expect(messageRefusalReason(new Error('boom'))).toBeNull()
  })
})

describe('the composer states the limit before sending (#3460)', () => {
  it('an over-limit draft cannot be sent, and the limit is named', async () => {
    await mountChat()
    const draft = 'x'.repeat(MAX + 1)
    await textarea().setValue(draft)

    const notice = wrapper.find(LIMIT)
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('8,000 characters')
    expect(notice.text()).toContain('8,001')
    expect(notice.attributes('role')).toBe('alert')
    expect(sendButton().attributes('disabled')).toBeDefined()

    // The button is not the only door: Enter and a programmatic submit reach
    // `send()` directly.
    await wrapper.find('form').trigger('submit')
    await textarea().trigger('keydown', { key: 'Enter' })
    await flushPromises()

    expect(store.startPortalChat).not.toHaveBeenCalled()
    // Nothing was appended to the thread, and the words are still there to edit.
    expect(textarea().element.value).toBe(draft)
    expect(wrapper.text()).not.toContain('Not delivered')
  })

  it('a draft AT the limit sends', async () => {
    await mountChat()
    const draft = 'x'.repeat(MAX)
    await textarea().setValue(draft)
    expect(sendButton().attributes('disabled')).toBeUndefined()
    // Near the limit the count is visible, as a count rather than an alert.
    expect(wrapper.find(LIMIT).text()).toBe('8,000 of 8,000 characters')
    expect(wrapper.find(LIMIT).attributes('role')).toBeUndefined()

    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(store.startPortalChat.mock.calls[0][1]).toBe(draft)
  })

  it('an ordinary draft shows no counter', async () => {
    await mountChat()
    await textarea().setValue('hello there')
    expect(wrapper.find(LIMIT).exists()).toBe(false)
    expect(sendButton().attributes('disabled')).toBeUndefined()
  })

  it('shortening an over-limit draft clears the refusal', async () => {
    await mountChat()
    await textarea().setValue('x'.repeat(MAX + 1))
    expect(sendButton().attributes('disabled')).toBeDefined()
    await textarea().setValue('short enough')
    expect(wrapper.find(LIMIT).exists()).toBe(false)
    expect(sendButton().attributes('disabled')).toBeUndefined()
  })
})

describe('when the server still refuses (#3460)', () => {
  it('shows the reason in words, keeps the draft, and offers no pointless Retry', async () => {
    // A server whose limit is LOWER than the client believes: the one way a
    // message under the client limit can still be refused for length.
    store.startPortalChat = vi.fn().mockRejectedValue(tooLong422(4000))
    await mountChat()
    const draft = 'y'.repeat(5000)
    await textarea().setValue(draft)
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('4,000 characters')
    expect(wrapper.text()).not.toMatch(/error 422/)
    // The words are back in the composer to shorten…
    expect(textarea().element.value).toBe(draft)
    // …and re-sending the same payload could only earn the same refusal.
    expect(retryButton()).toBeUndefined()
  })

  it('a failure that is not a validation refusal is untouched: Retry stays, the composer stays empty', async () => {
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 503, data: { detail: 'The agent is busy' } } })
    await mountChat()
    await textarea().setValue('ping')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('The agent is busy')
    expect(textarea().element.value).toBe('')
    expect(retryButton()).toBeDefined()
  })
})
