// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#925 — the Permissions panel wires the "not
 * enforced in-container" line under the peer skill grants, for the agent the
 * panel is open on, with its status (start/stop re-reads the in-agent check).
 *
 * `PeerSkillEnforcementLine.spec.js` proves the line; this proves the hand-off
 * line in `PermissionsPanel.vue` exists, so deleting it goes red.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PermissionsPanel from '../../src/components/PermissionsPanel.vue'
import { useAgentsStore } from '../../src/stores/agents'

async function mountPanel() {
  setActivePinia(createPinia())
  const agents = useAgentsStore()
  vi.spyOn(agents, 'getAgentPermissions').mockResolvedValue({
    available_agents: [{ name: 'billing', permitted: true, status: 'running' }],
  })
  vi.spyOn(agents, 'getAgentPeerSkills').mockResolvedValue({ caller: 'finance', targets: [] })
  const enforcement = vi.spyOn(agents, 'getAgentPeerSkillEnforcement')
    .mockResolvedValue({ reachable: true, hook: 'missing' })
  const wrapper = mount(PermissionsPanel, { props: { agentName: 'finance', agentStatus: 'running' } })
  await flushPromises()
  return { wrapper, enforcement }
}

describe('PermissionsPanel → PeerSkillEnforcementLine (mounted)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('renders the line for the agent the panel is open on, below the grants', async () => {
    const { wrapper, enforcement } = await mountPanel()
    expect(enforcement).toHaveBeenCalledWith('finance')
    const grants = wrapper.find('[data-testid="peer-skill-grants"]')
    const line = wrapper.find('[data-testid="peer-skill-enforcement"]')
    expect(grants.exists()).toBe(true)
    expect(line.exists()).toBe(true)
    expect(grants.element.compareDocumentPosition(line.element) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('passes the agent status through, so a start or stop reads the check again', async () => {
    const { wrapper, enforcement } = await mountPanel()
    await wrapper.setProps({ agentStatus: 'stopped' })
    await flushPromises()
    expect(enforcement).toHaveBeenCalledTimes(2)
  })
})
