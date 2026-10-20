// @vitest-environment jsdom
/**
 * trinity-enterprise#955 — the display name and the avatar are editable IN
 * PLACE on the agent header again (ent#887 moved both to Settings › General;
 * the operator asked for the quick path back, Settings keeps the full one).
 * Mounted, not read as source (#2918).
 *
 * Pinned here:
 *   - the pencil beside the name: owners only (never viewers, never a system
 *     agent); it opens an inline editor pre-filled with the LABEL, never the
 *     slug; Enter and blur save through `set-label`; Esc cancels; an unchanged
 *     value emits nothing; an empty value clears (`null`);
 *   - the avatar overlay: owners only; camera → `open-avatar-modal` when there
 *     is no avatar, change-prompt + next-emotion when there is one with an
 *     identity prompt;
 *   - the page wires all three events to the handlers Settings › General
 *     already uses — so the header and the pane cannot drift apart.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, shallowMount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { h, nextTick } from 'vue'

const backend = vi.hoisted(() => {
  window.matchMedia = () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
  const state = { agent: null, puts: [] }
  const get = async (url) => {
    if (/^\/api\/agents\/[^/]+$/.test(String(url))) return { data: { ...state.agent } }
    return { data: {} }
  }
  const put = async (url, body) => {
    state.puts.push({ url: String(url), body })
    if (String(url).endsWith('/label')) return { data: { label: body.label } }
    return { data: {} }
  }
  const client = { get, post: get, put, patch: get, delete: get }
  return { state, client }
})

vi.mock('../../src/api', () => ({ default: backend.client }))
vi.mock('axios', () => {
  const instance = {
    ...backend.client,
    defaults: { headers: { common: {} } },
    interceptors: { request: { use() {} }, response: { use() {} } },
  }
  instance.create = () => instance
  return { default: instance }
})
vi.mock('../../src/utils/websocket', () => ({
  useWebSocket: () => ({ connect() {}, on() {}, off() {} }),
}))

import AgentHeader from '../../src/components/AgentHeader.vue'
import AgentDetail from '../../src/views/AgentDetail.vue'
import AvatarGenerateModal from '../../src/components/AvatarGenerateModal.vue'

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })

const baseAgent = (over = {}) => ({
  name: 'alpha', status: 'running', runtime: 'claude-code', can_share: true,
  is_system: false, is_shared: false, autonomy_enabled: false, avatar_url: null, display_label: null, ...over,
})

let wrapper
function render(props = {}) {
  wrapper = mount(AgentHeader, {
    props: { agent: baseAgent(), ...props },
    // The sparklines draw on a canvas 2D context jsdom does not provide (an
    // async `clearRect` on null surfaces as an unhandled error and fails the
    // run); nothing here asserts on the drawing, so the chart is stubbed.
    global: { plugins: [router], stubs: { SparklineChart: true } },
    attachTo: document.body,
  })
  return wrapper
}
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

const q = (id) => wrapper.find(`[data-testid="${id}"]`)
const pencil = () => q('agent-header-edit-label')
const input = () => q('agent-header-label-input')

describe('the pencil beside the name', () => {
  it('is there for an owner, and the heading still reads the display name', () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    expect(pencil().exists()).toBe(true)
    expect(wrapper.find('h1').text()).toBe('Alpha Prime')
    expect(input().exists()).toBe(false)
  })

  it.each([
    ['a viewer', baseAgent({ can_share: false })],
    ['a system agent', baseAgent({ is_system: true })],
  ])('is absent for %s', (_, agent) => {
    render({ agent })
    expect(pencil().exists()).toBe(false)
  })

  it('opens an editor pre-filled with the label, never the slug', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    expect(input().exists()).toBe(true)
    expect(input().element.value).toBe('Alpha Prime')
    expect(document.activeElement).toBe(input().element)
  })

  it('an agent with no label opens an EMPTY editor with the slug as placeholder', async () => {
    render()
    await pencil().trigger('click')
    expect(input().element.value).toBe('')
    expect(input().attributes('placeholder')).toBe('alpha')
  })

  it('Enter saves the trimmed label through set-label and closes the editor', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    await input().setValue('  Alpha Two  ')
    await input().trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('set-label')).toEqual([['Alpha Two']])
    expect(input().exists()).toBe(false)
    expect(wrapper.find('h1').exists()).toBe(true)
  })

  it('blur saves too', async () => {
    render()
    await pencil().trigger('click')
    await input().setValue('Named')
    await input().trigger('blur')
    expect(wrapper.emitted('set-label')).toEqual([['Named']])
  })

  it('Esc abandons the edit without emitting', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    await input().setValue('Something else')
    await input().trigger('keydown', { key: 'Escape' })
    expect(wrapper.emitted('set-label')).toBeUndefined()
    expect(input().exists()).toBe(false)
    expect(wrapper.find('h1').text()).toBe('Alpha Prime')
  })

  it('an unchanged value emits nothing', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    await input().trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('set-label')).toBeUndefined()
    expect(input().exists()).toBe(false)
  })

  it('an empty value clears the label (null), it is not an error', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    await input().setValue('   ')
    await input().trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('set-label')).toEqual([[null]])
  })

  it('the hint names what an empty value does', async () => {
    render({ agent: baseAgent({ display_label: 'Alpha Prime' }) })
    await pencil().trigger('click')
    expect(q('agent-header-label-hint').text()).toContain('alpha')
  })
})

describe('the avatar overlay', () => {
  const change = () => q('agent-header-avatar-change')
  const cycle = () => q('agent-header-avatar-cycle')
  const add = () => q('agent-header-avatar-add')

  it('offers the camera when there is no avatar yet, and it opens the modal', async () => {
    render()
    expect(add().exists()).toBe(true)
    expect(change().exists()).toBe(false)
    expect(cycle().exists()).toBe(false)
    await add().trigger('click')
    expect(wrapper.emitted('open-avatar-modal')).toHaveLength(1)
  })

  it('offers change-prompt and next-emotion when an avatar with a prompt exists', async () => {
    render({ agent: baseAgent({ avatar_url: '/api/agents/alpha/avatar?v=1' }), hasAvatarPrompt: true })
    expect(add().exists()).toBe(false)
    await change().trigger('click')
    expect(wrapper.emitted('open-avatar-modal')).toHaveLength(1)
    await cycle().trigger('click')
    expect(wrapper.emitted('cycle-emotion')).toHaveLength(1)
  })

  it('an avatar WITHOUT a prompt (a bundled default) gets the camera, not the emotion pair', () => {
    render({ agent: baseAgent({ avatar_url: '/api/agents/alpha/avatar?v=1' }), hasAvatarPrompt: false })
    expect(add().exists()).toBe(true)
    expect(cycle().exists()).toBe(false)
  })

  it.each([
    ['a viewer', baseAgent({ can_share: false, avatar_url: '/a' })],
    ['a system agent', baseAgent({ is_system: true, avatar_url: '/a' })],
  ])('shows nothing to %s', (_, agent) => {
    render({ agent, hasAvatarPrompt: true })
    expect(add().exists()).toBe(false)
    expect(change().exists()).toBe(false)
    expect(cycle().exists()).toBe(false)
  })
})

describe('the restored rows (pre-#3798 shape)', () => {
  const AUTH = { auth_mode: 'subscription', subscription_name: 'team-max' }
  const SUBS = [{ id: 1, name: 'team-max' }, { id: 2, name: 'solo' }]
  const STATS = { cpu_percent: 12, memory_percent: 40, memory_used_bytes: 400 * 1024 * 1024, uptime_seconds: 3600 }
  const TOKENS = { lifetime_executions: 5, lifetime_cost: 1.5, cost_24h: 0.2, avg_daily_cost: 0.1, trend_cost_pct: 100, daily_breakdown: [{ cost: 0.1 }, { cost: 0.2 }] }

  it('the funding badge names the subscription and its overlaid select switches it', async () => {
    render({ authStatus: AUTH, subscriptions: SUBS })
    expect(q('agent-header-funding').text()).toContain('team-max')
    const select = q('agent-header-funding-select')
    expect(select.exists()).toBe(true)
    await select.setValue('solo')
    expect(wrapper.emitted('change-subscription')).toEqual([['solo']])
  })

  it('a viewer sees the funding badge but no switcher', () => {
    render({ agent: baseAgent({ can_share: false }), authStatus: AUTH, subscriptions: SUBS })
    expect(q('agent-header-funding').exists()).toBe(true)
    expect(q('agent-header-funding-select').exists()).toBe(false)
  })

  it('the read-only toggle and the resources gear reach the page', async () => {
    render()
    await wrapper.find('[aria-label^="Read-only mode"]').trigger('click')
    expect(wrapper.emitted('toggle-read-only')).toHaveLength(1)
    await q('agent-header-resources').trigger('click')
    expect(wrapper.emitted('open-resources')).toHaveLength(1)
  })

  it('a running agent shows live CPU / MEM / uptime against its limits', () => {
    render({ agentStats: STATS, resourceLimits: { current_cpu: '2', current_memory: '4g' } })
    const live = q('agent-header-live').text()
    expect(live).toContain('12%')
    expect(live).toContain('/ 2 cores')
    expect(live).toContain('4G')
  })

  it('a stopped agent shows its allocation instead', () => {
    render({ agent: baseAgent({ status: 'stopped', created: '2026-10-01T00:00:00Z' }), resourceLimits: { current_cpu: '2', current_memory: '4g' } })
    const live = q('agent-header-live').text()
    expect(live).toContain('2 CPU')
    expect(live).toContain('4G')
  })

  it('the 7-day spend row renders once the agent has run anything', () => {
    render({ authStatus: AUTH, tokenStats: TOKENS })
    const cost = q('agent-header-cost').text()
    expect(cost).toContain('7d')
    expect(cost).toContain('Today')
    expect(cost).toContain('≈')
    expect(cost).toContain('5 runs')
    expect(cost).toContain('vs avg')
  })

  it('the spend row keeps its footprint: skeleton in flight, "no spend yet" after a first run-less fetch', () => {
    render({ tokenStats: null })
    expect(q('agent-header-cost').exists()).toBe(true)
    expect(q('agent-header-cost').find('[aria-busy="true"]').exists()).toBe(true)
    wrapper.unmount()
    render({ tokenStats: { ...TOKENS, lifetime_executions: 0 } })
    expect(q('agent-header-cost-empty').exists()).toBe(true)
    expect(q('agent-header-cost').text()).not.toContain('Today')
  })

  it('the GitHub row is reserved up front for a github: template, and only then', () => {
    // Running, status in flight: skeleton, no pop later.
    render({ agent: baseAgent({ template: 'github:acme/alpha' }), gitStatus: null })
    expect(q('agent-header-git').exists()).toBe(true)
    expect(q('agent-header-git').find('[aria-busy="true"]').exists()).toBe(true)
    wrapper.unmount()
    // Stopped: the status cannot be read, the row says so at the same height.
    render({ agent: baseAgent({ template: 'github:acme/alpha', status: 'stopped' }), gitStatus: null })
    expect(q('agent-header-git-idle').text()).toContain('start it')
    wrapper.unmount()
    // A local template with no status: no row at all.
    render({ agent: baseAgent({ template: 'local:alpha' }), gitStatus: null })
    expect(q('agent-header-git').exists()).toBe(false)
  })

  it('the funding badge keeps its footprint while the auth status is in flight', () => {
    render({ authStatus: null })
    expect(q('agent-header-funding-skeleton').exists()).toBe(true)
    expect(q('agent-header-funding').exists()).toBe(false)
  })
})

describe('the page wires the header to the same handlers as Settings › General', () => {
  const Blank = { render: () => h('div') }
  let page

  async function open() {
    backend.state.agent = { name: 'alpha', status: 'running', can_share: true, can_delete: true, display_label: null }
    backend.state.puts = []
    const r = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', name: 'Dashboard', component: Blank },
        { path: '/agents/:name', name: 'AgentDetail', component: Blank },
      ],
    })
    await r.push('/agents/alpha')
    const pinia = createPinia()
    setActivePinia(pinia)
    page = shallowMount(AgentDetail, { global: { plugins: [r, pinia] } })
    await flushPromises()
  }

  beforeEach(() => localStorage.clear())
  afterEach(() => { page?.unmount(); page = null })

  it('set-label from the header reaches PUT /label and the heading follows', async () => {
    await open()
    page.findComponent(AgentHeader).vm.$emit('set-label', 'Alpha Prime')
    await flushPromises()
    expect(backend.state.puts).toEqual([{ url: '/api/agents/alpha/label', body: { label: 'Alpha Prime' } }])
    expect(page.findComponent(AgentHeader).props('agent').display_label).toBe('Alpha Prime')
  })

  it('open-avatar-modal from the header shows the generate modal', async () => {
    await open()
    expect(page.findComponent(AvatarGenerateModal).props('show')).toBe(false)
    page.findComponent(AgentHeader).vm.$emit('open-avatar-modal')
    await nextTick()
    expect(page.findComponent(AvatarGenerateModal).props('show')).toBe(true)
  })

  it('the header is told whether an identity prompt exists, so it can offer the emotion pair', async () => {
    await open()
    expect(page.findComponent(AgentHeader).props()).toHaveProperty('hasAvatarPrompt')
  })
})
