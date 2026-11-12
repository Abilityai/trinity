// @vitest-environment jsdom
/**
 * trinity-enterprise#1236 — Trinity wakes the agent steward, and the Steward
 * card says so.
 *
 * The read's `steward.wake` drives the card: whether Trinity wakes the agent,
 * the one named reason it does not (not started — waking is off until a
 * person starts it — paused, autonomy off …), the last run, and what wakes it
 * next. The creator or the agent's owner gets Start / Pause, which call the
 * store. While it is woken, the card never says "No sweep scheduled". An older
 * backend sends no `wake`, and the card keeps its earlier lines.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ProjectStewardCard from '@/components/portal/projects/ProjectStewardCard.vue'
import { useProjectsStore } from '@/stores/projects'

const ME = 'alice@example.com'

const WAKE = {
  state: 'off', reason: 'not_started', started: false, paused_by: null, paused_at: null,
  last_run: null, last_skip: null, next: null, can_change: true, is_agent_owner: true,
}

function project(wake, stewardOver = {}) {
  return {
    id: 'prj_1', created_by: ME, can: { edit: true },
    steward: { kind: 'agent', ref: 'pm-steward', last_sweep_at: null,
      schedule: { state: 'none', next_run_at: null }, ...(wake === undefined ? {} : { wake }), ...stewardOver },
    rollup: { by_status: {}, health: null, health_due: false },
    proposal: null, steward_access: null,
  }
}

const card = (p) => mount(ProjectStewardCard, {
  props: { project: p, myEmail: ME },
  global: { stubs: { RouterLink: RouterLinkStub } },
})
const tid = (w, id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => setActivePinia(createPinia()))

describe('ent#1236 — the card names the wake state', () => {
  it('says "Not started" by default and offers Start, which calls the store', async () => {
    const start = vi.spyOn(useProjectsStore(), 'startStewardWake').mockResolvedValue({})
    const w = card(project(WAKE))
    expect(tid(w, 'project-steward-wake-state').text()).toBe('Not started. A person starts waking the steward.')
    await tid(w, 'project-steward-wake-start').trigger('click')
    expect(start).toHaveBeenCalledWith('prj_1')
    expect(tid(w, 'project-steward-wake-pause').exists()).toBe(false)
    expect(tid(w, 'project-steward-wake-start-hint').text()).toContain('one catch-up turn')
  })

  it('while it is woken: the next wake, the last run, a Pause, and never "No sweep scheduled"', async () => {
    const pause = vi.spyOn(useProjectsStore(), 'pauseStewardWake').mockResolvedValue({})
    const w = card(project({
      ...WAKE, state: 'on', reason: null, started: true,
      last_run: { at: '2026-11-12T09:00:00Z', status: 'success', execution_id: 'ex-1' },
      next: { kind: 'owed', at: '2026-11-12T09:01:00Z', reasons: ['ask', 'task'] },
    }))
    expect(tid(w, 'project-steward-wake-state').text()).toBe('Trinity wakes it')
    expect(tid(w, 'project-steward-wake-next').text()).toContain('Next wake soon (an ask was answered, a task changed)')
    expect(tid(w, 'project-steward-last-run').text()).toContain('finished')
    expect(w.text()).not.toContain('No sweep scheduled')
    expect(w.text()).not.toContain("Trinity doesn't run it")
    expect(tid(w, 'project-steward-schedule').text()).toBe('')
    expect(tid(w, 'project-steward-wake-start-hint').exists()).toBe(false)
    await tid(w, 'project-steward-wake-pause').trigger('click')
    expect(pause).toHaveBeenCalledWith('prj_1')
  })

  it('a person pause names who paused it and offers Start again', () => {
    const w = card(project({ ...WAKE, reason: 'paused', paused_by: 'bob@example.com', paused_at: '2026-11-12T08:00:00Z' }))
    expect(tid(w, 'project-steward-wake-state').text()).toBe('Paused by bob@example.com')
    expect(tid(w, 'project-steward-wake-start').exists()).toBe(true)
  })

  it('autonomy off is named; only the agent owner is linked to the setting, with the warning', () => {
    const off = { ...WAKE, reason: 'autonomy_off', started: true }
    const owner = card(project(off))
    expect(tid(owner, 'project-steward-wake-state').text()).toBe("pm-steward has autonomy off, so it isn't woken.")
    expect(tid(owner, 'project-steward-autonomy').text()).toContain('Autonomy also lets its schedules and reminders run.')
    expect(owner.findComponent(RouterLinkStub).props('to')).toEqual({ path: '/agents/pm-steward' })
    expect(tid(owner, 'project-steward-wake-pause').exists()).toBe(true)
    const member = card(project({ ...off, is_agent_owner: false, can_change: false }))
    expect(tid(member, 'project-steward-autonomy').exists()).toBe(false)
    expect(tid(member, 'project-steward-wake-pause').exists()).toBe(false)
  })

  it('a member reads the state but gets no Start or Pause', () => {
    const w = card(project({ ...WAKE, can_change: false, is_agent_owner: false }))
    expect(tid(w, 'project-steward-wake-state').text()).toContain('Not started')
    expect(w.find('[data-testid^="project-steward-wake-st"]').exists()).toBe(true)   // the state line only
    expect(tid(w, 'project-steward-wake-start').exists()).toBe(false)
  })

  it('a project that is not active offers no verb', () => {
    const w = card(project({ ...WAKE, reason: 'project_not_running', started: true }))
    expect(tid(w, 'project-steward-wake-state').text()).toBe("This project isn't active, so the steward isn't woken.")
    expect(tid(w, 'project-steward-wake-start').exists()).toBe(false)
    expect(tid(w, 'project-steward-wake-pause').exists()).toBe(false)
  })

  it('an unreadable wake is said, never shown as on', () => {
    const w = card(project({ ...WAKE, state: 'unknown', reason: 'unknown', can_change: false }))
    expect(tid(w, 'project-steward-wake-state').text()).toBe("Couldn't check whether Trinity wakes it.")
  })

  it('a skipped wake is said while it is on', () => {
    const w = card(project({ ...WAKE, state: 'on', reason: null, started: true,
      last_skip: { reason: 'agent_stopped', at: '2026-11-12T08:00:00Z' }, next: { kind: 'daily', at: null } }))
    expect(tid(w, 'project-steward-wake-skip').text()).toBe('Last wake skipped: the agent was stopped')
    expect(tid(w, 'project-steward-wake-next').text()).toContain('daily while work is open')
  })

  it("the server's refusal renders next to the button", async () => {
    vi.spyOn(useProjectsStore(), 'startStewardWake').mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'steward_not_agent', message: 'Trinity wakes only an agent steward; this one is a person.' } } },
    })
    const w = card(project(WAKE))
    await tid(w, 'project-steward-wake-start').trigger('click')
    await flushPromises()
    expect(tid(w, 'project-steward-wake').find('[role="alert"]').text()).toBe('Trinity wakes only an agent steward; this one is a person.')
  })
})

describe('ent#1236 — an older backend', () => {
  it('without `wake` keeps the earlier lines', () => {
    const w = card(project(undefined))
    expect(tid(w, 'project-steward-wake').exists()).toBe(false)
    expect(tid(w, 'project-steward-schedule').text()).toContain('No sweep scheduled')
    expect(tid(w, 'project-steward-how').text()).toContain("Trinity doesn't run it")
  })
})

describe('ent#1236 — the store calls the routes and re-reads the project', () => {
  it('Start and Pause post to the steward routes', async () => {
    const { portalHttp } = await import('@/stores/clientPortal')
    const post = vi.spyOn(portalHttp, 'post').mockResolvedValue({ data: {} })
    const get = vi.spyOn(portalHttp, 'get').mockResolvedValue({ data: { projects: [] } })
    const store = useProjectsStore()
    await store.startStewardWake('prj_1')
    await store.pauseStewardWake('prj_1')
    const urls = post.mock.calls.map((c) => c[0])
    expect(urls.some((u) => u.endsWith('/prj_1/steward/resume'))).toBe(true)
    expect(urls.some((u) => u.endsWith('/prj_1/steward/pause'))).toBe(true)
    expect(get).toHaveBeenCalled()
  })
})
