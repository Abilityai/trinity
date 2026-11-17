// @vitest-environment jsdom
/**
 * ent#1173 (AC-4) — PortalBriefing shows agent.display_label (trimmed) in
 * the h1 heading, falling back to agent.name when absent or blank.
 * MOUNTED: this is a rendered output, so only a mount proves it.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalBriefing from '@/components/portal/PortalBriefing.vue'

const agent = (over = {}) => ({
  name: 'corbin',
  avatar_url: null,
  playbooks: [],
  ...over,
})

function heading(w) {
  return w.get('h1').text()
}

describe('PortalBriefing — display_label (ent#1173 AC-4)', () => {
  it('shows display_label when present', () => {
    const w = mount(PortalBriefing, {
      props: { agent: agent({ display_label: 'Corbin AI' }) },
    })
    expect(heading(w)).toBe('Corbin AI')
    w.unmount()
  })

  it('trims surrounding whitespace from display_label', () => {
    const w = mount(PortalBriefing, {
      props: { agent: agent({ display_label: '  Hub Agent  ' }) },
    })
    expect(heading(w)).toBe('Hub Agent')
    w.unmount()
  })

  it('falls back to agent.name when display_label is absent', () => {
    const w = mount(PortalBriefing, {
      props: { agent: agent({ display_label: undefined }) },
    })
    expect(heading(w)).toBe('corbin')
    w.unmount()
  })

  it('falls back to agent.name when display_label is blank', () => {
    const w = mount(PortalBriefing, {
      props: { agent: agent({ display_label: '   ' }) },
    })
    expect(heading(w)).toBe('corbin')
    w.unmount()
  })
})
