// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#662 — the grant surface beside the edge.
 *
 * A peer may run what an agent declares; what an admin assigned needs a grant.
 * Mounted, not read: the admin gate decides whether a switch that WRITES a
 * grant exists at all, and a regex over the SFC passes an inverted predicate
 * byte-identically (#2756, #2778).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PeerSkillGrants from '../../src/components/PeerSkillGrants.vue'
import { useAgentsStore } from '../../src/stores/agents'
import { useAuthStore } from '../../src/stores/auth'

const VIEW = {
  caller: 'sales',
  targets: [
    {
      target: 'finance',
      not_granted: 1,
      skills: [
        { name: 'pay-invoice', individual: true, assigned_by: 'admin', granted: true, granted_by: 'admin', granted_at: 'x' },
        { name: 'refund', individual: false, assigned_by: 'system', granted: false, granted_by: null, granted_at: null },
      ],
    },
    { target: 'ops', not_granted: 0, skills: [] },
  ],
}

async function mountAs(role, view = VIEW) {
  setActivePinia(createPinia())
  const agents = useAgentsStore()
  const auth = useAuthStore()
  auth.user = { username: 'u', role }
  const get = vi.spyOn(agents, 'getAgentPeerSkills').mockResolvedValue(JSON.parse(JSON.stringify(view)))
  const set = vi.spyOn(agents, 'setAgentPeerSkillGrant').mockResolvedValue({
    changed: true, consent: 'sales may now run the skill refund on finance, with finance\'s identity, credentials and write access.',
  })
  const wrapper = mount(PeerSkillGrants, { props: { agentName: 'sales' } })
  await flushPromises()
  return { wrapper, get, set }
}

describe('PeerSkillGrants (mounted)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('reads the edges of the agent it is mounted for', async () => {
    const { get } = await mountAs('admin')
    expect(get).toHaveBeenCalledWith('sales')
  })

  it('lists only targets that hold library skills, and marks set-propagated ones', async () => {
    const { wrapper } = await mountAs('admin')
    expect(wrapper.text()).toContain('finance')
    expect(wrapper.text()).not.toContain('ops')
    const refund = wrapper.get('[data-testid="peer-skill-finance-refund"]')
    expect(refund.text()).toContain('via skill set')
    expect(wrapper.text()).toContain('1 not granted')
  })

  it('an admin gets a switch per skill, and turning one on writes the grant and shows the consent line', async () => {
    const { wrapper, set } = await mountAs('admin')
    const switches = wrapper.findAll('[role="switch"]')
    expect(switches).toHaveLength(2)
    await wrapper.get('[data-testid="peer-skill-finance-refund"] [role="switch"]').trigger('click')
    await flushPromises()
    expect(set).toHaveBeenCalledWith('sales', 'finance', 'refund', true)
    expect(wrapper.text()).toContain('sales may now run the skill refund on finance')
    expect(wrapper.text()).not.toContain('1 not granted')
  })

  it('a non-admin sees the state and no control that could write a grant', async () => {
    const { wrapper, set } = await mountAs('user')
    expect(wrapper.findAll('[role="switch"]')).toHaveLength(0)
    expect(wrapper.get('[data-testid="peer-skill-finance-pay-invoice"]').text()).toContain('Granted')
    expect(wrapper.get('[data-testid="peer-skill-finance-refund"]').text()).toContain('Not granted')
    expect(set).not.toHaveBeenCalled()
  })

  it('no library skill on any target says so, never an empty list', async () => {
    const { wrapper } = await mountAs('admin', { caller: 'sales', targets: [{ target: 'ops', not_granted: 0, skills: [] }] })
    expect(wrapper.find('[data-testid="peer-skill-grants-empty"]').exists()).toBe(true)
  })

  it('says when a library skill was last used over the edge', async () => {
    const view = JSON.parse(JSON.stringify(VIEW))
    view.targets[0].skills[1].last_used_at = '2026-10-12T00:00:00Z'
    const { wrapper } = await mountAs('admin', view)
    expect(wrapper.get('[data-testid="peer-skill-finance-refund"]').text()).toContain('used')
    expect(wrapper.get('[data-testid="peer-skill-finance-pay-invoice"]').text()).not.toContain('used')
  })

  it('an agent this one spawned runs every skill: no switch, no count', async () => {
    const view = { caller: 'sales', targets: [{ ...JSON.parse(JSON.stringify(VIEW.targets[0])), spawned: true, not_granted: 0 }] }
    const { wrapper } = await mountAs('admin', view)
    expect(wrapper.findAll('[role="switch"]')).toHaveLength(0)
    expect(wrapper.text()).toContain('Spawned by this agent')
  })

  it('a failed read is named, with a retry — not the empty copy', async () => {
    setActivePinia(createPinia())
    const agents = useAgentsStore()
    useAuthStore().user = { role: 'admin' }
    vi.spyOn(agents, 'getAgentPeerSkills').mockRejectedValue(new Error('boom'))
    const wrapper = mount(PeerSkillGrants, { props: { agentName: 'sales' } })
    await flushPromises()
    expect(wrapper.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="peer-skill-grants-empty"]').exists()).toBe(false)
  })
})
