// @vitest-environment jsdom
/**
 * trinity-enterprise#950 — the agent header after the ent#887 rewrite gets the
 * bound repo, the tags and a small context chart back, and loses the Talk and
 * Workspace doors. Mounted, not read as source (#2918).
 *
 * Pinned here:
 *   - repo: shown (linked, branch, changes / behind) only when the live git
 *     status says a repo is bound; never a non-http(s) href;
 *   - tags: chips render, the set is capped with a counted "+N", and an owner
 *     edits through the same update-tags / add-tag / remove-tag events;
 *   - context: hidden for a stopped agent, "—" (never 0%) while there is no
 *     reading — including the backend's 0 for an offline probe;
 *   - the doors are gone.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { nextTick } from 'vue'

// ent#955: the header renders sparklines again, and their module-level theme
// read asks `matchMedia`, which jsdom does not ship — so the stub must land
// before the import graph evaluates (imports hoist; this hoists above them).
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

describe('the doors are gone', () => {
  it('renders neither Talk nor Workspace', () => {
    render()
    expect(q('agent-talk').exists()).toBe(false)
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels).not.toContain('Talk')
    expect(labels).not.toContain('Workspace')
  })
})

describe('repository', () => {
  it('shows the bound repo, linked, with branch, commit, changes and behind — and no Pull / Push', () => {
    render({ gitStatus: { ...GIT, last_commit: { short_sha: 'abc1234', message: 'm', author: 'a', date: 'd' } }, gitChangesCount: 3, gitBehind: 2 })
    const link = q('agent-header-repo').find('a')
    expect(link.attributes('href')).toBe('https://github.com/acme/alpha')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.text()).toContain('acme/alpha')
    expect(q('agent-header-branch').text()).toBe('trinity/alpha/abc')
    expect(q('agent-header-commit').text()).toBe('abc1234')
    expect(q('agent-header-git-changes').text()).toContain('3 changed')
    expect(q('agent-header-git-behind').text()).toContain('2 behind')
    // ent#955: the GitHub line is back as its own row, verbs stay on the Git tab
    expect(q('agent-header-git').exists()).toBe(true)
    const labels = wrapper.findAll('button').map(b => b.text())
    expect(labels.some(t => /^Pull|^Push/.test(t))).toBe(false)
  })

  it('each count carries a glyph that survives the narrow (phone) label', () => {
    // Below `sm` only the glyph + number show (the 375px header has ~184px of
    // content width); the words come back at `sm` and live in the tooltip.
    render({ gitStatus: { ...GIT, ahead: 1 }, gitChangesCount: 3, gitBehind: 2 })
    expect(q('agent-header-git-changes').text()).toContain('±')
    expect(q('agent-header-git-ahead').text()).toContain('↑')
    expect(q('agent-header-git-behind').text()).toContain('↓')
    expect(q('agent-header-git-behind').attributes('title')).toContain('2 commit(s) on GitHub')
  })

  it('reads "in sync" when there is nothing to push or pull', () => {
    render({ gitStatus: { ...GIT, changes_count: 0, behind: 0 }, gitChangesCount: 0, gitBehind: 0 })
    expect(q('agent-header-git-changes').exists()).toBe(false)
    expect(q('agent-header-git-behind').exists()).toBe(false)
    expect(q('agent-header-git-insync').exists()).toBe(true)
  })

  it('counts changed files and unpushed commits separately — one number would mean neither', () => {
    render({ gitStatus: { ...GIT, changes_count: 12, ahead: 1, behind: 0 }, gitChangesCount: 12, gitBehind: 0 })
    expect(q('agent-header-git-changes').text()).toContain('12 changed')
    expect(q('agent-header-git-ahead').text()).toContain('1 ahead')
    expect(q('agent-header-git-insync').exists()).toBe(false)
  })

  it('a clean tree with unpushed commits is not "in sync"', () => {
    render({ gitStatus: { ...GIT, changes_count: 0, ahead: 4, behind: 0 }, gitChangesCount: 0, gitBehind: 0 })
    expect(q('agent-header-git-changes').exists()).toBe(false)
    expect(q('agent-header-git-ahead').text()).toContain('4 ahead')
    expect(q('agent-header-git-insync').exists()).toBe(false)
  })

  it.each([
    ['no status yet', null],
    ['git not enabled', { git_enabled: false, message: 'Git sync not enabled for this agent' }],
    ['enabled but no remote', { ...GIT, remote_url: '' }],
  ])('shows nothing when %s', (_, gitStatus) => {
    render({ gitStatus })
    expect(q('agent-header-repo').exists()).toBe(false)
  })

  it('a non-http origin is named but never linked', () => {
    render({ gitStatus: { ...GIT, remote_url: 'javascript:alert(1)' } })
    expect(q('agent-header-repo').find('a').exists()).toBe(false)
  })
})

describe('tags', () => {
  it('renders the first tags as chips and counts the rest', () => {
    render({ tags: ['alpha', 'beta', 'gamma', 'delta'] })
    const chips = wrapper.findAll('[data-testid="agent-header-tag"]')
    expect(chips.map(c => c.text())).toEqual(['#alpha', '#beta'])
    expect(q('agent-header-tags-more').text()).toBe('+2')
  })

  it('an owner opens the editor and its events reach the page', async () => {
    render({ tags: ['alpha'], allTags: ['alpha', 'beta'] })
    await q('agent-header-tags-edit').trigger('click')
    await nextTick()
    const dialog = document.body.querySelector('[role="dialog"]')
    expect(dialog).not.toBeNull()
    const remove = dialog.querySelector('button[title="Remove alpha"]')
    remove.click()
    await nextTick()
    expect(wrapper.emitted('remove-tag')?.[0]).toEqual(['alpha'])
  })

  it('a viewer sees read-only chips and no edit affordance', () => {
    render({ agent: baseAgent({ can_share: false }), tags: ['alpha'] })
    expect(wrapper.findAll('[data-testid="agent-header-tag"]')).toHaveLength(1)
    expect(q('agent-header-tags-edit').exists()).toBe(false)
  })

  it('a viewer with no tags gets no empty chrome', () => {
    render({ agent: baseAgent({ can_share: false }), tags: [] })
    expect(q('agent-header-tags').exists()).toBe(false)
  })
})

describe('context chart', () => {
  it('is hidden for a stopped agent', () => {
    render({ agent: baseAgent({ status: 'stopped' }), contextStats: { contextPercent: 40, contextUsed: 80000, contextMax: 200000, activityState: 'offline' } })
    expect(q('agent-header-context').exists()).toBe(false)
  })

  it('shows "—" and no arc before stats arrive', () => {
    render({ contextStats: null })
    expect(q('agent-header-context').text()).toBe('—')
    expect(q('agent-header-context-arc').exists()).toBe(false)
  })

  it('treats the backend\'s 0 for an offline probe as no reading, not 0%', () => {
    render({ contextStats: { contextPercent: 0, contextUsed: 0, contextMax: 200000, activityState: 'offline' } })
    expect(q('agent-header-context').text()).toBe('—')
    expect(q('agent-header-context').text()).not.toContain('0%')
  })

  it('shows the percentage with a level-coloured arc', () => {
    render({ contextStats: { contextPercent: 91.6, contextUsed: 183000, contextMax: 200000, activityState: 'idle' } })
    expect(q('agent-header-context').text()).toBe('92%')
    expect(q('agent-header-context-arc').classes().join(' ')).toContain('stroke-status-danger-500')
    expect(q('agent-header-context').attributes('title')).toContain('183k / 200k')
  })
})
