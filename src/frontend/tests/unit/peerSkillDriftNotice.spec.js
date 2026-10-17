// @vitest-environment jsdom
/**
 * abilityai/trinity-enterprise#662 — the honest upgrade, in Settings.
 *
 * Every agent-to-agent call that used a library skill in the last 30 days and
 * is now refused is listed for an admin, with a one-click grant. Mounted: the
 * grant button is a store write, and a regex over the SFC cannot prove a click.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PeerSkillDriftNotice from '../../src/components/PeerSkillDriftNotice.vue'
import { useAgentsStore } from '../../src/stores/agents'

const REPORT = {
  window_days: 30,
  pairs: [
    { caller: 'sales', target: 'finance', skill: 'pay-invoice', uses: 3, last_used_at: '2026-10-12T00:00:00Z' },
    { caller: 'ops', target: 'finance', skill: 'refund', uses: 1, last_used_at: '2026-10-11T00:00:00Z' },
  ],
}

async function mountWith(report) {
  setActivePinia(createPinia())
  const agents = useAgentsStore()
  const get = vi.spyOn(agents, 'getPeerSkillDrift').mockResolvedValue(JSON.parse(JSON.stringify(report)))
  const set = vi.spyOn(agents, 'setAgentPeerSkillGrant').mockResolvedValue({ changed: true, consent: 'sales may now run the skill pay-invoice on finance.' })
  const wrapper = mount(PeerSkillDriftNotice)
  await flushPromises()
  return { wrapper, get, set }
}

describe('PeerSkillDriftNotice (mounted)', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('lists every refused pair the fleet used, with its count', async () => {
    const { wrapper, get } = await mountWith(REPORT)
    expect(get).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="peer-skill-drift"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('2 agent-to-agent skill calls')
    const row = wrapper.get('[data-testid="drift-sales-finance-pay-invoice"]')
    expect(row.text()).toContain('sales')
    expect(row.text()).toContain('finance')
    expect(row.text()).toContain('3')
  })

  it('Grant writes the grant for exactly that pair and drops the row', async () => {
    const { wrapper, set } = await mountWith(REPORT)
    await wrapper.get('[data-testid="drift-sales-finance-pay-invoice"] button').trigger('click')
    await flushPromises()
    expect(set).toHaveBeenCalledWith('sales', 'finance', 'pay-invoice', true)
    expect(wrapper.find('[data-testid="drift-sales-finance-pay-invoice"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('sales may now run the skill pay-invoice on finance.')
  })

  it('nothing to report renders nothing', async () => {
    const { wrapper } = await mountWith({ window_days: 30, pairs: [] })
    expect(wrapper.find('[data-testid="peer-skill-drift"]').exists()).toBe(false)
  })
})
