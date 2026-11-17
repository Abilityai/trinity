// @vitest-environment jsdom
/**
 * trinity-enterprise#1054 — Settings › Users: invite a person, and the list of
 * invitations with who accepted which terms version, when.
 *
 * Mounted against a stubbed `api`; what is read is the rendered panel and the
 * requests it sends. Every write here is a grant (an invitation lets a person
 * sign in), so the tests pin:
 *  - the form opens pre-filled (role `user`, the 14-day expiry) and sends the
 *    name, email, role and expiry;
 *  - an unset terms version is a NAMED refusal next to the form, not a raw code;
 *  - the list shows each invitation's status, inviter, sent and accepted
 *    times, and the terms version accepted;
 *  - revoke asks first and restates the consequence; cancel sends nothing;
 *  - resend is offered only on a pending invitation;
 *  - loading, failed and empty are three different things.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import InvitesPanel from '@/components/settings/InvitesPanel.vue'

const BASE = '/api/enterprise/user-management'
const USERS = [{ id: 1, username: 'admin', email: 'admin@example.com' }]

const PENDING = () => ({
  id: 'inv_1', email: 'maya@example.com', name: 'Maya', role: 'user', token: 't1', status: 'pending',
  invited_by: '1', created_at: '2026-11-01T10:00:00Z', email_sent_at: '2026-11-01T10:00:05Z',
  expires_at: '2026-11-15T10:00:00Z',
})
const ACCEPTED = () => ({
  id: 'inv_2', email: 'lee@example.com', name: 'Lee', role: 'user', token: 't2', status: 'accepted',
  invited_by: '1', created_at: '2026-10-20T10:00:00Z', email_sent_at: '2026-10-20T10:00:05Z',
  accepted_at: '2026-10-21T09:00:00Z', accepted_user_id: 7,
})
const REVOKED = () => ({
  id: 'inv_3', email: 'old@example.com', name: null, role: 'user', token: 't3', status: 'revoked',
  invited_by: '1', created_at: '2026-10-01T10:00:00Z', revoked_at: '2026-10-02T10:00:00Z',
})
const ACCEPTANCES = () => [{ id: 1, user_id: 7, email: 'lee@example.com', terms_version: '2026-10', accepted_at: '2026-10-21T09:00:00Z', invite_id: 'inv_2' }]

let invites
function routeGets() {
  api.get.mockImplementation(async (url) => {
    if (url === `${BASE}/invites`) return { data: invites.map((r) => ({ ...r })) }
    if (url === `${BASE}/terms/acceptances`) return { data: ACCEPTANCES() }
    throw new Error(`unexpected GET ${url}`)
  })
}

const refused = (status, detail) => Object.assign(new Error(String(status)), { response: { status, data: { detail } } })
const render = () => mount(InvitesPanel, { props: { users: USERS }, attachTo: document.body })
const rowOf = (w, email) => w.findAll('[data-testid="invite-row"]').find((r) => r.text().includes(email))
const clickDialog = async (which) => {
  document.body.querySelector(`[data-testid="confirm-dialog-${which}"]`).click()
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  for (const fn of Object.values(api)) fn.mockReset()
  invites = [PENDING(), ACCEPTED(), REVOKED()]
  routeGets()
})

describe('inviting a person', () => {
  it('opens pre-filled and sends name, email, role and expiry', async () => {
    api.post.mockResolvedValue({ data: { ...PENDING(), id: 'inv_9', email: 'sam@example.com', name: 'Sam', email_sent: true } })
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="invite-role"]').element.value).toBe('user')
    expect(w.find('[data-testid="invite-expiry"]').element.value).toBe('14')
    await w.find('[data-testid="invite-name"]').setValue('Sam')
    await w.find('[data-testid="invite-email"]').setValue('sam@example.com')
    await w.find('[data-testid="invite-form"]').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`${BASE}/invites`, { email: 'sam@example.com', name: 'Sam', role: 'user', expires_days: 14 })
    expect(w.find('[data-testid="invite-result"]').text()).toContain('sam@example.com')
    // the list is re-read so the new invitation appears
    expect(api.get.mock.calls.filter(([u]) => u === `${BASE}/invites`).length).toBe(2)
  })

  it('the send action is disabled until there is an email', async () => {
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="invite-send"]').attributes('disabled')).toBeDefined()
    await w.find('[data-testid="invite-email"]').setValue('sam@example.com')
    expect(w.find('[data-testid="invite-send"]').attributes('disabled')).toBeUndefined()
  })

  it('an unset terms version is a named refusal next to the form', async () => {
    api.post.mockRejectedValue(refused(422, { code: 'terms_version_unset', message: 'set the terms version first' }))
    const w = render()
    await flushPromises()
    await w.find('[data-testid="invite-email"]').setValue('sam@example.com')
    await w.find('[data-testid="invite-form"]').trigger('submit')
    await flushPromises()
    const err = w.find('[data-testid="invite-form"] [data-testid="inline-error"]')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('terms version')
    expect(err.text()).toContain('terms_version')
    expect(w.find('[data-testid="invite-email"]').element.value).toBe('sam@example.com')
  })

  it('says so when the email could not be sent', async () => {
    api.post.mockResolvedValue({ data: { ...PENDING(), email: 'sam@example.com', email_sent: false } })
    const w = render()
    await flushPromises()
    await w.find('[data-testid="invite-email"]').setValue('sam@example.com')
    await w.find('[data-testid="invite-form"]').trigger('submit')
    await flushPromises()
    expect(w.find('[data-testid="invite-result"]').text()).toMatch(/not sent/i)
  })
})

describe('the list of invitations', () => {
  it('shows status, inviter, sent, accepted and the terms version', async () => {
    const w = render()
    await flushPromises()
    const lee = rowOf(w, 'lee@example.com')
    expect(lee.text()).toContain('Lee')
    expect(lee.find('[data-testid="invite-status"]').text()).toBe('accepted')
    expect(lee.text()).toContain('admin@example.com')
    expect(lee.find('[data-testid="invite-terms-version"]').text()).toBe('2026-10')
    expect(lee.find('[data-testid="invite-accepted-at"] time').attributes('datetime')).toBe('2026-10-21T09:00:00Z')
    expect(rowOf(w, 'maya@example.com').find('[data-testid="invite-status"]').text()).toBe('pending')
    expect(rowOf(w, 'old@example.com').find('[data-testid="invite-status"]').text()).toBe('revoked')
    expect(w.find('[data-testid="invites-total"]').text()).toContain('3')
  })

  it('offers resend and revoke on a pending invitation only', async () => {
    const w = render()
    await flushPromises()
    expect(rowOf(w, 'maya@example.com').find('[data-testid="invite-resend"]').exists()).toBe(true)
    expect(rowOf(w, 'maya@example.com').find('[data-testid="invite-revoke"]').exists()).toBe(true)
    expect(rowOf(w, 'lee@example.com').find('[data-testid="invite-resend"]').exists()).toBe(false)
    expect(rowOf(w, 'old@example.com').find('[data-testid="invite-revoke"]').exists()).toBe(false)
  })

  it('revoke asks first, restates the consequence, and a cancel sends nothing', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'maya@example.com').find('[data-testid="invite-revoke"]').trigger('click')
    await flushPromises()
    const msg = document.body.querySelector('[data-testid="confirm-dialog-message"]').textContent
    expect(msg).toContain('maya@example.com')
    expect(msg).toMatch(/can no longer sign in/i)
    await clickDialog('cancel')
    expect(api.delete).not.toHaveBeenCalled()
  })

  it('a confirmed revoke deletes the invitation and re-reads the list', async () => {
    api.delete.mockResolvedValue({ data: null })
    const w = render()
    await flushPromises()
    await rowOf(w, 'maya@example.com').find('[data-testid="invite-revoke"]').trigger('click')
    await flushPromises()
    invites = [{ ...PENDING(), status: 'revoked' }, ACCEPTED(), REVOKED()]
    await clickDialog('confirm')
    expect(api.delete).toHaveBeenCalledWith(`${BASE}/invites/inv_1`)
    expect(rowOf(w, 'maya@example.com').find('[data-testid="invite-status"]').text()).toBe('revoked')
  })

  it('a failed revoke is shown beside the list and persists', async () => {
    api.delete.mockRejectedValue(refused(403, 'Interactive session required'))
    const w = render()
    await flushPromises()
    await rowOf(w, 'maya@example.com').find('[data-testid="invite-revoke"]').trigger('click')
    await flushPromises()
    await clickDialog('confirm')
    expect(w.find('[data-testid="invites-action-error"] [data-testid="inline-error"]').exists()).toBe(true)
  })

  it('resend posts to the invitation and re-reads the list', async () => {
    api.post.mockResolvedValue({ data: { ...PENDING(), email_sent: true } })
    const w = render()
    await flushPromises()
    await rowOf(w, 'maya@example.com').find('[data-testid="invite-resend"]').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`${BASE}/invites/inv_1/resend`)
    expect(api.get.mock.calls.filter(([u]) => u === `${BASE}/invites`).length).toBe(2)
  })
})

describe('loading, failed and empty', () => {
  it('a skeleton before the first answer, never the empty copy', async () => {
    api.get.mockReturnValue(new Promise(() => {}))
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="invites-loading"]').attributes('aria-busy')).toBe('true')
    expect(w.find('[data-testid="invites-empty"]').exists()).toBe(false)
  })

  it('a failed load offers a retry, never the empty copy', async () => {
    api.get.mockRejectedValue(refused(500, 'down'))
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="invites-empty"]').exists()).toBe(false)
  })

  it('an empty answer names the next step', async () => {
    invites = []
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="invites-empty"]').text()).toMatch(/invite/i)
  })

  it('a failed acceptance read still shows the invitations', async () => {
    api.get.mockImplementation(async (url) => {
      if (url === `${BASE}/invites`) return { data: invites }
      throw refused(500, 'acceptances down')
    })
    const w = render()
    await flushPromises()
    expect(rowOf(w, 'lee@example.com').find('[data-testid="invite-terms-version"]').text()).toBe('—')
  })
})
