// @vitest-environment jsdom
/**
 * The Dashboard list's Read-only toggle is the shortcut to the Strict
 * read-only tool-access profile (trinity-enterprise#341), MOUNTED:
 *
 *   - it shows ON for Strict read-only only — an agent on Read-only + report
 *     has the read-only layer on and still reads OFF (ruling 5: one
 *     mechanism, no contradictory state);
 *   - a click goes through the agents store (`PUT /read-only`, never a raw
 *     axios call in the component), adopts the profile the backend answers,
 *     and reports the restart the tool list now waits for.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const http = vi.hoisted(() => ({ put: null }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(() => Promise.resolve({ data: {} })),
    post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn((...args) => http.put(...args)),
    delete: vi.fn(() => Promise.resolve({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import AgentListPanel from '../../src/components/AgentListPanel.vue'
import { useAgentRestartStore } from '../../src/stores/agentRestart'
import { useAgentsStore } from '../../src/stores/agents'

const STUBS = {
  AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true, AutonomyToggle: true,
  CapacityMeter: true, RouterLink: true,
}

let wrapper
beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  http.put = vi.fn(async () => ({
    data: { profile: 'strict_read_only', profile_label: 'Strict read-only', read_only_layer: true,
      read_only_applied: true, restart_required: true },
  }))
})
afterEach(() => wrapper?.unmount())

function mountList(agent) {
  wrapper = mount(AgentListPanel, {
    props: { agents: [{ name: 'alpha', status: 'running', runtime: 'claude-code', tags: [], is_owner: true, ...agent }] },
    global: { stubs: STUBS },
  })
  return wrapper
}

const toggle = () => wrapper.find('[data-agent="alpha"] [role="switch"]')

describe('the list Read-only toggle', () => {
  it('reads OFF on Read-only + report although the read-only layer is on', () => {
    mountList({ tool_access_profile: 'read_only_report', read_only_enabled: true })
    expect(toggle().attributes('aria-checked')).toBe('false')
  })

  it('reads ON on Strict read-only', () => {
    mountList({ tool_access_profile: 'strict_read_only', read_only_enabled: true })
    expect(toggle().attributes('aria-checked')).toBe('true')
  })

  it('a click selects Strict through the store and reports the waiting restart', async () => {
    const viaStore = vi.spyOn(useAgentsStore(), 'setReadOnly')
    mountList({ tool_access_profile: 'workspace_only', read_only_enabled: false })
    await toggle().trigger('click')
    await flushPromises()
    expect(viaStore).toHaveBeenCalledWith('alpha', true)
    expect(axios.put).toHaveBeenCalledWith('/api/agents/alpha/read-only', { enabled: true }, expect.anything())
    expect(toggle().attributes('aria-checked')).toBe('true')
    expect(useAgentRestartStore().pendingFor('alpha')).toEqual([{ key: 'tool-access', label: 'Tool access' }])
  })

  it('OFF from Strict asks to leave it', async () => {
    http.put = vi.fn(async () => ({
      data: { profile: 'workspace_only', profile_label: 'Workspace-only', read_only_layer: false,
        read_only_applied: true, restart_required: false },
    }))
    mountList({ tool_access_profile: 'strict_read_only', read_only_enabled: true })
    await toggle().trigger('click')
    await flushPromises()
    expect(axios.put).toHaveBeenCalledWith('/api/agents/alpha/read-only', { enabled: false }, expect.anything())
    expect(toggle().attributes('aria-checked')).toBe('false')
  })
})
