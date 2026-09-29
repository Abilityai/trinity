// @vitest-environment jsdom
/**
 * trinity-enterprise#527 rider — the owner's readiness stamp, mounted (not a
 * regex over the template) on BOTH surfaces: the fleet grid tile and the agents
 * list rows (the `lg` and `md` secondary lines). The words come from the shared
 * `readinessBadge` predicate (readinessBadge.spec.js); this pins the placement
 * and that each surface passes the row's `brief_held` through.
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
import AgentListPanel from '../../src/components/AgentListPanel.vue'

const STUBS = {
  AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true, AutonomyToggle: true,
  ScanlineReveal: { template: '<div><slot /></div>' },
}

function tile(readiness, briefHeld = false) {
  return mount(AgentTile, {
    props: {
      agent: { name: 'sales-companion', status: 'running', runtime: 'claude-code', tags: [], readiness, brief_held: briefHeld },
      now: Date.parse('2026-09-28T12:00:00Z'),
    },
    global: { stubs: STUBS },
  })
}

beforeEach(() => setActivePinia(createPinia()))

describe('AgentTile — readiness stamp (ent#527 rider)', () => {
  it('a calibrating companion shows a warning badge that says what it holds back', () => {
    const b = tile({ status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' }, true)
      .find('[data-testid="readiness-badge"]')
    expect(b.exists()).toBe(true)
    expect(b.text()).toBe('calibrating')
    expect(b.attributes('title')).toContain('scheduled brief is paused')
    expect(b.classes().join(' ')).toContain('status-warning')
  })

  it('a calibrating companion with no held brief claims no pause', () => {
    const b = tile({ status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' }, false)
      .find('[data-testid="readiness-badge"]')
    expect(b.text()).toBe('calibrating')
    expect(b.attributes('title')).not.toContain('paused')
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

describe('AgentListPanel — readiness stamp on both secondary lines (ent#527 rider)', () => {
  function panel() {
    const row = (name, extra) => ({
      name, status: 'running', runtime: 'claude-code', tags: [], is_owner: true, ...extra,
    })
    return mount(AgentListPanel, {
      props: {
        agents: [
          row('held-companion', {
            readiness: { status: 'calibrating', changed_at: '2026-09-28T10:00:00Z', source: 'owner' },
            brief_held: true,
          }),
          row('ready-companion', {
            readiness: { status: 'ready', changed_at: '2026-09-27T10:00:00Z', source: 'owner' },
            brief_held: false,
          }),
          row('plain-agent', { readiness: null, brief_held: false }),
        ],
      },
      global: { stubs: { ...STUBS, ReadOnlyToggle: true, CapacityMeter: true, RouterLink: true } },
    })
  }

  for (const line of ['row-secondary-lg', 'row-secondary-md']) {
    it(`${line}: one badge per stamped row, none on the unstamped one`, () => {
      const lines = panel().findAll(`[data-testid="${line}"]`)
      expect(lines.length).toBe(3)
      const badges = lines.map(l => l.findAll('[data-testid="readiness-badge"]'))
      expect(badges.map(b => b.length).sort()).toEqual([0, 1, 1])
      const all = badges.flat()
      const cal = all.find(b => b.text() === 'calibrating')
      const ready = all.find(b => b.text() === 'ready')
      expect(cal.attributes('title')).toContain('scheduled brief is paused')
      expect(cal.classes().join(' ')).toContain('status-warning')
      expect(ready.classes().join(' ')).toContain('status-success')
    })
  }
})
