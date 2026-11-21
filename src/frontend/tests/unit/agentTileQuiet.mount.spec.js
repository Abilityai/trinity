// @vitest-environment jsdom
/**
 * AgentTile with nothing to chart — MOUNTED.
 *
 * A new agent's tile used to show two flat lines and "No tasks (24h)". It now
 * draws the outline of the charts to come (dashed, never filled, so it cannot
 * be read as data) and says in words that nothing has run. As soon as there is
 * a run, the real chart takes its place.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import AgentTile from '../../src/components/AgentTile.vue'
import { useFleetGridStore } from '../../src/stores/fleetGrid'
import { useExecutionsStore } from '../../src/stores/executions'

const NAME = 'new-agent'
const NOW = Date.parse('2026-11-19T12:00:00Z')
const DAY = (total) => ({ date: '2026-11-18', by_type: total ? { Schedules: total } : {} })

function mountTile(analytics, created = '2026-11-19T09:00:00Z') {
  const grid = useFleetGridStore()
  grid.hydrate = vi.fn()
  if (analytics) {
    useExecutionsStore().analyticsCache = { [`${NAME}:14d`]: analytics }
    grid.analyticsState = { [NAME]: 'done' }
  }
  return mount(AgentTile, {
    props: { agent: { name: NAME, status: 'running', created }, now: NOW },
    global: { stubs: {
      AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true, AutonomyToggle: true, 'router-link': true,
      ScanlineReveal: { template: '<div><slot /></div>' },
    } },
  })
}

beforeEach(() => setActivePinia(createPinia()))

describe('AgentTile with nothing to chart', () => {
  it('draws the outline of both charts and says it is waiting for its first run', () => {
    const wrapper = mountTile({ total_executions: 0, timeline: [DAY(0), DAY(0)] })
    expect(wrapper.findAll('[data-testid="tile-ghost-activity"] .gb')).toHaveLength(14)
    expect(wrapper.find('[data-testid="tile-ghost-context"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="tile-quiet"]').text()).toBe('Waiting for its first run')
    expect(wrapper.find('.stack .col i:not(.gb)').exists()).toBe(false)   // no bar that looks like a run
  })

  it('does not call an old idle agent new', () => {
    const wrapper = mountTile({ total_executions: 0, timeline: [] }, '2026-06-01T09:00:00Z')
    expect(wrapper.get('[data-testid="tile-quiet"]').text()).toBe('No runs in 14 days')
  })

  it('shows the real chart as soon as there is a run', () => {
    const wrapper = mountTile({ total_executions: 3, timeline: [DAY(0), DAY(3)] })
    expect(wrapper.find('[data-testid="tile-ghost-activity"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="tile-quiet"]').exists()).toBe(false)
    expect(wrapper.find('.stack:not(.ghost) .col i:not(.stub)').exists()).toBe(true)
  })

  it('draws no outline while the charts are still loading', () => {
    const wrapper = mountTile(null)
    expect(wrapper.find('[data-testid="tile-ghost-activity"]').exists()).toBe(false)
  })
})
