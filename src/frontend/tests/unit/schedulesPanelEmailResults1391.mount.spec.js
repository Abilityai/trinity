// @vitest-environment jsdom
/**
 * trinity-enterprise#1391 — the Schedules tab carries the control the result
 * mail and the assistant card name ("Stop emailing"), and says when the last
 * result was not emailed and why. MOUNTED against the real panel.
 *
 * The read never carries an address: the row says "to you" only when the
 * server says `email_results_to_me`. The button posts `{ enabled: false }` to
 * the schedule's `email-me` route and the list is read again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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

const schedule = (id, over = {}) => ({
  id, agent_name: 'fin', name: `Daily digest: 9am ${id}`, cron_expression: '0 9 * * *',
  message: 'Summarise', enabled: true, timezone: 'UTC', description: null,
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-11-01T08:00:00Z',
  last_run_at: null, next_run_at: null, timeout_seconds: 3600, allowed_tools: null, model: null,
  validation_enabled: false, validation_prompt: null, deliver_to_workspace_email: null,
  can_manage_webhook: true, runs_as: 'owner', runs_as_reason: { code: 'creator_owner_tier', message: 'owner' },
  created_by_you: true, created_by: null, can_take_over: false,
  email_results: false, email_results_to_me: false, email_results_error: null, email_results_error_at: null,
  ...over,
})

async function flush() {
  for (let i = 0; i < 6; i++) { await nextTick(); await flushPromises() }
}

let lists
beforeEach(() => {
  setActivePinia(createPinia())
  axios.get.mockClear()
  axios.post.mockClear()
  document.body.innerHTML = ''
  lists = [[]]
  server.get = vi.fn(async (url) => {
    if (/\/schedules$/.test(url)) return { data: lists.length > 1 ? lists.shift() : lists[0] }
    return { data: [] }
  })
  server.post = vi.fn(async () => ({ data: {} }))
})

const mountPanel = (props = {}) => mount(SchedulesPanel, {
  props: { agentName: 'fin', ownerTier: true, ...props },
  global: { stubs: { 'router-link': true, ModelSelector: true, SkeletonLoader: true, ScheduleAnalyticsCard: true } },
  attachTo: document.body,
})
const rowOf = (w, id) => w.find(`[data-schedule-id="${id}"]`)

describe('result mail on a schedule row', () => {
  it('"Stop emailing" turns it off through the email-me route and the row reads again', async () => {
    lists = [[schedule('a', { email_results: true, email_results_to_me: true })], [schedule('a')]]
    const w = mountPanel()
    await flush()
    const row = rowOf(w, 'a')
    expect(row.find('[data-testid="schedule-email-results"]').text()).toContain("emailed to you")
    await row.find('[data-testid="schedule-email-stop"]').trigger('click')
    await flush()
    expect(axios.post).toHaveBeenCalledWith('/api/agents/fin/schedules/a/email-me', { enabled: false }, expect.anything())
    expect(rowOf(w, 'a').find('[data-testid="schedule-email-results"]').exists()).toBe(false)
    w.unmount()
  })

  it('a dropped mail says why, and someone else\'s setting never says "to you"', async () => {
    lists = [[schedule('a', { email_results: true, email_results_to_me: false,
      email_results_error: 'You reached today\'s limit of result emails for this address.' })]]
    const w = mountPanel()
    await flush()
    const row = rowOf(w, 'a')
    expect(row.find('[data-testid="schedule-email-error"]').text())
      .toBe('Last result not emailed: You reached today\'s limit of result emails for this address.')
    expect(row.text()).toContain('emailed to the person who turned it on')
    expect(row.text()).not.toContain('emailed to you')
    w.unmount()
  })

  it('a person without the owner tier sees the state but no control (the route would refuse them)', async () => {
    lists = [[schedule('a', { email_results: true })]]
    const w = mountPanel({ ownerTier: false })
    await flush()
    expect(rowOf(w, 'a').find('[data-testid="schedule-email-results"]').exists()).toBe(true)
    expect(rowOf(w, 'a').find('[data-testid="schedule-email-stop"]').exists()).toBe(false)
    w.unmount()
  })

  it('nothing is shown on a schedule that does not mail', async () => {
    lists = [[schedule('a')]]
    const w = mountPanel()
    await flush()
    expect(rowOf(w, 'a').find('[data-testid="schedule-email-results"]').exists()).toBe(false)
    w.unmount()
  })
})
