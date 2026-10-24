// @vitest-environment jsdom
/**
 * trinity-enterprise#968 — the Workspace agent band feeds its activity wave the
 * right VERDICTS. Mounted (#2918), with the wave stubbed so what is under test
 * is the band's wiring: which data it passes, when it says "loading", when it
 * says "reveal", and which faces it keeps as sentences.
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

const state = { stats: ref({}), loaded: ref(true), error: ref(null) }
vi.mock('@/composables/usePortalAgentPage', () => ({
  usePortalAgentPage: () => ({
    stats: state.stats,
    ratings: ref({ total: 0, unavailable: false }),
    loaded: state.loaded,
    error: state.error,
    denied: ref(false),
    reload: vi.fn(),
  }),
}))

import PortalAgentBand from '@/components/portal/PortalAgentBand.vue'

const WaveStub = {
  props: ['data', 'buckets', 'colors', 'labels', 'loading', 'reveal', 'height', 'label'],
  template: '<div data-stub-wave :data-loading="String(loading)" :data-reveal="String(reveal)" :data-n="data.length" :data-height="height" :data-label="label" :data-buckets="buckets.join(\'|\')" />',
}

function band({ stats = {}, loaded = true, error = null } = {}) {
  state.stats.value = stats
  state.loaded.value = loaded
  state.error.value = error
  return mount(PortalAgentBand, {
    props: { agentName: 'scout' },
    global: { stubs: { ActivityWave: WaveStub } },
  })
}
const timeline = Array.from({ length: 8 }, (_, i) => ({ date: `2026-10-${15 + i}`, total: i === 4 ? 31 : 0, by_type: i === 4 ? { 'Chat/Tasks': 31 } : {} }))
const BUSY = { total_executions: 31, timeline, buckets: ['Chat/Tasks'] }
const EMPTY = { total_executions: 0, timeline: timeline.map((d) => ({ ...d, total: 0, by_type: {} })), buckets: [] }

describe('ent#968 — the band wires the wave', () => {
  it('passes the timeline, the bucket order and the window label', () => {
    const w = band({ stats: BUSY })
    const wave = w.find('[data-stub-wave]')
    expect(wave.exists()).toBe(true)
    expect(wave.attributes('data-n')).toBe('8')
    expect(wave.attributes('data-buckets')).toBe('Chat/Tasks')
    expect(wave.attributes('data-height')).toBe('23')
    expect(wave.attributes('data-label')).toBe('Activity, last 7 days')
    expect(wave.attributes('data-loading')).toBe('false')
    expect(wave.attributes('data-reveal')).toBe('true')
    w.unmount()
  })

  it('an empty week is the wave’s own face — no sentence — and it settles rather than celebrates', () => {
    const w = band({ stats: EMPTY })
    expect(w.find('[data-stub-wave]').exists()).toBe(true)
    expect(w.find('[data-stub-wave]').attributes('data-reveal')).toBe('false')
    expect(w.text()).not.toMatch(/No activity/)
    w.unmount()
  })

  it('"unavailable" stays a sentence, visually distinct from empty', () => {
    const w = band({ stats: { ...EMPTY, unavailable: true } })
    expect(w.find('[data-stub-wave]').exists()).toBe(false)
    expect(w.text()).toContain('Stats are unavailable right now.')
    w.unmount()
  })

  it('says "loading" only while there is no verdict yet', () => {
    const w = band({ stats: {}, loaded: false })
    expect(w.find('[data-stub-wave]').attributes('data-loading')).toBe('true')
    w.unmount()
  })

  it('a failed first load is "unavailable", never the resting wave claiming "no runs"', () => {
    const w = band({ stats: { total_executions: 0, timeline: [] }, loaded: false, error: 'nope' })
    expect(w.find('[data-stub-wave]').exists()).toBe(false)
    expect(w.text()).toContain('Stats are unavailable right now.')
    expect(w.text()).toContain('nope')
    w.unmount()
  })

  it('no longer wraps the chart in the scanline primitive', () => {
    const w = band({ stats: BUSY })
    // rendered classes, not comment text — the band's own comments name the primitive
    expect(w.html()).not.toMatch(/class="[^"]*scan-(track|beam|content)/)
    w.unmount()
  })
})
