// @vitest-environment jsdom
/**
 * #2899 — what the compatibility panel SAYS on an agent's landing tab.
 *
 * Two defects, both about presentation rather than the checks themselves:
 *   1. the headline counted hard + soft in one warning-toned number, so an
 *      agent whose findings were all advisory announced "5 compatibility
 *      issues" in a warning banner on the first screen a new operator sees;
 *   2. with no Anthropic API key, 30 of 89 checks came back skipped and each
 *      one rendered its own row, burying the real findings.
 *
 * Mounted rather than grepped (the #2829 rule): every property here is about
 * what renders for a given report, which a regex over the SFC cannot prove.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const getCompatibility = vi.fn()
const fixCompatibilityIssue = vi.fn()

vi.mock('../../src/stores/agents', () => ({
  useAgentsStore: () => ({ getCompatibility, fixCompatibilityIssue }),
}))

// eslint-disable-next-line import/first
import CompatibilityPanel from '../../src/components/CompatibilityPanel.vue'

const check = (over = {}) => ({
  check_id: 'F-010',
  category: 'File Structure',
  severity: 'soft',
  type: 'static',
  status: 'fail',
  message: 'dashboard.yaml is missing',
  auto_fixable: false,
  explanation: null,
  confidence: null,
  detail: null,
  skip_reason: null,
  ...over,
})

const report = (over = {}) => ({
  agent_name: 'acme-scout',
  container_running: true,
  overall_status: 'issues',
  runtime: 'claude',
  checks: [],
  hard_count: 0,
  soft_count: 0,
  info_count: 0,
  ai_ran_at: '2026-09-21T09:00:00Z',
  static_ran_at: '2026-09-21T09:00:00Z',
  message: null,
  ...over,
})

async function mountWith(r) {
  getCompatibility.mockResolvedValue(r)
  const wrapper = mount(CompatibilityPanel, { props: { agent: { name: 'acme-scout' } } })
  await nextTick()
  await nextTick()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  getCompatibility.mockReset()
  fixCompatibilityIssue.mockReset()
})

describe('headline', () => {
  it('counts must-fix findings only, and names advisory ones separately', async () => {
    const w = await mountWith(report({ hard_count: 2, soft_count: 1 }))
    const text = w.text().replace(/\s+/g, ' ')
    expect(text).toContain('2 must-fix compatibility issues')
    expect(text).toContain('· 1 recommendation')
    expect(w.get('button').classes().join(' ')).toContain('status-danger')
  })

  it('does not call an advisory-only agent a compatibility issue', async () => {
    // The reported case: cornelius, 5 soft + 0 hard, announced "5 compatibility
    // issues" in warning tone on the landing tab.
    const w = await mountWith(report({ hard_count: 0, soft_count: 5 }))
    const text = w.text().replace(/\s+/g, ' ')
    expect(text).toContain('No must-fix issues · 5 recommendations')
    expect(text).not.toContain('compatibility issues')
    const cls = w.get('button').classes().join(' ')
    expect(cls).not.toContain('status-danger')
    // Not the success tone either: the API still calls this state `issues`, and
    // soft covers security findings (S-006/S-008) nobody should read as clean.
    expect(cls).not.toContain('status-success')
  })

  it('still says compatible when nothing at all was found', async () => {
    const w = await mountWith(report({ overall_status: 'compatible' }))
    expect(w.text()).toContain('Compatible — all checks passing')
    expect(w.get('button').classes().join(' ')).toContain('status-success')
  })

  it('keeps the unavailable state ahead of the count arms', async () => {
    // A stopped agent with no persisted snapshot has hard=soft=0, which would
    // otherwise render as a green "Compatible".
    const w = await mountWith(report({
      container_running: false,
      overall_status: 'unavailable',
      message: 'Agent is stopped — compatibility checks require a running agent.',
    }))
    expect(w.text()).toContain('Agent is stopped')
    expect(w.text()).not.toContain('Compatible — all checks passing')
  })
})

describe('skipped AI checks', () => {
  const keyless = () => report({
    soft_count: 1,
    checks: [
      check(),
      ...Array.from({ length: 30 }, (_, i) => check({
        check_id: `AI-${i}`,
        category: 'Persona',
        type: 'ai',
        status: 'skipped',
        skip_reason: 'no_api_key',
        message: 'AI check skipped (no API key configured)',
      })),
    ],
  })

  it('collapses them to one line and keeps the real findings visible', async () => {
    const w = await mountWith(keyless())
    await w.get('button').trigger('click')
    await nextTick()

    const text = w.text().replace(/\s+/g, ' ')
    expect(text).toContain('30 AI checks were skipped')
    expect(text).toContain('no Anthropic API key is configured')
    expect(text).toContain('dashboard.yaml is missing')

    const rows = w.findAll('li')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('dashboard.yaml is missing')
  })

  it('says something different when the analysis simply has not run', async () => {
    const w = await mountWith(report({
      ai_ran_at: null,
      checks: [check({
        check_id: 'X-002',
        type: 'ai',
        status: 'skipped',
        skip_reason: 'ai_not_run',
        message: 'AI check not yet run',
      })],
    }))
    await w.get('button').trigger('click')
    await nextTick()

    const text = w.text().replace(/\s+/g, ' ')
    expect(text).toContain('1 AI check was skipped')
    expect(text).toContain('has not run yet')
    expect(text).not.toContain('Anthropic API key')
  })

  it('leaves an AI check that produced a verdict in the list', async () => {
    const w = await mountWith(report({
      soft_count: 1,
      checks: [check({
        check_id: 'P-005',
        category: 'Persona',
        type: 'ai',
        status: 'fail',
        message: 'Did not meet the best-practice bar (see explanation)',
        explanation: 'The skills are generic dev methodology.',
      })],
    }))
    await w.get('button').trigger('click')
    await nextTick()

    expect(w.findAll('li')).toHaveLength(1)
    expect(w.text()).toContain('best-practice bar')
  })
})
