// @vitest-environment jsdom
/**
 * #3302 — the Operations subtitle states the queue's real numbers.
 *
 * MOUNTED (#2918): the subtitle is a computed over three store fields; only a
 * render proves the view reads the right ones. The store is real and is fed by
 * the in-memory route (`helpers/fakeOperatorQueue.js`); every child is stubbed.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const route = { query: { tab: 'needs-response' } }
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}))
vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('@/api', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: [] })), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})

import axios from 'axios'
import Operations from '@/views/Operations.vue'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'

let wrapper

async function mountOn(tab, queue) {
  route.query.tab = tab
  axios.get.mockImplementation((url, config = {}) =>
    url === '/api/operator-queue' ? queue.get(config.params) : Promise.resolve({ data: {} }))
  const pinia = createPinia()
  setActivePinia(pinia)
  // #3451: the lists render in BoundedRegion's slot — render it, or a shallow
  // mount drops the cards this spec counts.
  wrapper = shallowMount(Operations, { global: { plugins: [pinia], stubs: { BoundedRegion: false } } })
  await flushPromises()
  return wrapper.find('p.mt-1.text-sm').text()
}

describe('Operations — the subtitle states the queue (#3302)', () => {
  beforeEach(() => { vi.clearAllMocks() })
  afterEach(() => { wrapper?.unmount(); wrapper = null })

  it('counts every pending ask past the old 200-row window', async () => {
    const text = await mountOn('needs-response', makeQueue([
      ...rowsOf(260, { agents: 11 }),
      ...rowsOf(40, { prefix: 'done', status: 'responded' }),
    ]))
    expect(text).toBe('260 pending responses')
  })

  it('a partial read says how many of the pending asks are shown', async () => {
    const text = await mountOn('needs-response', makeQueue(rowsOf(703, { agents: 29 }), {
      walkError: { status: 503, on: 'start' },
    }))
    expect(text).toBe('703 pending responses (500 shown)')
  })

  it('the Resolved tab states its window — "N resolved · latest 200 shown" — even with 200+ pending', async () => {
    const text = await mountOn('resolved', makeQueue([
      ...rowsOf(230, { agents: 10 }),
      ...rowsOf(340, { prefix: 'done', status: 'cancelled' }),
    ]))
    expect(text).toBe('340 resolved · latest 200 shown')
    expect(wrapper.findAllComponents({ name: 'ResolvedCard' })).toHaveLength(200)
  })

  it('a Resolved list that fits says only how many', async () => {
    const text = await mountOn('resolved', makeQueue([
      ...rowsOf(2),
      ...rowsOf(12, { prefix: 'done', status: 'expired' }),
    ]))
    expect(text).toBe('12 resolved')
  })
})
