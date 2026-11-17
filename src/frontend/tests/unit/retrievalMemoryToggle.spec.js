// @vitest-environment jsdom
/**
 * trinity-enterprise#1027 — the per-agent memory row says WHAT will extract
 * this agent's facts: its subscription, the platform key, or nothing (and
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

  it("names the agent's subscription when that is what will extract", async () => {
    const text = await mountWith({ mode: 'auto', effective: 'subscription', subscription: 'lilu-max', model: 'claude-haiku-4-5-20251001', reason: null })
    expect(text).toMatch(/4 facts/)
    expect(text).toMatch(/extracts on subscription lilu-max/)
  })

  it('names the platform key when that is the fallback in effect', async () => {
    const text = await mountWith({ mode: 'auto', effective: 'api_key', subscription: null, model: 'claude-haiku-5-5', reason: null })
    expect(text).toMatch(/extracts on the platform key/)
  })

  it('says nothing is captured, and why, when neither can answer', async () => {
    const text = await mountWith({ mode: 'subscription', effective: 'none', subscription: null, model: null, reason: 'the agent has no subscription assigned' })
    expect(text).toMatch(/nothing captured — the agent has no subscription assigned/)
  })

  it('still renders the plain status when the backend sends no extractor field', async () => {
    const text = await mountWith(undefined)
    expect(text).toMatch(/On · 4 facts/)
    expect(text).not.toMatch(/extracts on/)
  })
})
