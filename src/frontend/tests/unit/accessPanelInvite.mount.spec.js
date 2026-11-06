// @vitest-environment jsdom
/**
 * trinity-enterprise#459 — Agent Detail › Access: Resend invite on a Pending row,
 * and Add person saying what happened to the invitation.
 *
 * Mounted: the button sends real mail, so the tests read the requests it makes.
 * What is pinned: the button only on Pending rows; disabled, with the reason,
 * when the server says nothing can be sent; the in-flight, sent and failed states;
 * a refusal shown on its own row; never "emailed" when no mail went out.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))

import AccessPanel from '@/components/AccessPanel.vue'
import { useAuthStore } from '@/stores/auth'

const AGENT = 'scout'
const ACCESS_URL = `/api/agents/${AGENT}/access`
const LEE = 'lee@example.com'
const INVITE_URL = `/api/agents/${AGENT}/share/${encodeURIComponent(LEE)}/invite`
const SHARE_URL = `/api/agents/${AGENT}/share`

let rows
const lee = (over = {}) => ({ email: LEE, username: null, role: null, status: 'pending', last_active: null, allow_proactive: false, invite_available: true, ...over })
const sam = () => ({ email: 'sam@example.com', username: 'sam', role: 'operator', status: 'active', last_active: null, allow_proactive: false, invite_available: false })

function render() {
  const auth = useAuthStore()
  auth.user = { role: 'creator' }
  auth.isAuthenticated = true
  return mount(AccessPanel, {
    props: { agentName: AGENT, agentOwner: 'olga' },
    attachTo: document.body,
    global: { stubs: { AgentAssignmentsSection: true } },
  })
}

const rowOf = (w, email) => w.findAll('li').find((li) => li.text().includes(email))
const button = (w, email) => rowOf(w, email).find('[data-testid="access-resend-invite"]')
const refused = (status, detail) => Object.assign(new Error(String(status)), { response: { status, data: { detail } } })
const invites = () => ax.post.mock.calls.filter(([u]) => u === INVITE_URL)

async function add(w, email) {
  await w.find('input[type="email"]').setValue(email)
  await w.find('form').trigger('submit')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  rows = [sam(), lee()]
  ax.get.mockImplementation(async (url) => (url === ACCESS_URL ? { data: rows.map((r) => ({ ...r })) } : { data: [] }))
  api.get.mockResolvedValue({ data: [] })
  ax.post.mockResolvedValue({ data: { email: LEE, sent: true } })
})

describe('the Resend invite button', () => {
  it('is on a Pending row and not on an Active one', async () => {
    const w = render()
    await flushPromises()
    expect(button(w, LEE).exists()).toBe(true)
    expect(button(w, LEE).text()).toBe('Resend invite')
    expect(button(w, 'sam@example.com').exists()).toBe(false)
  })

  it('sends for that row and then says it went', async () => {
    const w = render()
    await flushPromises()
    await button(w, LEE).trigger('click')
    await flushPromises()
    expect(invites()).toHaveLength(1)
    expect(button(w, LEE).text()).toBe('Invite sent')
  })

  it('shows it is in flight and sends once', async () => {
    let finish
    ax.post.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
    const w = render()
    await flushPromises()
    await button(w, LEE).trigger('click')
    await flushPromises()
    expect(button(w, LEE).text()).toContain('Sending…')
    expect(button(w, LEE).attributes('disabled')).toBeDefined()
    await button(w, LEE).trigger('click')
    expect(invites()).toHaveLength(1)
    finish({ data: { email: LEE, sent: true } })
    await flushPromises()
    expect(button(w, LEE).text()).toBe('Invite sent')
  })

  it('is disabled and says why when the server says nothing can be sent', async () => {
    rows = [lee({ invite_available: false })]
    const w = render()
    await flushPromises()
    expect(button(w, LEE).attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="access-invite-unavailable"]').text()).toContain('tell people how to sign in')
    expect(button(w, LEE).attributes('title')).toContain('tell people how to sign in')
    await button(w, LEE).trigger('click')
    expect(invites()).toHaveLength(0)
  })

  it('states no reason when an invitation can be sent', async () => {
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="access-invite-unavailable"]').exists()).toBe(false)
    expect(button(w, LEE).attributes('title')).toBeUndefined()
  })

  it.each([
    [429, 'Too many invitations sent recently. Try again in 60 seconds.', 'Too many invitations'],
    [502, { code: 'invite_not_sent', message: "The invitation email couldn't be sent. Try again." }, "couldn't be sent"],
    [409, { code: 'invite_unavailable', message: "Email isn't set up for sign-in on this instance." }, "isn't set up"],
  ])('shows a %i refusal on its own row and does not claim it went', async (status, detail, expected) => {
    ax.post.mockRejectedValue(refused(status, detail))
    const w = render()
    await flushPromises()
    await button(w, LEE).trigger('click')
    await flushPromises()
    expect(rowOf(w, LEE).text()).toContain(expected)
    expect(button(w, LEE).text()).toBe('Resend invite')
    expect(button(w, LEE).attributes('disabled')).toBeUndefined()
  })
})

describe('Add person says what happened to the invitation', () => {
  it.each([
    ['sent', 'emailed them an invitation', true],
    ['not_needed', 'Added new@example.com.', false],
    ['invalid_address', "isn't a single email address", false],
    ['unavailable', 'No invitation went out', false],
    ['rate_limited', 'too many were sent recently', false],
    ['failed', 'the invitation email failed', false],
  ])('%s', async (invitation, expected, claimsMail) => {
    ax.post.mockImplementation(async (url) => (url === SHARE_URL ? { data: { invitation } } : { data: {} }))
    const w = render()
    await flushPromises()
    await add(w, 'new@example.com')
    const status = w.find('[role="status"]').text()
    expect(status).toContain(expected)
    expect(status.includes('emailed them')).toBe(claimsMail)
  })
})
