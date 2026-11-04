// @vitest-environment jsdom
/**
 * #3872 — the agent header's GitHub row (row 4) is reserved at first paint for
 * every agent that has a repository bound, so the live git status (an exec into
 * the running container, seconds later) cannot push the tab strip down when it
 * lands. Mounted, not read as source (#2918).
 *
 * Pinned here:
 *   - the reservation keys off the detail payload's `git_configured` (the
 *     persisted git config), not only a `github:` template string — an agent
 *     deployed locally and bound afterwards carries a `local:` label;
 *   - a `github:` template still reserves (the pre-#3872 signal keeps working
 *     against a backend that does not carry the flag);
 *   - an agent with neither gets no row, as before;
 *   - every branch of the row shares one content floor, so a reserved row and
 *     the bound line are the same height (principle 6).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.hoisted(() => {
  window.matchMedia = () => ({
    matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
})

import AgentHeader from '../../src/components/AgentHeader.vue'

const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<div/>' } }] })

const baseAgent = (over = {}) => ({
  name: 'alpha', status: 'running', runtime: 'claude-code', can_share: true,
  is_system: false, is_shared: false, autonomy_enabled: false, avatar_url: null, ...over,
})

const GIT = {
  git_enabled: true,
  remote_url: 'https://github.com/acme/alpha.git',
  branch: 'trinity/alpha/abc',
  changes_count: 3,
  ahead: 0,
  behind: 2,
}

let wrapper
function render(props = {}) {
  wrapper = mount(AgentHeader, {
    props: { agent: baseAgent(), ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  return wrapper
}
afterEach(() => { wrapper?.unmount(); wrapper = null; document.body.innerHTML = '' })

const q = (id) => wrapper.find(`[data-testid="${id}"]`)
const row = () => q('agent-header-git')
const skeleton = () => row().find('[aria-busy="true"]')

describe('row 4 is reserved before the live status lands', () => {
  it('for a local-template agent whose detail payload says a repo is bound', () => {
    render({ agent: baseAgent({ template: 'local:scout', git_configured: true }), gitStatus: null })
    expect(row().exists()).toBe(true)
    expect(skeleton().exists()).toBe(true)
    expect(q('agent-header-repo').exists()).toBe(false)
  })

  it('for a github: template agent, as before', () => {
    render({ agent: baseAgent({ template: 'github:acme/alpha' }), gitStatus: null })
    expect(row().exists()).toBe(true)
    expect(skeleton().exists()).toBe(true)
  })

  it('not for an agent with neither signal', () => {
    render({ agent: baseAgent({ template: 'local:scout', git_configured: false }), gitStatus: null })
    expect(row().exists()).toBe(false)
  })

  it('the reserved row then carries the bound line in place — same row, no second mount', async () => {
    render({ agent: baseAgent({ template: 'local:scout', git_configured: true }), gitStatus: null })
    const before = row().element
    await wrapper.setProps({ gitStatus: GIT, gitChangesCount: 3, gitBehind: 2 })
    expect(row().element).toBe(before)
    expect(skeleton().exists()).toBe(false)
    expect(q('agent-header-repo').exists()).toBe(true)
    expect(q('agent-header-git-changes').text()).toContain('3 changed')
  })
})

describe('every branch of row 4 shares the bound line\'s content floor (principle 6)', () => {
  // jsdom lays nothing out, so the floor is pinned on the class contract: the
  // bound `AgentHeaderMeta` line carries `min-h-[1.5rem]`, and the skeleton
  // and idle branches must carry the same floor so the row's height is the
  // same 41px before and after the status lands. e2e/agent-header.spec.js
  // measures the real thing.
  const FLOOR = 'min-h-[1.5rem]'

  it('the in-flight skeleton', () => {
    render({ agent: baseAgent({ git_configured: true }), gitStatus: null })
    expect(skeleton().classes()).toContain(FLOOR)
  })

  it('the stopped-agent sentence', () => {
    render({ agent: baseAgent({ status: 'stopped', git_configured: true }), gitStatus: null })
    expect(q('agent-header-git-idle').classes()).toContain(FLOOR)
  })

  it('the bound line', () => {
    render({ agent: baseAgent({ git_configured: true }), gitStatus: GIT, gitChangesCount: 3, gitBehind: 2 })
    expect(q('agent-header-meta').classes()).toContain(FLOOR)
  })
})
