// @vitest-environment jsdom
/**
 * Systems → manifest preview: the one "after create, also configured" line
 * (trinity-enterprise#1030).
 *
 * The deploy dry run adds `configured` counts to an agent in
 * `agents_to_create` only when that member declares skills, skill sets,
 * gates, grants, a seat or person seats. The preview sums them into one line
 * and renders nothing when no agent carries counts, so every manifest that
 * uses none of the new keys previews exactly as before. Mounted, so the sum,
 * the plurals and the absent case are proven on the rendered component.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

import ManifestPreview from '@/components/systems/ManifestPreview.vue'

const PREVIEW = (agents) => ({
  status: 'valid',
  agents_to_create: agents,
  failed: [],
  warnings: [],
  permission_edges: {},
  schedules_preview: [],
  prompt_updated: false,
})

const LINE = '[data-testid="manifest-configured-line"]'

function render(agents) {
  return mount(ManifestPreview, { props: { preview: PREVIEW(agents) } })
}

describe('ManifestPreview configured line', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('sums every agent\'s counts into one line', () => {
    const w = render([
      { name: 'acme-lead', short_name: 'lead', template: 'local:plain',
        configured: { skills: 2, gates: 1, grants: 1, seat: 1, person_seats: 1 } },
      { name: 'acme-helper', short_name: 'helper', template: 'local:plain',
        configured: { skills: 1, skill_sets: 1, person_seats: 2 } },
    ])
    const lines = w.findAll(LINE)
    expect(lines).toHaveLength(1)
    expect(lines[0].text()).toBe(
      'After create, also configured: 3 skills, 1 skill set, 1 approval gate, 1 admin grant, '
      + '1 agent seat. 3 person seats reported, never filled.')
  })

  it('renders nothing when no agent declares a configuration group', () => {
    const w = render([{ name: 'acme-lead', short_name: 'lead', template: 'local:plain' }])
    expect(w.find(LINE).exists()).toBe(false)
  })

  it('says person seats alone without an empty "configured" clause', () => {
    const w = render([{ name: 'acme-lead', short_name: 'lead', template: 'local:plain',
      configured: { person_seats: 1 } }])
    expect(w.find(LINE).text()).toBe('1 person seat reported, never filled.')
  })
})
