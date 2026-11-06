// @vitest-environment jsdom
/**
 * trinity-enterprise#736 + #3136 — MOUNTED, on the real components.
 *
 * ent#736: ent#672 marked a deprecated library skill on the Library card and
 * the Skills tab; four more surfaces show or assign library skills:
 *   1. the skill-set member lists (Library → Skills sets, the agent's sets
 *      dialog) — the same `deprecated` badge and hover text;
 *   2. the set-assign notes — the deprecated member(s) named with the ent#672
 *      sentence, read from the response's member rows so a RE-assign (nothing
 *      added, `delivery: null`) names them too;
 *   3. the skill-runner picker — the option reads "name (deprecated)".
 * #3136: an assign that landed a skill whose declared binary / env key the agent
 * lacks keeps its green delivery note and adds its own amber line naming it —
 * on the Library card and the Skills tab (dialog + the tab's one-line notice),
 * and binaries only on the set notes, whose prerequisites line names env keys.
 *
 * Each site has its own test id, so removing one cannot hide behind another.
 * Mount harness per the #2918 precedent (skillDeprecation.spec.js).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const { api, axiosMock } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  axiosMock: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('axios', () => ({ default: axiosMock }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true, authHeader: {} }),
}))

import SkillsTab from '../../src/components/skills/SkillsTab.vue'
import LibrarySkillsSection from '../../src/components/LibrarySkillsSection.vue'
import SkillRunnerPanel from '../../src/components/settings/SkillRunnerPanel.vue'

const AGENT = 'agent-x'
const LIBRARY = [
  { name: 'old', description: 'retired', deprecated: true, superseded_by: 'backlog' },
  { name: 'live', description: 'fine', deprecated: false, superseded_by: null },
  { name: 'pdf-report', description: 'pdf', deprecated: false, superseded_by: null },
]
const LIB_SETS = [{
  name: 'pack', source_id: 's1', source_name: 'Default', shadowed_by: [], status: 'ok', problems: [],
  members: [
    { name: 'old', present: true, version: 'aaaaaaa111', shadowed_source: null, deprecated: true },
    { name: 'live', present: true, version: 'bbbbbbb222', shadowed_source: null, deprecated: false },
  ],
  requires: { env: [] }, schedules: [],
}]
const member = (name, over = {}) => ({
  name, state: 'assigned', version: 'v', shadowed_source: null,
  deprecated: false, deprecation: null, ...over,
})
const OLD = member('old', { deprecated: true, deprecation: 'deprecated:backlog' })
const AGENT_SETS = [{
  name: 'held', status: 'partial', source_id: 's1', drift: false,
  members: [
    member('live'),
    OLD,
    member('gone', { state: 'not_assigned', deprecated: true, deprecation: 'deprecated' }),
  ],
  prerequisites: { state: 'none', missing_env: [] }, suggested_schedules: [],
}]

function respond({ libSets = LIB_SETS, sets = AGENT_SETS, rows = [] } = {}) {
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, cloned: true, skill_count: LIBRARY.length } })
    if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
    if (url === '/api/skills/library/sets') return Promise.resolve({ data: libSets })
    if (url === `/api/agents/${AGENT}/skills`) return Promise.resolve({ data: rows })
    if (url === `/api/agents/${AGENT}/skill-sets`) return Promise.resolve({ data: sets })
    if (url === '/api/skills/assignments') {
      return Promise.resolve({ data: { assignments: {}, scope: 'all', assignable_agents: [{ name: AGENT, display_label: null }] } })
    }
    return Promise.resolve({ data: [] })
  })
}

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
const AMBER = 'text-status-warning-700'

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of [...Object.values(api), ...Object.values(axiosMock)]) f.mockReset()
  respond()
})

// =============================================================================
// Library → Skills
// =============================================================================

async function mountLibrary({ stubSets = false } = {}) {
  const stubs = { SkillContractChips: true, 'router-link': true }
  if (stubSets) stubs.LibrarySkillSets = true
  const w = mount(LibrarySkillsSection, { attachTo: document.body, global: { stubs } })
  await flushPromises()
  return w
}

async function assignSet(w, response) {
  api.post.mockResolvedValue({ data: response })
  await tid(w, 'library-set-picker-pack').setValue(AGENT)
  await tid(w, 'library-set-assign-pack').trigger('click')
  await flushPromises()
}

describe('Library → Skills — a set with a deprecated member (ent#736)', () => {
  it('badges the deprecated member, with the ent#672 hover text, and only that one', async () => {
    const w = await mountLibrary()
    await tid(w, 'library-set-toggle-pack').trigger('click')
    const badge = tid(w, 'library-set-member-deprecated-pack-old')
    expect(badge.isVisible()).toBe(true)
    expect(badge.text()).toBe('deprecated')
    expect(badge.attributes('title')).toContain('still works')
    expect(tid(w, 'library-set-member-deprecated-pack-live').exists()).toBe(false)
  })

  it('assigning it names the deprecated member and a missing binary, under a green note', async () => {
    const w = await mountLibrary()
    await assignSet(w, {
      set_name: 'pack', members_added: ['old', 'live'],
      delivery: { status: 'injected', skills: {
        old: { status: 'injected', warnings: ['deprecated:backlog', 'missing_binary:jq', 'missing_env:TOKEN'] },
        live: { status: 'injected' },
      } },
      status: { prerequisites: { state: 'missing', missing_env: ['TOKEN'] }, members: [OLD, member('live')] },
    })
    // The existing note still owns the env keys and their next step…
    expect(tid(w, 'library-set-note-pack').text()).toContain('missing credentials TOKEN')
    // …so the dependency line names the binary only.
    const deps = tid(w, 'library-set-dependencies-pack')
    expect(deps.text()).toBe("old needs the binary jq, which this agent doesn't have.")
    expect(deps.classes()).toContain(AMBER)
    expect(tid(w, 'library-set-deprecation-pack').text()).toBe('old is deprecated — superseded by backlog.')
  })

  it('a re-assign that added nothing still names the deprecated member', async () => {
    const w = await mountLibrary()
    await assignSet(w, {
      set_name: 'pack', members_added: [], delivery: null,
      status: { prerequisites: { state: 'none', missing_env: [] }, members: [OLD, member('live')] },
    })
    const note = tid(w, 'library-set-note-pack')
    expect(note.text()).toContain('assigned')
    expect(note.classes()).toContain('text-status-success-700')
    expect(tid(w, 'library-set-deprecation-pack').text()).toBe('old is deprecated — superseded by backlog.')
    expect(tid(w, 'library-set-dependencies-pack').exists()).toBe(false)
  })

  it('a set of live skills adds neither line', async () => {
    const w = await mountLibrary()
    await assignSet(w, {
      set_name: 'pack', members_added: ['live'],
      delivery: { status: 'injected', skills: { live: { status: 'injected' } } },
      status: { prerequisites: { state: 'none', missing_env: [] }, members: [member('live')] },
    })
    expect(tid(w, 'library-set-note-pack').exists()).toBe(true)
    expect(tid(w, 'library-set-deprecation-pack').exists()).toBe(false)
    expect(tid(w, 'library-set-dependencies-pack').exists()).toBe(false)
  })
})

describe('Library → Skills card — a missing dependency (#3136)', () => {
  async function assignFromCard(w, skill) {
    const select = w.find(`#assign-${skill}`)
    await select.setValue(AGENT)
    await select.element.parentElement.querySelector('button').click()
    await flushPromises()
  }

  it('keeps the delivery note green and names what the agent lacks on its own line', async () => {
    api.post.mockResolvedValue({ data: { success: true, delivery: { status: 'injected', skills: {
      'pdf-report': { status: 'injected', warnings: ['missing_binary:pdftotext', 'missing_env:PDF_API_KEY'] },
    } } } })
    const w = await mountLibrary({ stubSets: true })
    await assignFromCard(w, 'pdf-report')

    const note = tid(w, 'skill-delivery-note')
    expect(note.attributes('data-tone')).toBe('ok')
    expect(note.text()).toContain('delivered — available now')
    const deps = tid(w, 'skill-dependency-note')
    expect(deps.text()).toBe(
      "pdf-report needs the binary pdftotext and the environment variable PDF_API_KEY, which this agent doesn't have.")
    expect(deps.classes()).toContain(AMBER)
  })

  it('orders the lines delivery → dependency → deprecation', async () => {
    api.post.mockResolvedValue({ data: { success: true, delivery: { status: 'injected', skills: {
      old: { status: 'injected', warnings: ['deprecated:backlog', 'missing_binary:jq'] },
    } } } })
    const w = await mountLibrary({ stubSets: true })
    await assignFromCard(w, 'old')
    const ids = [...w.element.querySelectorAll('[data-testid]')].map((e) => e.dataset.testid)
      .filter((id) => ['skill-delivery-note', 'skill-dependency-note', 'skill-deprecation-note'].includes(id))
    expect(ids).toEqual(['skill-delivery-note', 'skill-dependency-note', 'skill-deprecation-note'])
  })

  it('a clean delivery adds no dependency line', async () => {
    api.post.mockResolvedValue({ data: { success: true, delivery: { status: 'injected', skills: {
      live: { status: 'injected' },
    } } } })
    const w = await mountLibrary({ stubSets: true })
    await assignFromCard(w, 'live')
    expect(tid(w, 'skill-delivery-note').exists()).toBe(true)
    expect(tid(w, 'skill-dependency-note').exists()).toBe(false)
  })
})

// =============================================================================
// Agent → Skills tab
// =============================================================================

async function mountTab({ openSets = false } = {}) {
  const w = mount(SkillsTab, {
    props: { agentName: AGENT, agentStatus: 'running', canManage: true },
    attachTo: document.body,
    global: { stubs: { SkillContractChips: true, teleport: true, 'router-link': true } },
  })
  await flushPromises()
  await w.find(`[data-testid="${openSets ? 'skills-sets-open' : 'skills-assign-open'}"]`).trigger('click')
  await flushPromises()
  return w
}

describe('Agent → Skills tab — saving a skill that lacks a dependency (#3136)', () => {
  it('the dialog and the tab line both say it, after the green note and before the deprecation', async () => {
    const w = await mountTab()
    api.put.mockResolvedValue({ data: { delivery: { status: 'injected', skills: {
      old: { status: 'injected', warnings: ['deprecated:backlog', 'missing_binary:jq'] },
    } } } })
    await w.find('input[type="checkbox"][value="old"]').setValue(true)
    await w.findAll('button').find((b) => b.text().includes('Save assignments')).trigger('click')
    await flushPromises()

    const sentence = "old needs the binary jq, which this agent doesn't have."
    const deps = w.findAll('[data-testid="skills-saved-dependencies"]')
    expect(deps.length).toBe(2)                              // the dialog's line + the tab's one-line notice
    for (const d of deps) {
      expect(d.text()).toBe(sentence)
      expect(d.classes()).toContain(AMBER)
      const parent = d.element.parentElement
      const order = [...parent.querySelectorAll('[data-testid]')].map((e) => e.dataset.testid)
      expect(order.indexOf('skills-saved-dependencies')).toBeLessThan(order.indexOf('skills-saved-deprecation'))
    }
    for (const n of w.findAll('[data-testid="skills-saved-note"]')) {
      expect(n.attributes('data-tone')).toBe('ok')
    }
    // The tab's line is one truncated row; its hover carries every clause.
    const line = w.findAll('[data-testid="skills-saved-dependencies"]')
      .map((d) => d.element.closest('p')).find((p) => p.getAttribute('title'))
    expect(line.getAttribute('title')).toContain(sentence)
    expect(line.getAttribute('title')).toContain('old is deprecated')
  })
})

describe("Agent → Skills tab — the agent's sets (ent#736)", () => {
  it('badges a deprecated member, assigned or not, and leaves live members unlisted', async () => {
    const w = await mountTab({ openSets: true })
    expect(tid(w, 'set-member-deprecated-held-old').text()).toBe('deprecated')
    expect(tid(w, 'set-member-deprecated-held-old').attributes('title')).toContain('still works')
    // A problem member keeps its state text and gains the badge.
    expect(tid(w, 'set-member-held-gone').text()).toContain('not assigned yet')
    expect(tid(w, 'set-member-deprecated-held-gone').exists()).toBe(true)
    expect(tid(w, 'set-member-held-live').exists()).toBe(false)
  })

  it('assigning a set from the dialog names its deprecated member and a missing binary', async () => {
    respond({ libSets: LIB_SETS, sets: [] })
    const w = await mountTab({ openSets: true })
    api.post.mockResolvedValue({ data: {
      set_name: 'pack', members_added: ['old'],
      delivery: { status: 'injected', skills: { old: { status: 'injected', warnings: ['missing_binary:jq', 'missing_env:K'] } } },
      status: { members: [OLD, member('live')], prerequisites: { state: 'missing', missing_env: ['K'] } },
    } })
    await tid(w, 'set-picker').setValue('pack')
    await tid(w, 'set-assign').trigger('click')
    await flushPromises()

    expect(tid(w, 'set-assigned-note').text()).toContain('Assigned pack')
    expect(tid(w, 'set-assigned-dependencies').text()).toBe("old needs the binary jq, which this agent doesn't have.")
    expect(tid(w, 'set-assigned-deprecation').text()).toBe('old is deprecated — superseded by backlog.')
  })
})

// =============================================================================
// Settings → Skill runner
// =============================================================================

describe('Settings → Skill runner picker (ent#736)', () => {
  it('a deprecated library skill reads "name (deprecated)"; its value stays the name', async () => {
    axiosMock.get.mockImplementation((url) => {
      if (url.endsWith('/status')) {
        return Promise.resolve({ data: { enabled: true, library: { configured: true, synced: true, skill_count: 3 },
          runner_agent: 'skill-runner', runner_exists: true, runner_running: true, exposed_skill_count: 3, grant_count: 0 } })
      }
      if (url.endsWith('/access')) return Promise.resolve({ data: { grants: [] } })
      if (url === '/api/agents') return Promise.resolve({ data: [{ name: 'caller' }] })
      if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
      return Promise.resolve({ data: [] })
    })
    const w = mount(SkillRunnerPanel, { attachTo: document.body })
    await flushPromises()

    const options = w.findAll('option').filter((o) => ['old', 'live', 'pdf-report'].includes(o.element.value))
    expect(options.map((o) => [o.element.value, o.text()])).toEqual([
      ['live', 'live'],
      ['old', 'old (deprecated)'],
      ['pdf-report', 'pdf-report'],
    ])
  })
})
