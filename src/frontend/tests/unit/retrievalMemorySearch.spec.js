// @vitest-environment jsdom
/**
 * trinity-enterprise#1028 — the owner searches what an agent remembers, MOUNTED.
 *
 * The search box drives `GET /api/agents/{name}/retrieval-memory/search` and
 * the results region has four honest faces on one footprint: idle (no search
 * yet), loading (no data for this query), empty (a fetch that SUCCEEDED and
 * returned nothing), ready (hits with their `(date, source)` line and a
 * relevance percent), plus a failed verb that persists as an InlineError while
 * any earlier hits stay on screen. Store/wire writes behind an input are
 * proven by driving the real component (#2829, #2918), never a regex.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import RetrievalMemorySearch from '../../src/components/agent-settings/RetrievalMemorySearch.vue'

const HITS = [
  { id: 'f1', text: 'Prefers bullet-point answers', line: '(2026-10-21, scheduled run) Prefers bullet-point answers',
    source: 'schedule', execution_id: 'e1', created_at: '2026-10-21T10:00:00Z', updated_at: '2026-10-21T10:00:00Z',
    relevance: 0.734, score: 0.7 },
  { id: 'f2', text: 'The Q4 report is due 12 November', line: '(2026-10-28, chat) The Q4 report is due 12 November',
    source: 'public', execution_id: 'e2', created_at: '2026-10-28T10:00:00Z', updated_at: '2026-10-28T10:00:00Z',
    relevance: 0.51, score: 0.5 },
]

async function search(w, q) {
  await w.get('[data-testid="retrieval-memory-search-input"]').setValue(q)
  await w.get('[data-testid="retrieval-memory-search-input"]').trigger('keydown.enter')
  await flushPromises()
}

describe('RetrievalMemorySearch (agent settings → Memory)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders the idle copy before any search and makes no request', () => {
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    expect(w.find('[data-testid="retrieval-memory-search-idle"]').exists()).toBe(true)
    expect(w.find('[data-testid="retrieval-memory-search-hit"]').exists()).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  it('Enter searches the agent scope with q and k', async () => {
    api.get.mockResolvedValue({ data: HITS })
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    await search(w, 'report deadline')
    expect(api.get).toHaveBeenCalledWith('/api/agents/analyst/retrieval-memory/search',
      { params: { q: 'report deadline', k: 20 } })
  })

  it('renders each hit with its text, the (date, source) line and a relevance percent', async () => {
    api.get.mockResolvedValue({ data: HITS })
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    await search(w, 'report')
    const hits = w.findAll('[data-testid="retrieval-memory-search-hit"]')
    expect(hits).toHaveLength(2)
    expect(hits[0].text()).toContain('Prefers bullet-point answers')
    expect(hits[0].text()).toContain('2026-10-21, scheduled run')
    expect(hits[0].text()).toContain('73%')
    expect(w.text()).toMatch(/2 facts/)
    expect(w.find('[data-testid="retrieval-memory-search-idle"]').exists()).toBe(false)
  })

  it('a search that succeeds with nothing shows the empty copy, not the idle copy', async () => {
    api.get.mockResolvedValue({ data: [] })
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    await search(w, 'unicorns')
    expect(w.find('[data-testid="retrieval-memory-search-empty"]').exists()).toBe(true)
    expect(w.get('[data-testid="retrieval-memory-search-empty"]').text()).toMatch(/Nothing matched/)
    expect(w.find('[data-testid="retrieval-memory-search-idle"]').exists()).toBe(false)
  })

  it('a failed search raises an InlineError and keeps the earlier hits on screen', async () => {
    api.get.mockResolvedValueOnce({ data: HITS })
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    await search(w, 'report')
    api.get.mockRejectedValueOnce(new Error('boom'))
    await search(w, 'report again')
    expect(w.find('[data-testid="retrieval-memory-search-error"]').exists()).toBe(true)
    expect(w.findAll('[data-testid="retrieval-memory-search-hit"]')).toHaveLength(2)
    expect(w.find('[data-testid="retrieval-memory-search-empty"]').exists()).toBe(false)
  })

  it('a blank query does not call the API', async () => {
    const w = mount(RetrievalMemorySearch, { props: { agentName: 'analyst' } })
    await search(w, '   ')
    expect(api.get).not.toHaveBeenCalled()
    await w.get('[data-testid="retrieval-memory-search-submit"]').trigger('click')
    expect(api.get).not.toHaveBeenCalled()
  })
})
