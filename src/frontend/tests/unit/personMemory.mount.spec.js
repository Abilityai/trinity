// @vitest-environment jsdom
/**
 * trinity-enterprise#962 — Agent Settings → Person memory, MOUNTED.
 *
 * The owner's view of what an agent remembers about each person. Undo and
 * Forget are destructive verbs behind a confirm, and the dialog owns a focus
 * contract, so every gate here is proven by driving the real components
 * against the real store with the HTTP client mocked (#2918), never a regex.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import api from '../../src/api'
import PersonMemorySection from '../../src/components/agent-settings/PersonMemorySection.vue'
import { usePersonMemoryStore } from '../../src/stores/personMemory'

const AGENT = 'analyst'
const BASE = `/api/agents/${AGENT}`
const ago = (ms) => new Date(Date.now() - ms).toISOString()

const PEOPLE = {
  agent_name: AGENT, total: 2, offset: 0, limit: 50,
  people: [
    { user_email: 'ann@example.com', last_changed_at: ago(3600_000), has_notes: true, has_summary: true,
      write_count: 3, fact_count: 2, agent_scope: false, channel: 'telegram', channel_name: 'Ann T', channel_handle: 'anntg' },
    { user_email: 'owner@example.com', last_changed_at: ago(86400_000), has_notes: true, has_summary: false,
      write_count: 1, fact_count: null, agent_scope: true, channel: null, channel_name: null, channel_handle: null },
  ],
}

function write(over) {
  return {
    id: 'w', section: 'notes', kind: 'conversation', execution_id: 'e', schedule_name: null,
    written_at: ago(3600_000), undone_at: null, undone_by: null, undone_by_operator: false,
    undoable: false, text: 'text', previous_text: '', ...over,
  }
}

function detail(over = {}) {
  return {
    agent_name: AGENT, user_email: 'ann@example.com', agent_scope: false,
    notes: 'Prefers PDF invoices. <b>not markup</b>', summary: '- Based in Lyon',
    updated_at: ago(3600_000), channel: 'telegram', channel_name: 'Ann T', channel_handle: 'anntg',
    history: {
      writes: [
        write({ id: 'w3', section: 'summary', kind: 'summarizer', undoable: true, text: '- Based in Lyon' }),
        write({ id: 'w2', undoable: true, text: 'Prefers PDF invoices.' }),
        write({ id: 'w1', undone_at: ago(7200_000), undone_by: 'op@example.com', undone_by_operator: true, text: 'old' }),
      ],
      next_before: 'T|w1',
    },
    ...over,
  }
}

const FACTS = [
  { id: 'f1', text: 'Lives in Lyon', source: 'public', created_at: ago(3600_000), updated_at: ago(3600_000), recall_count: 2 },
]

/** Route the mocked GETs by URL, like the backend would. */
function serve({ people = PEOPLE, person = detail(), facts = FACTS, status = { enabled_platform: true, opt_out: false }, events = [] } = {}) {
  api.get.mockImplementation((url, cfg = {}) => {
    if (url === `${BASE}/person-memory`) return Promise.resolve({ data: people })
    if (url === `${BASE}/person-memory/person`) return Promise.resolve({ data: typeof person === 'function' ? person(cfg) : person })
    if (url === `${BASE}/person-memory/writes`) {
      return Promise.resolve({ data: { writes: [write({ id: 'w0', text: 'oldest' })], next_before: null } })
    }
    if (url === `${BASE}/retrieval-memory/facts`) return Promise.resolve({ data: facts })
    if (url === `${BASE}/retrieval-memory`) return Promise.resolve({ data: status })
    if (url === `${BASE}/retrieval-memory/events`) return Promise.resolve({ data: events })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

let w
async function mountSection() {
  w = mount(PersonMemorySection, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  return w
}
const q = (sel) => document.querySelector(sel)
const qa = (sel) => [...document.querySelectorAll(sel)]
async function click(el) { el.click(); await flushPromises() }
async function openAnn() {
  await mountSection()
  await click(qa('[data-testid="person-memory-person"]')[0])
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('PersonMemorySection — the people list', () => {
  it('shows a skeleton first, then everyone with memory, their channel identity and counts', async () => {
    serve()
    w = mount(PersonMemorySection, { props: { agentName: AGENT }, attachTo: document.body })
    expect(w.find('[data-testid="person-memory-person"]').exists()).toBe(false)   // no data yet: skeleton
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith(`${BASE}/person-memory`, { params: { offset: 0, limit: 50 } })
    const rows = qa('[data-testid="person-memory-person"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('ann@example.com')
    expect(rows[0].textContent).toContain('Telegram · @anntg · Ann T')
    expect(rows[0].textContent).toContain('Notes · Summary · 3 changes · 2 facts')
    expect(rows[1].textContent).toContain('Agent owner')
    expect(q('[data-testid="person-memory-total"]').textContent).toContain('2 people')
  })

  it('an empty agent says so only after a read that succeeded; a failed read is a failed state', async () => {
    serve({ people: { ...PEOPLE, total: 0, people: [] } })
    await mountSection()
    expect(q('[data-testid="person-memory-empty"]').textContent).toContain('No one has memory with this agent yet.')
    w.unmount(); document.body.innerHTML = ''

    setActivePinia(createPinia())
    api.get.mockRejectedValue(new Error('network'))
    await mountSection()
    expect(q('[data-testid="person-memory-empty"]')).toBeNull()
    expect(document.body.textContent).toContain("Couldn't load person memory")
  })
})

describe('PersonMemoryModal — one person', () => {
  it('opens with notes and summary as plain text, the history with one Undo per section, and the facts', async () => {
    serve()
    await openAnn()
    expect(api.get).toHaveBeenCalledWith(`${BASE}/person-memory/person`, { params: { user_email: 'ann@example.com' } })
    const notes = q('[data-testid="person-memory-notes"]')
    expect(notes.textContent).toContain('<b>not markup</b>')       // rendered as text, never as markup
    expect(notes.querySelector('b')).toBeNull()
    expect(q('[data-testid="person-memory-summary"]').textContent).toContain('Based in Lyon')
    const writes = qa('[data-testid="person-memory-write"]')
    expect(writes).toHaveLength(3)
    expect(writes[0].textContent).toContain('Conversation summary')
    expect(writes[2].textContent).toContain('undone by op@example.com')
    expect(qa('[data-testid="person-memory-undo"]')).toHaveLength(2)   // latest notes + latest summary
    expect(api.get).toHaveBeenCalledWith(`${BASE}/retrieval-memory/facts`,
      { params: { user_email: 'ann@example.com', limit: 20, offset: 0 } })
    expect(qa('[data-testid="person-memory-fact"]')).toHaveLength(1)
  })

  it('Undo asks first; Cancel does nothing, Confirm posts the person and re-reads', async () => {
    serve()
    await openAnn()
    await click(qa('[data-testid="person-memory-undo"]')[1])           // the notes write
    expect(q('[data-testid="confirm-dialog-message"]').textContent)
      .toContain("Their notes go back to what they were before it. This can't be redone.")
    await click(q('[data-testid="confirm-dialog-cancel"]'))
    expect(api.post).not.toHaveBeenCalled()

    await click(qa('[data-testid="person-memory-undo"]')[1])
    api.post.mockResolvedValueOnce({ data: { write_id: 'w2', notes: '', summary: '- Based in Lyon' } })
    await click(q('[data-testid="confirm-dialog-confirm"]'))
    expect(api.post).toHaveBeenCalledWith(`${BASE}/person-memory/writes/w2/undo`, { user_email: 'ann@example.com' })
    // The re-read covers as many rows as were on screen.
    expect(api.get.mock.calls.filter(([u]) => u === `${BASE}/person-memory/person`)).toHaveLength(2)
  })

  it('a summary Undo names the summary in its confirm', async () => {
    serve()
    await openAnn()
    await click(qa('[data-testid="person-memory-undo"]')[0])
    expect(q('[data-testid="confirm-dialog-message"]').textContent).toContain('Their summary goes back')
  })

  it('a refused Undo shows the server reason beside the history and releases the button', async () => {
    serve()
    await openAnn()
    await click(qa('[data-testid="person-memory-undo"]')[1])
    api.post.mockRejectedValueOnce({ response: { status: 409, data: { detail: {
      code: 'not_latest', message: 'A later change exists — undo the most recent one first.' } } } })
    await click(q('[data-testid="confirm-dialog-confirm"]'))
    expect(q('[data-testid="person-memory-undo-error"]').textContent).toContain('undo the most recent one first')
    expect(usePersonMemoryStore().undoing).toBeNull()
  })

  it('focus never falls out of the dialog when the Undo button disappears', async () => {
    serve({ person: () => detail() })
    await openAnn()
    const undoBtn = qa('[data-testid="person-memory-undo"]')[1]
    undoBtn.focus()
    await click(undoBtn)
    // After the undo the row is no longer undoable: its button unmounts.
    api.post.mockResolvedValueOnce({ data: { write_id: 'w2', notes: '', summary: '' } })
    serve({ person: () => detail({ history: { writes: [write({ id: 'w2', undone_at: ago(1000), undone_by: 'owner@example.com', undone_by_operator: true })], next_before: null } }) })
    await click(q('[data-testid="confirm-dialog-confirm"]'))
    await flushPromises()
    expect(qa('[data-testid="person-memory-undo"]')).toHaveLength(0)
    expect(document.activeElement).toBe(q('[data-testid="person-memory-close"]'))
  })

  it('Load more follows the history cursor', async () => {
    serve()
    await openAnn()
    await click(q('[data-testid="person-memory-writes-more"]'))
    expect(api.get).toHaveBeenCalledWith(`${BASE}/person-memory/writes`,
      { params: { user_email: 'ann@example.com', before: 'T|w1', limit: 20 } })
    expect(qa('[data-testid="person-memory-write"]')).toHaveLength(4)
    expect(q('[data-testid="person-memory-writes-more"]')).toBeNull()
  })

  it('Forget asks first, then deletes the fact and drops it from the list', async () => {
    serve()
    await openAnn()
    await click(q('[data-testid="person-memory-forget"]'))
    expect(q('[data-testid="confirm-dialog-title"]').textContent).toContain('Forget this fact?')
    expect(q('[data-testid="confirm-dialog-message"]').textContent).toContain('The agent will stop recalling it.')
    api.delete.mockResolvedValueOnce({ data: { forgotten: true } })
    await click(q('[data-testid="confirm-dialog-confirm"]'))
    expect(api.delete).toHaveBeenCalledWith(`${BASE}/retrieval-memory/facts/f1`)
    expect(qa('[data-testid="person-memory-fact"]')).toHaveLength(0)
  })

  it("a fact's History reads its journal by fact id", async () => {
    serve({ events: [{ id: 'ev1', op: 'add', created_at: ago(3600_000) }, { id: 'ev2', op: 'search', candidate_text: 'where', created_at: ago(60_000) }] })
    await openAnn()
    await click(q('[data-testid="person-memory-fact-history"]'))
    expect(api.get).toHaveBeenCalledWith(`${BASE}/retrieval-memory/events`, { params: { fact_id: 'f1', limit: 50 } })
    const lineage = q('[data-testid="person-memory-lineage"]').textContent
    expect(lineage).toContain('Kept')
    expect(lineage).toContain('Found by a search')
    expect(lineage).toContain('for “where”')
  })

  it("search inside the dialog is scoped to the person", async () => {
    serve()
    await openAnn()
    const input = document.querySelector('[data-testid="person-memory-modal"] [data-testid="retrieval-memory-search-input"]')
    input.value = 'invoices'
    input.dispatchEvent(new Event('input'))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith(`${BASE}/retrieval-memory/search`,
      { params: { q: 'invoices', k: 20, user_email: 'ann@example.com' } })
  })

  it("the owner's own row points at the agent scope and reads no person facts", async () => {
    serve({ person: detail({ user_email: 'owner@example.com', agent_scope: true }) })
    await mountSection()
    await click(qa('[data-testid="person-memory-person"]')[1])
    expect(q('[data-testid="person-memory-agent-scope"]')).not.toBeNull()
    expect(api.get.mock.calls.some(([u]) => u === `${BASE}/retrieval-memory/facts`)).toBe(false)
  })

  it('with retrieval memory off, the empty Facts reason uses the toggle\'s wording', async () => {
    serve({ facts: [], status: { enabled_platform: false, opt_out: false } })
    await openAnn()
    expect(q('[data-testid="person-memory-facts-empty"]').textContent).toContain('Platform switch is off')
  })
})

describe('#4276 review round — the four unpinned behaviours, and the >200 re-read', () => {
  it('a history row renders its text as text, never as markup', async () => {
    serve({ person: detail({ history: { writes: [
      write({ id: 'w9', text: '<img src=x onerror="alert(1)"> <b>poisoned</b>' }),
    ], next_before: null } }) })
    await openAnn()
    const row = q('[data-testid="person-memory-write"]')
    expect(row.textContent).toContain('<b>poisoned</b>')
    expect(row.querySelector('b')).toBeNull()
    expect(row.querySelector('img')).toBeNull()
  })

  it('after an Undo the owner\'s history is re-read to the length on screen, past the first page', async () => {
    serve()
    await openAnn()
    await click(q('[data-testid="person-memory-writes-more"]'))
    expect(qa('[data-testid="person-memory-write"]')).toHaveLength(4)
    await click(qa('[data-testid="person-memory-undo"]')[1])
    api.post.mockResolvedValueOnce({ data: { write_id: 'w2', notes: '', summary: '' } })
    await click(q('[data-testid="confirm-dialog-confirm"]'))
    expect(qa('[data-testid="person-memory-write"]')).toHaveLength(4)
    expect(api.get.mock.calls.filter(([u]) => u === `${BASE}/person-memory/writes`)).toHaveLength(2)
  })

  it('Load more on facts asks for the next page at the count on screen', async () => {
    const page = (n, from) => Array.from({ length: n }, (_, i) => ({
      id: `f${from + i}`, text: `Fact ${from + i}`, source: 'public', created_at: ago(1000), updated_at: ago(1000),
    }))
    serve()
    api.get.mockImplementation((url, cfg = {}) => {
      if (url === `${BASE}/retrieval-memory/facts`) {
        return Promise.resolve({ data: cfg.params.offset === 0 ? page(20, 0) : page(3, 20) })
      }
      if (url === `${BASE}/person-memory`) return Promise.resolve({ data: PEOPLE })
      if (url === `${BASE}/person-memory/person`) return Promise.resolve({ data: detail() })
      if (url === `${BASE}/retrieval-memory`) return Promise.resolve({ data: { enabled_platform: true } })
      return Promise.reject(new Error(`unexpected GET ${url}`))
    })
    await openAnn()
    await click(q('[data-testid="person-memory-facts-more"]'))
    expect(api.get).toHaveBeenCalledWith(`${BASE}/retrieval-memory/facts`,
      { params: { user_email: 'ann@example.com', limit: 20, offset: 20 } })
    expect(qa('[data-testid="person-memory-fact"]')).toHaveLength(23)
  })

  it('a slow answer for the previous person never lands under the next one', async () => {
    const store = usePersonMemoryStore()
    serve()
    await store.loadPeople(AGENT)
    let resolveA
    api.get.mockImplementationOnce(() => new Promise((r) => { resolveA = r }))
    const a = store.openPerson('ann@example.com')
    api.get.mockImplementationOnce(() => Promise.resolve({ data: detail({ user_email: 'bob@example.com', notes: 'Bob notes' }) }))
    await store.openPerson('bob@example.com')
    resolveA({ data: detail() })
    await a
    expect(store.personEmail).toBe('bob@example.com')
    expect(store.person.notes).toBe('Bob notes')
  })

  it('a quiet re-read keeps a list longer than one 200-row page', async () => {
    const people = (n, from) => Array.from({ length: n }, (_, i) => ({
      user_email: `p${from + i}@example.com`, last_changed_at: ago(1000), has_notes: true, has_summary: false,
      write_count: 1, fact_count: 0, agent_scope: false, channel: null, channel_name: null, channel_handle: null,
    }))
    const store = usePersonMemoryStore()
    api.get.mockImplementation((url, cfg = {}) => {
      if (url !== `${BASE}/person-memory`) return Promise.reject(new Error(`unexpected GET ${url}`))
      const { offset, limit } = cfg.params
      return Promise.resolve({ data: { agent_name: AGENT, total: 250, offset, limit,
        people: people(Math.max(0, Math.min(limit, 250 - offset)), offset) } })
    })
    await store.loadPeople(AGENT)
    while (store.people.length < 250) await store.loadMorePeople()
    expect(store.people).toHaveLength(250)
    await store.loadPeople(AGENT)                       // the re-read after an Undo or a Forget
    expect(store.people).toHaveLength(250)
    expect(new Set(store.people.map((p) => p.user_email)).size).toBe(250)
  })
})
