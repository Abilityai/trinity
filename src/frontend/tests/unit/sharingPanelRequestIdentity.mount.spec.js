// @vitest-environment jsdom
/**
 * trinity-enterprise#737 — the Channels tab's pending access requests show the
 * requester as a person, not a string.
 *
 * Mounted against stubbed transports; channel panels are stubbed. Pinned:
 *  - a captured identity leads with the name, then @handle, the channel and its
 *    own id, then the email as one attribute;
 *  - where the viewer has seen this identity before ("known as"), it says so,
 *    capped at three chips with a counted overflow;
 *  - a public-link request says it carries an email only, and a request with no
 *    identity says "Identity not captured" — never a blank;
 *  - Approve still decides the request by its id.
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
const base = (over) => ({
  id: 'r', agent_name: AGENT, email: 'x@example.com', channel: 'telegram', requested_at: '2026-11-03T09:00:00Z',
  status: 'pending', requester_channel_id: null, requester_handle: null, requester_name: null,
  requester_chat_id: null, requester_thread_id: null, identity_captured: false, known_as: [], ...over,
})
const CAROL = () => base({
  id: 'r1', email: 'carol@example.com', requester_channel_id: '4242', requester_handle: 'carol_tg',
  requester_name: 'Carol Jones', identity_captured: true,
  known_as: [
    { agent_name: 'a-two', status: 'approved', email: 'carol@work.example' },
    { agent_name: 'a-two', status: 'verified', email: 'carol@work.example' },
    { agent_name: 'a-three', status: 'denied', email: 'carol@example.com' },
    { agent_name: 'a-four', status: 'pending', email: 'carol@example.com' },
  ],
})
const HANDLE_ONLY = () => base({ id: 'r2', email: 'dee@example.com', channel: 'slack', requester_channel_id: 'U9',
                                 requester_handle: 'dee', identity_captured: true })
const WEB = () => base({ id: 'r3', email: 'web@example.com', channel: 'web' })
const OLD = () => base({ id: 'r4', email: 'old@example.com', channel: 'whatsapp' })

beforeEach(() => {
  setActivePinia(createPinia())
  Object.values(ax).forEach((f) => f.mockReset())
  Object.values(api).forEach((f) => f.mockReset())
  ax.get.mockImplementation((url) => {
    if (url.endsWith('/access-requests')) return Promise.resolve({ data: [CAROL(), HANDLE_ONLY(), WEB(), OLD()] })
    if (url.endsWith('/access-policy')) return Promise.resolve({ data: { require_email: true, open_access: false, group_auth_mode: 'none' } })
    if (url.endsWith('/clients')) return Promise.resolve({ data: [] })
    return Promise.resolve({ data: {} })
  })
  ax.post.mockResolvedValue({ data: {} })
  api.get.mockResolvedValue({ data: {} })
})

const STUB = { template: '<div />' }
async function render() {
  const w = mount(SharingPanel, {
    props: { agentName: AGENT },
    global: {
      stubs: {
        ChannelDisclosure: STUB, ChannelConfigRow: STUB, PublicLinksPanel: STUB, SlackChannelPanel: STUB,
        SlackAgentBotPanel: STUB, TelegramChannelPanel: STUB, WhatsAppChannelPanel: STUB,
        VoipChannelPanel: STUB, ConnectorChannelPanel: STUB, PortalClientsPanel: STUB,
        'router-link': { props: ['to'], template: '<a :data-to="JSON.stringify(to)"><slot /></a>' },
      },
      mocks: { $route: { query: {} } },
    },
  })
  await flushPromises()
  return w
}
const row = (w, id) => w.find(`[data-testid="access-request-${id}"]`)
const text = (r, t) => r.find(`[data-testid="access-request-${t}"]`).text()

describe('pending access requests name the person (ent#737)', () => {
  it('leads with the name, then handle, channel id and the email', async () => {
    const w = await render()
    const r = row(w, 'r1')
    expect(text(r, 'who')).toBe('Carol Jones')
    expect(text(r, 'facts')).toContain('@carol_tg · Telegram id 4242 · carol@example.com')
    expect(r.find('[data-testid="access-request-no-identity"]').exists()).toBe(false)
  })

  it('a handle with no name leads with the handle', async () => {
    const w = await render()
    const r = row(w, 'r2')
    expect(text(r, 'who')).toBe('@dee')
    expect(text(r, 'facts')).toContain('Slack id U9 · dee@example.com')
  })

  it('says where this person was seen before, three chips and a counted rest', async () => {
    const w = await render()
    const chips = row(w, 'r1').findAll('[data-testid="access-request-known-as"]').map((c) => c.text())
    expect(chips).toEqual(['Approved on a-two', 'Verified on a-two', 'Denied on a-three'])
    expect(row(w, 'r1').text()).toContain('+1 more')
  })

  it('a public-link request says email only; an older one says not captured', async () => {
    const w = await render()
    expect(text(row(w, 'r3'), 'who')).toBe('web@example.com')
    expect(text(row(w, 'r3'), 'facts')).toContain('via Public link')
    expect(text(row(w, 'r3'), 'no-identity')).toBe('Email only — public link')
    expect(text(row(w, 'r4'), 'no-identity')).toBe('Identity not captured')
    expect(text(row(w, 'r4'), 'facts')).toContain('via WhatsApp')
  })

  it('Approve still decides the request by its id', async () => {
    const w = await render()
    await row(w, 'r1').findAll('button').find((b) => b.text() === 'Approve').trigger('click')
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(`/api/agents/${AGENT}/access-requests/r1/decide`, { approve: true }, expect.anything())
  })
})

// trinity-enterprise#225: the panel names itself after its tab, and points at where
// teammates moved to rather than at the Access tab that no longer exists.
describe('how the Channels tab introduces itself', () => {
  it('is headed Channels and never says Share this agent', async () => {
    const w = await render()
    expect(w.find('h3').text()).toBe('Channels')
    expect(w.text()).not.toContain('Share this agent')
  })

  it('sends teammates to Settings › People, not the Access tab', async () => {
    const w = await render()
    const link = w.find('[data-testid="channels-people-link"]')
    expect(link.text()).toBe('Settings › People')
    expect(JSON.parse(link.attributes('data-to'))).toEqual({ query: { tab: 'settings', section: 'people' } })
    expect(w.text()).not.toContain('Access tab')
  })
})
