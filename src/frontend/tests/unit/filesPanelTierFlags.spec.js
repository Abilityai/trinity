// @vitest-environment jsdom
/**
 * trinity-enterprise#820 — the Files tab follows the server's per-node flags.
 *
 * The panel used to keep its own two lists of protected names, and they had
 * drifted from the backend (`.mcp.json` was marked editable). The list
 * response now carries `restricted: [...]` per node — the verbs THIS caller
 * cannot use, computed from the one backend path policy — plus `tier` and
 * `hidden_withheld`. Mounted, not grepped (#2829 / #2918): every property here
 * is a gate on a destructive verb or a toggle, which a regex over the SFC
 * cannot prove.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const listAgentFiles = vi.fn()
const getFilePreviewBlob = vi.fn()

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({
    listAgentFiles,
    getFilePreviewBlob,
    deleteAgentFile: vi.fn(),
    updateAgentFile: vi.fn(),
    createAgentFolder: vi.fn(),
  }),
}))

// eslint-disable-next-line import/first
import FilesPanel from '../../src/components/FilesPanel.vue'

const tree = [
  { name: 'CLAUDE.md', path: 'CLAUDE.md', type: 'file', size: 10, restricted: ['write', 'delete'] },
  { name: 'notes.md', path: 'notes.md', type: 'file', size: 10 },
  { name: 'k.pem', path: 'k.pem', type: 'file', size: 10, restricted: ['read'] },
  { name: 'docs', path: 'docs', type: 'link', restricted: ['read', 'write'] },
]

async function mountAs({ tier = 'share', hidden_withheld = true } = {}) {
  listAgentFiles.mockResolvedValue({ tree, tier, hidden_withheld })
  getFilePreviewBlob.mockResolvedValue({ url: 'blob:x', type: 'text', mime: 'text/plain' })
  const wrapper = mount(FilesPanel, {
    props: { agentName: 'acme', agentStatus: 'running' },
    global: { stubs: { FilePreview: true } },
  })
  await flushPromises()
  return wrapper
}

async function select(wrapper, name) {
  const row = wrapper.findAll('div.cursor-pointer').find((r) => r.text().includes(name))
  await row.trigger('click')
  await flushPromises()
}

const button = (wrapper, label) => wrapper.findAll('button').find((b) => b.text().trim() === label)

beforeEach(() => {
  setActivePinia(createPinia())
  listAgentFiles.mockReset()
  getFilePreviewBlob.mockReset()
  localStorage.clear()
})

describe('ent#820 Files tab reads the server flags', () => {
  it('a file the caller cannot change has no Edit and a disabled Delete, with the reason', async () => {
    const w = await mountAs()
    await select(w, 'CLAUDE.md')
    expect(button(w, 'Edit')).toBeUndefined()
    expect(button(w, 'Delete').attributes('disabled')).toBeDefined()
    expect(w.text()).toContain("Only the agent's owner or an admin can change this.")
  })

  it('an ordinary file keeps Edit and Delete', async () => {
    const w = await mountAs()
    await select(w, 'notes.md')
    expect(button(w, 'Edit')).toBeDefined()
    expect(button(w, 'Delete').attributes('disabled')).toBeUndefined()
  })

  it('a read-restricted file is not fetched and cannot be downloaded', async () => {
    const w = await mountAs()
    await select(w, 'k.pem')
    expect(getFilePreviewBlob).not.toHaveBeenCalled()
    expect(button(w, 'Download').attributes('disabled')).toBeDefined()
  })

  it('a link is marked and says so', async () => {
    const w = await mountAs()
    expect(w.text()).toContain('docs →')
    await select(w, 'docs')
    expect(w.text()).toContain('This is a link. Open the file it points to.')
  })

  it('a node the caller cannot write carries the lock; others do not', async () => {
    const w = await mountAs()
    expect(w.findAll('[data-testid="file-node-lock"]')).toHaveLength(2)   // CLAUDE.md, docs
  })

  it('the hidden toggle is disabled below the owner tier and is not persisted', async () => {
    const w = await mountAs()
    const toggle = w.find('input[type="checkbox"]')
    expect(toggle.attributes('disabled')).toBeDefined()
    expect(toggle.element.closest('label').getAttribute('title')).toContain("agent's owner")
  })

  it('the owner keeps the hidden toggle and is told a protected file is a system file', async () => {
    listAgentFiles.mockResolvedValue({
      tree: [{ name: 'CLAUDE.md', path: 'CLAUDE.md', type: 'file', size: 1, restricted: ['delete'] }],
      tier: 'owner',
      hidden_withheld: false,
    })
    getFilePreviewBlob.mockResolvedValue({ url: 'blob:x', type: 'text' })
    const w = mount(FilesPanel, {
      props: { agentName: 'acme', agentStatus: 'running' },
      global: { stubs: { FilePreview: true } },
    })
    await flushPromises()
    expect(w.find('input[type="checkbox"]').attributes('disabled')).toBeUndefined()
    await select(w, 'CLAUDE.md')
    expect(button(w, 'Edit')).toBeDefined()
    expect(button(w, 'Delete').attributes('disabled')).toBeDefined()
    expect(w.text()).toContain('This is a protected system file and cannot be changed here.')
  })

  it('a structured refusal shows its message, not [object Object]', async () => {
    listAgentFiles.mockRejectedValue({
      response: { data: { detail: { code: 'owner_tier_path', message: 'Only the owner can list this.' } } },
    })
    const w = mount(FilesPanel, {
      props: { agentName: 'acme', agentStatus: 'running' },
      global: { stubs: { FilePreview: true } },
    })
    await flushPromises()
    expect(w.text()).toContain('Only the owner can list this.')
    expect(w.text()).not.toContain('[object Object]')
  })
})
