// @vitest-environment jsdom
/**
 * ent#1173 (AC-4) — PortalWorkCard shows item.agent_display_label in the
 * agent attribution line, falling back to item.agent_name when absent.
 * MOUNTED: the v-if gate on the agent line can only be exercised by mounting.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalWorkCard from '@/components/portal/PortalWorkCard.vue'

const item = (over = {}) => ({
  id: 'x1',
  agent_name: 'corbin',
  status: 'done',
  outcome: 'success',
  kind: 'turn',
  title: 'Help with something',
  steps: 2,
  ...over,
})

function agentLine(w) {
  // The agent attribution paragraph: shown when showAgent && item.agent_name
  return w.find('p.truncate')
}

describe('PortalWorkCard — agent_display_label (ent#1173 AC-4)', () => {
  it('shows agent_display_label when present', () => {
    const w = mount(PortalWorkCard, {
      props: { item: item({ agent_display_label: 'Corbin AI' }), showAgent: true },
    })
    expect(agentLine(w).text()).toBe('Corbin AI')
    w.unmount()
  })

  it('falls back to agent_name when agent_display_label is absent', () => {
    const w = mount(PortalWorkCard, {
      props: { item: item({ agent_display_label: undefined }), showAgent: true },
    })
    expect(agentLine(w).text()).toBe('corbin')
    w.unmount()
  })

  it('falls back to agent_name when agent_display_label is empty string', () => {
    const w = mount(PortalWorkCard, {
      props: { item: item({ agent_display_label: '' }), showAgent: true },
    })
    expect(agentLine(w).text()).toBe('corbin')
    w.unmount()
  })

  it('agent line is not rendered when showAgent is false', () => {
    const w = mount(PortalWorkCard, {
      props: { item: item({ agent_display_label: 'Corbin AI' }), showAgent: false },
    })
    expect(agentLine(w).exists()).toBe(false)
    w.unmount()
  })
})
