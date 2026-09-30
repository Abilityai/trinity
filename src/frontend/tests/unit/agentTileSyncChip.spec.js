// @vitest-environment jsdom
/**
 * AgentTile's sync chip (trinity-enterprise#707) — MOUNTED.
 *
 * The tile used to carry two sync chips: `sync failing ×N` (only after a failed
 * push, with the raw git error as its tooltip) and a bare `git ✓`. A tile of an
 * agent 15 commits ahead with auto-sync off showed neither. Now it carries ONE
 * chip whose kind follows the backend's `state` (red → crit, yellow → warn,
 * green → calm; nothing for unknown), whose text is the numbers
 * (`↑7 ↓0 · 12 dirty · pushed 3h ago`) and whose tooltip is the reason and the
 * recommendation — never the raw error.
 *
 * What an operator sees is asserted on the rendered chip strip, so an
 * inverted kind map or a chip that silently stopped rendering fails here.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import AgentTile from '../../src/components/AgentTile.vue'
import { useFleetGridStore } from '../../src/stores/fleetGrid'

const NAME = 'tile-agent'
const NOW = Date.parse('2026-09-27T12:00:00Z')
const RAW_ERROR = 'remote: LEAKMARKER fatal: unable to access'

function sync(over = {}) {
  return {
    agent_name: NAME,
    auto_sync_enabled: false,
    last_sync_status: 'success',
    consecutive_failures: 0,
    last_error_summary: null,
    ahead_working: 7,
    behind_working: 0,
    dirty_files: 12,
    last_successful_push_at: '2026-09-27T09:00:00.000000Z',
    state: 'red',
    reason: 'diverged 0 behind / 7 ahead for 26h',
    recommendation: 'enable auto-sync',
    ...over,
  }
}

function mountTile(entry) {
  const grid = useFleetGridStore()
  grid.hydrate = vi.fn() // the tile asks the store to hydrate; no network here
  if (entry) grid.syncHealth = { [NAME]: entry }
  return mount(AgentTile, {
    props: { agent: { name: NAME, status: 'running' }, now: NOW },
    global: {
      stubs: {
        AgentAvatar: true,
        RuntimeBadge: true,
        RunningStateToggle: true,
        AutonomyToggle: true,
        ScanlineReveal: true,
        'router-link': true,
      },
    },
  })
}

const chips = (wrapper) => wrapper.findAll('.t-chips .chip')
const syncChips = (wrapper) => chips(wrapper).filter((c) => c.text().includes('↑'))

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('AgentTile sync chip', () => {
  it.each([
    ['red', 'crit'],
    ['yellow', 'warn'],
    ['green', 'calm'],
  ])('state %s renders one %s chip', async (state, kind) => {
    const wrapper = mountTile(sync({ state }))
    await nextTick()
    const found = syncChips(wrapper)
    expect(found).toHaveLength(1)
    expect(found[0].classes()).toContain(kind)
    // The glyph is its own aria-hidden element (scaled 2x); the chip's flex gap spaces it.
    expect(found[0].find('.chip-icon.chip-icon-x2').text()).toBe('⟳')
    expect(found[0].find('.chip-icon').attributes('aria-hidden')).toBe('true')
    expect(found[0].text()).toBe('⟳↑7 ↓0 · 12 dirty · pushed 3h ago')
  })

  it('the tooltip is the reason and the recommendation, never the raw error', async () => {
    const wrapper = mountTile(sync({
      last_sync_status: 'failed',
      consecutive_failures: 3,
      last_error_summary: RAW_ERROR,
      reason: 'last sync failed (seen on 3 polls)',
      recommendation: 'push via git_sync strategy=pull_first',
    }))
    await nextTick()
    const title = syncChips(wrapper)[0].attributes('title')
    expect(title.split('\n')[0]).toBe(
      'last sync failed (seen on 3 polls) — push via git_sync strategy=pull_first')
    expect(wrapper.html()).not.toContain('LEAKMARKER')
  })

  it('replaces the old chips: no `sync failing ×N`, no `git ✓`', async () => {
    const wrapper = mountTile(sync({
      state: 'red', last_sync_status: 'failed', consecutive_failures: 4,
      auto_sync_enabled: true,
    }))
    await nextTick()
    const text = chips(wrapper).map((c) => c.text()).join(' | ')
    expect(text).not.toMatch(/sync failing/)
    expect(text).not.toMatch(/git ✓/)
    expect(syncChips(wrapper)).toHaveLength(1)
  })

  it('a problem leads the strip; a calm sync fact trails it', async () => {
    const red = mountTile(sync({ state: 'red' }))
    await nextTick()
    const redTexts = chips(red).map((c) => c.text())
    expect(redTexts.findIndex((t) => t.includes('↑'))).toBeLessThan(
      redTexts.findIndex((t) => /schedules/.test(t)))

    setActivePinia(createPinia())
    const green = mountTile(sync({ state: 'green', ahead_working: 0, dirty_files: 0 }))
    await nextTick()
    const greenTexts = chips(green).map((c) => c.text())
    expect(greenTexts[greenTexts.length - 1]).toBe('⟳↑0 ↓0 · pushed 3h ago')
  })

  it('/review I2: a divergence-frozen agent whose last sync SUCCEEDED shows a crit chip', async () => {
    // The old chip needed last_sync_status === 'failed', so an agent frozen for
    // divergence (its pushes never fail — it never pushes) showed nothing in
    // grid mode while the list-mode dot was red.
    const wrapper = mountTile(sync({
      state: 'red', last_sync_status: 'success', consecutive_failures: 0, freeze: true,
    }))
    await nextTick()
    const found = syncChips(wrapper)
    expect(found).toHaveLength(1)
    expect(found[0].classes()).toContain('crit')
    expect(found[0].attributes('title')).toMatch(/^Scheduled runs are paused until it syncs$/m)
  })

  it('an agent that is not frozen does not claim paused schedules', async () => {
    const wrapper = mountTile(sync({ state: 'red', freeze: false }))
    await nextTick()
    expect(syncChips(wrapper)[0].attributes('title')).not.toMatch(/paused/)
  })

  it('no chip for an unknown state or no entry', async () => {
    expect(syncChips(mountTile(sync({ state: 'unknown' })))).toHaveLength(0)
    setActivePinia(createPinia())
    expect(syncChips(mountTile(null))).toHaveLength(0)
  })
})
