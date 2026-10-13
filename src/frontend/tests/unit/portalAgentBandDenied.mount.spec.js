// @vitest-environment jsdom
/**
 * #3140 — the agent band offers "Try again" only when trying again can help.
 * A 403/404 is the server refusing this viewer the agent; a retry gets the same
 * answer, so the band says what happened and offers nothing to click.
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ref } from 'vue'

const state = { denied: ref(false), error: ref(null), loaded: ref(false) }
vi.mock('@/composables/usePortalAgentPage', () => ({
  usePortalAgentPage: () => ({
    stats: ref({}), ratings: ref({ total: 0, unavailable: false }),
    loaded: state.loaded, error: state.error, denied: state.denied, reload: vi.fn(),
  }),
}))

import PortalAgentBand from '@/components/portal/PortalAgentBand.vue'

function band({ denied, loaded = false }) {
  state.denied.value = denied
  state.loaded.value = loaded
  state.error.value = denied ? "You don't have access to this agent." : "Couldn't load this agent right now."
  return mount(PortalAgentBand, {
    props: { agentName: 'stranger' },
    global: { stubs: { StackedBarChart: true, ScanlineReveal: { template: '<div><slot /></div>' } } },
  })
}

const retry = (w) => w.findAll('button').filter((b) => /try again/i.test(b.text()))

describe('#3140 — PortalAgentBand', () => {
  it.each([[false], [true]])('refused (loaded=%s): the message, and no retry', (loaded) => {
    const w = band({ denied: true, loaded })
    expect(w.text()).toContain("You don't have access to this agent.")
    expect(retry(w)).toHaveLength(0)
  })

  it('a failure retrying can fix keeps its retry', () => {
    expect(retry(band({ denied: false }))).toHaveLength(1)
  })
})
