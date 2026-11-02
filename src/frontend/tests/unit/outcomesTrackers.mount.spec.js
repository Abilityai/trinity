// @vitest-environment jsdom
/**
 * trinity-enterprise#941 — Outcomes › Trackers, MOUNTED (ruled 2026-10-29).
 *
 * The GitHub repos the fleet analysis reads were env-only; an admin now edits
 * them here. The card says where the list comes from, opens the editor
 * pre-filled, names a bad entry before the round trip, saves the parsed list,
 * resets a saved list to the env default, and keeps a failed save in an
 * InlineError beside the control.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const http = vi.hoisted(() => ({ get: null, put: null, delete: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    put: vi.fn((...a) => http.put(...a)),
    delete: vi.fn((...a) => http.delete(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import OutcomesTrackers from '../../src/components/outcomes/OutcomesTrackers.vue'
import { REGISTRIES_URL, parseTrackers } from '../../src/stores/outcomes'

const ENV = { repos: ['acme/plans'], source: 'env', env_default: ['acme/plans'] }

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

function mountCard(state = ENV) {
  http.get = vi.fn(async () => ({ data: state }))
  http.put = vi.fn(async (url, body) => ({ data: { repos: body.repos, source: 'setting', env_default: ['acme/plans'] } }))
  http.delete = vi.fn(async () => ({ data: ENV }))
  setActivePinia(createPinia())
  return mount(OutcomesTrackers, { global: { stubs: { SkeletonLoader: true } } })
}

describe('parseTrackers', () => {
  it('ignores blanks, dedupes, and names the first bad entry', () => {
    expect(parseTrackers('acme/a\n\n acme/b \nacme/a')).toEqual({ repos: ['acme/a', 'acme/b'], error: null })
    expect(parseTrackers('acme/a\nhttps://github.com/acme/b').error).toContain('https://github.com/acme/b')
    expect(parseTrackers(Array.from({ length: 21 }, (_, i) => `acme/r${i}`).join('\n')).error).toContain('20')
  })
})

describe('OutcomesTrackers', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('lists the env default and says where it comes from, with no reset', async () => {
    const w = mountCard()
    await flush()
    expect(http.get.mock.calls[0][0]).toBe(REGISTRIES_URL)
    expect(w.find('[data-testid="outcomes-trackers-source"]').text()).toBe('from the environment default')
    expect(w.find('[data-testid="outcomes-trackers-list"]').text()).toContain('acme/plans')
    expect(w.find('[data-testid="outcomes-trackers-reset"]').exists()).toBe(false)
  })

  it('opens pre-filled, blocks a bad entry, and saves the parsed list', async () => {
    const w = mountCard()
    await flush()
    await w.find('[data-testid="outcomes-trackers-edit"]').trigger('click')
    // BaseTextarea puts the test id on the control itself.
    const ta = w.find('textarea[data-testid="outcomes-trackers-draft"]')
    expect(ta.element.value).toBe('acme/plans')
    await ta.setValue('acme/plans\nnot a repo')
    expect(w.text()).toContain('"not a repo" is not owner/repo')
    expect(w.find('[data-testid="outcomes-trackers-save"]').attributes('disabled')).toBeDefined()
    await ta.setValue('acme/plans\n acme/roadmap \n')
    await w.find('[data-testid="outcomes-trackers-save"]').trigger('click')
    await flush()
    expect(http.put).toHaveBeenCalledWith(REGISTRIES_URL, { repos: ['acme/plans', 'acme/roadmap'] })
    expect(w.find('[data-testid="outcomes-trackers-draft"]').exists()).toBe(false)
    expect(w.find('[data-testid="outcomes-trackers-source"]').text()).toBe('saved here')
    expect(w.find('[data-testid="outcomes-trackers-list"]').text()).toContain('acme/roadmap')
  })

  it('resets a saved list to the env default', async () => {
    const w = mountCard({ repos: ['acme/roadmap'], source: 'setting', env_default: ['acme/plans'] })
    await flush()
    await w.find('[data-testid="outcomes-trackers-reset"]').trigger('click')
    await flush()
    expect(http.delete).toHaveBeenCalledWith(REGISTRIES_URL)
    expect(w.find('[data-testid="outcomes-trackers-source"]').text()).toBe('from the environment default')
  })

  it('keeps the editor open and shows a failed save beside it', async () => {
    const w = mountCard()
    await flush()
    http.put = vi.fn(async () => { throw { response: { status: 422, data: { detail: { code: 'invalid_repo', message: 'nope' } } } } })
    await w.find('[data-testid="outcomes-trackers-edit"]').trigger('click')
    await w.find('[data-testid="outcomes-trackers-save"]').trigger('click')
    await flush()
    expect(w.find('[data-testid="outcomes-trackers-draft"]').exists()).toBe(true)
    expect(w.find('[data-testid="outcomes-trackers-error"]').exists()).toBe(true)
  })

  it('says nothing is read when the list is empty', async () => {
    const w = mountCard({ repos: [], source: 'none', env_default: [] })
    await flush()
    expect(w.find('[data-testid="outcomes-trackers-empty"]').exists()).toBe(true)
    expect(w.find('[data-testid="outcomes-trackers-source"]').text()).toBe('none configured')
  })
})
