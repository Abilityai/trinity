// @vitest-environment jsdom
/**
 * #3302 — `/m` lists every pending ask, and a partial read loses nothing.
 *
 * The phone read one `limit: 100` page: past 100 pending, asks could not be
 * answered from the phone, and — the route lists pending first — "Recently
 * ended" disappeared. MOUNTED (#2918): the list, the note and the prune are
 * template + `<script setup>` internals; only a render proves them together.
 * The route is the in-memory fake (`helpers/fakeOperatorQueue.js`); polls are
 * driven by faking `setInterval` only, so promises still flush for real.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'ops' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('@/utils/boundedHttp', () => ({
  http: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import { http } from '@/utils/boundedHttp'
import { useAuthStore } from '@/stores/auth'
import MobileAdmin from '@/views/MobileAdmin.vue'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'

let wrapper

async function mountOn(queue) {
  http.get.mockImplementation((url, config = {}) => {
    if (url === '/api/operator-queue') return queue.get(config.params)
    if (url === '/api/notifications') return Promise.resolve({ data: { notifications: [], count: 0 } })
    return Promise.reject(new Error(`unexpected ${url}`))
  })
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().isAuthenticated = true
  wrapper = mount(MobileAdmin, { global: { plugins: [pinia], stubs: { LoadFailed: true, InlineError: true } } })
  await flushPromises()
  return wrapper
}

async function poll() {
  vi.advanceTimersByTime(15000)
  await flushPromises()
}

const card = (w, id) => w.find(`[data-testid="queue-card"][data-item-id="${id}"]`)

describe('/m — the Queue tab past the old 100-row window (#3302)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    document.body.innerHTML = ''
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.useRealTimers()
  })

  it('every pending ask is on the phone, and "Recently ended" survives a full page of them', async () => {
    const w = await mountOn(makeQueue([
      ...rowsOf(130, { agents: 6 }),
      ...rowsOf(8, { prefix: 'done', status: 'cancelled', disposition: 'cancelled', disposed_by_email: 'op@example.com' }),
    ]))
    expect(w.findAll('[data-testid="queue-card"]')).toHaveLength(130)
    expect(w.findAll('[data-testid="queue-ended-card"]')).toHaveLength(5)
    expect(w.find('[data-testid="queue-partial"]').exists()).toBe(false)
  })

  it('a read that cannot finish says what it shows', async () => {
    const w = await mountOn(makeQueue(rowsOf(520, { agents: 21 }), { walkError: { status: 503, on: 'start' } }))
    expect(w.findAll('[data-testid="queue-card"]')).toHaveLength(500)
    expect(w.find('[data-testid="queue-partial"]').text()).toBe('Showing 500 of 520 pending')
  })

  it('a poll never supersedes a queue read still in flight, so a slow read still lands', async () => {
    // A read is several requests now; a phone slower than the 15 s poll used to
    // have every read superseded by the next poll, so the list never landed.
    const queue = makeQueue(rowsOf(3))
    let release
    const gate = new Promise((r) => { release = r })
    let reads = 0
    const w = await mountOn({
      get: (params) => {
        reads += 1
        return reads === 1 ? gate.then(() => queue.get(params)) : queue.get(params)
      },
    })
    await poll()
    await poll()
    expect(reads).toBe(1)
    release()
    await flushPromises()
    expect(w.findAll('[data-testid="queue-card"]')).toHaveLength(3)
  })

  it('a partial read after a complete one keeps the answer typed into a card it could not show', async () => {
    const opts = {}
    const w = await mountOn(makeQueue(rowsOf(520, { agents: 21 }), opts))
    // p-0001 is the oldest ask: last in the route's order, outside a 500-row first page
    await card(w, 'p-0001').find('[data-testid="queue-answer"]').setValue('ship it on Friday')

    opts.walkError = { status: 503, on: 'start' }
    await poll()
    expect(card(w, 'p-0001').exists()).toBe(false)

    delete opts.walkError
    await poll()
    expect(card(w, 'p-0001').find('[data-testid="queue-answer"]').element.value).toBe('ship it on Friday')
  })
})
