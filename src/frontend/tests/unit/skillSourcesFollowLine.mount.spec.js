// @vitest-environment jsdom
/**
 * trinity-enterprise#529 — Settings → Library says when the bundled default
 * source followed the platform's pin.
 *
 * On upgrade the untouched default source moves from its old tag to the
 * platform's bundled pin, and its sync status resets. Without a line saying so,
 * an admin sees a ref they never set and "Never synced" with no explanation.
 * The approved surface (option A of the claim-time mock) is one text-xs line
 * under the row's status line (after the error line when a sync failed): "Followed bundled pin vA → vB on <date>". It
 * shows only the latest follow, and only while the row carries one.
 *
 * Mounted (#2918), on the harness of `librarySkillShadowedTooltip.mount.spec.js`.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))

import SkillSourcesPanel from '../../src/components/SkillSourcesPanel.vue'

const FOLLOWED_AT = '2026-10-29T09:14:00+00:00'

const followed = {
  id: 'src_default', name: 'Trinity Community Skills',
  url: 'github.com/abilityai/trinity-skills', ref: 'v0.5.0', ref_type: 'tag',
  is_default: true, enabled: true, priority: 1000,
  last_sync: null, last_sync_status: 'never', last_error: null, skill_count: 0,
  follows_bundled_pin: true, followed_from_ref: 'v0.4.0', followed_at: FOLLOWED_AT,
}

const custom = {
  id: 'src_custom', name: 'Our skills',
  url: 'https://github.com/example/skills', ref: 'main', ref_type: 'branch',
  is_default: false, enabled: true, priority: 100,
  last_sync: '2026-10-28T08:00:00+00:00', last_sync_status: 'success',
  last_error: null, skill_count: 3,
  follows_bundled_pin: false, followed_from_ref: null, followed_at: null,
}

function serve (sources) {
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/sources') {
      return Promise.resolve({ data: { sources, skill_count: 0, shadowed_count: 0 } })
    }
    return Promise.resolve({ data: {} })
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  api.get.mockReset()
})

async function mountPanel (sources) {
  serve(sources)
  const wrapper = mount(SkillSourcesPanel, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

const rowOf = (w, name) => w.findAll('li').find(li => li.text().includes(name))
const followLine = (row) => row.find('[data-testid="skill-source-followed"]')

describe('ent#529 — the bundled default source shows its latest follow', () => {
  it('says which pins it followed and when', async () => {
    const w = await mountPanel([followed])
    const line = followLine(rowOf(w, 'Trinity Community Skills'))

    expect(line.exists()).toBe(true)
    const date = new Date(FOLLOWED_AT).toLocaleDateString(undefined, {
      day: 'numeric', month: 'short', year: 'numeric',
    })
    expect(line.text()).toBe(`Followed bundled pin v0.4.0 → v0.5.0 on ${date}`)
  })

  it('sits directly under the status line, which reads "Never synced" until the next sync', async () => {
    const w = await mountPanel([followed])
    const lines = rowOf(w, 'Trinity Community Skills').findAll('p')
    const at = lines.findIndex(p => p.attributes('data-testid') === 'skill-source-followed')

    expect(at).toBeGreaterThan(0)
    expect(lines[at - 1].text()).toBe('Never synced')
  })

  it('after a failed sync, follows the error so the two red lines stay together', async () => {
    const w = await mountPanel([{
      ...followed, last_sync_status: 'failed', last_error: "tag 'v0.5.0' not found upstream",
    }])
    const lines = rowOf(w, 'Trinity Community Skills').findAll('p')
    const at = lines.findIndex(p => p.attributes('data-testid') === 'skill-source-followed')

    expect(at).toBeGreaterThan(1)
    expect(lines[at - 2].text()).toBe('Last sync failed')
    expect(lines[at - 1].text()).toBe("tag 'v0.5.0' not found upstream")
    expect(at).toBe(lines.length - 1)
  })

  it('shows nothing for a source that never followed', async () => {
    const w = await mountPanel([
      { ...followed, followed_from_ref: null, followed_at: null },
      custom,
    ])

    expect(followLine(rowOf(w, 'Trinity Community Skills')).exists()).toBe(false)
    expect(followLine(rowOf(w, 'Our skills')).exists()).toBe(false)
  })
})
