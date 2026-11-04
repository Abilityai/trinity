// @vitest-environment jsdom
/**
 * The agent Settings home, MOUNTED (trinity-enterprise#887, #353; #2918):
 *
 *   - the rail moves between panes through the URL (`?section=`), and an old
 *     `?tab=info` link lands on General;
 *   - the ONE restart banner names every waiting change and its button asks
 *     for the restart;
 *   - the execution-timeout row (ent#353) refuses out-of-range input with a
 *     named error, saves minutes as seconds, and turns #929's
 *     `agent_timeout_below_active_schedules` refusal into a sentence plus a
 *     way to the Schedules tab.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { h } from 'vue'

const store = vi.hoisted(() => ({
  getAgentInfo: vi.fn(),
  getAgentTimeout: vi.fn(),
  setAgentTimeout: vi.fn(),
  getAgentCapacity: vi.fn(),
  setAgentCapacity: vi.fn(),
  getAgentModel: vi.fn(),
  setAgentModel: vi.fn(),
  resetToMainPreserveState: vi.fn(),
  getGitConfig: vi.fn(),
}))
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => store }))

import AgentSettingsHome from '../../src/components/agent-settings/AgentSettingsHome.vue'
import SettingsRestartBanner from '../../src/components/agent-settings/SettingsRestartBanner.vue'
import GeneralPane from '../../src/components/agent-settings/GeneralPane.vue'
import { settingsSections } from '../../src/utils/agentSettings'
import { useAgentRestartStore } from '../../src/stores/agentRestart'

const Blank = { render: () => h('div') }
let wrapper
let pinia

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  vi.clearAllMocks()
  store.getAgentInfo.mockResolvedValue({ description: 'Keeps the books.', template_name: 'github:Org/books' })
  store.getAgentTimeout.mockResolvedValue({ execution_timeout_seconds: 3600 })
  store.getAgentCapacity.mockResolvedValue({ max_parallel_tasks: 3, effective_max_parallel_tasks: 3, ceiling: 10, active_slots: 0 })
  store.getAgentModel.mockResolvedValue({ model: 'sonnet' })
  store.getGitConfig.mockResolvedValue({ git_enabled: false })
})
afterEach(() => wrapper?.unmount())

async function makeRouter(start) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/agents/:name', name: 'AgentDetail', component: Blank }],
  })
  await router.push(start)
  return router
}

describe('the rail moves between panes through the URL', () => {
  async function mountHome(start) {
    const router = await makeRouter(start)
    wrapper = mount(AgentSettingsHome, {
      props: { agentName: 'atlas', agentStatus: 'running', sections: settingsSections() },
      slots: { default: (p) => h('p', { 'data-testid': 'pane' }, p.section) },
      global: { plugins: [pinia, router] },
    })
    await flushPromises()
    return router
  }

  it('opens General by default', async () => {
    await mountHome('/agents/atlas?tab=settings')
    expect(wrapper.get('[data-testid="pane"]').text()).toBe('general')
  })

  it('lands an old ?tab=credentials link on its own Credentials pane (ent#951)', async () => {
    await mountHome('/agents/atlas?tab=credentials')
    expect(wrapper.get('[data-testid="pane"]').text()).toBe('credentials')
  })

  it('a rail click pushes ?section= so Back returns to the previous pane', async () => {
    const router = await mountHome('/agents/atlas?tab=settings&section=general')
    await wrapper.get('[data-section="guardrails"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.query).toMatchObject({ tab: 'settings', section: 'guardrails' })
    expect(wrapper.get('[data-testid="pane"]').text()).toBe('guardrails')
    expect(wrapper.get('[data-section="guardrails"]').attributes('aria-current')).toBe('page')
    router.back()
    await flushPromises()
    expect(wrapper.get('[data-testid="pane"]').text()).toBe('general')
  })

  it('"Find a setting" narrows the rail to the pane that holds the word', async () => {
    await mountHome('/agents/atlas?tab=settings')
    await wrapper.get('[data-testid="settings-find"]').setValue('timeout')
    const shown = wrapper.findAll('[data-testid="settings-rail"] [data-section]').map((b) => b.attributes('data-section'))
    expect(shown).toEqual(['general'])
  })
})

describe('the one restart banner', () => {
  function mountBanner(status = 'running') {
    wrapper = mount(SettingsRestartBanner, {
      props: { agentName: 'atlas', agentStatus: status },
      global: { plugins: [pinia] },
    })
  }

  it('names every waiting change and offers one restart', async () => {
    const restart = useAgentRestartStore()
    restart.report('atlas', 'file-sharing', 'File sharing', true)
    restart.report('atlas', 'folders', 'Shared folders', true)
    mountBanner()
    await flushPromises()
    expect(wrapper.text()).toContain('2 changes wait for a restart')
    expect(wrapper.get('[data-testid="settings-restart-names"]').text()).toBe('File sharing, Shared folders')
    await wrapper.get('[data-testid="settings-restart-now"]').trigger('click')
    expect(wrapper.emitted('restart')).toHaveLength(1)
  })

  it('keeps its footprint with nothing waiting, and offers no button', async () => {
    mountBanner()
    await flushPromises()
    expect(wrapper.get('[data-testid="settings-restart-banner"]').attributes('data-pending')).toBe('false')
    expect(wrapper.find('[data-testid="settings-restart-now"]').exists()).toBe(false)
  })

  it('says "start" for a stopped agent — starting applies the same changes', async () => {
    useAgentRestartStore().report('atlas', 'resources', 'Memory and CPU', true)
    mountBanner('stopped')
    await flushPromises()
    expect(wrapper.text()).toContain('1 change waits for the agent to start')
    expect(wrapper.get('[data-testid="settings-restart-now"]').text()).toBe('Start now')
  })
})

describe('General › Memory and CPU', () => {
  const agent = { name: 'atlas', status: 'running', can_share: true, is_system: false, can_delete: true, runtime: 'claude-code' }

  it('offers "Inherit default" so a per-agent override can be cleared, and saves it as null', async () => {
    wrapper = mount(GeneralPane, {
      props: { agent, resourceLimits: { memory: '8g', cpu: '4', current_memory: '8g', current_cpu: '4' } },
      global: { plugins: [pinia], stubs: { InfoPanel: true, ModelSelector: true, TagsEditor: true } },
      attachTo: document.body,
    })
    await flushPromises()
    await wrapper.get('[data-testid="general-resources"] button').trigger('click')
    const selects = wrapper.findAll('[data-testid="general-resources"] select')
    expect(selects[0].find('option[value=""]').text()).toMatch(/inherit default/i)
    await selects[0].setValue('')
    await wrapper.get('[data-testid="general-resources"] form').trigger('submit')
    expect(wrapper.emitted('save-resources')).toEqual([[{ memory: null, cpu: '4' }]])
  })

  it('names the fleet ceiling when a stored capacity is above it', async () => {
    store.getAgentCapacity.mockResolvedValue({ max_parallel_tasks: 12, effective_max_parallel_tasks: 10, ceiling: 10, active_slots: 0 })
    wrapper = mount(GeneralPane, {
      props: { agent, resourceLimits: {} },
      global: { plugins: [pinia], stubs: { InfoPanel: true, ModelSelector: true, TagsEditor: true } },
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="general-capacity"]').text()).toMatch(/12 .*above the fleet ceiling/i)
  })
})

describe('General › Execution timeout (ent#353)', () => {
  const agent = { name: 'atlas', status: 'running', can_share: true, is_system: false, can_delete: true, runtime: 'claude-code' }

  async function mountGeneral() {
    wrapper = mount(GeneralPane, {
      props: { agent, authStatus: { auth_mode: 'api_key' }, resourceLimits: { current_memory: '4g', current_cpu: '2' } },
      global: { plugins: [pinia], stubs: { InfoPanel: true, ModelSelector: true, TagsEditor: true } },
      attachTo: document.body,
    })
    await flushPromises()
    await wrapper.get('[data-testid="general-timeout"] button').trigger('click')
  }
  const save = async (value) => {
    await wrapper.get('[data-testid="general-timeout-input"]').setValue(value)
    await wrapper.get('[data-testid="general-timeout"] form').trigger('submit')
    await flushPromises()
  }

  it('shows the stored value in minutes', async () => {
    wrapper = mount(GeneralPane, {
      props: { agent, resourceLimits: {} },
      global: { plugins: [pinia], stubs: { InfoPanel: true, ModelSelector: true, TagsEditor: true } },
    })
    await flushPromises()
    expect(wrapper.get('[data-testid="general-timeout-value"]').text()).toBe('60 min')
  })

  it('refuses an out-of-range value with a named error and never calls the API', async () => {
    await mountGeneral()
    await save('500')
    expect(wrapper.text()).toContain('between 1 and 120 minutes')
    expect(store.setAgentTimeout).not.toHaveBeenCalled()
  })

  it('saves minutes as seconds', async () => {
    store.setAgentTimeout.mockResolvedValue({ execution_timeout_seconds: 1800 })
    await mountGeneral()
    await save('30')
    expect(store.setAgentTimeout).toHaveBeenCalledWith('atlas', 1800)
    expect(wrapper.get('[data-testid="general-timeout-value"]').text()).toBe('30 min')
  })

  it('names the schedule conflict and offers the Schedules tab', async () => {
    store.setAgentTimeout.mockRejectedValue({
      response: {
        status: 400,
        data: { detail: {
          error: 'agent_timeout_below_active_schedules',
          max_schedule_timeout_seconds: 5400,
          blocking_schedules: [{ id: 's1', timeout_seconds: 5400 }],
        } },
      },
    })
    await mountGeneral()
    await save('10')
    expect(wrapper.text()).toContain('An active schedule runs for up to 90 min')
    await wrapper.get('[data-testid="general-timeout-open-schedules"]').trigger('click')
    expect(wrapper.emitted('navigate-tab')).toEqual([['schedules']])
  })
})
