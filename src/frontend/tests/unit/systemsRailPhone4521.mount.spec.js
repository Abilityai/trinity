// @vitest-environment jsdom
/**
 * #4521 — below 640px the Systems rail starts collapsed; from 640px up it
 * starts open; a saved preference wins at either width. MOUNTED: the default
 * is read from `matchMedia` at setup and shows only as rendered output (the
 * rail's width class and whether the checklist is in the tree), which the
 * source-text pins in firstRunSteps.spec.js cannot see — they stay green with
 * the predicate inverted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(() => Promise.resolve({ data: [] })), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
// The checklist is the rail's passenger, not the subject: only whether it is
// in the tree matters here, so its own stores and API client stay out.
vi.mock('@/components/onboarding/ActivationChecklist.vue', () => ({
  default: { name: 'ActivationChecklist', template: '<div data-checklist />' },
}))

import SystemViewsSidebar from '@/components/SystemViewsSidebar.vue'

const KEY = 'trinity-sidebar-collapsed'
let wrapper

function viewport({ phone }) {
  vi.stubGlobal('matchMedia', (query) => ({
    matches: query === '(max-width: 639px)' ? phone : false,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }))
}

async function render() {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(SystemViewsSidebar, {
    global: { plugins: [pinia] },
  })
  await flushPromises()
  return wrapper
}

const collapsed = (w) => w.classes().includes('w-12')
const hasChecklist = (w) => w.find('[data-checklist]').exists()

describe('Systems rail default by width (#4521)', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { wrapper?.unmount(); wrapper = null; vi.unstubAllGlobals(); localStorage.clear() })

  it('starts collapsed on a phone, with the checklist out of the tree', async () => {
    viewport({ phone: true })
    const w = await render()
    expect(collapsed(w)).toBe(true)
    expect(hasChecklist(w)).toBe(false)
    // The default is not a preference: nothing is saved until the person taps.
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('starts open from 640px up, with the checklist shown', async () => {
    viewport({ phone: false })
    const w = await render()
    expect(collapsed(w)).toBe(false)
    expect(w.classes()).toContain('w-56')
    expect(hasChecklist(w)).toBe(true)
  })

  it('a saved open preference wins on a phone', async () => {
    localStorage.setItem(KEY, 'false')
    viewport({ phone: true })
    const w = await render()
    expect(collapsed(w)).toBe(false)
    expect(hasChecklist(w)).toBe(true)
  })

  it('a saved collapsed preference wins on a desktop', async () => {
    localStorage.setItem(KEY, 'true')
    viewport({ phone: false })
    expect(collapsed(await render())).toBe(true)
  })

  it('one tap opens it on a phone and saves that choice', async () => {
    viewport({ phone: true })
    const w = await render()
    await w.find('button[title="Expand sidebar"]').trigger('click')
    expect(collapsed(w)).toBe(false)
    expect(hasChecklist(w)).toBe(true)
    expect(localStorage.getItem(KEY)).toBe('false')
  })

  it('stays open where matchMedia is missing', async () => {
    vi.stubGlobal('matchMedia', undefined)
    expect(collapsed(await render())).toBe(false)
  })
})
