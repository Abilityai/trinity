// @vitest-environment jsdom
/**
 * The two editors in Settings › Guardrails share one stored row
 * (trinity-enterprise#341), MOUNTED:
 *
 *   - once a tool-access profile is stored, the rules editor leaves the profile
 *     and its tool list out of its whole-config save (the backend keeps them),
 *     so a copy taken when the editor opened can never write back an older
 *     list — while an agent with no profile keeps echoing its list exactly as
 *     before;
 *   - a Tool access save makes the pane re-read the rules;
 *   - the store actions address the tool-access and read-only routes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'

const store = vi.hoisted(() => ({
  getGuardrails: vi.fn(),
  setGuardrails: vi.fn(),
}))
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => store }))

import GuardrailsPanel from '../../src/components/GuardrailsPanel.vue'
import GuardrailsPane from '../../src/components/agent-settings/GuardrailsPane.vue'

const SEND = ['mcp__trinity__send_message', 'mcp__trinity__share_file']

let wrapper
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store.setGuardrails.mockResolvedValue({})
})
afterEach(() => wrapper?.unmount())

async function saveTurnCaps(stored) {
  store.getGuardrails.mockResolvedValue({ guardrails: stored })
  wrapper = mount(GuardrailsPanel, { props: { agentName: 'scout', embedded: true }, attachTo: document.body })
  await flushPromises()
  await wrapper.find('#gr-max-turns-chat').setValue(42)
  await wrapper.find('form').trigger('submit')
  await flushPromises()
  return store.setGuardrails.mock.calls[0][1]
}

describe('the rules editor and a stored profile', () => {
  it('leaves the profile and its tool list out once a profile is stored', async () => {
    const sent = await saveTurnCaps({
      max_turns_chat: 5, extra_bash_deny: ['curl'],
      disallowed_tools: SEND, tool_access: { profile: 'workspace_only' },
    })
    expect(sent).toEqual({ max_turns_chat: 42, extra_bash_deny: ['curl'] })
  })

  it('an agent with no profile keeps echoing its list, as before', async () => {
    const sent = await saveTurnCaps({ max_turns_chat: 5, disallowed_tools: ['Bash'] })
    expect(sent).toEqual({ max_turns_chat: 42, disallowed_tools: ['Bash'] })
  })
})

describe('the visitor-shell toggle and a stored profile (ent#926 × #341)', () => {
  async function flipVisitorShell(stored) {
    store.getGuardrails.mockResolvedValue({ guardrails: stored })
    wrapper = mount(GuardrailsPanel, { props: { agentName: 'scout', embedded: true }, attachTo: document.body })
    await flushPromises()
    await wrapper.find('[data-testid="guardrails-visitor-shell"] [role="switch"]').trigger('click')
    await flushPromises()
    return store.setGuardrails.mock.calls[0][1]
  }

  it('leaves the profile and its tool list out once a profile is stored', async () => {
    const sent = await flipVisitorShell({
      max_turns_chat: 5, disallowed_tools: SEND, tool_access: { profile: 'workspace_only' },
    })
    expect(sent).toEqual({ max_turns_chat: 5, visitor_shell: false })
  })

  it('an agent with no profile keeps its list, as before', async () => {
    const sent = await flipVisitorShell({ disallowed_tools: ['Bash'] })
    expect(sent).toEqual({ disallowed_tools: ['Bash'], visitor_shell: false })
  })
})

describe('the Guardrails pane', () => {
  it('re-reads the rules after a Tool access save', async () => {
    store.getGuardrails.mockResolvedValue({ guardrails: {} })
    const ToolAccessStub = defineComponent({
      name: 'ToolAccessSection',
      emits: ['saved'],
      setup(_, { emit }) {
        return () => h('button', { 'data-testid': 'ta-stub', onClick: () => emit('saved', {}) })
      },
    })
    wrapper = mount(GuardrailsPane, {
      props: { agentName: 'scout' },
      global: {
        stubs: {
          ToolAccessSection: ToolAccessStub, SelfChangePermissionsPanel: true, CrossModelValidationPanel: true,
          ReliabilityPanel: true, RetrievalMemoryToggle: true, RetrievalMemorySearch: true,
        },
      },
    })
    await flushPromises()
    const before = store.getGuardrails.mock.calls.length
    await wrapper.find('[data-testid="ta-stub"]').trigger('click')
    await flushPromises()
    expect(store.getGuardrails.mock.calls.length).toBe(before + 1)
    // ...and tells the page, whose header toggle reads the profile off the agent.
    expect(wrapper.emitted('tool-access-saved')).toEqual([[{}]])
  })
})
