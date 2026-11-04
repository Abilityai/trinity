// @vitest-environment jsdom
/**
 * Settings › Credentials — its own pane (trinity-enterprise#951), MOUNTED.
 *
 *   1. the Credentials pane hosts the full CredentialsPanel, and Storage no
 *      longer does;
 *   2. the file list tells the truth about what it knows: a stopped agent or a
 *      failed read is not "Not present", and an agent with no credential files
 *      gets a next action instead of three bare rows.
 *
 * The rail, the deep link and the legacy `?tab=credentials` alias are pure
 * modules, executed in `agentSettingsHome.spec.js`.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import { useAgentsStore } from '../../src/stores/agents'
import CredentialsPane from '../../src/components/agent-settings/CredentialsPane.vue'
import StoragePane from '../../src/components/agent-settings/StoragePane.vue'
import CredentialsPanel from '../../src/components/CredentialsPanel.vue'

let store

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useAgentsStore()
  store.getCredentialRequirements = vi.fn().mockResolvedValue(null)
  store.getGitConfig = vi.fn().mockResolvedValue({ git_enabled: false })
})

const STUB_CHECKLIST = { stubs: { CredentialSetupChecklist: true } }

async function mountPanel ({ status = 'running', files, fail } = {}) {
  store.getCredentialStatus = fail
    ? vi.fn().mockRejectedValue(new Error('Request failed with status code 503'))
    : vi.fn().mockResolvedValue({ files: files || {} })
  const w = mount(CredentialsPanel, {
    props: { agentName: 'atlas', agentStatus: status, embedded: true },
    global: STUB_CHECKLIST,
  })
  await flushPromises()
  return w
}

const fileRows = (w) => w.findAll('[data-testid="credential-file-row"]')

describe('the pane (ent#951)', () => {
  it('hosts the full Credentials panel, embedded', async () => {
    store.getCredentialStatus = vi.fn().mockResolvedValue({ files: {} })
    const w = mount(CredentialsPane, {
      props: { agentName: 'atlas', agentStatus: 'running' },
      global: STUB_CHECKLIST,
    })
    await flushPromises()
    const panel = w.findComponent(CredentialsPanel)
    expect(panel.exists()).toBe(true)
    expect(panel.props()).toMatchObject({ agentName: 'atlas', agentStatus: 'running', embedded: true })
  })

  it('Storage no longer contains the Credentials section', async () => {
    const w = mount(StoragePane, {
      props: { agentName: 'atlas', agentStatus: 'running' },
      global: { stubs: { CredentialsPanel: true, FoldersPanel: true, FileSharingPanel: true } },
    })
    await flushPromises()
    expect(w.findComponent(CredentialsPanel).exists()).toBe(false)
    expect(w.text()).not.toContain('Credentials')
    expect(w.text()).toContain('Shared folders')
    expect(w.text()).toContain('File sharing')
  })
})

describe('the file list says what it knows', () => {
  it('an agent with no .env gets a next action, not three bare rows', async () => {
    // Every real agent has a platform-written .mcp.json, so "no credential
    // file at all" never happens — the empty state keys on .env.
    const w = await mountPanel({ files: { '.mcp.json': { exists: true, size: 299, modified: '2026-10-21T00:00:00Z' } } })
    const empty = w.find('[data-testid="credentials-empty"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toContain('Quick Inject')
    // The rows stay: each one carries its own Add.
    expect(fileRows(w)).toHaveLength(3)
  })

  it('"Add .env" in the empty state opens the editor on an empty .env', async () => {
    store.downloadAgentFile = vi.fn().mockRejectedValue(Object.assign(new Error('404'), {
      response: { status: 404, data: { detail: 'File not found' } },
    }))
    const w = await mountPanel({ files: {} })
    await w.find('[data-testid="credentials-empty-add-env"]').trigger('click')
    await flushPromises()
    expect(store.downloadAgentFile).toHaveBeenCalledWith('atlas', '/home/developer/.env')
    expect(w.find('textarea[rows="20"]').exists()).toBe(true)
  })

  it('an agent with a .env shows no empty state', async () => {
    const w = await mountPanel({ files: { '.env': { exists: true, size: 12, modified: '2026-10-06T00:00:00Z' } } })
    expect(w.find('[data-testid="credentials-empty"]').exists()).toBe(false)
    expect(fileRows(w)).toHaveLength(3)
  })

  it('a stopped agent is not reported as having no files', async () => {
    const w = await mountPanel({ status: 'stopped' })
    expect(store.getCredentialStatus).not.toHaveBeenCalled()
    expect(w.find('[data-testid="credentials-stopped"]').exists()).toBe(true)
    expect(w.find('[data-testid="credentials-empty"]').exists()).toBe(false)
    expect(fileRows(w)).toHaveLength(0)
    expect(w.text()).not.toContain('Not present')
  })

  it('a failed read shows the failure and retries, never "Not present"', async () => {
    const w = await mountPanel({ fail: true })
    expect(w.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="credentials-empty"]').exists()).toBe(false)
    expect(fileRows(w)).toHaveLength(0)

    store.getCredentialStatus.mockResolvedValue({ files: {} })
    await w.find('[data-testid="load-failed"] button').trigger('click')
    await flushPromises()
    expect(store.getCredentialStatus).toHaveBeenCalledTimes(2)
    expect(w.find('[data-testid="load-failed"]').exists()).toBe(false)
    expect(w.find('[data-testid="credentials-empty"]').exists()).toBe(true)
  })
})
