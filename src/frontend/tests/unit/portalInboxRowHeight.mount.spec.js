// @vitest-environment jsdom
/**
 * #4229 — an Inbox ask row keeps its height when it is answered.
 *
 * A pending ask with no priority / expiry badge used to render its badge line
 * EMPTY (§3g A10 dropped the always-true "Waiting on you"); the ended badge then
 * landed in that empty line and the row grew by one badge, shifting every row
 * below it under the reader. Thread and mention rows never had that gap — New
 * swaps to Read in place — so this spec pins the same shape for asks: the badge
 * line always holds exactly one badge, pending or ended, and a pending row that
 * already carries a priority / expiry badge gets no second filler badge.
 *
 * Mounted (#2918): asserts on the rendered badge line, not the source text.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

import PortalInboxList from '@/components/portal/PortalInboxList.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const inMin = (m) => new Date(Date.now() + m * 60_000).toISOString()
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium', title: `Ask ${id}`, question: 'Go?',
  options: ['Approve', 'Reject'], status: 'pending', created_at: '2026-11-03T09:00:00Z',
  expires_at: null, ended_at: null, ended_by: null, replaces: null, replaced_by: null,
  agent_ending: null, ending_reason: null, ...over,
})
const item = (a) => ({ key: `ask:${a.id}`, type: 'ask', id: a.id, agent_name: a.agent_name,
  title: a.title, status: a.status, at: a.ended_at || a.created_at, ask: a })
const badgeLine = (w, id) => w.find(`[data-testid="inbox-row-badges-ask:${id}"]`)
const badges = (w, id) => badgeLine(w, id).findAll('span.rounded-full').map((b) => b.text())

let wrapper
afterEach(() => { wrapper?.unmount(); wrapper = null })

describe('PortalInboxList — an ask row is one height in every state (#4229)', () => {
  it('a pending ask with nothing urgent about it still carries one badge: Unanswered', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [item(ask('a'))] } })
    await flushPromises()
    const pending = wrapper.find('[data-testid="inbox-row-pending-ask:a"]')
    expect(pending.exists()).toBe(true)
    expect(pending.text()).toBe('Unanswered')
    expect(badges(wrapper, 'a')).toEqual(['Unanswered'])
  })

  it('answering swaps the badge in place — one badge before, one after, nothing added', async () => {
    const a = ask('a')
    wrapper = mount(PortalInboxList, { props: { items: [item(a)] } })
    await flushPromises()
    const before = badgeLine(wrapper, 'a')
    expect(before.element.children.length).toBe(1)

    const answered = { ...a, status: 'answered', ended_by: 'you', ended_at: '2026-11-03T09:05:00Z', answer: 'Approve' }
    await wrapper.setProps({ items: [item(answered)] })
    const after = badgeLine(wrapper, 'a')
    expect(after.element.children.length).toBe(1)
    expect(wrapper.find('[data-testid="inbox-row-pending-ask:a"]').exists()).toBe(false)
    const ended = wrapper.find('[data-testid="inbox-row-ended-ask:a"]')
    expect(ended.exists()).toBe(true)
    expect(ended.text()).toMatch(/· you$/)
  })

  it('a pending row that already says what differs gets no filler badge (§3g A10 holds)', async () => {
    wrapper = mount(PortalInboxList, { props: { items: [
      item(ask('p', { priority: 'high' })),
      item(ask('e', { expires_at: inMin(30.5) })), // minutes are floored
    ] } })
    await flushPromises()
    expect(wrapper.find('[data-testid="inbox-row-pending-ask:p"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="inbox-row-pending-ask:e"]').exists()).toBe(false)
    expect(badges(wrapper, 'p')).toEqual(['High'])
    expect(badges(wrapper, 'e')).toEqual(['Expires in 30m'])
  })

  it('every ended state is one badge too — expired, cancelled, dismissed, no longer available', async () => {
    const ended = (id, over) => item(ask(id, { ended_at: '2026-11-03T10:00:00Z', ...over }))
    wrapper = mount(PortalInboxList, { props: { items: [
      ended('x', { status: 'expired' }),
      ended('c', { status: 'cancelled' }),
      ended('d', { status: 'dismissed', ended_by: 'you' }),
      ended('u', { status: 'unavailable' }),
    ] } })
    await flushPromises()
    expect(badges(wrapper, 'x')).toEqual(['Expired'])
    expect(badges(wrapper, 'c')).toEqual(['Cancelled'])
    expect(badges(wrapper, 'd')).toEqual(['Dismissed · you'])
    expect(badges(wrapper, 'u')).toEqual(['No longer available'])
  })
})
