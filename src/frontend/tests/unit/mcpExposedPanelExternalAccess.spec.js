// @vitest-environment jsdom
/**
 * trinity-enterprise#1101 — the "Connect an external client" block of the
 * exposed-agent panel reads `ConnectorStatus.external_access`. When the
 * instance refuses off-box MCP callers, copying a config would hand the user a
 * URL that does not answer, so the copy actions are disabled with the reason
 * and the refusal is named. When on (or when an older backend sends no
 * `external_access` at all), nothing changes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const apiMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: apiMock }))
vi.mock('@/stores/agents', () => ({
  useAgentsStore: () => ({
    getMcpExposedStatus: vi.fn(async () => ({ enabled: true, tool_name: 'chat_with_demo' })),
    setMcpExposed: vi.fn(),
  }),
}))
vi.mock('@/components/ExposedToolsPanel.vue', () => ({ default: { template: '<div />' } }))

import McpExposedPanel from '@/components/McpExposedPanel.vue'

function connector(overrides = {}) {
  return {
    agent_name: 'demo',
    has_key: false,
    enabled: true,
    key_prefix: null,
    mcp_url: 'https://trinity.example.com/mcp',
    snippets: [{ client: 'claude-code-json', label: '.mcp.json', content: '{}' }],
    ...overrides,
  }
}

let w
async function render(data) {
  apiMock.get.mockResolvedValue({ data })
  w = mount(McpExposedPanel, { props: { agentName: 'demo' }, attachTo: document.body })
  await flushPromises()
  return w
}

const findButton = (text) => w.findAll('button').find((b) => b.text().includes(text))
const offCallout = () => w.find('[data-testid="mcp-external-access-off"]')

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('ent#1101 exposed panel honours external MCP access', () => {
  it('disables Copy connection config with the reason when external access is off', async () => {
    await render(connector({ external_access: { enabled: false, source: 'default' } }))
    const copy = findButton('Copy connection config')
    expect(copy).toBeDefined()
    expect(copy.attributes('disabled')).toBeDefined()
    expect(copy.attributes('title')).toContain('External MCP access is off')
    expect(offCallout().exists()).toBe(true)
    expect(offCallout().text()).toContain('external_mcp_access_off')
    await copy.trigger('click')
    expect(apiMock.post).not.toHaveBeenCalled()
  })

  it('disables the copy actions for an existing key too, leaving Revoke usable', async () => {
    await render(connector({ has_key: true, key_prefix: 'trinity_mcp_c', external_access: { enabled: false, source: 'settings' } }))
    expect(findButton('Copy config').attributes('disabled')).toBeDefined()
    expect(findButton('Regenerate & copy').attributes('disabled')).toBeDefined()
    expect(findButton('Revoke').attributes('disabled')).toBeUndefined()
  })

  it('enables Copy connection config when external access is on', async () => {
    await render(connector({ external_access: { enabled: true, source: 'settings' } }))
    const copy = findButton('Copy connection config')
    expect(copy.attributes('disabled')).toBeUndefined()
    expect(offCallout().exists()).toBe(false)
  })

  it('treats a payload with no external_access as on (an older backend has no gate)', async () => {
    await render(connector())
    expect(findButton('Copy connection config').attributes('disabled')).toBeUndefined()
    expect(offCallout().exists()).toBe(false)
  })
})
