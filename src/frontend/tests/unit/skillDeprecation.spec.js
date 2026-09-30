// @vitest-environment jsdom
/**
 * trinity-enterprise#672 — a deprecated library skill says so, MOUNTED.
 *
 * The library marks a retired skill `deprecated: true` and may say what
 * supersedes it. The platform dropped both keys, so the skill rendered and
 * assigned exactly like a live one. What this pins, on the real components:
 *
 *   1. the marker is on EVERY surface that lists a library skill — the Library
 *      card, the Skills tab's assigned row (an agent that already holds one)
 *      and the Skills tab's picker (before the tick) — and on none of them for
 *      a live skill. Each site has its own test id, so removing one site
 *      cannot hide behind another;
 *   2. the successor line shows the author's text as written, prose included,
 *      and is absent when none was declared;
 *   3. assigning one SUCCEEDS and says so next to the control that did it, on
 *      a delivery outcome that never ran an injection (a stopped agent);
 *   4. the note belongs to the save it describes — it goes when that note goes.
 *
 * Mount harness per the #2918 precedent (skillsPanelDraftSurvivesSync.spec.js).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true }),
}))

import SkillsPanel from '../../src/components/SkillsPanel.vue'
import LibrarySkillsSection from '../../src/components/LibrarySkillsSection.vue'
import { deprecationText } from '../../src/utils/skillDelivery'

const AGENT = 'agent-x'
// Synthetic stand-ins for the two shapes a library ships: a bare skill name,
// and a sentence saying the successor lives somewhere else.
const PROSE = 'other-skill — another catalog (not reachable from this one)'
const LIBRARY = [
  { name: 'add-backlog', description: 'old', deprecated: true, superseded_by: 'backlog' },
  { name: 'add-canon', description: 'prose', deprecated: true, superseded_by: PROSE },
  { name: 'retired', description: 'no successor', deprecated: true, superseded_by: null },
  { name: 'research', description: 'live', deprecated: false, superseded_by: null },
]

function respond({ assigned = [] } = {}) {
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') {
      return Promise.resolve({ data: { configured: true, cloned: true, skill_count: LIBRARY.length } })
    }
    if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
    if (url === '/api/skills/assignments') {
      return Promise.resolve({
        data: {
          assignments: {},
          scope: 'all',
          assignable_agents: [{ name: 'scout', display_label: null }, { name: 'scribe', display_label: null }],
        },
      })
    }
    if (url === `/api/agents/${AGENT}/skills`) return Promise.resolve({ data: assigned })
    return Promise.resolve({ data: [] })
  })
}

const tid = (wrapper, id) => wrapper.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  api.get.mockReset(); api.post.mockReset(); api.put.mockReset(); api.delete.mockReset()
  respond()
})

describe('Library → Skills card', () => {
  async function mountLibrary() {
    const wrapper = mount(LibrarySkillsSection, {
      attachTo: document.body,
      // AssignedAgents is deliberately REAL: the post-assign note lives in it.
      global: { stubs: { 'router-link': true, LibrarySkillSets: true } },
    })
    await flushPromises()
    return wrapper
  }

  it('marks a deprecated card and names its successor', async () => {
    const wrapper = await mountLibrary()
    const badge = tid(wrapper, 'skill-deprecated-library-add-backlog')
    expect(badge.text()).toBe('deprecated')
    expect(badge.attributes('title')).toContain('still works')      // what it means for a holder
    expect(tid(wrapper, 'skill-superseded-library-add-backlog').text()).toBe('Superseded by backlog')
  })

  it('shows a prose successor as the author wrote it, in full on hover', async () => {
    const wrapper = await mountLibrary()
    const line = tid(wrapper, 'skill-superseded-library-add-canon')
    expect(line.text()).toBe(`Superseded by ${PROSE}`)
    expect(line.attributes('title')).toBe(`Superseded by ${PROSE}`)
  })

  it('marks a deprecated card with no declared successor, without a successor line', async () => {
    const wrapper = await mountLibrary()
    expect(tid(wrapper, 'skill-deprecated-library-retired').exists()).toBe(true)
    expect(tid(wrapper, 'skill-superseded-library-retired').exists()).toBe(false)
  })

  it('leaves a live card unmarked, and every card in the grid', async () => {
    const wrapper = await mountLibrary()
    expect(tid(wrapper, 'skill-deprecated-library-research').exists()).toBe(false)
    expect(tid(wrapper, 'skill-superseded-library-research').exists()).toBe(false)
    for (const s of LIBRARY) expect(wrapper.text()).toContain(s.name)
  })

  async function assignFromCard(wrapper, skill, agent = 'scout') {
    const select = wrapper.find(`#assign-${skill}`)
    await select.setValue(agent)
    await select.element.parentElement.querySelector('button').click()
    await flushPromises()
  }

  it('assigning a deprecated skill succeeds and says so beside the control', async () => {
    // A stopped agent: no injection ran, so the notice cannot come from one.
    api.post.mockResolvedValue({
      data: {
        success: true,
        delivery: {
          status: 'pending_start',
          skills: { 'add-backlog': { status: 'pending_start', warnings: ['deprecated:backlog'] } },
        },
      },
    })
    const wrapper = await mountLibrary()

    await assignFromCard(wrapper, 'add-backlog')

    expect(api.post.mock.calls[0][0]).toBe('/api/agents/scout/skills/add-backlog')
    expect(tid(wrapper, 'skill-delivery-note').text()).toContain('applies on next start')
    expect(tid(wrapper, 'skill-deprecation-note').text())
      .toBe('add-backlog is deprecated — superseded by backlog.')
  })

  it('a later failed assign on the same card shows the failure, not the earlier note', async () => {
    api.post.mockResolvedValueOnce({
      data: {
        success: true,
        delivery: {
          status: 'injected',
          skills: { 'add-backlog': { status: 'injected', warnings: ['deprecated:backlog'] } },
        },
      },
    })
    const wrapper = await mountLibrary()
    await assignFromCard(wrapper, 'add-backlog', 'scout')
    expect(tid(wrapper, 'skill-deprecation-note').exists()).toBe(true)

    api.post.mockRejectedValueOnce({ response: { data: { detail: 'Agent not found' } } })
    await assignFromCard(wrapper, 'add-backlog', 'scribe')

    expect(wrapper.text()).toContain('Agent not found')
    expect(tid(wrapper, 'skill-deprecation-note').exists()).toBe(false)
  })

  it('assigning a live skill says nothing about deprecation', async () => {
    api.post.mockResolvedValue({
      data: { success: true, delivery: { status: 'injected', skills: { research: { status: 'injected' } } } },
    })
    const wrapper = await mountLibrary()

    await assignFromCard(wrapper, 'research')

    expect(tid(wrapper, 'skill-delivery-note').exists()).toBe(true)
    expect(tid(wrapper, 'skill-deprecation-note').exists()).toBe(false)
  })
})

describe('Agent → Skills tab', () => {
  async function mountPanel(assigned) {
    respond({ assigned })
    const wrapper = mount(SkillsPanel, {
      props: { agentName: AGENT, canManage: true, agentRunning: true },
      attachTo: document.body,
      global: { stubs: { AgentSkillSets: true } },
    })
    await flushPromises()
    return wrapper
  }
  const held = [{ skill_name: 'add-backlog', delivery_status: null }]

  it('an agent already holding a deprecated skill shows it on the assigned row', async () => {
    const wrapper = await mountPanel(held)
    const badge = tid(wrapper, 'skill-deprecated-assigned-add-backlog')
    expect(badge.text()).toBe('deprecated')
    expect(badge.attributes('title')).toContain('still works')
    expect(tid(wrapper, 'skill-superseded-assigned-add-backlog').text()).toBe('Superseded by backlog')
    // Not assigned → no assigned-row marker, whatever the picker shows.
    expect(tid(wrapper, 'skill-deprecated-assigned-add-canon').exists()).toBe(false)
  })

  it('the picker marks a deprecated skill before it is ticked, and only those', async () => {
    const wrapper = await mountPanel(held)
    expect(tid(wrapper, 'skill-deprecated-picker-add-canon').text()).toBe('deprecated')
    expect(tid(wrapper, 'skill-deprecated-picker-add-canon').attributes('title')).toContain('still works')
    expect(tid(wrapper, 'skill-superseded-picker-add-canon').text()).toBe(`Superseded by ${PROSE}`)
    expect(tid(wrapper, 'skill-deprecated-picker-retired').exists()).toBe(true)
    expect(tid(wrapper, 'skill-superseded-picker-retired').exists()).toBe(false)
    expect(tid(wrapper, 'skill-deprecated-picker-research').exists()).toBe(false)
  })

  async function saveWith(wrapper, name, delivery) {
    api.put.mockResolvedValue({ data: { delivery } })
    await wrapper.find(`input[type="checkbox"][value="${name}"]`).setValue(true)
    const save = wrapper.findAll('button').find((b) => b.text().includes('Save assignments'))
    await save.trigger('click')
    await flushPromises()
  }

  it('saving a draft that adds a deprecated skill says so under the save note', async () => {
    const wrapper = await mountPanel(held)

    await saveWith(wrapper, 'add-canon', {
      status: 'injected',
      skills: { 'add-canon': { status: 'injected', warnings: ['deprecated'] } },
    })

    expect(api.put.mock.calls[0][1]).toEqual({ skills: ['add-backlog', 'add-canon'] })
    expect(tid(wrapper, 'skills-saved-note').text()).toContain('delivered')
    expect(tid(wrapper, 'skills-saved-deprecation').text()).toBe('add-canon is deprecated.')
  })

  it('saving a live skill adds no deprecation note', async () => {
    const wrapper = await mountPanel(held)
    await saveWith(wrapper, 'research', {
      status: 'injected', skills: { research: { status: 'injected' } },
    })
    expect(tid(wrapper, 'skills-saved-note').exists()).toBe(true)
    expect(tid(wrapper, 'skills-saved-deprecation').exists()).toBe(false)
  })

  async function sync(wrapper, results) {
    api.post.mockResolvedValue({ data: { success: true, results } })
    await wrapper.findAll('button').find((b) => b.text().includes('Sync now')).trigger('click')
    await flushPromises()
  }

  it('the note goes when the save note it belongs to goes', async () => {
    const wrapper = await mountPanel(held)
    await saveWith(wrapper, 'add-canon', {
      status: 'injected',
      skills: { 'add-canon': { status: 'injected', warnings: ['deprecated'] } },
    })
    expect(tid(wrapper, 'skills-saved-deprecation').exists()).toBe(true)

    await sync(wrapper, {})

    expect(tid(wrapper, 'skills-saved-note').exists()).toBe(false)
    expect(tid(wrapper, 'skills-saved-deprecation').exists()).toBe(false)
  })

  it('a later unassign note does not inherit the previous save\'s deprecation line', async () => {
    // The conflict row's own verb writes a save note without going through a
    // save — it must not drag the earlier save's line along.
    const wrapper = await mountPanel([{ skill_name: 'add-backlog', delivery_status: 'conflict' }])
    await saveWith(wrapper, 'add-canon', {
      status: 'injected',
      skills: { 'add-canon': { status: 'injected', warnings: ['deprecated'] } },
    })
    expect(tid(wrapper, 'skills-saved-deprecation').exists()).toBe(true)

    api.put.mockResolvedValue({ data: { delivery: null } })
    await tid(wrapper, 'skill-conflict-unassign').trigger('click')
    await flushPromises()

    expect(tid(wrapper, 'skills-saved-note').text()).toContain('Unassigned add-backlog')
    expect(tid(wrapper, 'skills-saved-deprecation').exists()).toBe(false)
  })

  it('a manual sync does not repeat the deprecation as a delivery warning', async () => {
    // The per-run list is for what went wrong with the delivery. A retired
    // skill that landed fine is not that; the badge on the row already says it.
    const wrapper = await mountPanel(held)

    await sync(wrapper, {
      'add-backlog': {
        success: true, status: 'unchanged', warnings: ['deprecated:backlog', 'missing_env:GH_TOKEN'],
      },
    })

    expect(wrapper.text()).toContain('GH_TOKEN is not set')            // a real warning still shows
    expect(wrapper.text()).not.toContain('deprecated:backlog')
    expect(tid(wrapper, 'skill-deprecated-assigned-add-backlog').exists()).toBe(true)
  })
})

describe('deprecationText', () => {
  it('is null when there is nothing to say', () => {
    expect(deprecationText(null)).toBeNull()
    expect(deprecationText({ status: 'injected' })).toBeNull()
    expect(deprecationText({ status: 'injected', skills: { research: { status: 'injected' } } })).toBeNull()
    expect(deprecationText({
      status: 'injected', skills: { research: { status: 'injected', warnings: ['missing_env:X'] } },
    })).toBeNull()
  })

  it('names the skill, and the successor only when the code carries one', () => {
    expect(deprecationText({
      skills: { 'add-backlog': { status: 'pending_start', warnings: ['deprecated:backlog'] } },
    })).toBe('add-backlog is deprecated — superseded by backlog.')
    expect(deprecationText({
      skills: { retired: { status: 'injected', warnings: ['deprecated'] } },
    })).toBe('retired is deprecated.')
  })

  it('names every deprecated skill in a bulk save, and only those', () => {
    expect(deprecationText({
      status: 'injected',
      skills: {
        'add-backlog': { status: 'injected', warnings: ['deprecated:backlog'] },
        research: { status: 'injected' },
        retired: { status: 'injected', warnings: ['deprecated'] },
      },
    })).toBe('add-backlog is deprecated — superseded by backlog; retired is deprecated.')
  })
})
