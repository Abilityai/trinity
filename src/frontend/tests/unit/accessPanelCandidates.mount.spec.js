// @vitest-environment jsdom
/**
 * trinity-enterprise#450 — the Access tab's add field suggests registered accounts.
 *
 * Mounted, because the keyboard contract and the store write are the feature. What
 * is pinned: no query on one character; a pause before asking; only the newest
 * answer shows; Enter submits the text unless a row was chosen; a pick fills the
 * field without submitting or re-opening; Esc and blur close; a failed lookup
 * leaves a plain input; a query with no match says how to invite anyway.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))

import AccessPanel from '@/components/AccessPanel.vue'
import { useAuthStore } from '@/stores/auth'
import { candidateRows, nextActive, noMatchMessage, wantsSuggestions } from '@/utils/accessCandidates'

const AGENT = 'scout'
const ACCESS_URL = `/api/agents/${AGENT}/access`
const CANDIDATES_URL = `/api/agents/${AGENT}/access/candidates`
const SHARE_URL = `/api/agents/${AGENT}/share`
const PAT = { email: 'pat@example.com', label: 'Pat Doe' }
const PAM = { email: 'pam@example.com', label: 'Pam Roe' }

let answer

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

const input = (w) => w.find('[data-testid="access-add-email"]')
const options = (w) => w.findAll('[role="option"]')
const lookups = () => ax.get.mock.calls.filter(([u]) => u === CANDIDATES_URL)
const shares = () => ax.post.mock.calls.filter(([u]) => u === SHARE_URL)

async function type(w, text) {
  await input(w).setValue(text)
  vi.advanceTimersByTime(200)
  await flushPromises()
}

const press = (w, key) => input(w).trigger('keydown', { key })

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()   // jsdom has no layout
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  answer = async () => ({ data: [PAT, PAM] })
  ax.get.mockImplementation(async (url, cfg) => {
    if (url === ACCESS_URL) return { data: [] }
    if (url === CANDIDATES_URL) return answer(cfg)
    return { data: {} }
  })
  api.get.mockResolvedValue({ data: [] })
  ax.post.mockResolvedValue({ data: { invitation: 'not_needed' } })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('when it asks', () => {
  it('never on one character', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'p')
    expect(lookups()).toHaveLength(0)
    expect(options(w)).toHaveLength(0)
  })

  it('after a pause, once, with what was typed', async () => {
    const w = render()
    await flushPromises()
    await input(w).setValue('pa')
    vi.advanceTimersByTime(100)
    await input(w).setValue('pat')
    expect(lookups()).toHaveLength(0)
    vi.advanceTimersByTime(200)
    await flushPromises()
    expect(lookups()).toHaveLength(1)
    expect(lookups()[0][1].params).toEqual({ q: 'pat' })
    expect(options(w).map((o) => o.text())).toEqual([
      expect.stringContaining('pat@example.com'), expect.stringContaining('pam@example.com'),
    ])
  })

  it('shows only the newest answer', async () => {
    const w = render()
    await flushPromises()
    const late = []
    answer = () => new Promise((resolve) => late.push(resolve))
    await type(w, 'pa')
    await type(w, 'pat')
    late[1]({ data: [PAT] })
    await flushPromises()
    late[0]({ data: [PAM] })
    await flushPromises()
    expect(options(w)).toHaveLength(1)
    expect(options(w)[0].text()).toContain('pat@example.com')
  })

  it('an answer arriving after the field was emptied opens nothing', async () => {
    const w = render()
    await flushPromises()
    let release
    answer = () => new Promise((resolve) => { release = resolve })
    await type(w, 'pat')
    await input(w).setValue('')
    release({ data: [PAT] })
    await flushPromises()
    expect(options(w)).toHaveLength(0)
  })
})

describe('the keyboard', () => {
  it('Enter with nothing chosen submits the typed text', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'someone@new.example')
    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    input(w).element.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(input(w).element.value).toBe('someone@new.example')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(shares()[0][1]).toEqual({ email: 'someone@new.example' })
    expect(options(w)).toHaveLength(0)
  })

  it('a refused add does not leave the popup open over the error', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pat')
    ax.post.mockRejectedValue(Object.assign(new Error('400'), { response: { status: 400, data: { detail: 'No.' } } }))
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(input(w).element.value).toBe('pat')
    expect(options(w)).toHaveLength(0)
    expect(w.text()).toContain('No.')
  })

  it('arrows choose a row, wrapping, and the input points at it', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pa')
    await press(w, 'ArrowDown')
    expect(options(w)[0].attributes('aria-selected')).toBe('true')
    expect(input(w).attributes('aria-activedescendant')).toBe(options(w)[0].attributes('id'))
    await press(w, 'ArrowDown')
    await press(w, 'ArrowDown')
    expect(options(w)[0].attributes('aria-selected')).toBe('true')
    await press(w, 'ArrowUp')
    expect(options(w)[1].attributes('aria-selected')).toBe('true')
  })

  it('Enter on a chosen row fills the field, submits nothing, and stays closed', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pa')
    await press(w, 'ArrowDown')
    const event = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    input(w).element.dispatchEvent(event)
    await flushPromises()
    vi.advanceTimersByTime(400)
    await flushPromises()
    expect(event.defaultPrevented).toBe(true)
    expect(input(w).element.value).toBe('pat@example.com')
    expect(shares()).toHaveLength(0)
    expect(lookups()).toHaveLength(1)
    expect(options(w)).toHaveLength(0)
  })

  it('Esc closes and keeps what was typed', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pa')
    await press(w, 'Escape')
    expect(options(w)).toHaveLength(0)
    expect(input(w).element.value).toBe('pa')
  })

  it('leaving the field closes it', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pa')
    await input(w).trigger('blur')
    expect(options(w)).toHaveLength(0)
  })

  it('a mouse pick fills the field', async () => {
    const w = render()
    await flushPromises()
    await type(w, 'pa')
    await options(w)[1].trigger('mousedown')
    expect(input(w).element.value).toBe('pam@example.com')
    expect(options(w)).toHaveLength(0)
  })
})

describe('when there is nothing to offer', () => {
  it('a failed lookup leaves a plain input with no error', async () => {
    const w = render()
    await flushPromises()
    answer = async () => { throw Object.assign(new Error('429'), { response: { status: 429 } }) }
    await type(w, 'pat')
    expect(w.find('[role="listbox"]').exists()).toBe(false)
    expect(w.text()).not.toContain('No account matches')
    expect(input(w).attributes('aria-expanded')).toBe('false')
    expect(input(w).element.disabled).toBe(false)
  })

  it('a full address with no match says Add person will invite it', async () => {
    const w = render()
    await flushPromises()
    answer = async () => ({ data: [] })
    await type(w, 'new@example.com')
    expect(w.text()).toContain('Add person will invite this address')
  })

  it('a partial query with no match says to type the full email', async () => {
    const w = render()
    await flushPromises()
    answer = async () => ({ data: [] })
    await type(w, 'zzz')
    expect(w.text()).toContain('Type their full email')
  })
})

describe('the pure rules', () => {
  it('asks from two characters, ignoring spaces', () => {
    expect(wantsSuggestions(' a ')).toBe(false)
    expect(wantsSuggestions('ab')).toBe(true)
  })

  it('shows role only when the server sent it', () => {
    expect(candidateRows([PAT])[0].secondary).toBe('pat@example.com')
    expect(candidateRows([{ ...PAT, role: 'admin' }])[0].secondary).toMatch(/^pat@example\.com · ./)
  })

  it('moves from no row to the ends, and has nowhere to go with no rows', () => {
    expect(nextActive(-1, 3, 1)).toBe(0)
    expect(nextActive(-1, 3, -1)).toBe(2)
    expect(nextActive(2, 3, 1)).toBe(0)
    expect(nextActive(-1, 0, 1)).toBe(-1)
  })

  it('tells an address from a partial query', () => {
    expect(noMatchMessage('a@b.co')).toContain('invite this address')
    expect(noMatchMessage('a@b')).toContain('full email')
  })
})
