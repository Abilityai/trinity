// @vitest-environment jsdom
/**
 * trinity-enterprise#527 / #663 — the role card, mounted.
 *
 * The card is a projection of the agent's files, so what the component owns is
 * the honest rendering: no role → nothing; a failed role file says so; stale
 * says stale; readiness shows who/when; the owner's flip goes through a
 * confirm and a refusal lands next to the control. Mounted against the real
 * store with HTTP mocked (#2918 — a regex over the SFC proves none of that).
 *
 * ent#676 — the objectives are the portal projection of the ONE objective ↔
 * metric join: `actual` / `target` formatted by the declared type, `gap.status`
 * as a badge, `freshness` from the backend's verdict (never recomputed here),
 * a finding as the Workspace's own sentence for its code, and a NAMED line
 * when the objectives could not be read — never an empty block.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  // vitest 5's jsdom exposes `window.localStorage` as a getter-only accessor,
  // so a plain assignment throws ("has only a getter"). Define it instead.
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    },
  })
})
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(true)
  return { useAuthStore: () => ({ get isAuthenticated() { return authed.value }, get authHeader() { return { Authorization: 'Bearer jwt' } } }) }
})
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentRole from '@/components/portal/PortalAgentRole.vue'

const AGENT = 'sales-companion'
const URL = `/api/enterprise/client-portal/agents/${AGENT}/role`
const fresh = new Date(Date.now() - 3600_000).toISOString()
const hoursAgo = new Date(Date.now() - 3 * 3600_000).toISOString()
const longAgo = new Date(Date.now() - 40 * 86400_000).toISOString()

/** One projected metric row (the `PortalRoleMetric` shape). */
function metric(name, over = {}) {
  return {
    name, type: 'gauge', unit: null, target: null, actual: null, last_point_at: null,
    stale: false, freshness: null, gap: { status: 'not_computable' }, finding: null,
    ...over,
  }
}

function card(over = {}) {
  return {
    agent_name: AGENT,
    role: { id: 'sales-lead', title: 'Sales Lead', mission: 'Close ICP-fit pipeline.', status: 'active',
            review_by: '2099-12-01', stale: false, path: 'canon/roles/sales-lead.yaml', error: null },
    seat_source: 'person',
    objectives: [{
      id: 'q4-close-rate', statement: 'Raise close rate this quarter.', horizon: 'quarter', status: 'active', owned: true,
      metrics: [
        metric('close_rate', { type: 'percentage', unit: '%', target: 35, actual: 30, last_point_at: fresh, freshness: 'fresh', gap: { status: 'behind' } }),
        metric('reply_rate', { type: 'percentage', unit: '%', target: 20, actual: 25, last_point_at: hoursAgo, stale: true, freshness: 'stale', gap: { status: 'ahead' } }),
        metric('demo_count', { type: 'counter', target: 8, freshness: 'no_points' }),
        metric('deal_size', { unit: 'k', target: 10, actual: 12, last_point_at: longAgo, freshness: 'no_cadence', gap: { status: 'ahead' } }),
        metric('cycle_time', { type: 'duration', target: 3600, actual: 5400, last_point_at: fresh, freshness: 'fresh', gap: { status: 'behind' } }),
        metric('ghost_metric', { type: null, target: 1, finding: { code: 'metric_undeclared' } }),
      ],
    }],
    objectives_error: null,
    objectives_partial: false,
    finding_codes: [],
    readiness: { status: 'calibrating', changed_at: null, changed_by: null, source: 'template', unstamped_ready: false },
    walkthrough: { asks: 4, target: 10, rated_down: 1, unavailable: false },
    relationship: null,
    can_flip_readiness: true,
    unavailable: null,
    ...over,
  }
}

async function mountWith(payload) {
  axios.get.mockResolvedValueOnce({ data: payload })
  const w = mount(PortalAgentRole, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); document.body.innerHTML = '' })

