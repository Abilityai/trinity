// @vitest-environment jsdom
/**
 * trinity-enterprise#926 — the owner's switch for channel and public visitors.
 *
 * `visitor_shell` lives in the same guardrails blob the PUT replaces whole, so
 * the toggle must save exactly what is stored plus its own key: never an
 * unsaved edit in the max-turns fields, and never drop a key it does not own.
 * Mounted, not grepped (#2918): the switch gates a security-relevant write.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const getGuardrails = vi.fn()
const setGuardrails = vi.fn()

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({ getGuardrails, setGuardrails }),
}))

// eslint-disable-next-line import/first
import GuardrailsPanel from '../../src/components/GuardrailsPanel.vue'
// eslint-disable-next-line import/first
import BaseToggle from '../../src/components/base/BaseToggle.vue'

async function open(guardrails) {
  getGuardrails.mockResolvedValue({ guardrails })
  const notify = vi.fn()
  const w = mount(GuardrailsPanel, { props: { agentName: 'atlas', notify } })
  await flushPromises()
  return { w, notify, toggle: () => w.findComponent(BaseToggle) }
}

beforeEach(() => {
  getGuardrails.mockReset()
  setGuardrails.mockReset()
})

describe('the visitor shell switch', () => {
  it('reads absent as on (the default keeps channel agents working)', async () => {
    const { toggle } = await open({})
    expect(toggle().props('modelValue')).toBe(true)
  })

  it('reads a stored false as off', async () => {
    const { toggle } = await open({ visitor_shell: false })
    expect(toggle().props('modelValue')).toBe(false)
  })

  it('saves what is stored plus its key, never an unsaved field edit', async () => {
    setGuardrails.mockResolvedValue({})
    const { w, toggle } = await open({ max_turns_chat: 10, execution_timeout_sec: 900 })
    await w.find('#gr-max-turns-chat').setValue(99)     // typed, not saved
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(setGuardrails).toHaveBeenCalledWith('atlas', {
      max_turns_chat: 10, execution_timeout_sec: 900, visitor_shell: false,
    })
    expect(toggle().props('modelValue')).toBe(false)
  })

  it('a later Save keeps the switch as set', async () => {
    setGuardrails.mockResolvedValue({})
    const { w, toggle } = await open({})
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    getGuardrails.mockResolvedValue({ guardrails: { visitor_shell: false, max_turns_chat: 20 } })
    await w.find('#gr-max-turns-chat').setValue(20)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(setGuardrails).toHaveBeenLastCalledWith('atlas', { visitor_shell: false, max_turns_chat: 20 })
  })

  it('a failed save puts the switch back and says why', async () => {
    setGuardrails.mockRejectedValue({ response: { data: { detail: 'visitor_shell must be true or false' } } })
    const { w, toggle, notify } = await open({})
    toggle().vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(toggle().props('modelValue')).toBe(true)
    expect(w.text()).toContain('visitor_shell must be true or false')
    expect(notify).toHaveBeenCalledWith('visitor_shell must be true or false', 'error')
  })
})
