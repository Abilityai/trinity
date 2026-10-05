// @vitest-environment jsdom
/**
 * #3010 — the Git sync settings panel: the per-agent auto-sync toggle and the
 * schedule-freeze-when-sync-failing toggle, reachable without the API.
 *
 * Mounted (the #2829 / #2918 rule): each toggle gates a store write, so the
 * proof is a click that reaches the store with the right value — and the
 * honest states: not-connected is its own copy (never an error), a failed load
 * is LoadFailed (never the not-connected copy), a failed save keeps the prior
 * value and says so next to the control.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const getGitAutoSync = vi.fn()
const setGitAutoSync = vi.fn()
const getGitFreezeSchedules = vi.fn()
const setGitFreezeSchedules = vi.fn()
const getGitPullSync = vi.fn()
const setGitPullSync = vi.fn()

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({
    getGitAutoSync, setGitAutoSync, getGitFreezeSchedules, setGitFreezeSchedules,
    getGitPullSync, setGitPullSync,
  }),
}))

// eslint-disable-next-line import/first
import GitSyncSettingsPanel from '../../src/components/GitSyncSettingsPanel.vue'

const httpError = (status, detail) => Object.assign(new Error(detail), { response: { status, data: { detail } } })

async function mountPanel(notify = vi.fn()) {
  const wrapper = mount(GitSyncSettingsPanel, { props: { agentName: 'a1', notify } })
  await flushPromises()
  return wrapper
}

const toggle = (wrapper, id) => wrapper.find(`[data-testid="${id}"]`)

beforeEach(() => {
  vi.clearAllMocks()
  getGitAutoSync.mockResolvedValue({ agent_name: 'a1', auto_sync_enabled: true })
  getGitFreezeSchedules.mockResolvedValue({ agent_name: 'a1', freeze_schedules_if_sync_failing: false })
  getGitPullSync.mockResolvedValue({ agent_name: 'a1', pull_sync_enabled: true })
})

describe('GitSyncSettingsPanel', () => {
  it('opens on the persisted values', async () => {
    const wrapper = await mountPanel()
    expect(toggle(wrapper, 'auto-sync-toggle').attributes('aria-checked')).toBe('true')
    expect(toggle(wrapper, 'freeze-toggle').attributes('aria-checked')).toBe('false')
    expect(getGitAutoSync).toHaveBeenCalledWith('a1')
  })

  it('turning auto-sync off writes false and reflects the saved value', async () => {
    setGitAutoSync.mockResolvedValue({ agent_name: 'a1', auto_sync_enabled: false })
    const notify = vi.fn()
    const wrapper = await mountPanel(notify)
    await toggle(wrapper, 'auto-sync-toggle').trigger('click')
    await flushPromises()
    expect(setGitAutoSync).toHaveBeenCalledWith('a1', false)
    expect(toggle(wrapper, 'auto-sync-toggle').attributes('aria-checked')).toBe('false')
    expect(notify).toHaveBeenCalledWith(expect.stringMatching(/next cycle is skipped/), 'success')
  })

  it('the pull toggle opens on the persisted value and turning it off writes false', async () => {
    // trinity-enterprise#703
    setGitPullSync.mockResolvedValue({ agent_name: 'a1', pull_sync_enabled: false })
    const wrapper = await mountPanel()
    expect(toggle(wrapper, 'pull-sync-toggle').attributes('aria-checked')).toBe('true')
    await toggle(wrapper, 'pull-sync-toggle').trigger('click')
    await flushPromises()
    expect(setGitPullSync).toHaveBeenCalledWith('a1', false)
    expect(toggle(wrapper, 'pull-sync-toggle').attributes('aria-checked')).toBe('false')
  })

  it('a failed pull save keeps the prior value and says so beside the control', async () => {
    setGitPullSync.mockRejectedValue(httpError(403, 'Only the owner can change this'))
    const wrapper = await mountPanel()
    await toggle(wrapper, 'pull-sync-toggle').trigger('click')
    await flushPromises()
    expect(toggle(wrapper, 'pull-sync-toggle').attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).toContain("Couldn't turn pulling off: Only the owner can change this")
  })

  it('turning the schedule pause on writes true', async () => {
    setGitFreezeSchedules.mockResolvedValue({ agent_name: 'a1', freeze_schedules_if_sync_failing: true })
    const wrapper = await mountPanel()
    await toggle(wrapper, 'freeze-toggle').trigger('click')
    await flushPromises()
    expect(setGitFreezeSchedules).toHaveBeenCalledWith('a1', true)
    expect(toggle(wrapper, 'freeze-toggle').attributes('aria-checked')).toBe('true')
  })

  it('a failed save keeps the prior value and names the failure beside the control', async () => {
    setGitAutoSync.mockRejectedValue(httpError(403, 'Only the owner can change this'))
    const wrapper = await mountPanel()
    await toggle(wrapper, 'auto-sync-toggle').trigger('click')
    await flushPromises()
    expect(toggle(wrapper, 'auto-sync-toggle').attributes('aria-checked')).toBe('true')
    expect(wrapper.text()).toContain("Couldn't turn auto-sync off: Only the owner can change this")
  })

  it('an agent with no GitHub binding gets the not-connected copy, not an error', async () => {
    getGitAutoSync.mockRejectedValue(httpError(404, 'Git not configured'))
    getGitFreezeSchedules.mockRejectedValue(httpError(404, 'Git not configured'))
    getGitPullSync.mockRejectedValue(httpError(404, 'Git not configured'))
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="git-sync-unbound"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="load-failed"]').exists()).toBe(false)
    expect(toggle(wrapper, 'auto-sync-toggle').exists()).toBe(false)
  })

  it('a failed load is LoadFailed with a working retry, never the not-connected copy', async () => {
    getGitAutoSync.mockRejectedValueOnce(httpError(500, 'boom'))
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="git-sync-unbound"]').exists()).toBe(false)
    await wrapper.find('[data-testid="load-failed"] button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="git-sync-ready"]').exists()).toBe(true)
  })

  it('shows a skeleton, not the toggles, before the first load resolves', () => {
    getGitAutoSync.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(GitSyncSettingsPanel, { props: { agentName: 'a1' } })
    expect(wrapper.find('[data-testid="git-sync-loading"]').exists()).toBe(true)
    expect(toggle(wrapper, 'auto-sync-toggle').exists()).toBe(false)
  })
})