describe('PortalAgentRole (mounted)', () => {
  it('an agent with no role renders nothing at all (AC 5)', async () => {
    const w = await mountWith({ agent_name: AGENT, role: null })
    expect(axios.get).toHaveBeenCalledWith(URL, expect.anything())
    expect(w.find('[data-testid="portal-agent-role"]').exists()).toBe(false)
  })

  it('shows the role, its objectives with stale marked stale, the relationship line, and the walkthrough', async () => {
    const w = await mountWith(card())
    expect(w.get('[data-testid="portal-role-title"]').text()).toBe('Sales Lead')
    expect(w.text()).toContain('Close ICP-fit pipeline.')
    expect(w.text()).toContain('canon/roles/sales-lead.yaml')
    expect(w.findAll('[data-testid="portal-role-objective"]')).toHaveLength(1)
    expect(w.findAll('[data-testid="portal-role-metric"]')).toHaveLength(5)          // everything not stale
    expect(w.findAll('[data-testid="portal-role-metric-stale"]')).toHaveLength(1)    // reply_rate
    expect(w.get('[data-testid="portal-role-metric-stale"]').text()).toContain('stale')
    expect(w.get('[data-testid="portal-role-relationship"]').text()).toContain('no assignment recorded')
    expect(w.get('[data-testid="portal-role-walkthrough"]').text()).toContain('4 of 10 asks')
    expect(w.get('[data-testid="portal-role-walkthrough"]').text()).toContain('1 rated down')
  })

  // --- ent#676: the projection of the objective ↔ metric join ---------------

  const row = (w, name) => w.findAll('[data-testid^="portal-role-metric"]')
    .find((el) => el.element.tagName === 'LI' && el.text().startsWith(name))

  it('shows actual against target, formatted by the declared type', async () => {
    const w = await mountWith(card())
    expect(row(w, 'close_rate').text()).toContain('30% / 35%')
    expect(row(w, 'cycle_time').text()).toContain('1h 30m / 1h 0m')    // seconds, never "5400 / 3600"
    expect(row(w, 'deal_size').text()).toContain('12 k / 10 k')
    expect(row(w, 'demo_count').text()).toContain('— / 8')             // no number yet is a dash, never a zero
  })

  it('a text target reads as the author wrote it, and no target shows none', async () => {
    const w = await mountWith(card({ objectives: [{ id: 'o', statement: 'S', owned: true, metrics: [
      metric('stage', { type: 'status', target: 'won', actual: 'negotiating', last_point_at: fresh, freshness: 'fresh' }),
      metric('bare', { actual: 3, last_point_at: fresh, freshness: 'fresh' }),
    ] }] }))
    expect(row(w, 'stage').text()).toContain('negotiating / won')
    expect(row(w, 'bare').text()).toContain('3')
    expect(row(w, 'bare').text()).not.toContain('/')
  })

  it.each([
    ['behind', 'behind', 'warning'],
    ['off_target', 'off target', 'warning'],
    ['ahead', 'ahead', 'success'],
    ['on_target', 'on target', 'success'],
  ])('gap %s reads "%s" as a %s badge', async (status, label, tone) => {
    const w = await mountWith(card({ objectives: [{ id: 'o', statement: 'S', owned: true, metrics: [
      metric('m', { target: 10, actual: 7, last_point_at: fresh, freshness: 'fresh', gap: { status } }),
    ] }] }))
    const badge = w.get('[data-testid="portal-role-metric-gap"]')
    expect(badge.text()).toBe(label)
    expect(badge.classes().join(' ')).toContain(`status-${tone}`)
  })

  it('a gap that cannot be computed shows no gap badge', async () => {
    const w = await mountWith(card())
    expect(row(w, 'demo_count').find('[data-testid="portal-role-metric-gap"]').exists()).toBe(false)
    expect(row(w, 'ghost_metric').find('[data-testid="portal-role-metric-gap"]').exists()).toBe(false)
  })

  it('freshness is the backend verdict: stale, no points yet, or the point time', async () => {
    const w = await mountWith(card())
    // Stale is orthogonal to the gap: both show, and the last value stays.
    const stale = row(w, 'reply_rate')
    expect(stale.text()).toContain('25% / 20%')
    expect(stale.text()).toContain('ahead')
    expect(stale.text()).toContain('stale')
    expect(stale.text()).not.toContain('as of')
    // Declared, never recorded: not late, not started.
    expect(row(w, 'demo_count').text()).toContain('no points yet')
    // Fresh shows when the point landed.
    expect(row(w, 'close_rate').text()).toContain('as of 1h ago')
    // No cadence declared is never stale, however old — the age still shows.
    const noCadence = row(w, 'deal_size')
    expect(noCadence.attributes('data-testid')).toBe('portal-role-metric')
    expect(noCadence.text()).toContain('as of')
    expect(noCadence.text()).not.toContain('stale')
  })

  it.each([
    ['metric_undeclared', "isn't being measured yet"],
    ['metric_not_declared_here', 'Another agent measures this one'],
    ['metric_retired', 'no longer measured'],
    ['direction_mismatch', 'disagree on which way is good'],
    ['direction_undeclared', 'whether higher or lower is better'],
    ['a_code_this_build_has_never_heard_of', "can't be compared with its target"],
  ])('finding %s is the Workspace\'s own sentence, never a blank', async (code, copy) => {
    const w = await mountWith(card({ objectives: [{ id: 'o', statement: 'S', owned: true, metrics: [
      metric('m', { target: 1, finding: { code } }),
    ] }] }))
    const line = w.get('[data-testid="portal-role-metric-finding"]')
    expect(line.text()).toContain(copy)
    // A row with no point has no time to show — never a dangling "as of".
    expect(row(w, 'm').text()).not.toContain('as of')
  })

  it('a row with no finding shows no finding line', async () => {
    const w = await mountWith(card())
    expect(row(w, 'close_rate').find('[data-testid="portal-role-metric-finding"]').exists()).toBe(false)
    expect(w.findAll('[data-testid="portal-role-metric-finding"]')).toHaveLength(1)   // ghost_metric only
  })

  it.each([
    ['objectives_rate_limited', 'being read a lot right now'],
    ['objectives_timeout', 'took too long to answer'],
    ['objectives_unreadable', "couldn't be read right now"],
    ['agent_unreachable', 'stopped answering'],
    ['objectives_incomplete', "Some objective files"],
    ['a_code_this_build_has_never_heard_of', "couldn't be read"],
  ])('no objectives because of %s says so instead of rendering nothing', async (code, copy) => {
    const w = await mountWith(card({ objectives: [], objectives_error: code }))
    expect(w.get('[data-testid="portal-role-objectives-error"]').text()).toContain(copy)
    expect(w.findAll('[data-testid="portal-role-objective"]')).toHaveLength(0)
    // The rest of the card is untouched — the owner can still flip readiness.
    expect(w.get('[data-testid="portal-role-title"]').text()).toBe('Sales Lead')
    expect(w.get('[data-testid="portal-role-readiness"]').text()).toContain('calibrating')
    expect(w.find('[data-testid="portal-role-flip"]').exists()).toBe(true)
  })

  it('an agent that simply has no objectives shows no objectives block', async () => {
    const w = await mountWith(card({ objectives: [], objectives_error: null }))
    expect(w.find('[data-testid="portal-role-objectives-error"]').exists()).toBe(false)
    expect(w.text()).not.toContain('Objectives')
  })

  it('an agent with no seat on record says so, and still lists the work it supports (ent#812)', async () => {
    const sentence = 'This agent neither holds a seat nor serves someone who holds one, so it owns no objective yet.'
    const none = await mountWith(card({ objectives: [], objectives_error: null, finding_codes: ['no_seat'] }))
    expect(none.get('[data-testid="portal-role-no-seat"]').text()).toBe(sentence)
    expect(none.find('[data-testid="portal-role-objectives-error"]').exists()).toBe(false)   // not a failed read
    const supporting = await mountWith(card({ finding_codes: ['no_seat'] }))
    expect(supporting.get('[data-testid="portal-role-no-seat"]').text()).toBe(sentence)
    expect(supporting.findAll('[data-testid="portal-role-objective"]')).toHaveLength(1)
  })

  it('a list with objective files that failed to read says it may be incomplete', async () => {
    const w = await mountWith(card({ objectives_partial: true }))
    expect(w.findAll('[data-testid="portal-role-objective"]')).toHaveLength(1)   // what did load still shows
    expect(w.get('[data-testid="portal-role-objectives-partial"]').text())
      .toBe("Some objective files in the agent's canon couldn't be read, so this list may be incomplete.")
    expect(w.find('[data-testid="portal-role-objectives-error"]').exists()).toBe(false)
  })

  it('a complete list carries no partial line', async () => {
    const w = await mountWith(card())
    expect(w.find('[data-testid="portal-role-objectives-partial"]').exists()).toBe(false)
  })

  it('objectives that did load never show the error line', async () => {
    const w = await mountWith(card())
    expect(w.find('[data-testid="portal-role-objectives-error"]').exists()).toBe(false)
  })

  it('a role file that failed to load says so, never an empty role', async () => {
    const w = await mountWith(card({ role: { id: 'sales-lead', error: 'role_file_not_found', path: 'canon/roles/sales-lead.yaml' }, objectives: [] }))
    expect(w.get('[data-testid="portal-role-title"]').text()).toBe('sales-lead')
    expect(w.get('[data-testid="portal-role-error"]').text()).toContain("not in the agent's canon yet")
  })

  it('says where the seat comes from, for this viewer — never a person (ent#814)', async () => {
    const mine = await mountWith(card({ seat_source: 'person' }))
    expect(mine.get('[data-testid="portal-role-seat-source"]').text()).toBe('Your seat on this agent')
    const held = await mountWith(card({ seat_source: 'holds' }))
    expect(held.get('[data-testid="portal-role-seat-source"]').text()).toBe('The agent holds this seat itself')
    const primary = await mountWith(card({ seat_source: 'primary' }))
    expect(primary.get('[data-testid="portal-role-seat-source"]').text()).toBe("Its primary's seat")
    const none = await mountWith(card({ seat_source: 'none' }))
    expect(none.find('[data-testid="portal-role-seat-source"]').exists()).toBe(false)
  })

  it("the viewer's relationship comes from their assignment, and is stated when there is none", async () => {
    const primary = await mountWith(card({ relationship: 'primary' }))
    expect(primary.get('[data-testid="portal-role-relationship"]').text()).toContain('primary — it works for you')
    const approver = await mountWith(card({ relationship: 'approver' }))
    expect(approver.get('[data-testid="portal-role-relationship"]').text()).toContain('approver')
    const unknown = await mountWith(card({ relationship: 'owner-of-everything' }))
    expect(unknown.get('[data-testid="portal-role-relationship"]').text()).toContain('no assignment recorded')
  })

  it('no seat on record is said once, on the role, and the supported work still shows', async () => {
    const sentence = 'This agent neither holds a seat nor serves someone who holds one, so it owns no objective yet.'
    const w = await mountWith(card({
      seat_source: 'none', finding_codes: ['no_seat'],
      role: { id: null, title: null, error: 'no_seat', path: null },
    }))
    expect(w.get('[data-testid="portal-role-error"]').text()).toBe(sentence)
    expect(w.find('[data-testid="portal-role-no-seat"]').exists()).toBe(false)
    expect(w.findAll('[data-testid="portal-role-objective"]').length).toBe(1)
  })

  it('a stopped agent says the files live in the container', async () => {
    const w = await mountWith(card({ role: null, unavailable: 'agent_stopped', objectives: [] }))
    expect(w.get('[data-testid="portal-role-unavailable"]').text()).toContain('stopped')
  })

  it('a rollout stamp is carried over, never attributed to a person (ent#689)', async () => {
    const w = await mountWith(card({ readiness: { status: 'ready', changed_at: fresh, changed_by: null, source: 'rollout', unstamped_ready: false } }))
    const r = w.get('[data-testid="portal-role-readiness"]').text()
    expect(r).toContain('carried over when the readiness gate shipped')
    expect(r).not.toContain(' by ')
    expect(r).not.toContain('ent#689')
  })

  it('a held brief is said beside the control that releases it (ent#689)', async () => {
    const owner = await mountWith(card({ brief_held: true }))
    expect(owner.get('[data-testid="portal-role-brief-held"]').text()).toBe('Its scheduled brief is paused until you mark it ready.')
    setActivePinia(createPinia()); document.body.innerHTML = ''
    const viewer = await mountWith(card({ brief_held: true, can_flip_readiness: false }))
    expect(viewer.get('[data-testid="portal-role-brief-held"]').text()).toBe('Its scheduled brief is paused until its owner marks it ready.')
    setActivePinia(createPinia()); document.body.innerHTML = ''
    const none = await mountWith(card({ brief_held: false }))
    expect(none.find('[data-testid="portal-role-brief-held"]').exists()).toBe(false)
  })

  it('readiness shows who flipped it and when; a template-claimed ready without a stamp is called out', async () => {
    const stamped = await mountWith(card({ readiness: { status: 'ready', changed_at: fresh, changed_by: 'owner@example.com', source: 'owner', unstamped_ready: false } }))
    const r = stamped.get('[data-testid="portal-role-readiness"]').text()
    expect(r).toContain('ready')
    expect(r).toContain('by owner@example.com')
    expect(stamped.find('[data-testid="portal-role-walkthrough"]').exists()).toBe(false)   // only while calibrating

    setActivePinia(createPinia()); document.body.innerHTML = ''
    const unstamped = await mountWith(card({ readiness: { status: 'calibrating', source: 'template', unstamped_ready: true } }))
    expect(unstamped.get('[data-testid="portal-role-unstamped"]').text()).toContain('no owner has stamped it')
  })

  it('the flip is offered to the owner only, goes through a confirm, posts, and re-reads', async () => {
    const w = await mountWith(card())
    await w.get('[data-testid="portal-role-flip"]').trigger('click')
    await flushPromises()
    // The confirm restates the consequence and is explicit that no schedule is switched on.
    expect(document.body.textContent).toContain('Mark this companion ready?')
    expect(document.body.textContent).toContain('does not switch any schedule on')
    expect(axios.post).not.toHaveBeenCalled()

    axios.post.mockResolvedValueOnce({ data: { status: 'ready' } })
    axios.get.mockResolvedValueOnce({ data: card({ readiness: { status: 'ready', changed_at: fresh, changed_by: 'owner@example.com', source: 'owner', unstamped_ready: false } }) })
    const confirm = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Mark ready' && b.closest('[role="dialog"], .fixed'))
    confirm.click()
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(`${URL}/readiness`, { status: 'ready' }, expect.anything())
    expect(w.get('[data-testid="portal-role-readiness"]').text()).toContain('ready')
    expect(w.get('[data-testid="portal-role-flip"]').text()).toBe('Back to calibrating')
  })

  it('a non-owner sees no flip control', async () => {
    const w = await mountWith(card({ can_flip_readiness: false }))
    expect(w.find('[data-testid="portal-role-flip"]').exists()).toBe(false)
  })

  it('a refused flip lands next to the control with the server\'s reason', async () => {
    const w = await mountWith(card())
    const store = useClientPortalStore()
    axios.post.mockRejectedValueOnce({ response: { status: 403, data: { detail: { code: 'readiness_owner_only', message: "Only the agent's owner can change its readiness — the person who created it on this instance, not an assignment." } } } })
    await store.flipAgentReadiness(AGENT, 'ready')
    await flushPromises()
    expect(w.text()).toContain("Only the agent's owner can change its readiness")
    expect(store.roleFlipping).toBe(false)
  })
})
