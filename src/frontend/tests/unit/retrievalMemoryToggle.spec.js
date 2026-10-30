// @vitest-environment jsdom
/**
 * trinity-enterprise#1027 — the per-agent memory row says WHAT will extract
 * this agent's facts: its own runtime, the platform key, or nothing (and
 * why). The text is read off `GET /api/agents/{name}/retrieval-memory`
 * `extractor`, so the real component is mounted (#2918).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import RetrievalMemoryToggle from '../../src/components/agent-settings/RetrievalMemoryToggle.vue'

function agentStatus(extractor) {
  return { enabled_platform: true, opt_out: false, available: true, reason: null, fact_count: 4, extractor }
}

async function mountWith(extractor) {
  api.get.mockResolvedValue({ data: agentStatus(extractor) })
  const w = mount(RetrievalMemoryToggle, { props: { agentName: 'beta' } })
  await flushPromises()
  return w.get('[data-testid="retrieval-memory-agent-status"]').text()
}

describe('RetrievalMemoryToggle — where extraction runs (ent#1027)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it("names the agent's own runtime when that is what will extract", async () => {
    const text = await mountWith({ mode: 'auto', effective: 'agent', runtime: 'claude-code', reason: null })
    expect(text).toMatch(/4 facts/)
    expect(text).toMatch(/extracts on claude-code/)
  })

  it('names the platform key when that is the fallback in effect', async () => {
    const text = await mountWith({ mode: 'auto', effective: 'platform', runtime: null, reason: null })
    expect(text).toMatch(/extracts on the platform key/)
  })

  it('says nothing is captured, and why, when neither can answer', async () => {
    const text = await mountWith({ mode: 'agent', effective: 'none', runtime: null, reason: 'agent is not running' })
    expect(text).toMatch(/nothing captured — agent is not running/)
  })

  it('still renders the plain status when the backend sends no extractor field', async () => {
    const text = await mountWith(undefined)
    expect(text).toMatch(/On · 4 facts/)
    expect(text).not.toMatch(/extracts on/)
  })
})
