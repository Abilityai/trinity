// @vitest-environment jsdom
/**
 * trinity-enterprise#21 — per-client controls on the Sharing-tab client roster.
 *
 * Mounted against stubbed transports; the channel panels are stubbed (their own
 * specs cover them). What is pinned:
 *  - each row shows its access state, and only the actions that state allows:
 *    Approve on pending, Revoke on approved, Block on everything not blocked,
 *    Unblock on blocked;
 *  - Block and Revoke ask first and say what they do; a cancel sends nothing;
 *  - each control posts {channel, client_id} to its route and reloads the roster;
 *  - Approve is the existing access-request decision, by the row's request id.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import SharingPanel from '@/components/SharingPanel.vue'

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
beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(ax).forEach((f) => f.mockReset())
  Object.values(api).forEach((f) => f.mockReset())
  roster = [ALICE(), CAROL(), BOB(), DAN()]
  ax.get.mockImplementation((url) => {
    if (url === CLIENTS_URL) return Promise.resolve({ data: roster })
    if (url.endsWith('/access-policy')) return Promise.resolve({ data: { require_email: true, open_access: false, group_auth_mode: 'none' } })
    if (url.endsWith('/access-requests')) return Promise.resolve({ data: [] })
    return Promise.resolve({ data: {} })
  })
  ax.post.mockResolvedValue({ data: {} })
  api.get.mockResolvedValue({ data: {} })
})

const STUB = { template: '<div />' }
async function render() {
  const w = mount(SharingPanel, {
    props: { agentName: AGENT },
    attachTo: document.body,
    global: {
      stubs: {
        ChannelDisclosure: STUB, ChannelConfigRow: STUB, PublicLinksPanel: STUB, SlackChannelPanel: STUB,
        SlackAgentBotPanel: STUB, TelegramChannelPanel: STUB, WhatsAppChannelPanel: STUB,
        VoipChannelPanel: STUB, ConnectorChannelPanel: STUB, PortalClientsPanel: STUB,
        'router-link': { template: '<a><slot /></a>' },
      },
      mocks: { $route: { query: {} } },
    },
  })
  await flushPromises()
  return w
}

const rowOf = (w, text) => w.findAll('tbody tr').find((tr) => tr.text().includes(text))
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
    const badge = w.find('[data-testid="client-access-telegram-1003"]')
    expect(badge.attributes('title')).toContain('owner@example.com')
    expect(badge.attributes('title')).toContain('spam')
    w.unmount()
  })

  it('Block asks first, posts the client, and reloads the roster', async () => {
    const w = await render()
    await rowOf(w, '1004').findAll('button').find((b) => b.text() === 'Block').trigger('click')
    await flushPromises()
    expect(dialog().textContent).toContain('dropped with no reply')
    expect(ax.post).not.toHaveBeenCalled()
    roster = [ALICE(), CAROL(), BOB(), row({ identity: '1004', client_id: '1004', access: 'blocked' })]
    await clickDialog('confirm')
    expect(ax.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/block`,
      { channel: 'telegram', client_id: '1004' }, expect.anything())
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
    expect(ax.post).not.toHaveBeenCalled()
    await revoke().trigger('click')
    await flushPromises()
    await clickDialog('confirm')
    expect(ax.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/revoke`,
      { channel: 'telegram', client_id: '1001' }, expect.anything())
    w.unmount()
  })

  it('Unblock posts straight away; Approve decides the row\'s request', async () => {
    const w = await render()
    await rowOf(w, 'bob').findAll('button').find((b) => b.text() === 'Unblock').trigger('click')
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/clients/unblock`,
      { channel: 'telegram', client_id: '1003' }, expect.anything())
    await rowOf(w, 'carol@example.com').findAll('button').find((b) => b.text() === 'Approve').trigger('click')
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/access-requests/req-1/decide`,
      { approve: true }, expect.anything())
    w.unmount()
  })
})
