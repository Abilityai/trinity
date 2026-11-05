// @vitest-environment jsdom
/**
 * trinity-enterprise#1066 — the masked secret field a `credential` ask opens in
 * the Workspace. Mounted (#2918): the real PortalAsks over the real store, the
 * one HTTP seam (axios) mocked, so the assertions are on what is rendered and
 * what is POSTed.
 *
 * The contract: masked by default (show/hide toggle), one POST to the ask's
 * `/credential` route with the value in the BODY (never the URL), the field
 * empty the moment it is submitted — so no component state holds the value
 * after Send, whatever the server answers — never written to the drafts or any
 * storage, never logged, and every refusal said in words. A credential ask
 * never shows the typed-answer box.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
const http = vi.hoisted(() => ({ post: null }))
vi.mock('axios', () => {
  const mk = () => {
    const inst = {
      get: vi.fn(() => Promise.resolve({ data: [] })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
      interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
      defaults: { headers: { common: {} } },
    }
    http.post = inst.post
    return inst
  }
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import PortalCredentialField from '@/components/portal/PortalCredentialField.vue'

const SECRET = 'ops_' + 'g'.repeat(40)

const credentialAsk = (over = {}) => ({
  id: 'c1', agent_name: 'scout', kind: 'credential', priority: 'medium',
  title: 'Needs OP_SERVICE_ACCOUNT_TOKEN', question: 'To open your vault.', options: [],
  created_at: '2026-11-02T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null,
  credential_name: 'OP_SERVICE_ACCOUNT_TOKEN', ...over,
})
const answered = () => credentialAsk({
  status: 'answered', ended_at: '2026-11-02T10:01:00Z', ended_by: 'you', answer: 'provided',
})

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
const field = (w) => tid(w, 'portal-credential-input-c1')
let store
let wrapper
let logs

beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
  http.post.mockReset()
  localStorage.clear()
  sessionStorage.clear()
  logs = []
  for (const level of ['log', 'info', 'warn', 'error', 'debug']) {
    vi.spyOn(console, level).mockImplementation((...args) => { logs.push(args.map(String).join(' ')) })
  }
})
afterEach(() => { wrapper?.unmount(); wrapper = null; vi.restoreAllMocks() })

function mountAsks(asks = [credentialAsk()]) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout' }, attachTo: document.body })
  return wrapper
}

function refusal(status, detail) {
  return Object.assign(new Error(`Request failed with status code ${status}`), { response: { status, data: { detail } } })
}

function leaked() {
  const stores = [JSON.stringify({ ...localStorage }), JSON.stringify({ ...sessionStorage })]
  return [...stores, ...logs, window.location.href].some((s) => s.includes(SECRET))
}

describe('PortalAsks renders a credential ask as the secret field', () => {
  it('shows the name, the reason and a masked field — never the typed-answer box', () => {
    const w = mountAsks()
    expect(w.findComponent(PortalCredentialField).exists()).toBe(true)
    expect(w.text()).toContain('OP_SERVICE_ACCOUNT_TOKEN')
    expect(field(w).attributes('type')).toBe('password')
    expect(field(w).attributes('autocomplete')).toBe('off')
    expect(field(w).attributes('spellcheck')).toBe('false')
    expect(tid(w, 'portal-ask-input-c1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-ack-c1').exists()).toBe(false)
    // Talking it through would invite pasting the value into a chat.
    expect(tid(w, 'portal-ask-discuss-c1').exists()).toBe(false)
  })

  // trinity-enterprise#1109 — a destination: the value lands on another
  // Trinity, in the agent it is for; the card and the caption say so in plain
  // words, and the key's variable name (which the projection withholds) is
  // nowhere on screen.
  it('says where the value lands when the ask carries a destination', () => {
    const w = mountAsks([credentialAsk({ credential_destination: { agent_name: 'lopez-digest', host: 'member.example.com' } })])
    expect(tid(w, 'portal-ask-destination-c1').text()).toBe('lopez-digest')
    expect(tid(w, 'portal-ask-asker-c1').text()).toContain('for your agent lopez-digest on your Trinity at member.example.com')
    const caption = tid(w, 'portal-credential-caption-c1').text()
    expect(caption).toContain('Sent to your agent lopez-digest on your Trinity at member.example.com')
    expect(caption).toContain('Nothing is kept here after delivery')
    expect(w.text()).not.toContain('MEMBER_KEY')
  })

  it('without a destination the card and the caption read as before', () => {
    const w = mountAsks()
    expect(tid(w, 'portal-ask-destination-c1').exists()).toBe(false)
    expect(tid(w, 'portal-credential-caption-c1').text()).toContain('Stored on the agent as OP_SERVICE_ACCOUNT_TOKEN')
  })

  it('names the asking agent and quotes its reason as its own words, wherever the card is drawn', () => {
    // A credential can be asked from any turn (a schedule reaches the owner's
    // Inbox), so the card itself says WHO asks — not only when the surface
    // happens to show agent names — and the reason reads as the agent's
    // claim, never as the platform's.
    store.agents = [{ name: 'scout', display_label: 'Scout Bot' }]
    const w = mountAsks()
    const asker = tid(w, 'portal-ask-asker-c1')
    expect(asker.exists()).toBe(true)
    expect(asker.text()).toContain('Scout Bot')
    expect(asker.text()).toContain('OP_SERVICE_ACCOUNT_TOKEN')
    const why = tid(w, 'portal-ask-why-c1')
    expect(why.element.tagName).toBe('BLOCKQUOTE')
    expect(why.text()).toBe('To open your vault.')
    expect(tid(w, 'portal-ask-why-by-c1').text()).toContain('Scout Bot')
    // the reason is drawn once, attributed — not also as the card's own body
    expect(tid(w, 'portal-ask-question-c1').exists()).toBe(false)
  })

  it('falls back to the agent’s name, and says so when it gave no reason', () => {
    const w = mountAsks([credentialAsk({ question: null })])
    expect(tid(w, 'portal-ask-asker-c1').text()).toContain('scout')
    expect(tid(w, 'portal-ask-why-c1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-asker-c1').text()).toContain('gave no reason')
  })

  it('the show/hide control reveals and re-masks the value', async () => {
    const w = mountAsks()
    const toggle = tid(w, 'portal-credential-toggle-c1')
    await toggle.trigger('click')
    expect(field(w).attributes('type')).toBe('text')
    expect(toggle.attributes('aria-pressed')).toBe('true')
    await toggle.trigger('click')
    expect(field(w).attributes('type')).toBe('password')
  })

  it('Send is disabled until something is typed', async () => {
    const w = mountAsks()
    expect(tid(w, 'portal-credential-send-c1').attributes('disabled')).toBeDefined()
    await field(w).setValue(SECRET)
    expect(tid(w, 'portal-credential-send-c1').attributes('disabled')).toBeUndefined()
  })
})

describe('sending the value', () => {
  it('POSTs once to the ask’s credential route with the value in the body, then the ask reads as provided', async () => {
    http.post.mockResolvedValue({ data: answered() })
    const w = mountAsks()
    await field(w).setValue(SECRET)
    await tid(w, 'portal-credential-form-c1').trigger('submit')
    // The field is empty the moment it is submitted — before the server answers.
    expect(field(w).exists() ? field(w).element.value : '').toBe('')
    await flushPromises()

    expect(http.post).toHaveBeenCalledTimes(1)
    const [url, body] = http.post.mock.calls[0]
    expect(url).toBe('/api/enterprise/client-portal/asks/c1/credential')
    expect(url).not.toContain(SECRET)
    expect(body).toEqual({ value: SECRET })
    expect(store.asks[0].status).toBe('answered')
    expect(w.findComponent(PortalCredentialField).exists()).toBe(false)
    expect(w.text()).toContain('You provided OP_SERVICE_ACCOUNT_TOKEN')
    expect(w.html()).not.toContain(SECRET)
    expect(leaked()).toBe(false)
  })

  it('a second Enter while the first is on the wire does not POST again', async () => {
    let resolve
    http.post.mockReturnValue(new Promise((r) => { resolve = r }))
    const w = mountAsks()
    await field(w).setValue(SECRET)
    await tid(w, 'portal-credential-form-c1').trigger('submit')
    await tid(w, 'portal-credential-form-c1').trigger('submit')
    resolve({ data: answered() })
    await flushPromises()
    expect(http.post).toHaveBeenCalledTimes(1)
  })

  for (const [code, status, extra, says] of [
    ['agent_not_running', 409, {}, /isn.t running/i],
    ['scrub_unavailable', 503, {}, /nothing was stored/i],
    ['asks_unavailable', 503, {}, /try again/i],
    ['invalid_value', 422, { message: 'The value cannot contain a line break.' }, /line break/],
    ['already_resolved', 409, { credential_written: true }, /stored/i],
    ['already_resolved', 400, {}, /already/i],
  ]) {
    it(`a ${status} ${code}${extra.credential_written ? ' (written)' : ''} is said in words, and the field is empty`, async () => {
      http.post.mockRejectedValue(refusal(status, { code, message: 'Server words.', ...extra }))
      const w = mountAsks()
      await field(w).setValue(SECRET)
      await tid(w, 'portal-credential-form-c1').trigger('submit')
      await flushPromises()
      const err = tid(w, 'portal-credential-error-c1')
      expect(err.exists()).toBe(true)
      expect(err.text()).toMatch(says)
      expect(err.text()).not.toContain(SECRET)
      if (field(w).exists()) expect(field(w).element.value).toBe('')
      expect(w.html()).not.toContain(SECRET)
      expect(leaked()).toBe(false)
    })
  }

  it('a refusal the person fixes by typing again: the error goes when they type', async () => {
    http.post.mockRejectedValueOnce(refusal(409, { code: 'agent_not_running', message: 'x' }))
    const w = mountAsks()
    await field(w).setValue(SECRET)
    await tid(w, 'portal-credential-form-c1').trigger('submit')
    await flushPromises()
    await field(w).setValue('again')
    expect(tid(w, 'portal-credential-error-c1').exists()).toBe(false)
  })
})

describe('the value does not outlive the field', () => {
  it('typing writes nothing to storage, and unmounting empties the field', async () => {
    const w = mountAsks()
    await field(w).setValue(SECRET)
    expect(leaked()).toBe(false)
    const input = field(w).element
    w.unmount()
    wrapper = null
    expect(input.value).toBe('')
  })

  it('an ask that ends elsewhere (dismissed, withdrawn) takes the typed value with it', async () => {
    const w = mountAsks()
    await field(w).setValue(SECRET)
    store.asks = [credentialAsk({ status: 'cancelled', ended_at: '2026-11-02T10:02:00Z' })]
    await flushPromises()
    expect(w.findComponent(PortalCredentialField).exists()).toBe(false)
    expect(w.html()).not.toContain(SECRET)
  })
})
