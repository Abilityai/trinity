// @vitest-environment jsdom
/**
 * Settings → Usage sharing, the header sentence (abilityai/trinity-enterprise#1403), mounted.
 *
 * Accepting terms at setup that say accepting them switches usage sharing on
 * now does so. The panel then opens with sharing ON although the operator never
 * touched this switch, so the header must not say "nothing is shared until you
 * turn this on". It names both ways sharing comes on, and holds for an instance
 * that is off as well as one the terms switched on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import TelemetrySharingPanel from '@/components/settings/TelemetrySharingPanel.vue'

let w
async function render(status) {
  api.get.mockImplementation(async () => ({ data: { recent_sends: [], ...status } }))
  w = mount(TelemetrySharingPanel, { attachTo: document.body })
  await flushPromises()
  return w
}
const header = () => w.find('h2').element.parentElement.querySelector('p').textContent.replace(/\s+/g, ' ')

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks() })
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('TelemetrySharingPanel header', () => {
  it.each([
    ['switched on by accepting terms', { enabled: true, consent_at: '2026-11-27T10:00:00Z', sharing_id: 'abc' }],
    ['off', { enabled: false }],
  ])('is true for an instance %s', async (_label, status) => {
    await render(status)
    expect(w.find('[role="switch"]').attributes('aria-checked')).toBe(String(status.enabled))
    const text = header()
    expect(text).not.toMatch(/nothing is shared until you turn this on/i)
    expect(text).not.toMatch(/Off by default/)
    expect(text).toMatch(/accept terms at setup that say accepting them switches it on/)
    expect(text).toMatch(/switch it off here any time/)
  })
})
