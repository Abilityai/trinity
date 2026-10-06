// @vitest-environment jsdom
/**
 * trinity-enterprise#819 — a refused file read shows the server's message.
 *
 * The file routes answer a refusal with a structured `detail`
 * (`{code, message, path}`): credential files are owner-tier, and a file that
 * is a link is not opened. The two store calls read the body as text and as a
 * blob, so without parsing it the UI showed "Request failed with status code
 * 403", and the Credentials panel opened an empty, saveable editor for a file
 * it could not read.
 *
 *   1. the store turns a text or blob error body back into
 *      `{detail: <message>, code, path}`; a string detail stays a string;
 *   2. the Credentials panel (MOUNTED) opens the editor only after a read or a
 *      404, and shows the server's message next to the file list otherwise;
 *   3. the Files panel's Download shows the server's message.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import axios from 'axios'
import { useAgentsStore } from '../../src/stores/agents'
import CredentialsPanel from '../../src/components/CredentialsPanel.vue'
import FilesPanel from '../../src/components/FilesPanel.vue'
import FileTreeNode from '../../src/components/file-manager/FileTreeNode.vue'

const MESSAGE =
  "Credential files can be opened only by the agent's owner or an admin. " +
  'You can still chat with this agent, and it keeps using its credentials. ' +
  "To view or change them, ask the agent's owner or an admin."
const BODY = { detail: { code: 'owner_tier_path', message: MESSAGE, path: '/home/developer/.env' } }

function httpError (status, data) {
  const err = new Error(`Request failed with status code ${status}`)
  err.response = { status, data }
  return err
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('store: a refused read carries the server message', () => {
  it('parses a text body (download)', async () => {
    axios.get.mockRejectedValueOnce(httpError(403, JSON.stringify(BODY)))
    const err = await useAgentsStore().downloadAgentFile('a', '.env').catch((e) => e)
    expect(err.response.data).toEqual({ detail: MESSAGE, code: 'owner_tier_path', path: '/home/developer/.env' })
  })

  it('parses a blob body (preview)', async () => {
    axios.get.mockRejectedValueOnce(httpError(403, new Blob([JSON.stringify(BODY)], { type: 'application/json' })))
    const err = await useAgentsStore().getFilePreviewBlob('a', '.env').catch((e) => e)
    expect(err.response.data.detail).toBe(MESSAGE)
    expect(err.response.data.code).toBe('owner_tier_path')
  })

  it('keeps a string detail a string', async () => {
    axios.get.mockRejectedValueOnce(httpError(404, JSON.stringify({ detail: 'File not found: x' })))
    const err = await useAgentsStore().downloadAgentFile('a', 'x').catch((e) => e)
    expect(err.response.data).toEqual({ detail: 'File not found: x' })
  })

  it('leaves an unparseable body alone', async () => {
    axios.get.mockRejectedValueOnce(httpError(502, '<html>bad gateway</html>'))
    const err = await useAgentsStore().downloadAgentFile('a', 'x').catch((e) => e)
    expect(err.response.data).toBe('<html>bad gateway</html>')
  })
})

async function mountCredentials ({ read }) {
  const store = useAgentsStore()
  store.getCredentialStatus = vi.fn().mockResolvedValue({
    files: { '.env': { exists: true, size: 12, modified: '2026-10-06T00:00:00Z' } },
  })
  store.getCredentialRequirements = vi.fn().mockResolvedValue(null)
  store.downloadAgentFile = read
  const w = mount(CredentialsPanel, {
    props: { agentName: 'shared-agent', agentStatus: 'running' },
    global: { stubs: { CredentialSetupChecklist: true } },
  })
  await flushPromises()
  return w
}

// The file editor modal's textarea (the panel has a Quick-inject textarea too).
const editor = (w) => w.find('textarea[rows="20"]')

function button (w, label) {
  const b = w.findAll('button').find((x) => x.text().trim() === label)
  expect(b, `no "${label}" button`).toBeTruthy()
  return b
}

const refused = () => Promise.reject(Object.assign(new Error('Request failed with status code 403'), {
  response: { status: 403, data: { detail: MESSAGE, code: 'owner_tier_path' } },
}))

describe('Credentials panel: the editor opens only on content or a 404', () => {
  it('a refused Edit keeps the editor closed and shows the server message', async () => {
    const w = await mountCredentials({ read: vi.fn(refused) })
    await button(w, 'Edit').trigger('click')
    await flushPromises()
    expect(editor(w).exists()).toBe(false)
    expect(w.find('[data-testid="inline-error"]').text()).toContain(MESSAGE)
  })

  it('a refused View shows the server message', async () => {
    const w = await mountCredentials({ read: vi.fn(refused) })
    await button(w, 'View').trigger('click')
    await flushPromises()
    expect(editor(w).exists()).toBe(false)
    expect(w.find('[data-testid="inline-error"]').text()).toContain(MESSAGE)
  })

  it('a 404 opens an empty editor', async () => {
    const missing = () => Promise.reject(Object.assign(new Error('404'), {
      response: { status: 404, data: { detail: 'File not found' } },
    }))
    const w = await mountCredentials({ read: vi.fn(missing) })
    await button(w, 'Edit').trigger('click')
    await flushPromises()
    expect(editor(w).exists()).toBe(true)
    expect(editor(w).element.value).toBe('')
  })

  it('a successful read opens the editor with the content', async () => {
    const w = await mountCredentials({ read: vi.fn().mockResolvedValue('KEY=value\n') })
    await button(w, 'Edit').trigger('click')
    await flushPromises()
    expect(editor(w).element.value).toBe('KEY=value\n')
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(false)
  })
})

describe('Files panel: Download shows the server message', () => {
  it('names the refusal rather than the status code', async () => {
    globalThis.ResizeObserver = class { observe () {} unobserve () {} disconnect () {} }
    const store = useAgentsStore()
    store.listAgentFiles = vi.fn().mockResolvedValue({
      tree: [{ name: '.env', path: '.env', type: 'file', size: 12, modified: '2026-10-06T00:00:00Z' }],
    })
    store.getFilePreviewBlob = vi.fn(refused)
    const w = mount(FilesPanel, { props: { agentName: 'shared-agent', agentStatus: 'running' } })
    await flushPromises()
    w.findComponent(FileTreeNode).vm.$emit('select', { name: '.env', path: '.env', type: 'file', size: 12 })
    await flushPromises()
    await button(w, 'Download').trigger('click')
    await flushPromises()
    expect(w.text()).toContain(`Failed to download: ${MESSAGE}`)
    expect(w.text()).not.toContain('status code 403')
  })
})
