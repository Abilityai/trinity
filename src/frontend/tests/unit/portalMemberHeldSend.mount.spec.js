// @vitest-environment jsdom
/**
 * ent#1201 — a member's message sent while their assistant is starting.
 *
 * During the hand-over the member's agent is being recreated and the server
 * refuses every turn (502). The row reads ready and the progress card says
 * "Your assistant is starting", so the composer must not walk the person into
 * that refusal: the send is held here, sent once the assistant_ready step
 * flips to done, and handed back as text if it has not after three minutes.
 * A refusal that does reach a member names no account.
 *
 * Mounted (#2918): the real PortalConversation (shallow), the store's network
 * calls stubbed at the store seam, the progress store seeded directly.
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
import { portalHttp, useClientPortalStore } from '@/stores/clientPortal'
import { usePortalProgressStore } from '@/stores/portalProgress'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const MEMBER = { name: 'bob-assistant', playbooks: [], is_member_agent: true, owned: false,
                 owner: 'ops-account@example.com', availability: 'stopped' }
const OPERATOR = { name: 'scout', playbooks: [], owner: 'alice', availability: 'ready' }

let wrapper
let store
let progress
let warn
// The progress GET, at the HTTP seam the store reads through.
const progressGets = () => portalHttp.get.mock.calls.filter(([url]) => String(url).endsWith('/progress'))
function progressReads(stateOrFn) {
  portalHttp.get.mockImplementation((url) => {
    if (!String(url).endsWith('/progress')) return Promise.resolve({ data: {} })
    const state = typeof stateOrFn === 'function' ? stateOrFn() : stateOrFn
    if (state instanceof Promise) return state
    if (state === 'error') return Promise.reject(new Error('network'))
    return Promise.resolve({ data: { active: true, all_done: false,
                                     steps: [{ key: 'assistant_ready', state }] } })
  })
}
function deferred() {
  let resolve
  const promise = new Promise((r) => { resolve = r })
  return { promise, resolve }
}
const rereadWarnings = () => warn.mock.calls.filter(([m]) => /re-read/.test(String(m)))
const doneBody = { data: { active: true, all_done: false, steps: [{ key: 'assistant_ready', state: 'done' }] } }
function seed(name, state) {
  progress.byAgent[name] = { loading: false, loaded: true, error: null, active: true, all_done: false,
                             steps: [{ key: 'assistant_ready', state }] }
}

beforeEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  setActivePinia(createPinia())
  store = useClientPortalStore()
  progress = usePortalProgressStore()
  store.fetchHistory = vi.fn(async () => ({ sessionId: 's1', messages: [] }))
  store.streamPortalExecution = vi.fn(async () => {})
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e1', session_id: 's1' }))
  portalHttp.get.mockReset()
  portalHttp.get.mockImplementation(() => Promise.resolve({ data: {} }))
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.useRealTimers(); warn.mockRestore() })

async function mountAndSend(agent, text = 'hello there') {
  wrapper = shallowMount(PortalConversation, {
    props: { agent, sessionId: 's1' },
    global: { renderStubDefaultSlot: true },
    attachTo: document.body,
  })
  await flushPromises()
  await wrapper.find('textarea').setValue(text)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
}
const held = () => wrapper.find('[data-testid="portal-held-send"]')

describe('ent#1201 a send while the member assistant is starting', () => {
  it('is held, says so, and goes out when the assistant is ready', async () => {
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    expect(store.startPortalChat).not.toHaveBeenCalled()
    expect(held().exists()).toBe(true)
    expect(held().text()).toContain("will send when it's ready")
    expect(wrapper.find('textarea').element.value).toBe('')
    // No chip, no banner during the start — and never an account.
    expect(wrapper.find('[data-testid="portal-availability-notice"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('ops-account')

    seed(MEMBER.name, 'done')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(store.startPortalChat.mock.calls[0][1]).toBe('hello there')
    expect(held().exists()).toBe(false)
  })

  it('is handed back as text after three minutes when a re-read still says starting', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    progressReads('in_progress')
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    expect(held().exists()).toBe(true)
    vi.advanceTimersByTime(3 * 60 * 1000)
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()
    expect(held().exists()).toBe(false)
    expect(wrapper.find('textarea').element.value).toBe('hello there')
    expect(progressGets().length).toBeGreaterThan(0)   // it asked before giving up
  })

  it('is handed back at once if the start failed', async () => {
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    seed(MEMBER.name, 'failed')
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()
    expect(wrapper.find('textarea').element.value).toBe('hello there')
  })

  it('survives a reload: the held words are the chat\'s draft until sent or handed back', async () => {
    store.clientEmail = 'member@example.com'
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    expect(held().exists()).toBe(true)
    // A reload never runs unmount hooks: what it finds is what is stored now.
    const before = wrapper
    setActivePinia(createPinia())
    useClientPortalStore().clientEmail = 'member@example.com'
    const reloaded = shallowMount(PortalConversation, {
      props: { agent: MEMBER, sessionId: 's1' }, global: { renderStubDefaultSlot: true },
    })
    await flushPromises()
    expect(reloaded.find('textarea').element.value).toBe('hello there')
    reloaded.unmount()

    // Sent on the ready flip: the draft lets go of it.
    seed(MEMBER.name, 'done')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(before.find('textarea').element.value).toBe('')
    setActivePinia(createPinia())
    useClientPortalStore().clientEmail = 'member@example.com'
    const again = shallowMount(PortalConversation, {
      props: { agent: MEMBER, sessionId: 's1' }, global: { renderStubDefaultSlot: true },
    })
    await flushPromises()
    expect(again.find('textarea').element.value).toBe('')
    again.unmount()
  })

  it('an operator-facing agent is never held', async () => {
    seed(OPERATOR.name, 'in_progress')
    await mountAndSend(OPERATOR)
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(held().exists()).toBe(false)
  })
})

describe('ent#1220 a held send re-reads the card instead of trusting one event', () => {
  it('sends, not hands back, when the re-read at three minutes says ready', async () => {
    progressReads('in_progress')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    vi.advanceTimersByTime(3 * 60 * 1000 - 1000)   // every poll so far: still starting
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()

    progressReads('done')                          // the ready event was lost
    vi.advanceTimersByTime(1000)
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)   // the watch and the hand-back: one send
    expect(store.startPortalChat.mock.calls[0][1]).toBe('hello there')
    expect(wrapper.find('textarea').element.value).toBe('')
    expect(held().exists()).toBe(false)
  })

  it('goes out within thirty seconds of a lost ready event, and says the poll caught it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    progressReads('done')
    vi.advanceTimersByTime(30 * 1000)
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(rereadWarnings()).toHaveLength(1)

    // The hold is over: no more polling, no hand-back.
    const reads = progressGets().length
    vi.advanceTimersByTime(5 * 60 * 1000)
    await flushPromises()
    expect(progressGets().length).toBe(reads)
    expect(wrapper.find('textarea').element.value).toBe('')
  })

  it('an event-driven flip sends without the warning', async () => {
    progressReads('in_progress')
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    seed(MEMBER.name, 'done')
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(rereadWarnings()).toHaveLength(0)
  })

  it('awaits a read already in flight at the hand-back instead of acting on the stale card', async () => {
    progressReads('in_progress')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    vi.advanceTimersByTime(3 * 60 * 1000 - 1000)
    await flushPromises()

    const slow = deferred()
    progressReads(() => slow.promise)
    void progress.fetchProgress(MEMBER.name, { force: true })   // e.g. a WS trigger's read
    vi.advanceTimersByTime(1000)                                // the ceiling fires mid-read
    await flushPromises()
    expect(wrapper.find('textarea').element.value).toBe('')     // not handed back on stale state

    slow.resolve(doneBody)
    await flushPromises()
    expect(store.startPortalChat).toHaveBeenCalledTimes(1)
    expect(wrapper.find('textarea').element.value).toBe('')
  })

  it('hands back when the re-read fails: unknown is not ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    progressReads('error')
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    vi.advanceTimersByTime(3 * 60 * 1000)
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()
    expect(wrapper.find('textarea').element.value).toBe('hello there')
  })

  it('leaving mid re-read sends nothing and keeps the words as the draft', async () => {
    progressReads('in_progress')
    vi.useFakeTimers({ shouldAdvanceTime: true })
    store.clientEmail = 'member@example.com'
    seed(MEMBER.name, 'in_progress')
    await mountAndSend(MEMBER)
    vi.advanceTimersByTime(3 * 60 * 1000 - 1000)
    await flushPromises()
    const slow = deferred()
    progressReads(() => slow.promise)
    vi.advanceTimersByTime(1000)
    await flushPromises()

    wrapper.unmount()
    wrapper = null
    slow.resolve(doneBody)
    await flushPromises()
    expect(store.startPortalChat).not.toHaveBeenCalled()

    progressReads('in_progress')
    setActivePinia(createPinia())
    useClientPortalStore().clientEmail = 'member@example.com'
    const back = shallowMount(PortalConversation, {
      props: { agent: MEMBER, sessionId: 's1' }, global: { renderStubDefaultSlot: true },
    })
    await flushPromises()
    expect(back.find('textarea').element.value).toBe('hello there')
    back.unmount()
  })
})

describe('ent#1201 a member whose assistant is down', () => {
  it('reads "restarting", never the owner, in the notice and on a refused turn', async () => {
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 502,
      data: { detail: "This agent isn't running right now — its owner needs to start it." } } })
    await mountAndSend(MEMBER)
    expect(wrapper.find('[data-testid="portal-availability-notice"]').text()).toBe('Your assistant is restarting.')
    expect(wrapper.text()).toContain('Your assistant is restarting.')
    expect(wrapper.text()).not.toMatch(/ops-account|its owner/)
  })

  it('hands the refused words back to the composer', async () => {
    store.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 502,
      data: { detail: "This agent isn't running right now — its owner needs to start it." } } })
    await mountAndSend(MEMBER, 'please look at this')
    expect(wrapper.find('textarea').element.value).toBe('please look at this')
  })
})
