// @vitest-environment jsdom
/**
 * trinity-enterprise#584 — "No description yet" is one empty state, rendered
 * by every surface that lists capabilities, MOUNTED (#2918).
 *
 * The rules are executed in capabilityDescriptions.spec.js; this proves each
 * component renders them: the Workspace rail (both tiers and the unreachable
 * agent), the Workspace new-chat hints, the Skills tab card and its details
 * dialog, the connector allow-list picker, and Settings › General › Technical
 * details (template tags, declared skills).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const { pageState, api, agentsStore } = vi.hoisted(() => ({
  pageState: { capabilities: [], capabilitiesState: 'ready' },
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  agentsStore: { getAgentInfo: vi.fn() },
}))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn(), role: 'admin' }),
}))
vi.mock('axios', () => {
  const inst = () => ({
    get: vi.fn(async () => ({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(inst(), { create: inst }) }
})
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => agentsStore }))
vi.mock('@/composables/usePortalAgentPage', async () => {
  const { computed, ref } = await vi.importActual('vue')
  return {
    usePortalAgentPage: () => ({
      page: ref({}),
      loading: ref(false),
      loaded: ref(true),
      error: ref(''),
      denied: ref(false),
      reload: vi.fn(),
      header: computed(() => ({ name: 'scout', description: null })),
      capabilities: computed(() => pageState.capabilities),
      capabilitiesState: computed(() => pageState.capabilitiesState),
      stats: computed(() => ({ total_executions: 0, timeline: [] })),
      ratings: computed(() => ({ up: 0, down: 0, total: 0, unavailable: false })),
      recentWork: computed(() => []),
    }),
  }
})

import PortalAgentDetails from '@/components/portal/PortalAgentDetails.vue'
import PortalBriefing from '@/components/portal/PortalBriefing.vue'
import SkillCard from '@/components/skills/SkillCard.vue'
import SkillDetailsModal from '@/components/skills/SkillDetailsModal.vue'
import ExposedToolsPanel from '@/components/ExposedToolsPanel.vue'
import InfoPanel from '@/components/InfoPanel.vue'
import LibrarySkillsSection from '@/components/LibrarySkillsSection.vue'
import SkillAssignModal from '@/components/skills/SkillAssignModal.vue'
import { useSkillsStore } from '@/stores/skills'
import { useSkillGatesStore } from '@/stores/skillGates'
import { DOMWrapper } from '@vue/test-utils'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
window.matchMedia = window.matchMedia || ((q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} }))

const pb = (title, description, kind = 'playbook') => ({ title, description, starter_prompt: `/${title} `, kind })

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { template: '<div />' } }] })
  await router.push('/')
  await router.isReady()
  pageState.capabilities = []
  pageState.capabilitiesState = 'ready'
  api.get.mockReset()
})

// ---------------------------------------------------------------------------
// The Workspace rail's Info tab
// ---------------------------------------------------------------------------

async function rail() {
  const w = shallowMount(PortalAgentDetails, {
    props: { agentName: 'scout', agent: { name: 'scout' }, threads: [] },
    global: { plugins: [router] },
  })
  await flushPromises()
  const section = w.findAll('section').find((s) => /What it can do|What you can ask/.test(s.find('h2').text()))
  return { w, section }
}

describe('the Workspace rail', () => {
  it('lists playbooks under "What it can do", the undescribed one saying so', async () => {
    pageState.capabilities = [pb('Weekly report', 'Writes the weekly report.'), pb('Quiet one', null)]
    const { section } = await rail()

    expect(section.find('h2').text()).toBe('What it can do')
    const cards = section.findAll('button')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toContain('Writes the weekly report.')
    const empty = cards[1].findAll('div')[1]
    expect(empty.text()).toBe('No description yet')
    expect(empty.classes()).toContain('italic')
    expect(cards[0].findAll('div')[1].classes()).not.toContain('italic')
  })

  it('a click still pre-fills the starter, never sends', async () => {
    pageState.capabilities = [pb('Weekly report', null)]
    const { w, section } = await rail()
    await section.find('button').trigger('click')
    expect(w.emitted('use-playbook')).toEqual([['/Weekly report ']])
  })

  it('lists use cases under "What you can ask", with no description line', async () => {
    pageState.capabilities = [pb('Summarise this week', null, 'use_case'), pb('Draft a reply', null, 'use_case')]
    const { section } = await rail()

    expect(section.find('h2').text()).toBe('What you can ask')
    expect(section.text()).not.toContain('No description yet')
    expect(section.findAll('button').map((b) => b.text())).toEqual(['Summarise this week', 'Draft a reply'])
  })

  it('an agent that never answered is not an agent with nothing published', async () => {
    pageState.capabilitiesState = 'unavailable'
    const { section } = await rail()
    expect(section.text()).toContain("Couldn't load what this agent can do right now.")
    expect(section.text()).not.toContain("hasn't published anything")
  })

  it('an agent that answered with nothing says so', async () => {
    const { section } = await rail()
    expect(section.text()).toContain("This agent hasn't published anything it can do yet.")
  })
})

// ---------------------------------------------------------------------------
// The Workspace new-chat hints
// ---------------------------------------------------------------------------

describe('the Workspace new-chat hints', () => {
  const briefing = (playbooks) => mount(PortalBriefing, {
    props: { agent: { name: 'scout', briefing_state: 'ready', playbooks } },
  })

  it('a playbook without a description says so; one with keeps it', () => {
    const w = briefing([pb('Weekly report', 'Writes it.'), pb('Quiet one', '  ')])
    const cards = w.findAll('#portal-briefing .grid > button')
    expect(cards[0].text()).toContain('Writes it.')
    expect(cards[1].text()).toContain('No description yet')
    expect(cards[1].find('.italic').text()).toBe('No description yet')
  })

  it('a use case has no description line', () => {
    const w = briefing([pb('Summarise this week', null, 'use_case')])
    expect(w.text()).toContain('Summarise this week')
    expect(w.text()).not.toContain('No description yet')
  })
})

// ---------------------------------------------------------------------------
// The Skills tab card and its details dialog
// ---------------------------------------------------------------------------

const card = (over = {}) => ({
  id: 'own:weekly-report', section: 'own', name: 'weekly-report', dir: 'weekly', description: null,
  argumentHint: null, mode: null, badges: [], note: null, gate: null, gateLine: null,
  run: { show: false }, canEditRun: false,
  controls: { approval: false, toggleDisabled: null, unassign: { show: false, disabled: true, title: '' }, clear: false },
  ...over,
})

describe('the Skills tab', () => {
  it('a card without a description tells the author where to write it', () => {
    const w = mount(SkillCard, { props: { card: card(), canRun: false } })
    const p = w.findAll('p').find((x) => x.text().startsWith('No description yet'))
    expect(p.text()).toBe('No description yet. Add description: to its SKILL.md.')
    expect(p.find('code').text()).toBe('description:')
    expect(p.classes()).toContain('italic')
    expect(p.attributes('title')).toBe('Add a description: line to the frontmatter of .claude/skills/weekly/SKILL.md')
  })

  it('a card with a description shows it, unmuted', () => {
    const w = mount(SkillCard, { props: { card: card({ description: 'Writes the weekly report.' }), canRun: false } })
    const p = w.findAll('p').find((x) => x.text() === 'Writes the weekly report.')
    expect(p.classes()).not.toContain('italic')
    expect(w.text()).not.toContain('No description yet')
  })

  it('the details dialog says the same', () => {
    const w = mount(SkillDetailsModal, {
      props: { modelValue: true, card: card({ section: 'shared', dir: 'weekly-report' }) },
      global: { stubs: { teleport: true } },
    })
    expect(w.text()).toContain('No description yet. Add description: to its SKILL.md.')
  })
})

// ---------------------------------------------------------------------------
// The connector allow-list picker
// ---------------------------------------------------------------------------

describe('the connector picker', () => {
  it('a playbook without a description says so', async () => {
    api.get.mockImplementation(async (url) => {
      if (url.endsWith('/connector')) return { data: { exposed_playbooks: null } }
      return { data: { skills: [
        { name: 'weekly-report', description: 'Writes it.', user_invocable: true },
        { name: 'quiet', description: null, user_invocable: true },
      ] } }
    })
    const w = mount(ExposedToolsPanel, { props: { agentName: 'scout' } })
    await flushPromises()
    const rows = w.findAll('li')
    expect(rows[0].text()).toContain('— Writes it.')
    expect(rows[1].text()).toContain('— No description yet')
  })
})

// ---------------------------------------------------------------------------
// Settings › General › Technical details
// ---------------------------------------------------------------------------

describe('Technical details', () => {
  async function info(payload) {
    agentsStore.getAgentInfo.mockResolvedValue({ has_template: true, name: 'scout', ...payload })
    const w = mount(InfoPanel, { props: { agentName: 'scout', agentStatus: 'running' } })
    await flushPromises()
    return w
  }

  it('shows capabilities: as raw template tags, never as capabilities', async () => {
    const w = await info({ capabilities: ['brain-orb', 'lead_triage', { name: 'obj-tag', description: 'x' }] })
    const block = w.find('[data-testid="info-template-tags"]')
    expect(block.exists()).toBe(true)
    expect(block.find('h3').text()).toBe('Template tags')
    expect(block.findAll('[data-testid="info-template-tag"]').map((t) => t.text()))
      .toEqual(['brain-orb', 'lead_triage', 'obj-tag'])
    expect(block.text()).toContain("What each skill does comes from its SKILL.md description:.")
    expect(w.findAll('h3').map((h) => h.text())).not.toContain('Capabilities')
  })

  it('lists declared skills by name only, and points at the Skills tab', async () => {
    const w = await info({ skills: [{ name: 'weekly-report', description: 'legacy template text' }, 'plain'] })
    const block = w.find('[data-testid="info-declared-skills"]')
    expect(block.text()).toContain('weekly-report')
    expect(block.text()).toContain('plain')
    expect(block.text()).not.toContain('legacy template text')
    expect(block.text()).toContain('What each skill does is on the Skills tab, from its SKILL.md.')
  })
})

// ---------------------------------------------------------------------------
// The Library and its assign dialog
// ---------------------------------------------------------------------------

const LIB = [
  { name: 'described', description: 'Does the thing.', source_id: 's', source_name: 'Team', category: null },
  { name: 'quiet', description: null, source_id: 's', source_name: 'Team', category: null },
]

function libraryAnswers(agent = 'agent-x') {
  api.get.mockImplementation(async (url) => {
    if (url === '/api/skills/library/status') return { data: { configured: true, cloned: true, skill_count: LIB.length } }
    if (url === '/api/skills/library') return { data: LIB }
    if (url === '/api/skills/assignments') return { data: { assignments: {}, scope: 'all', assignable_agents: [] } }
    if (url === `/api/agents/${agent}/skill-gates`) return { data: { agent_name: agent, gates: [], approvers: [] } }
    return { data: [] }
  })
}

describe('the Library', () => {
  it('a skill without a description says so on its card', async () => {
    libraryAnswers()
    const w = mount(LibrarySkillsSection, {
      attachTo: document.body,
      global: { stubs: { 'router-link': true, LibrarySkillSets: { template: '<section />' }, AssignedAgents: true } },
    })
    await flushPromises()
    expect(w.find('[data-testid="library-skill-described"]').text()).toContain('Does the thing.')
    const quiet = w.find('[data-testid="library-skill-quiet"]')
    const line = quiet.findAll('p').find((p) => p.text() === 'No description yet')
    expect(line).toBeTruthy()
    expect(line.classes()).toContain('italic')
    w.unmount()
  })

  it('the assign dialog says the same', async () => {
    libraryAnswers()
    await useSkillsStore().load('agent-x')
    await useSkillGatesStore().load('agent-x')
    const w = mount(SkillAssignModal, {
      props: { modelValue: true },
      attachTo: document.body,
      global: { stubs: { SkillContractChips: true } },
    })
    await flushPromises()
    const rows = new DOMWrapper(document.body).findAll('[data-testid="skill-assign-list"] li')
    const quiet = rows.find((r) => r.text().includes('quiet'))
    expect(quiet.text()).toContain('No description yet')
    expect(rows.find((r) => r.text().includes('described')).text()).toContain('Does the thing.')
    w.unmount()
  })
})
