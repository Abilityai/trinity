// @vitest-environment jsdom
/**
 * #4281 slice F — the Schedules tab says what tier a schedule's runs get,
 * lets an owner take a share holder's schedule over, warns on "Run now" and
 * marks a restricted run. MOUNTED against the real panel.
 *
 * The server decides every fact (`runs_as`, `runs_as_reason`, `created_by`,
 * `created_by_you`, `can_take_over`, `turn_integrity.restricted_turn`). The
 * page renders what the read carries and nothing else, so a share holder can
 * never be shown another creator's name (D5). The take-over sends back the
 * exact version the owner reviewed (`expected_updated_at` + `reviewed`), and
 * each 409 is said in the dialog.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const server = vi.hoisted(() => ({ get: null, post: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => server.get(...a)),
    post: vi.fn((...a) => server.post(...a)),
    delete: vi.fn(async () => ({ data: {} })),
    put: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import axios from 'axios'
import SchedulesPanel from '../../src/components/SchedulesPanel.vue'
import { useAuthStore } from '../../src/stores/auth'

const REASON = 'Sentinel reason from the server: no shell, no owner tools.'

const schedule = (id, over = {}) => ({
  id, agent_name: 'fin', name: `sched ${id}`, cron_expression: '0 9 * * *',
  message: `Full prompt of ${id}, longer than the 150-character row preview so the review must show what the row truncates. `.repeat(2),
  enabled: true, timezone: 'UTC', description: null,
  created_at: '2026-10-01T00:00:00Z', updated_at: `2026-11-0${id === 'b' ? 2 : 1}T08:00:00.123456Z`,
  last_run_at: null, next_run_at: null, timeout_seconds: 3600,
  allowed_tools: ['Read', 'Grep'], model: 'claude-sentinel-model',
  validation_enabled: true, validation_prompt: `Validate ${id}`,
  deliver_to_workspace_email: null, can_manage_webhook: true,
  runs_as: 'owner', runs_as_reason: { code: 'creator_owner_tier', message: 'owner' },
  created_by_you: false, created_by: 'Owner Person', can_take_over: false,
  ...over,
})

const restricted = (id, over = {}) => schedule(id, {
  runs_as: 'shared_person',
  runs_as_reason: { code: 'creator_not_owner_tier', message: REASON },
  created_by: 'Priya Q.', can_take_over: true, ...over,
})

async function flush() {
  for (let i = 0; i < 6; i++) { await nextTick(); await flushPromises() }
}

let lists
let webhook
let takeOver
let trigger
let executions
function serve() {
  server.get = vi.fn(async (url) => {
    if (/\/schedules$/.test(url)) {
      const next = lists.length > 1 ? lists.shift() : lists[0]
      if (next instanceof Error) throw next
      return { data: next }
    }
    if (/\/webhook$/.test(url)) {
      if (webhook instanceof Error) throw webhook
      return { data: webhook }
    }
    if (/\/executions\?limit=/.test(url)) return { data: executions }
    return { data: {} }
  })
  server.post = vi.fn(async (url, body) => {
    if (/\/take-over$/.test(url)) return takeOver(url, body)
    if (/\/trigger$/.test(url)) return trigger(url, body)
    return { data: {} }
  })
}

const mountPanel = (props = {}) => mount(SchedulesPanel, {
  props: { agentName: 'fin', ownerTier: true, ...props },
  global: { stubs: { 'router-link': true, ModelSelector: true, SkeletonLoader: true, ScheduleAnalyticsCard: true } },
  attachTo: document.body,
})

// BaseModal teleports to <body>, so dialogs are read from the document.
const page = () => new DOMWrapper(document.body)
const rowOf = (w, id) => w.find(`[data-schedule-id="${id}"]`)
const takeOverPosts = () => axios.post.mock.calls.filter(([u]) => /\/take-over$/.test(u))
const conflict = (code) => Object.assign(new Error('409'), { response: { status: 409, data: { detail: { code, message: `server ${code}` } } } })

beforeEach(() => {
  setActivePinia(createPinia())
  axios.get.mockClear()
  axios.post.mockClear()
  document.body.innerHTML = ''
  lists = [[]]
  webhook = { schedule_id: 'x', has_token: true, webhook_enabled: true, webhook_url: 'https://example.com/api/webhooks/tok', auth_enabled: false, has_secret: false }
  takeOver = vi.fn(async () => ({ data: {} }))
  trigger = vi.fn(async () => ({ data: {} }))
  executions = []
  serve()
})

describe('the run-tier badge on a schedule row', () => {
  it('a shared_person schedule carries the badge, the server reason and the creator name', async () => {
    lists = [[restricted('a'), schedule('b')]]
    const w = mountPanel()
    await flush()
    const a = rowOf(w, 'a')
    expect(a.find('[data-testid="schedule-runs-as-badge"]').text()).toBe('Runs without shell')
    expect(a.find('[data-testid="schedule-runs-as-reason"]').text()).toBe(REASON)
    expect(a.find('[data-testid="schedule-created-by"]').text()).toBe('Created by Priya Q.')
    expect(rowOf(w, 'b').find('[data-testid="schedule-runs-as-badge"]').exists()).toBe(false)
    w.unmount()
  })

  it('runs_as null (not computed) shows no badge, and no "owner" claim either', async () => {
    lists = [[schedule('a', { runs_as: null, runs_as_reason: null })]]
    const w = mountPanel()
    await flush()
    expect(rowOf(w, 'a').find('[data-testid="schedule-runs-as-badge"]').exists()).toBe(false)
    expect(rowOf(w, 'a').find('[data-testid="schedule-runs-as-reason"]').exists()).toBe(false)
    w.unmount()
  })

  it('a share holder sees "you created this", and no name, when the read carries none', async () => {
    lists = [[restricted('a', { created_by: null, created_by_you: true, can_take_over: false })]]
    const w = mountPanel({ ownerTier: false })
    await flush()
    const a = rowOf(w, 'a')
    expect(a.find('[data-testid="schedule-runs-as-badge"]').exists()).toBe(true)
    expect(a.find('[data-testid="schedule-created-by"]').text()).toBe('You created this schedule')
    expect(a.text()).not.toContain('Priya')
    expect(a.find('[data-testid="schedule-take-over"]').exists()).toBe(false)
    w.unmount()
  })

  it('a failed refresh keeps the badge on screen (#1634/#1926)', async () => {
    lists = [[restricted('a')], new Error('network down')]
    const w = mountPanel()
    await flush()
    expect(rowOf(w, 'a').find('[data-testid="schedule-runs-as-badge"]').exists()).toBe(true)
    await w.vm.$.setupState.loadSchedules()
    await flush()
    expect(w.text()).toContain("Couldn't refresh schedules")
    expect(rowOf(w, 'a').find('[data-testid="schedule-runs-as-badge"]').exists()).toBe(true)
    w.unmount()
  })
})

describe('the create dialog tells a share holder before they save', () => {
  const openCreate = async (w) => {
    await w.findAll('button').find((b) => b.text().includes('New Schedule')).trigger('click')
    await flush()
  }

  it('below the owner tier, the notice is there', async () => {
    const w = mountPanel({ ownerTier: false })
    await flush()
    await openCreate(w)
    expect(w.find('[data-testid="schedule-create-share-notice"]').text())
      .toMatch(/no shell commands and no tools the owner added/)
    w.unmount()
  })

  it('at the owner tier, no notice', async () => {
    const w = mountPanel({ ownerTier: true })
    await flush()
    await openCreate(w)
    expect(w.find('[data-testid="schedule-create-share-notice"]').exists()).toBe(false)
    w.unmount()
  })

  it("a named refusal from the save (D2's 409) shows its message, not the raw detail", async () => {
    const w = mountPanel({ ownerTier: false })
    await flush()
    server.post = vi.fn(async () => {
      throw Object.assign(new Error('409'), { response: { status: 409, data: { detail: {
        code: 'schedule_cannot_run_for_creator_on_runtime', message: 'Sentinel: this runtime cannot run it.', runtime: 'gemini-cli',
      } } } })
    })
    await openCreate(w)
    await w.find('input[placeholder="Daily report"]').setValue('brief')
    await w.find('input[placeholder="0 9 * * *"]').setValue('0 9 * * *')
    await w.find('textarea').setValue('do it')
    await w.find('form').trigger('submit')
    await flush()
    expect(w.text()).toContain('Sentinel: this runtime cannot run it.')
    expect(w.text()).not.toContain('[object Object]')
    expect(w.text()).not.toContain('schedule_cannot_run_for_creator_on_runtime')
    w.unmount()
  })
})

describe('Take over', () => {
  const open = async (w, id = 'a') => {
    await rowOf(w, id).find('[data-testid="schedule-take-over"]').trigger('click')
    await flush()
    return page().find('[data-testid="take-over-dialog"]')
  }

  it('is offered only where the read says can_take_over', async () => {
    lists = [[restricted('a'), restricted('b', { can_take_over: false }), schedule('c')]]
    const w = mountPanel()
    await flush()
    expect(rowOf(w, 'a').find('[data-testid="schedule-take-over"]').exists()).toBe(true)
    expect(rowOf(w, 'b').find('[data-testid="schedule-take-over"]').exists()).toBe(false)
    expect(rowOf(w, 'c').find('[data-testid="schedule-take-over"]').exists()).toBe(false)
    w.unmount()
  })

  it('the review shows the full prompt, tools, model, validation, delivery and webhook state', async () => {
    const s = restricted('a', { deliver_to_workspace_email: 'p@example.com' })
    lists = [[s]]
    const w = mountPanel()
    await flush()
    const d = await open(w)
    expect(d.find('[data-testid="take-over-prompt"]').text()).toBe(s.message.trim())
    expect(d.find('[data-testid="take-over-tools"]').text()).toContain('Read, Grep')
    expect(d.find('[data-testid="take-over-model"]').text()).toContain('claude-sentinel-model')
    expect(d.find('[data-testid="take-over-validation"]').text()).toContain('Validate a')
    expect(d.find('[data-testid="take-over-delivery"]').text()).toContain('p@example.com')
    expect(d.find('[data-testid="take-over-webhook"]').text()).toMatch(/webhook URL is live/)
    expect(d.text()).toContain('Priya Q.')
    w.unmount()
  })

  it('sends back exactly the version reviewed, and clears another person’s delivery by default (D8)', async () => {
    const s = restricted('a', { deliver_to_workspace_email: 'p@example.com' })
    lists = [[s], [schedule('a', { updated_at: '2026-12-01T00:00:00Z', message: 'a LATER prompt' })]]
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts()).toHaveLength(1)
    const [url, body] = takeOverPosts()[0]
    expect(url).toBe('/api/agents/fin/schedules/a/take-over')
    expect(body).toEqual({
      expected_updated_at: s.updated_at,
      reviewed: {
        message: s.message, allowed_tools: ['Read', 'Grep'], model: 'claude-sentinel-model',
        validation_enabled: true, validation_prompt: 'Validate a',
      },
      delivery: 'clear',
    })
    expect(page().find('[data-testid="take-over-dialog"]').exists()).toBe(false)
    w.unmount()
  })

  it('keeping the delivery repeats the address', async () => {
    lists = [[restricted('a', { deliver_to_workspace_email: 'p@example.com' })]]
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-delivery-keep"]').setValue(true)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts()[0][1]).toMatchObject({ delivery: 'keep', keep_delivery_to: 'p@example.com' })
    w.unmount()
  })

  it("a delivery to the owner's own Workspace is left alone (no delivery field)", async () => {
    lists = [[restricted('a', { deliver_to_workspace_email: 'Owner@Example.com' })]]
    const w = mountPanel()
    useAuthStore().user = { email: 'owner@example.com' }
    await flush()
    const d = await open(w)
    expect(d.find('[data-testid="take-over-delivery-keep"]').exists()).toBe(false)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts()[0][1]).not.toHaveProperty('delivery')
    w.unmount()
  })

  it('a list refetch while the review is open does not change what is sent', async () => {
    const s = restricted('a')
    lists = [[s], [restricted('a', { updated_at: '2026-12-01T00:00:00Z', message: 'a LATER prompt' })]]
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await w.vm.$.setupState.loadSchedules()
    await flush()
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts()[0][1].reviewed.message).toBe(s.message)
    expect(takeOverPosts()[0][1].expected_updated_at).toBe(s.updated_at)
    w.unmount()
  })

  it('409 changed_since_review: says so, and shows the current version to review again', async () => {
    const later = restricted('a', { updated_at: '2026-12-01T00:00:00Z', message: 'The LATER prompt the creator wrote' })
    lists = [[restricted('a')], [later]]
    takeOver = vi.fn(async () => { throw conflict('schedule_changed_since_review') })
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    const dialog = page().find('[data-testid="take-over-dialog"]')
    expect(dialog.exists()).toBe(true)
    expect(dialog.find('[data-testid="take-over-error"]').text()).toMatch(/changed after you opened this review/)
    expect(dialog.find('[data-testid="take-over-prompt"]').text()).toBe('The LATER prompt the creator wrote')
    takeOver = vi.fn(async () => ({ data: {} }))
    await dialog.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts().at(-1)[1].expected_updated_at).toBe('2026-12-01T00:00:00Z')
    w.unmount()
  })

  it('409 run_in_progress: says so in the dialog and keeps it open', async () => {
    lists = [[restricted('a')]]
    takeOver = vi.fn(async () => { throw conflict('schedule_run_in_progress') })
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(page().find('[data-testid="take-over-error"]').text()).toMatch(/in progress or waiting to retry/)
    expect(page().find('[data-testid="take-over-dialog"]').exists()).toBe(true)
    w.unmount()
  })

  it('a webhook read that fails says the take-over revokes any webhook anyway', async () => {
    lists = [[restricted('a')]]
    webhook = new Error('boom')
    const w = mountPanel()
    await flush()
    const d = await open(w)
    expect(d.find('[data-testid="take-over-webhook"]').text()).toMatch(/Couldn't read/)
    expect(d.find('[data-testid="take-over-webhook"]').text()).toMatch(/revokes any webhook/)
    w.unmount()
  })

  // Review of slice F: these pin the guards that a revert left green.

  it('a "keep" chosen on one version is not carried to the next (D8)', async () => {
    // The creator changes the delivery to someone else between the open and
    // the confirm. The next confirm must not keep that new address unasked.
    const later = restricted('a', { updated_at: '2026-12-01T00:00:00Z', deliver_to_workspace_email: 'q@example.com' })
    lists = [[restricted('a', { deliver_to_workspace_email: 'p@example.com' })], [later]]
    takeOver = vi.fn(async () => { throw conflict('schedule_changed_since_review') })
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-delivery-keep"]').setValue(true)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts()[0][1]).toMatchObject({ delivery: 'keep', keep_delivery_to: 'p@example.com' })
    expect(page().find('[data-testid="take-over-delivery"]').text()).toContain('q@example.com')
    takeOver = vi.fn(async () => ({ data: {} }))
    await page().find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    const last = takeOverPosts().at(-1)[1]
    expect(last.delivery).toBe('clear')
    expect(last).not.toHaveProperty('keep_delivery_to')
    w.unmount()
  })

  it('a "keep" chosen and cancelled is not carried into the next review', async () => {
    lists = [[restricted('a', { deliver_to_workspace_email: 'p@example.com' })]]
    const w = mountPanel()
    await flush()
    let d = await open(w)
    await d.find('[data-testid="take-over-delivery-keep"]').setValue(true)
    await d.find('[data-testid="take-over-cancel"]').trigger('click')
    await flush()
    d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(takeOverPosts().at(-1)[1].delivery).toBe('clear')
    w.unmount()
  })

  it('409 not_restricted: shows the current version and offers nothing to adopt', async () => {
    lists = [[restricted('a')], [schedule('a', { updated_at: '2026-12-01T00:00:00Z' })]]
    takeOver = vi.fn(async () => { throw conflict('schedule_not_restricted') })
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(page().find('[data-testid="take-over-error"]').text()).toMatch(/already have the owner's access/)
    const confirm = page().find('[data-testid="take-over-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    await flush()
    expect(takeOverPosts()).toHaveLength(1)
    w.unmount()
  })

  it('409 changed_since_review on a schedule that is gone: nothing left to adopt', async () => {
    lists = [[restricted('a')], []]
    takeOver = vi.fn(async () => { throw conflict('schedule_changed_since_review') })
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    expect(page().find('[data-testid="take-over-confirm"]').attributes('disabled')).toBeDefined()
    w.unmount()
  })

  it('Esc does not close the review while the take-over is in flight', async () => {
    lists = [[restricted('a')]]
    let release
    takeOver = vi.fn(() => new Promise((resolve) => { release = () => resolve({ data: {} }) }))
    const w = mountPanel()
    await flush()
    const d = await open(w)
    await d.find('[data-testid="take-over-confirm"]').trigger('click')
    await flush()
    await page().find('[role="dialog"]').trigger('keydown', { key: 'Escape' })
    await flush()
    expect(page().find('[data-testid="take-over-dialog"]').exists()).toBe(true)
    release()
    await flush()
    expect(page().find('[data-testid="take-over-dialog"]').exists()).toBe(false)
    w.unmount()
  })

  it('initial focus is not on the Take over button', async () => {
    lists = [[restricted('a')]]
    const w = mountPanel()
    await flush()
    await open(w)
    expect(document.activeElement?.getAttribute('data-testid')).not.toBe('take-over-confirm')
    w.unmount()
  })
})

describe('Run now on a share holder’s schedule (D9)', () => {
  const runNow = (w, id) => rowOf(w, id).find('button[title^="Run now"]')

  it('the owner tier is asked first, told this one run has their access', async () => {
    lists = [[restricted('a')]]
    const w = mountPanel()
    await flush()
    await runNow(w, 'a').trigger('click')
    await flush()
    expect(trigger).not.toHaveBeenCalled()
    expect(page().find('[data-testid="confirm-dialog-message"]').text()).toMatch(/this one run has your access/i)
    await page().find('[data-testid="confirm-dialog-confirm"]').trigger('click')
    await flush()
    expect(trigger).toHaveBeenCalledTimes(1)
    w.unmount()
  })

  it('an owner-tier schedule runs at once, with no question', async () => {
    lists = [[schedule('a')]]
    const w = mountPanel()
    await flush()
    await runNow(w, 'a').trigger('click')
    await flush()
    expect(trigger).toHaveBeenCalledTimes(1)
    w.unmount()
  })
})

describe('a restricted run is marked in the history and its detail', () => {
  const record = { tier: 'share', kind: 'person', cause: 'creator_not_owner_tier', schedule_id: 'a', creator_id: 7, shell: false }
  const exec = (id, ti) => ({
    id, schedule_id: 'a', status: 'success', triggered_by: 'schedule', started_at: '2026-11-08T09:00:00Z',
    message: 'm', response: 'r', turn_integrity: ti,
  })

  it('only the restricted run carries the badge; opening it shows the sentence', async () => {
    lists = [[restricted('a')]]
    executions = [exec('e1', JSON.stringify({ restricted_turn: record })), exec('e2', null)]
    const w = mountPanel()
    await flush()
    await rowOf(w, 'a').findAll('button').find((b) => b.text().includes('execution history')).trigger('click')
    await flush()
    const badges = w.findAll('[data-testid="schedule-run-restricted"]')
    expect(badges).toHaveLength(1)
    expect(badges[0].text()).toBe('ran without shell')
    await badges[0].element.closest('[data-testid="schedule-exec-row"]').click()
    await flush()
    expect(w.find('[data-testid="schedule-exec-restricted-line"]').text()).toMatch(/person who created the schedule/)
    w.unmount()
  })
})
