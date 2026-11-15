// @vitest-environment jsdom
/**
 * Settings › Guardrails › Tool access, MOUNTED (trinity-enterprise#341):
 *
 *   - the five cards come from the backend (label, statement, per-runtime
 *     coverage) and the current one is marked;
 *   - choosing a profile and saving sends exactly that profile, reports the
 *     restart the save left waiting, and toasts the approved sentence;
 *   - "Customize" opens Custom pre-filled from that profile; a list the
 *     backend would refuse is a named error and never reaches the store;
 *   - a derived Strict read-only whose send block is not applied offers Apply;
 *   - a failed save keeps the selection and says why; a failed load offers retry;
 *   - the system agent's profile is shown, not editable.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const store = vi.hoisted(() => ({
  getToolAccess: vi.fn(),
  setToolAccess: vi.fn(),
}))
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => store }))

import ToolAccessSection from '../../src/components/agent-settings/ToolAccessSection.vue'
import { useAgentRestartStore } from '../../src/stores/agentRestart'

const SEND = ['send_message', 'send_group_message', 'send_notification', 'send_voice_reply',
  'call_user', 'post_to_room', 'share_file'].map((t) => `mcp__trinity__${t}`)
const STRICT = [...SEND, 'mcp__trinity__report']

function cards(custom = { read_only: false, disallowed_tools: [] }) {
  return [
    { id: 'full', label: 'Full access', statement: 'Uses every tool.', read_only: false, disallowed_tools: [], coverage: '' },
    { id: 'workspace_only', label: 'Workspace-only', statement: "Can't send messages.", read_only: false, disallowed_tools: SEND, coverage: 'Sends are refused by Trinity on every runtime.' },
    { id: 'read_only_report', label: 'Read-only + report', statement: 'Reads everything.', read_only: true, disallowed_tools: SEND, coverage: 'Sends are refused by Trinity on every runtime.' },
    { id: 'strict_read_only', label: 'Strict read-only', statement: 'Reads and answers.', read_only: true, disallowed_tools: STRICT, coverage: "Read-only: the file tools can't write code or config files; shell commands aren't covered." },
    { id: 'custom', label: 'Custom', statement: 'You choose. Common ones: Bash, Write, Edit, WebFetch, WebSearch.', read_only: custom.read_only, disallowed_tools: custom.disallowed_tools, coverage: '' },
  ]
}

function view(profile = 'full', extra = {}) {
  const custom = extra.custom || { read_only: false, disallowed_tools: [] }
  const labels = { full: 'Full access', workspace_only: 'Workspace-only', read_only_report: 'Read-only + report', strict_read_only: 'Strict read-only', custom: 'Custom' }
  return {
    agent_name: 'scout', profile, profile_label: labels[profile], derived: false, applied: true,
    read_only: false, disallowed_tools: [], custom, before_strict: null, runtime: 'claude-code',
    profiles: cards(custom), restart_required: false, ...extra,
  }
}

let wrapper
let notify

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  notify = vi.fn()
  store.getToolAccess.mockResolvedValue(view('full'))
})
afterEach(() => wrapper?.unmount())

async function mountIt(props = {}) {
  // Attached: jsdom submits only a form that is connected to the document.
  wrapper = mount(ToolAccessSection, { props: { agentName: 'scout', notify, ...props }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

const card = (id) => wrapper.find(`[data-testid="tool-access-${id}"]`)
const radio = (id) => wrapper.find(`input[type="radio"][value="${id}"]`)
const saveButton = () => wrapper.find('[data-testid="tool-access-save"]')

describe('Tool access', () => {
  it('renders the five cards from the backend and marks the current one', async () => {
    await mountIt()
    for (const id of ['full', 'workspace_only', 'read_only_report', 'strict_read_only', 'custom']) {
      expect(card(id).exists()).toBe(true)
    }
    expect(card('full').text()).toContain('Current')
    expect(card('workspace_only').text()).not.toContain('Current')
    expect(card('strict_read_only').text()).toContain("shell commands aren't covered")
    expect(radio('full').element.checked).toBe(true)
    expect(saveButton().attributes('disabled')).toBeDefined()  // nothing changed yet
  })

  it('shows a skeleton until the first load answers', async () => {
    let resolve
    store.getToolAccess.mockReturnValue(new Promise((r) => { resolve = r }))
    wrapper = mount(ToolAccessSection, { props: { agentName: 'scout', notify } })
    await flushPromises()
    expect(wrapper.find('[aria-busy="true"]').exists()).toBe(true)
    resolve(view('full'))
    await flushPromises()
    expect(wrapper.find('[aria-busy="true"]').exists()).toBe(false)
  })

  it('saves the chosen profile, reports the waiting restart and toasts', async () => {
    store.setToolAccess.mockResolvedValue(view('workspace_only', { disallowed_tools: SEND, restart_required: true }))
    await mountIt()
    await radio('workspace_only').setValue(true)
    await saveButton().trigger('click')
    await flushPromises()
    expect(store.setToolAccess).toHaveBeenCalledWith('scout', { profile: 'workspace_only' })
    expect(useAgentRestartStore().pendingFor('scout')).toEqual([{ key: 'tool-access', label: 'Tool access' }])
    expect(notify).toHaveBeenCalledWith('Workspace-only saved. The tool list applies after a restart.', 'success')
    expect(wrapper.emitted('saved')).toHaveLength(1)
    expect(card('workspace_only').text()).toContain('Current')
  })

  it('Customize opens Custom pre-filled from that profile and saves it as Custom', async () => {
    store.setToolAccess.mockResolvedValue(view('custom', { custom: { read_only: true, disallowed_tools: STRICT } }))
    await mountIt()
    await card('strict_read_only').find('[data-testid="tool-access-customize"]').trigger('click')
    expect(radio('custom').element.checked).toBe(true)
    const textarea = wrapper.find('[data-testid="tool-access-custom"] textarea')
    expect(textarea.element.value.split('\n')).toEqual(STRICT)
    expect(wrapper.find('[data-testid="tool-access-custom-read-only"]').element.checked).toBe(true)
    await textarea.setValue([...STRICT, 'Bash'].join('\n'))
    await saveButton().trigger('click')
    await flushPromises()
    expect(store.setToolAccess).toHaveBeenCalledWith('scout', {
      profile: 'custom', custom: { read_only: true, disallowed_tools: [...STRICT, 'Bash'] },
    })
  })

  it('a list the backend would refuse is a named error and never reaches the store', async () => {
    await mountIt()
    await radio('custom').setValue(true)
    await wrapper.find('[data-testid="tool-access-custom"] textarea')
      .setValue(Array.from({ length: 51 }, (_, i) => `T${i}`).join('\n'))
    await saveButton().trigger('click')
    await flushPromises()
    expect(store.setToolAccess).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('at most 50')
  })

  it('offers Apply when the current profile is not applied', async () => {
    store.getToolAccess.mockResolvedValue(view('strict_read_only', { derived: true, applied: false, read_only: true }))
    store.setToolAccess.mockResolvedValue(view('strict_read_only', { read_only: true, disallowed_tools: STRICT, restart_required: true }))
    await mountIt()
    expect(card('strict_read_only').text()).toContain('Send block not applied')
    await card('strict_read_only').find('[data-testid="tool-access-apply"]').trigger('click')
    await flushPromises()
    expect(store.setToolAccess).toHaveBeenCalledWith('scout', { profile: 'strict_read_only' })
    expect(card('strict_read_only').text()).not.toContain('not applied')
  })

  it('a failed save keeps the selection and says why', async () => {
    store.setToolAccess.mockRejectedValue({ response: { data: { detail: 'This agent\'s tool access changed while you were editing. Reload and try again.' } } })
    await mountIt()
    await radio('workspace_only').setValue(true)
    await saveButton().trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('changed while you were editing')
    expect(radio('workspace_only').element.checked).toBe(true)
    expect(card('full').text()).toContain('Current')
    expect(notify).not.toHaveBeenCalled()
  })

  it('a failed load offers a retry, never an empty set of cards', async () => {
    store.getToolAccess.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
    await mountIt()
    expect(wrapper.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(card('full').exists()).toBe(false)
    store.getToolAccess.mockResolvedValue(view('full'))
    await wrapper.find('[data-testid="load-failed"] button').trigger('click')
    await flushPromises()
    expect(card('full').exists()).toBe(true)
  })

  it('reports a restart that was already waiting when it loads', async () => {
    store.getToolAccess.mockResolvedValue(view('workspace_only', { restart_required: true }))
    await mountIt()
    expect(useAgentRestartStore().pendingFor('scout')).toEqual([{ key: 'tool-access', label: 'Tool access' }])
  })

  it('re-reads when the header toggle changes the profile on the page', async () => {
    await mountIt({ currentProfile: 'full' })
    expect(store.getToolAccess).toHaveBeenCalledTimes(1)
    store.getToolAccess.mockResolvedValue(view('strict_read_only', { read_only: true }))
    await wrapper.setProps({ currentProfile: 'strict_read_only' })
    await flushPromises()
    expect(store.getToolAccess).toHaveBeenCalledTimes(2)
    expect(card('strict_read_only').text()).toContain('Current')
  })

  it('the system agent is shown, not editable', async () => {
    await mountIt({ isSystem: true })
    expect(wrapper.text()).toContain('managed by the platform')
    expect(radio('workspace_only').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="tool-access-customize"]').exists()).toBe(false)
  })
})
