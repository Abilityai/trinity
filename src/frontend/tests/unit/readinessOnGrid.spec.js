// @vitest-environment jsdom
/**
 * trinity-enterprise#527 rider — the fleet grid tile shows the owner's readiness
 * stamp, mounted (not a regex over the template). The agents list renders the
 * same `readinessBadge` predicate; its rows are covered by the shared helper's
 * spec (readinessBadge.spec.js).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import AgentTile from '../../src/components/AgentTile.vue'

const STUBS = {
  AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true, AutonomyToggle: true,
  ScanlineReveal: { template: '<div><slot /></div>' },
}

function tile(readiness) {
  return mount(AgentTile, {
    props: {
      agent: { name: 'sales-companion', status: 'running', runtime: 'claude-code', tags: [], readiness },
      now: Date.parse('2026-09-28T12:00:00Z'),
    },
    global: { stubs: STUBS },
  })
}

beforeEach(() => setActivePinia(createPinia()))

describe('AgentTile — readiness stamp (ent#527 rider)', () => {
  it('a calibrating companion shows a warning badge that says what it holds back', () => {
    const b = tile({ status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' })
      .find('[data-testid="readiness-badge"]')
    expect(b.exists()).toBe(true)
    expect(b.text()).toBe('calibrating')
    expect(b.attributes('title')).toContain('scheduled brief is paused')
    expect(b.classes().join(' ')).toContain('status-warning')
  })

  it('a ready companion shows a success badge', () => {
    const b = tile({ status: 'ready', changed_at: '2026-09-28T10:00:00Z', source: 'owner' })
      .find('[data-testid="readiness-badge"]')
    expect(b.text()).toBe('ready')
    expect(b.classes().join(' ')).toContain('status-success')
  })

  it('no stamp, no badge — never a guessed state', () => {
    expect(tile(null).find('[data-testid="readiness-badge"]').exists()).toBe(false)
    expect(tile(undefined).find('[data-testid="readiness-badge"]').exists()).toBe(false)
  })
})
