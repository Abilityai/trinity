// @vitest-environment jsdom
/**
 * trinity-enterprise#21 — per-client controls on the client roster, which lives
 * in Settings › Channels since trinity-enterprise#340 (`ClientRoster.vue`).
 *
 * Mounted against a stubbed api client. What is pinned:
 *  - each row shows its access state, and only the actions that state allows:
 *    Approve on pending, Revoke on approved, Block on everything not blocked,
 *    Unblock on blocked;
 *  - Block and Revoke ask first and say what they do; a cancel sends nothing;
 *  - each control posts {channel, client_id} to its route and reloads the roster;
 *  - Approve is the existing access-request decision, by the row's request id,
 *    and tells the pane so the pending list next door re-reads;
 *  - a different agent clears the previous agent's rows at once.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import ClientRoster from '@/components/agent-settings/channels/ClientRoster.vue'

const AGENT = 'scout'
const CLIENTS_URL = `/api/agents/${AGENT}/clients`

const row = (over) => ({
  channel: 'telegram', identity: '@x', display_name: null, verified_email: null,
  message_count: 1, last_active: '2026-11-01T10:00:00Z', client_id: 'x',
  access: 'none', access_request_id: null, blocked_at: null, blocked_by_email: null, block_reason: null,
  ...over,
})
const ALICE = () => row({ identity: '@alice', display_name: 'alice', client_id: '1001', verified_email: 'alice@example.com', access: 'granted' })
const CAROL = () => row({ channel: 'whatsapp', identity: 'whatsapp:+15559998888', client_id: 'whatsapp:+15559998888', verified_email: 'carol@example.com', access: 'pending', access_request_id: 'req-1' })
const BOB = () => row({ identity: '@bob', display_name: 'bob', client_id: '1003', access: 'blocked', blocked_at: '2026-11-01T09:00:00Z', blocked_by_email: 'owner@example.com', block_reason: 'spam' })
const DAN = () => row({ identity: '1004', client_id: '1004' })

let roster
let otherRoster = Promise.resolve({ data: [] })
beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(ax).forEach((f) => f.mockReset())
  Object.values(api).forEach((f) => f.mockReset())
  roster = [ALICE(), CAROL(), BOB(), DAN()]
  api.get.mockImplementation((url) => {
    if (url === CLIENTS_URL) return Promise.resolve({ data: roster })
    if (url === `/api/agents/other/clients`) return otherRoster
    return Promise.resolve({ data: {} })
  })
  api.post.mockResolvedValue({ data: {} })
  document.body.innerHTML = ''
})

const body = () => new DOMWrapper(document.body)
async function render() {
  const w = mount(ClientRoster, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  await w.find('[data-testid="channels-roster-open"]').trigger('click')
  await flushPromises()
  return w
}

const rowOf = (_w, text) => body().findAll('tbody tr').find((tr) => tr.text().includes(text))
const buttons = (tr) => tr.findAll('button').map((b) => b.text())
const dialog = () => document.body.querySelector('[data-testid="confirm-dialog"]')
const clickDialog = async (which) => {
  document.body.querySelector(`[data-testid="confirm-dialog-${which}"]`).click()
  await flushPromises()
}

describe('client roster access controls (ent#21)', () => {
  it('shows each state with only the actions it allows', async () => {
    const w = await render()
    expect(rowOf(w, 'alice').text()).toContain('Approved')
    expect(buttons(rowOf(w, 'alice'))).toEqual(['Revoke', 'Block'])
    expect(rowOf(w, 'carol@example.com').text()).toContain('Pending')
    expect(buttons(rowOf(w, 'carol@example.com'))).toEqual(['Approve', 'Block'])
    expect(rowOf(w, 'bob').text()).toContain('Blocked')
    expect(buttons(rowOf(w, 'bob'))).toEqual(['Unblock'])
    expect(rowOf(w, '1004').text()).toContain('By policy')
    expect(buttons(rowOf(w, '1004'))).toEqual(['Block'])
    const badge = body().find('[data-testid="client-access-telegram-1003"]')
    expect(badge.attributes('title')).toContain('owner@example.com')
    expect(badge.attributes('title')).toContain('spam')
    w.unmount()
  })

  it('Block asks first, posts the client, and reloads the roster', async () => {
    const w = await render()
    await rowOf(w, '1004').findAll('button').find((b) => b.text() === 'Block').trigger('click')
    await flushPromises()
    expect(dialog().textContent).toContain('dropped with no reply')
    expect(api.post).not.toHaveBeenCalled()
    roster = [ALICE(), CAROL(), BOB(), row({ identity: '1004', client_id: '1004', access: 'blocked' })]
    await clickDialog('confirm')
    expect(api.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/block`, { channel: 'telegram', client_id: '1004' })
    expect(buttons(rowOf(w, '1004'))).toEqual(['Unblock'])
    w.unmount()
  })

  it('a cancelled Revoke sends nothing; a confirmed one posts revoke', async () => {
    const w = await render()
    const revoke = () => rowOf(w, 'alice').findAll('button').find((b) => b.text() === 'Revoke')
    await revoke().trigger('click')
    await flushPromises()
    expect(dialog().textContent).toContain('alice@example.com loses access')
    await clickDialog('cancel')
    expect(api.post).not.toHaveBeenCalled()
    await revoke().trigger('click')
    await flushPromises()
    await clickDialog('confirm')
    expect(api.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/revoke`, { channel: 'telegram', client_id: '1001' })
    w.unmount()
  })

  it('Unblock posts straight away; Approve decides the row\'s request', async () => {
    const w = await render()
    await rowOf(w, 'bob').findAll('button').find((b) => b.text() === 'Unblock').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/unblock`, { channel: 'telegram', client_id: '1003' })
    await rowOf(w, 'carol@example.com').findAll('button').find((b) => b.text() === 'Approve').trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/access-requests/req-1/decide`, { approve: true })
    w.unmount()
  })

  it('an approval tells the pane, so the pending list next door re-reads', async () => {
    const w = await render()
    await rowOf(w, 'carol@example.com').findAll('button').find((b) => b.text() === 'Approve').trigger('click')
    await flushPromises()
    expect(w.emitted('requests-changed')).toHaveLength(1)
    expect(w.emitted('agent-updated')).toHaveLength(1)
    w.unmount()
  })

  it('a revoke tells the pane the shares changed; a block does not', async () => {
    const w = await render()
    await rowOf(w, '1004').findAll('button').find((b) => b.text() === 'Block').trigger('click')
    await flushPromises()
    await clickDialog('confirm')
    expect(w.emitted('agent-updated')).toBeUndefined()
    await rowOf(w, 'alice').findAll('button').find((b) => b.text() === 'Revoke').trigger('click')
    await flushPromises()
    await clickDialog('confirm')
    expect(w.emitted('agent-updated')).toHaveLength(1)
    w.unmount()
  })

  it('a bumped refresh key re-reads the roster', async () => {
    const w = await render()
    const before = api.get.mock.calls.filter(([u]) => u === CLIENTS_URL).length
    await w.setProps({ refreshKey: 1 })
    await flushPromises()
    expect(api.get.mock.calls.filter(([u]) => u === CLIENTS_URL).length).toBe(before + 1)
    w.unmount()
  })

  // #4310 review: the previous agent's rows must not stay on screen, or
  // clickable, while the next agent's roster loads.
  it('a different agent clears the rows at once, and a late answer for the old one is dropped', async () => {
    let release
    otherRoster = new Promise((resolve) => { release = () => resolve({ data: [DAN()] }) })
    const w = await render()
    expect(rowOf(w, 'alice')).toBeTruthy()
    await w.setProps({ agentName: 'other' })
    await flushPromises()
    expect(body().findAll('tbody tr')).toHaveLength(0)
    expect(w.find('[data-testid="channels-roster-open"]').exists()).toBe(false)
    release()
    await flushPromises()
    expect(w.find('[data-testid="channels-roster-summary"]').text()).toContain('1 person')
    w.unmount()
  })
})
