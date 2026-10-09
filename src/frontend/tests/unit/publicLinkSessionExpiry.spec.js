// @vitest-environment jsdom
/**
 * #3406 — a public link's expired session ends on the verify card, with the reason.
 *
 * A visitor who verified their email on `/chat/:token` holds a 24 h session in
 * `localStorage['public_session_<token>']`. After it expires the server answers
 * 401 on history, intro, session clear, chat send and Stop. The page used to
 * swallow the load-time 401s without clearing the key (every reload repeated
 * them), and the send path wrote its "Session expired" notice into `chatError`,
 * which renders only inside the chat block the verify card replaces — so the
 * visitor saw the form with no reason. Stop never sent the session at all, so
 * on an email link it could only ever 401.
 *
 * MOUNTED, through each real call site, with `axios` stubbed by URL — the same
 * harness `skillGateSenders.spec.js` uses for this view. The global 401 handler
 * (which no longer sends any of this to the operator /login) is executed in
 * `platformSessionVerdict.spec.js` / `platformSessionSync.spec.js`.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const routes = vi.hoisted(() => ({ get: null, post: null, delete: null }))

vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => routes.get(...a)),
    post: vi.fn((...a) => routes.post(...a)),
    delete: vi.fn((...a) => routes.delete(...a)),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { token: 'tok' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

import axios from 'axios'
import PublicChat from '../../src/views/PublicChat.vue'

const KEY = 'public_session_tok'
const NOTICE = 'Session expired. Please verify your email again.'
const INTRO = 'Hello, I am Fin. I help with invoices.'
const EMAIL_LINK = { data: { valid: true, agent_available: true, require_email: true, agent_name: 'fin' } }
const OPEN_LINK = { data: { valid: true, agent_available: true, require_email: false, agent_name: 'fin' } }

const unauthorized = () => Object.assign(new Error('Request failed with status code 401'), {
  response: { status: 401, data: { detail: 'Invalid or expired session. Please verify your email again.' } },
})
const reject401 = () => Promise.reject(unauthorized())

function answer(table, url, ...rest) {
  for (const [pattern, reply] of Object.entries(table)) {
    if (url.includes(pattern)) return typeof reply === 'function' ? reply(url, ...rest) : reply
  }
  return { data: {} }
}

function serve({ get = {}, post = {}, del = {} } = {}) {
  routes.get = vi.fn(async (url, ...rest) => answer(get, url, ...rest))
  routes.post = vi.fn(async (url, ...rest) => answer(post, url, ...rest))
  routes.delete = vi.fn(async (url, ...rest) => answer(del, url, ...rest))
}

const calls = (method, pattern) => axios[method].mock.calls.map(c => c[0]).filter(u => u.includes(pattern))

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

let wrapper

async function mountChat() {
  wrapper = mount(PublicChat)
  await flush()
  return wrapper
}

function onVerifyCard(w) {
  return w.text().includes('Verify Your Email')
}

function noticeShown(w) {
  return w.text().includes(NOTICE)
}

// Walks the card the way a visitor does: Send Code, then the code.
async function verifyAgain(w, { session = 'S2' } = {}) {
  routes.post = vi.fn(async (url) => {
    if (url.includes('/verify/request')) return { data: {} }
    if (url.includes('/verify/confirm')) return { data: { verified: true, session_token: session } }
    return { data: {} }
  })
  await w.find('input[type="email"]').setValue('visitor@example.com')
  await w.find('form').trigger('submit')
  await flush()
  await w.find('input[maxlength="6"]').setValue('123456')
  await w.find('form').trigger('submit')
  await flush()
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  setActivePinia(createPinia())
  // No SSE in this spec: the stream is `fetch`, not axios.
  globalThis.fetch = vi.fn(async () => { throw new Error('no SSE in this spec') })
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('an expired session found on load (AC3)', () => {
  it('a history 401 clears the stored session and shows the verify card with the reason', async () => {
    localStorage.setItem(KEY, 'expired-session')
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': reject401,
      '/api/public/playbooks/': { data: { skills: [{ name: 'invoice' }] } },
    } })
    const w = await mountChat()

    expect(localStorage.getItem(KEY)).toBeNull()
    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
    // A session-less intro request can only 401 again.
    expect(calls('get', '/api/public/intro/')).toEqual([])
    // The quick actions need no session, so they are loaded for after the card.
    expect(calls('get', '/api/public/playbooks/tok')).not.toEqual([])
  })

  it('an intro 401 (nothing in history yet) does the same', async () => {
    localStorage.setItem(KEY, 'expired-session')
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': { data: { messages: [] } },
      '/api/public/intro/': reject401,
    } })
    const w = await mountChat()

    expect(localStorage.getItem(KEY)).toBeNull()
    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
  })

  it('verifying again loads the introduction exactly once', async () => {
    localStorage.setItem(KEY, 'expired-session')
    let introAnswers = 0
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': { data: { messages: [] } },
      '/api/public/intro/': () => (introAnswers++ === 0 ? reject401() : { data: { intro: INTRO } }),
    } })
    const w = await mountChat()
    expect(onVerifyCard(w)).toBe(true)

    await verifyAgain(w)

    expect(onVerifyCard(w)).toBe(false)
    expect(calls('get', '/api/public/intro/')).toHaveLength(2)   // the 401, then one fresh fetch
    expect(w.text().split(INTRO)).toHaveLength(2)                 // rendered once
    expect(localStorage.getItem(KEY)).toBe('S2')
  })

  it('a stored session that is still valid is left alone', async () => {
    localStorage.setItem(KEY, 'live-session')
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': { data: { messages: [] } },
      '/api/public/intro/': { data: { intro: INTRO } },
    } })
    const w = await mountChat()

    expect(onVerifyCard(w)).toBe(false)
    expect(localStorage.getItem(KEY)).toBe('live-session')
    expect(w.text()).toContain(INTRO)
  })

  it("a stale tab's 401 leaves a sibling tab's fresh session in storage", async () => {
    // Two tabs on one link: this one still holds the expired session, the
    // other has just verified again and written a fresh one under the key.
    localStorage.setItem(KEY, 'expired-session')
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': () => {
        localStorage.setItem(KEY, 'fresh-from-the-other-tab')
        return reject401()
      },
    } })
    const w = await mountChat()

    expect(onVerifyCard(w)).toBe(true)                       // this tab's session is gone…
    expect(localStorage.getItem(KEY)).toBe('fresh-from-the-other-tab')   // …the sibling's is not
  })

  it('storage that refuses the removal still ends the session on screen', async () => {
    localStorage.setItem(KEY, 'expired-session')
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked') })
    serve({ get: { '/api/public/link/': EMAIL_LINK, '/api/public/history/': reject401 } })
    const w = await mountChat()

    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
  })
})

describe('an expired session found mid-conversation (AC4)', () => {
  async function verifiedConversation() {
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': { data: { messages: [] } },
      '/api/public/intro/': { data: { intro: INTRO } },
    } })
    const w = await mountChat()
    expect(onVerifyCard(w)).toBe(true)        // a first visit: no session yet
    await verifyAgain(w, { session: 'S1' })
    expect(onVerifyCard(w)).toBe(false)
    return w
  }

  it('a send 401 shows the card with the reason, on the email step, and keeps the words', async () => {
    const w = await verifiedConversation()
    routes.post = vi.fn(async (url) => (url.includes('/api/public/chat/') ? reject401() : { data: {} }))

    await w.vm.sendMessage('please pay invoice 42')
    await flush()

    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
    expect(localStorage.getItem(KEY)).toBeNull()
    // The email step, not the stale code form from the first verification.
    expect(w.find('input[type="email"]').exists()).toBe(true)

    await verifyAgain(w)

    // Back in the chat: the unsent words are in the input, not a bubble that
    // never reached the server, and the introduction is not doubled.
    expect(w.find('textarea').element.value).toBe('please pay invoice 42')
    expect(w.text()).not.toContain('please pay invoice 42')
    expect(w.text().split(INTRO)).toHaveLength(2)
  })

  it('an earlier error does not survive into the chat after verifying again', async () => {
    // A failed turn leaves its error on screen; New then meets the expired
    // session. `sendMessage` clears the error itself, so New is the path on
    // which a stale one would outlive the card.
    const w = await verifiedConversation()
    const failure = Object.assign(new Error('Request failed with status code 500'), {
      response: { status: 500, data: { detail: 'The agent hit a wall.' } },
    })
    routes.post = vi.fn(async () => Promise.reject(failure))
    await w.vm.sendMessage('first try')
    await flush()
    expect(w.text()).toContain('The agent hit a wall.')

    vi.spyOn(window, 'confirm').mockReturnValue(true)
    routes.delete = vi.fn(async () => reject401())
    await w.vm.confirmNewConversation()
    await flush()
    expect(onVerifyCard(w)).toBe(true)

    await verifyAgain(w)
    expect(w.text()).not.toContain('The agent hit a wall.')
  })

  it('a past session being read does not survive into the chat after verifying again', async () => {
    // The history dropdown (signed-in operators) opens a past session read-only;
    // a 401 on New from there must not leave the read-only banner and a hidden
    // composer behind the next verification.
    const w = await verifiedConversation()
    w.vm.handleHistorySessionSelected({
      messages: [{ role: 'user', content: 'an old question' }],
      session: { id: 'past-1' },
    })
    await flush()
    expect(w.text()).toContain('Viewing past session')
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    routes.delete = vi.fn(async () => reject401())
    await w.vm.confirmNewConversation()
    await flush()
    expect(onVerifyCard(w)).toBe(true)

    await verifyAgain(w)
    expect(w.text()).not.toContain('Viewing past session')
    expect(w.find('textarea').exists()).toBe(true)
  })

  it('a 401 on New (session clear) shows the card with the reason', async () => {
    const w = await verifiedConversation()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    routes.delete = vi.fn(async (url) => (url.includes('/api/public/session/') ? reject401() : { data: {} }))

    await w.vm.confirmNewConversation()
    await flush()

    expect(calls('delete', '/api/public/session/tok')).toHaveLength(1)
    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})

describe('Stop on an email-verified link (ent#155 + #3406)', () => {
  async function turnInFlight(terminate) {
    localStorage.setItem(KEY, 'S1')
    serve({
      get: {
        '/api/public/link/': EMAIL_LINK,
        '/api/public/history/': { data: { messages: [] } },
        '/api/public/intro/': { data: { intro: INTRO } },
        '/status': { data: { execution_id: 'e1', status: 'running' } },
      },
      post: {
        '/api/public/chat/': { data: { status: 'accepted', execution_id: 'e1', async_mode: true } },
        '/terminate': terminate,
      },
    })
    const w = await mountChat()
    vi.useFakeTimers()
    w.vm.sendMessage('a long report')
    await vi.advanceTimersByTimeAsync(0)
    expect(w.vm.canCancelTurn).toBe(true)
    return w
  }

  async function endTurn(w) {
    w.vm.chatLoading = false              // ends the poll loop the cancel did not
    await vi.advanceTimersByTimeAsync(5_000)
    vi.useRealTimers()
    await flush()
  }

  it("carries the visitor's session in the QUERY — the route has no body model", async () => {
    const w = await turnInFlight({ data: { status: 'cancelled' } })
    await w.vm.cancelTurn()

    const [url, body] = axios.post.mock.calls.find(c => c[0].includes('/terminate'))
    expect(url).toBe('/api/public/executions/tok/e1/terminate?session_token=S1')
    expect(body).toBeUndefined()
    await endTurn(w)
  })

  it('a terminate 401 ends the session on the card, and Escape there sends nothing more', async () => {
    const w = await turnInFlight(reject401)
    await w.vm.cancelTurn()
    await vi.advanceTimersByTimeAsync(0)

    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
    // The turn is still running server-side, but there is nothing to stop from
    // the card: Escape must not fire a session-less terminate and wipe the form.
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await vi.advanceTimersByTimeAsync(0)
    expect(calls('post', '/terminate')).toHaveLength(1)
    await endTurn(w)
  })
})

describe('the quick actions are there once the visitor gets through', () => {
  it('a first-time visitor on an email link gets them after verifying, without a reload', async () => {
    // No stored session: the page used to load them only on the "already
    // verified" branch, so a visitor who verified in-page had none until a
    // reload — and the same held after an expired session.
    serve({ get: {
      '/api/public/link/': EMAIL_LINK,
      '/api/public/history/': { data: { messages: [] } },
      '/api/public/intro/': { data: { intro: INTRO } },
      '/api/public/playbooks/': { data: { skills: [{ name: 'invoice-check', description: 'Check an invoice' }] } },
    } })
    const w = await mountChat()
    expect(onVerifyCard(w)).toBe(true)

    await verifyAgain(w, { session: 'S1' })

    expect(onVerifyCard(w)).toBe(false)
    expect(w.text()).toContain('Check an invoice')
  })
})

describe('Stop on an open link', () => {
  it("a 401 there is the agent's own status, not a reason to ask for an email", async () => {
    // The terminate route checks no session on an open link and passes the
    // agent's status through, so its 401 is a failed cancel, nothing more.
    serve({
      get: {
        '/api/public/link/': OPEN_LINK,
        '/api/public/history/': { data: { messages: [] } },
        '/api/public/intro/': { data: { intro: INTRO } },
        '/status': { data: { execution_id: 'e1', status: 'running' } },
      },
      post: {
        '/api/public/chat/': { data: { status: 'accepted', execution_id: 'e1', async_mode: true } },
        '/terminate': reject401,
      },
    })
    const w = await mountChat()
    vi.useFakeTimers()
    w.vm.sendMessage('a long report')
    await vi.advanceTimersByTimeAsync(0)
    await w.vm.cancelTurn()
    await vi.advanceTimersByTimeAsync(0)

    expect(onVerifyCard(w)).toBe(false)
    expect(w.vm.chatError).toBeTruthy()             // the ordinary "could not stop" message
    expect(noticeShown(w)).toBe(false)
    w.vm.chatLoading = false
    await vi.advanceTimersByTimeAsync(5_000)
    vi.useRealTimers()
    await flush()
  })
})

describe('a link whose policy changed to require an email after the page loaded', () => {
  it('a 401 on an open link shows the card instead of swallowing the message', async () => {
    serve({ get: {
      '/api/public/link/': OPEN_LINK,
      '/api/public/history/': reject401,
    } })
    const w = await mountChat()

    expect(onVerifyCard(w)).toBe(true)
    expect(noticeShown(w)).toBe(true)
  })
})
