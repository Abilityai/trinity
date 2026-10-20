// @vitest-environment jsdom
/**
 * trinity-enterprise#596 — the Skill managers panel, mounted.
 *
 * The panel is the only place an admin can let an agent change skills, and
 * every agent it does NOT list is refused (its own skills included). So the
 * panel has three jobs a regex over the SFC cannot prove:
 *
 *   1. say plainly who holds it, and — when nobody does — that no agent can
 *      change skills, with the next step;
 *   2. grant and revoke through the real store, with the right request;
 *   3. show the backend's named refusal verbatim (a grant to an ephemeral or
 *      system agent says WHY), never a generic message.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import SkillManagersPanel from '../../src/components/SkillManagersPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'

const HOLDERS = [
  { agent_name: 'trinity-pm', granted_by: 'admin', granted_at: '2026-09-23T10:00:00Z' },
]
const AGENTS = [
  { name: 'trinity-pm', is_system: false },
  { name: 'sales-companion', is_system: false },
  { name: 'trinity-system', is_system: true },
]

async function mountPanel (holders = HOLDERS) {
  api.get.mockResolvedValue({ data: { capability: 'skills.manage', holders } })
  const agents = useAgentsStore()
  agents.agents = AGENTS
  const w = mount(SkillManagersPanel)
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('Skill managers', () => {
  it('lists every holder with who granted it', async () => {
    const w = await mountPanel()
    expect(api.get).toHaveBeenCalledWith('/api/skills/managers')
    const row = w.find('[data-testid="skill-manager-trinity-pm"]')
    expect(row.exists()).toBe(true)
    expect(row.text()).toContain('Granted by admin')
  })

  it('says no agent can change skills when nobody holds it — and what to do', async () => {
    // The day this ships every install is here: orchestrators are refused
    // until granted. A blank list would read as "nothing to worry about".
    const w = await mountPanel([])
    const empty = w.find('[data-testid="skill-managers-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toMatch(/No agent can change skills/)
    expect(empty.text()).toMatch(/grant it below/)
  })

  it('offers only agents that do not hold it, never the system agent', async () => {
    const w = await mountPanel()
    const values = w.findAll('[data-testid="skill-managers-picker"] option').map(o => o.element.value)
    expect(values).toContain('sales-companion')
    expect(values).not.toContain('trinity-pm')     // already a holder
    expect(values).not.toContain('trinity-system') // holds every capability by scope
  })

  it('grants the chosen agent through the store and refreshes', async () => {
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { changed: true } })
    await w.find('select[data-testid="skill-managers-picker"]').setValue('sales-companion')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/agents/sales-companion/skill-manager', { granted: true })
    expect(api.get).toHaveBeenCalledTimes(2)
  })

  it('after a grant the picker shows its placeholder again — not a blank select', async () => {
    // Found by the live screenshot, not by a spec: the granted agent's option is
    // removed while it is still SELECTED, the DOM reports value '' with
    // selectedIndex -1, and resetting the model to '' is then a no-op for Vue
    // (it compares against the DOM's '' and skips). The select renders blank.
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { changed: true } })
    api.get.mockResolvedValue({ data: { capability: 'skills.manage', holders: [
      ...HOLDERS, { agent_name: 'sales-companion', granted_by: 'admin', granted_at: '2026-09-23T11:00:00Z' },
    ] } })
    const select = w.find('select[data-testid="skill-managers-picker"]')
    await select.setValue('sales-companion')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(select.element.selectedIndex).toBe(0)
    expect(select.element.options[select.element.selectedIndex].text).toMatch(/Choose an agent|Every agent/)
  })

  it('revokes a holder', async () => {
    const w = await mountPanel()
    api.put.mockResolvedValue({ data: { changed: true } })
    await w.find('[data-testid="skill-manager-trinity-pm"] button').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/agents/trinity-pm/skill-manager', { granted: false })
  })

  it("shows the backend's named refusal verbatim", async () => {
    const w = await mountPanel()
    api.put.mockRejectedValue({ response: { data: { detail: {
      code: 'ephemeral_agent_not_grantable',
      message: "An ephemeral agent can't hold a capability — it runs a workspace the platform can't vouch for.",
    } } } })
    await w.find('select[data-testid="skill-managers-picker"]').setValue('sales-companion')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(w.find('[data-testid="skill-managers-action-error"]').text()).toMatch(/ephemeral agent can't hold/)
  })

  it('a load failure is not an empty list — it says so and offers a retry', async () => {
    api.get.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    useAgentsStore().agents = AGENTS
    const w = mount(SkillManagersPanel)
    await flushPromises()
    expect(w.find('[data-testid="skill-managers-load-error"]').text()).toContain('boom')
    expect(w.find('[data-testid="skill-managers-empty"]').exists()).toBe(false)
  })
})
