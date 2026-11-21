// @vitest-environment jsdom
/**
 * trinity-enterprise#1318 — the owner's switch for the agent's primary person.
 *
 * A person seated as primary has the agent work in full for them (commands,
 * its own playbooks and tools, standing work). The owner can turn that off per
 * agent, beside the visitor switch; default on. Seats come from the assignments
 * module, so without it the switch is not shown. `primary_reach` lives in the
 * same guardrails blob the PUT replaces whole, so the toggle saves exactly what
 * is stored plus its own key. Mounted, not grepped (#2918).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const getGuardrails = vi.fn()
const setGuardrails = vi.fn()
const entitled = { value: true }

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({ getGuardrails, setGuardrails }),
}))
vi.mock('../../src/stores/enterprise', () => ({
  useEnterpriseStore: () => ({
    isEntitled: (f) => entitled.value && f === 'assignments',
    loadFeatureFlags: vi.fn().mockResolvedValue(undefined),
  }),
}))

// eslint-disable-next-line import/first
import GuardrailsPanel from '../../src/components/GuardrailsPanel.vue'
// eslint-disable-next-line import/first
import BaseToggle from '../../src/components/base/BaseToggle.vue'

async function open(guardrails) {
  getGuardrails.mockResolvedValue({ guardrails })
  const notify = vi.fn()
  const w = mount(GuardrailsPanel, { props: { agentName: 'finance', notify } })
  await flushPromises()
  const section = () => w.find('[data-testid="guardrails-primary-reach"]')
  const toggle = () => section().findComponent(BaseToggle)
  return { w, notify, section, toggle }
}

beforeEach(() => {
  getGuardrails.mockReset()
  setGuardrails.mockReset()
  entitled.value = true
})

describe('the primary person switch (ent#1318)', () => {
  it('is not shown without the assignments module', async () => {
    entitled.value = false
    const { section } = await open({})
    expect(section().exists()).toBe(false)
  })

  it('reads absent as on', async () => {
    const { toggle, section } = await open({})
    expect(toggle().props('modelValue')).toBe(true)
    expect(section().text()).toContain('primary')
  })

  it('reads a stored false as off', async () => {
    const { toggle } = await open({ primary_reach: false })
    expect(toggle().props('modelValue')).toBe(false)
  })

  it('saves what is stored plus its key, never an unsaved field edit', async () => {
    setGuardrails.mockResolvedValue({})
    const { w, toggle } = await open({ max_turns_chat: 10, visitor_shell: false })
    await w.find('#gr-max-turns-chat').setValue(99)
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(setGuardrails).toHaveBeenCalledWith('finance', {
      max_turns_chat: 10, visitor_shell: false, primary_reach: false,
    })
    expect(toggle().props('modelValue')).toBe(false)
  })

  it('and the visitor switch keeps it', async () => {
    setGuardrails.mockResolvedValue({})
    const { w, toggle } = await open({})
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    w.find('[data-testid="guardrails-visitor-shell"]').findComponent(BaseToggle)
      .vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(setGuardrails).toHaveBeenLastCalledWith('finance', { primary_reach: false, visitor_shell: false })
  })

  it('a failed save puts the switch back and says why', async () => {
    setGuardrails.mockRejectedValue({ response: { data: { detail: 'primary_reach must be true or false' } } })
    const { w, toggle, notify } = await open({})
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(toggle().props('modelValue')).toBe(true)
    expect(w.text()).toContain('primary_reach must be true or false')
    expect(notify).toHaveBeenCalledWith('primary_reach must be true or false', 'error')
  })
})
