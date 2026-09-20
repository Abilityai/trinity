/**
 * trinity-enterprise#657 — Workspace drafts: the rules and the store.
 *
 * Pure rules first (`components/portal/portalDrafts.js`), then the Pinia store
 * (`stores/portalDrafts.js`) executed against a fake `Storage` — two store
 * instances over ONE storage stand in for two Workspace tabs, which is the
 * case the whole-map design would have got wrong (both independent reviews
 * hit it). The composer binding and the mounted components live in
 * `portalComposerDraft.spec.js` (jsdom); the shell wiring is executed by the
 * hermetic e2e (`e2e/workspace-drafts.spec.js`).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'
import {
  DRAFTS_STORAGE_PREFIX, MAX_DRAFTS, MAX_PERSISTED_CHARS,
  draftKeyFor, threadKey, roomKey, newChatKey, isNewChatKey, agentOfNewChatKey,
  hasDraftText, draftStorageKey, normalizeDraftsMap, boundDrafts,
  loadDrafts, persistDraft, removeDraftsBucket,
  agentsWithDrafts, reconcileDraftOnKeyChange, shouldFocusOnRestore,
} from '../../src/components/portal/portalDrafts'
import { usePortalDraftsStore } from '../../src/stores/portalDrafts'

// The store namespaces by the portal principal the roster reported. Stubbed
// reactively (a Pinia computed over a plain `let` would never invalidate).
const { __portal } = vi.hoisted(() => ({ __portal: { stub: null } }))
vi.mock('@/stores/clientPortal', async () => {
  const { reactive } = await import('vue')
  __portal.stub = reactive({ clientEmail: null })
  return { useClientPortalStore: () => __portal.stub }
})

function fakeStorage({ failWrites = false, failReads = false } = {}) {
  const map = new Map()
  return {
    map,
    getItem: (k) => { if (failReads) throw new Error('blocked'); return map.has(k) ? map.get(k) : null },
    setItem: (k, v) => { if (failWrites) throw new Error('QuotaExceededError'); map.set(k, String(v)) },
    removeItem: (k) => { map.delete(k) },
  }
}

// ---------------------------------------------------------------- pure rules

describe('draftKeyFor — one key per conversation', () => {
  it('a room wins, then a thread, then an unsaved new chat; unresolved binds nothing', () => {
    expect(draftKeyFor({ roomId: 'r1' })).toBe('room:r1')
    expect(draftKeyFor({ sessionId: 's1', agentName: 'scribe', newChat: true })).toBe('thread:s1')
    expect(draftKeyFor({ sessionId: null, agentName: 'scribe', newChat: true })).toBe('new:scribe')
    // sessionId null + newChat false = "the thread is not known yet" (a cold
    // /workspace root resolving to the most recent thread) — no key.
    expect(draftKeyFor({ sessionId: null, agentName: 'scribe', newChat: false })).toBeNull()
    expect(draftKeyFor({ sessionId: null, agentName: '', newChat: true })).toBeNull()
    expect(draftKeyFor()).toBeNull()
  })

  it('the helpers agree with each other', () => {
    expect(threadKey('s1')).toBe('thread:s1')
    expect(roomKey('r1')).toBe('room:r1')
    expect(newChatKey('scribe')).toBe('new:scribe')
    expect(newChatKey('')).toBeNull()
    expect(isNewChatKey('new:scribe')).toBe(true)
    expect(isNewChatKey('thread:s1')).toBe(false)
    expect(isNewChatKey(null)).toBe(false)
    expect(agentOfNewChatKey('new:scribe')).toBe('scribe')
    expect(agentOfNewChatKey('new:a:b')).toBe('a:b')
    expect(agentOfNewChatKey('thread:s1')).toBeNull()
  })
})

describe('hasDraftText — whitespace is never a draft', () => {
  it.each([['hello', true], ['  x  ', true], ['', false], ['   ', false], ['\n\t', false], [null, false], [undefined, false], [42, false]])(
    '%j → %s', (text, expected) => { expect(hasDraftText(text)).toBe(expected) })
})

describe('storage key + codec', () => {
  it('namespaces by the lowercased, trimmed identity; no identity → no key', () => {
    expect(draftStorageKey('  Ada@Example.com ')).toBe(`${DRAFTS_STORAGE_PREFIX}:ada@example.com`)
    expect(draftStorageKey(null)).toBeNull()
    expect(draftStorageKey('')).toBeNull()
    expect(draftStorageKey('   ')).toBeNull()
  })

  it('normalizes: only string text with non-whitespace survives; updatedAt is a finite number', () => {
    const out = normalizeDraftsMap({
      v: 1,
      drafts: {
        'thread:a': { text: 'keep', updatedAt: 5 },
        'thread:b': { text: '   ', updatedAt: 6 },
        'thread:c': { text: 42, updatedAt: 7 },
        'thread:d': { text: 'no stamp' },
        'thread:e': 'not an object',
        '': { text: 'empty key', updatedAt: 1 },
      },
    })
    expect(out).toEqual({ 'thread:a': { text: 'keep', updatedAt: 5 }, 'thread:d': { text: 'no stamp', updatedAt: 0 } })
    expect(normalizeDraftsMap(null)).toEqual({})
    expect(normalizeDraftsMap('nope')).toEqual({})
    expect(normalizeDraftsMap({ v: 99, drafts: { 'thread:a': { text: 'x', updatedAt: 1 } } })).toEqual({})
    expect(normalizeDraftsMap({ drafts: [] })).toEqual({})
  })

  it('bounds the map to the newest MAX_DRAFTS entries', () => {
    const map = {}
    for (let i = 0; i < MAX_DRAFTS + 5; i++) map[`thread:${i}`] = { text: `t${i}`, updatedAt: i }
    const out = boundDrafts(map)
    expect(Object.keys(out)).toHaveLength(MAX_DRAFTS)
    expect(out['thread:0']).toBeUndefined()
    expect(out['thread:4']).toBeUndefined()
    expect(out['thread:5']).toBeDefined()
    expect(out[`thread:${MAX_DRAFTS + 4}`]).toBeDefined()
    expect(boundDrafts({ 'thread:a': { text: 'x', updatedAt: 1 } }, 3)).toEqual({ 'thread:a': { text: 'x', updatedAt: 1 } })
  })

  it('round-trips through a storage, one key at a time, without touching the others', () => {
    const s = fakeStorage()
    expect(loadDrafts(s, 'a@x.io')).toEqual({})
    expect(persistDraft(s, 'a@x.io', 'thread:1', { text: 'one', updatedAt: 10 })).toBe(true)
    expect(persistDraft(s, 'a@x.io', 'thread:2', { text: 'two', updatedAt: 11 })).toBe(true)
    expect(loadDrafts(s, 'a@x.io')).toEqual({
      'thread:1': { text: 'one', updatedAt: 10 },
      'thread:2': { text: 'two', updatedAt: 11 },
    })
    // A null entry removes that key only.
    expect(persistDraft(s, 'a@x.io', 'thread:1', null)).toBe(true)
    expect(loadDrafts(s, 'a@x.io')).toEqual({ 'thread:2': { text: 'two', updatedAt: 11 } })
    // Another identity is another bucket.
    expect(loadDrafts(s, 'b@x.io')).toEqual({})
    expect(persistDraft(s, null, 'thread:9', { text: 'x', updatedAt: 1 })).toBe(false)
    expect(removeDraftsBucket(s, 'a@x.io')).toBe(true)
    expect(loadDrafts(s, 'a@x.io')).toEqual({})
    expect(s.map.has(draftStorageKey('a@x.io'))).toBe(false)
  })

  it('every storage failure is silent: reads → empty, writes → false, corrupt JSON → empty', () => {
    expect(loadDrafts(null, 'a@x.io')).toEqual({})
    expect(persistDraft(null, 'a@x.io', 'thread:1', { text: 'x', updatedAt: 1 })).toBe(false)
    expect(removeDraftsBucket(null, 'a@x.io')).toBe(false)
    const blocked = fakeStorage({ failReads: true })
    expect(loadDrafts(blocked, 'a@x.io')).toEqual({})
    const full = fakeStorage({ failWrites: true })
    expect(persistDraft(full, 'a@x.io', 'thread:1', { text: 'x', updatedAt: 1 })).toBe(false)
    const corrupt = fakeStorage()
    corrupt.map.set(draftStorageKey('a@x.io'), '{not json')
    expect(loadDrafts(corrupt, 'a@x.io')).toEqual({})
    // A write over a corrupt bucket replaces it rather than throwing.
    expect(persistDraft(corrupt, 'a@x.io', 'thread:1', { text: 'x', updatedAt: 1 })).toBe(true)
    expect(loadDrafts(corrupt, 'a@x.io')).toEqual({ 'thread:1': { text: 'x', updatedAt: 1 } })
  })
})

describe('agentsWithDrafts — the unreadByAgent twin over decorated threads', () => {
  const threads = [
    { id: 't1', agent_name: 'scribe', hasDraft: true },
    { id: 't2', agent_name: 'atlas', hasDraft: false },
    { id: 'r1', is_room: true, agent_names: ['scribe', 'atlas', 'sage'], hasDraft: true },
    { id: 't3', agent_name: 'sage' },
  ]
  it('a listed thread with a draft lights its agent; a room lights nobody; a new-chat draft lights its agent', () => {
    expect([...agentsWithDrafts(threads, new Set())]).toEqual(['scribe'])
    expect([...agentsWithDrafts(threads, new Set(['atlas']))].sort()).toEqual(['atlas', 'scribe'])
    expect([...agentsWithDrafts([], new Set(['x']))]).toEqual(['x'])
    expect([...agentsWithDrafts(null, null)]).toEqual([])
  })
})

describe('reconcileDraftOnKeyChange — the four in-instance transitions', () => {
  const read = (k) => ({ 'thread:x': 'stored for x', 'thread:y': 'stored for y' }[k] || '')

  it('→ null keeps the composer and writes nothing (openAgentPage on the current agent)', () => {
    expect(reconcileDraftOnKeyChange({ oldKey: 'thread:x', newKey: null, composerText: 'typing', read }))
      .toEqual({ composer: 'typing', writes: [] })
  })
  it('null → thread fills only an EMPTY composer; typed text wins and is persisted', () => {
    expect(reconcileDraftOnKeyChange({ oldKey: null, newKey: 'thread:x', composerText: '', read }))
      .toEqual({ composer: 'stored for x', writes: [] })
    expect(reconcileDraftOnKeyChange({ oldKey: null, newKey: 'thread:x', composerText: 'mine', read }))
      .toEqual({ composer: 'mine', writes: [['thread:x', 'mine']] })
    expect(reconcileDraftOnKeyChange({ oldKey: null, newKey: 'thread:z', composerText: '', read }))
      .toEqual({ composer: '', writes: [] })
  })
  it('new: → thread MOVES the composer text (session adoption)', () => {
    expect(reconcileDraftOnKeyChange({ oldKey: 'new:scribe', newKey: 'thread:x', composerText: 'mid-turn', read }))
      .toEqual({ composer: 'mid-turn', writes: [['new:scribe', ''], ['thread:x', 'mid-turn']] })
    expect(reconcileDraftOnKeyChange({ oldKey: 'new:scribe', newKey: 'thread:x', composerText: '', read }))
      .toEqual({ composer: '', writes: [['new:scribe', ''], ['thread:x', '']] })
  })
  it('thread → thread shows the destination (the source is already stored by write-through)', () => {
    expect(reconcileDraftOnKeyChange({ oldKey: 'thread:x', newKey: 'thread:y', composerText: 'old text', read }))
      .toEqual({ composer: 'stored for y', writes: [] })
  })
  it('tolerates a non-string composer', () => {
    expect(reconcileDraftOnKeyChange({ oldKey: null, newKey: 'thread:z', composerText: undefined, read }))
      .toEqual({ composer: '', writes: [] })
  })
})

describe('shouldFocusOnRestore — a fine pointer only', () => {
  it('true on a fine pointer, false on coarse, false with no matchMedia', () => {
    expect(shouldFocusOnRestore((q) => ({ matches: q === '(pointer: fine)' }))).toBe(true)
    expect(shouldFocusOnRestore(() => ({ matches: false }))).toBe(false)
    expect(shouldFocusOnRestore(undefined)).toBe(false)
    expect(shouldFocusOnRestore(() => { throw new Error('no') })).toBe(false)
  })
})

// ---------------------------------------------------------------- the store

describe('usePortalDraftsStore', () => {
  let storage
  beforeEach(() => {
    storage = fakeStorage()
    globalThis.localStorage = storage
    __portal.stub.clientEmail = 'Ada@Example.com'
    setActivePinia(createPinia())
  })

  // The store reads `safeStorage()` when CREATED (not at import), so the fake
  // installed in `beforeEach` is what it gets.
  async function makeStore() {
    return usePortalDraftsStore()
  }

  it('set/get/has/clear, write-through to the identity bucket, whitespace clears', async () => {
    const s = await makeStore()
    expect(s.identity).toBe('ada@example.com')
    s.set('thread:1', 'hello')
    expect(s.get('thread:1')).toBe('hello')
    expect(s.has('thread:1')).toBe(true)
    expect(loadDrafts(storage, 'ada@example.com')['thread:1'].text).toBe('hello')
    s.set('thread:1', '   ')
    expect(s.has('thread:1')).toBe(false)
    expect(s.get('thread:1')).toBe('')
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({})
    s.set('thread:2', 'two'); s.clear('thread:2')
    expect(s.has('thread:2')).toBe(false)
    s.set(null, 'ignored')
    expect([...s.keys]).toEqual([])
  })

  it('keys changes identity only when MEMBERSHIP changes — a keystroke never re-renders the sidebar', async () => {
    const s = await makeStore()
    s.set('thread:1', 'a')
    await nextTick()
    const before = s.keys
    s.set('thread:1', 'ab')
    await nextTick()
    expect(s.keys).toBe(before)
    s.set('thread:2', 'x')
    await nextTick()
    expect(s.keys).not.toBe(before)
    expect([...s.keys].sort()).toEqual(['thread:1', 'thread:2'])
  })

  it('newChatDraftAgents is the set of agents with an unsaved-chat draft', async () => {
    const s = await makeStore()
    s.set('new:scribe', 'hi'); s.set('thread:1', 'x'); s.set('new:atlas', 'yo')
    await nextTick()
    expect([...s.newChatDraftAgents].sort()).toEqual(['atlas', 'scribe'])
  })

  it('move() carries a draft from one key to another (adoption, Reset)', async () => {
    const s = await makeStore()
    s.set('thread:old', 'carry me')
    s.move('thread:old', 'thread:new')
    expect(s.get('thread:new')).toBe('carry me')
    expect(s.has('thread:old')).toBe(false)
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({ 'thread:new': expect.objectContaining({ text: 'carry me' }) })
    s.move('thread:none', 'thread:x')     // nothing to move: no-op
    expect(s.has('thread:x')).toBe(false)
  })

  it('reloads from storage when the identity changes — swapped, never merged', async () => {
    const s = await makeStore()
    s.set('thread:1', "ada's")
    __portal.stub.clientEmail = 'bob@example.com'
    await nextTick()
    expect(s.identity).toBe('bob@example.com')
    expect(s.has('thread:1')).toBe(false)
    s.set('thread:2', "bob's")
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({ 'thread:1': expect.objectContaining({ text: "ada's" }) })
    expect(loadDrafts(storage, 'bob@example.com')).toEqual({ 'thread:2': expect.objectContaining({ text: "bob's" }) })
    __portal.stub.clientEmail = 'ada@example.com'
    await nextTick()
    expect(s.get('thread:1')).toBe("ada's")
    expect(s.has('thread:2')).toBe(false)
  })

  it('with no identity the map is memory-only and nothing is written', async () => {
    __portal.stub.clientEmail = null
    const s = await makeStore()
    expect(s.identity).toBeNull()
    s.set('thread:1', 'held')
    expect(s.get('thread:1')).toBe('held')
    expect(storage.map.size).toBe(0)
    // Identity arriving later swaps in that person's bucket; the anonymous
    // text is not smuggled into it.
    __portal.stub.clientEmail = 'ada@example.com'
    await nextTick()
    expect(s.has('thread:1')).toBe(false)
  })

  it('storage that refuses writes keeps working in memory (AC 8)', async () => {
    globalThis.localStorage = fakeStorage({ failWrites: true })
    setActivePinia(createPinia())
    const s = await makeStore()
    s.set('thread:1', 'still here')
    expect(s.get('thread:1')).toBe('still here')
    expect(s.has('thread:1')).toBe(true)
  })

  it('an oversize draft is session-only: held in memory, its storage entry removed', async () => {
    const s = await makeStore()
    s.set('thread:1', 'short')
    const huge = 'x'.repeat(MAX_PERSISTED_CHARS + 1)
    s.set('thread:1', huge)
    expect(s.get('thread:1')).toBe(huge)
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({})
    s.set('thread:1', 'short again')
    expect(loadDrafts(storage, 'ada@example.com')['thread:1'].text).toBe('short again')
  })

  it('clearBucket() removes the current identity\'s bucket and empties the map', async () => {
    const s = await makeStore()
    s.set('thread:1', 'gone soon')
    s.clearBucket()
    expect(s.has('thread:1')).toBe(false)
    expect(storage.map.has(draftStorageKey('ada@example.com'))).toBe(false)
  })

  it('two tabs over one storage: a write in one never erases the other\'s drafts, and reload() sees them', async () => {
    const a = await makeStore()
    setActivePinia(createPinia())
    const b = await makeStore()
    a.set('thread:A', 'from tab A')
    b.set('thread:B', 'from tab B')
    // Each tab's in-memory map is its own; the bucket holds both.
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({
      'thread:A': expect.objectContaining({ text: 'from tab A' }),
      'thread:B': expect.objectContaining({ text: 'from tab B' }),
    })
    // Tab A sends (clears A); tab B keeps typing elsewhere — A must stay cleared.
    a.set('thread:A', '')
    b.set('thread:B', 'from tab B, more')
    expect(loadDrafts(storage, 'ada@example.com')).toEqual({
      'thread:B': expect.objectContaining({ text: 'from tab B, more' }),
    })
    // The `storage` event's handler is `reload()`; here it is called directly
    // (node has no window). The jsdom spec dispatches the real event.
    b.reload()
    expect(b.has('thread:A')).toBe(false)
    expect(b.get('thread:B')).toBe('from tab B, more')
    a.reload()
    expect(a.get('thread:B')).toBe('from tab B, more')
  })
})

// ---------------------------------------------------------------- portalUtils

describe('portalUtils — the tab model, the collapse lift and the row title know about drafts', () => {
  const threads = [
    { id: 'main', agent_name: 'scribe', is_main: true, title: '', last_message_at: '2026-09-01T10:00:00Z' },
    { id: 't2', agent_name: 'scribe', title: 'Second', last_message_at: '2026-09-02T10:00:00Z' },
    { id: 'theirs', agent_name: 'atlas', title: 'Not this agent', last_message_at: '2026-09-03T10:00:00Z' },
  ]

  it('agentChatTabs flags exactly the tab whose thread id has a draft, by TAB id', async () => {
    const { agentChatTabs, NEW_CHAT_TAB_ID } = await import('../../src/components/portal/portalUtils')
    const tabs = agentChatTabs(threads, 'scribe', { activeId: 'main', draftKeys: new Set(['thread:t2', 'thread:theirs']) })
    expect(tabs.map((t) => [t.id, t.hasDraft])).toEqual([['main', false], ['t2', true]])
    // No draftKeys at all: every tab plain, no provisional tab (the #2579 rule).
    expect(agentChatTabs(threads, 'scribe', { activeId: 'main' }).map((t) => t.hasDraft)).toEqual([false, false])
    expect(agentChatTabs(threads, 'scribe', { activeId: 'main' }).some((t) => t.id === NEW_CHAT_TAB_ID)).toBe(false)
  })

  it('a new:<agent> draft lists the provisional tab after Main while another chat is open — once, marked', async () => {
    const { agentChatTabs, NEW_CHAT_TAB_ID } = await import('../../src/components/portal/portalUtils')
    const tabs = agentChatTabs(threads, 'scribe', { activeId: 'main', draft: false, draftKeys: new Set(['new:scribe']) })
    expect(tabs.map((t) => t.id)).toEqual(['main', NEW_CHAT_TAB_ID, 't2'])
    expect(tabs[1]).toMatchObject({ provisional: true, thread: null, hasDraft: true })
    // Active AND drafted: still one provisional tab, carrying the mark.
    const active = agentChatTabs(threads, 'scribe', { activeId: null, draft: true, draftKeys: new Set(['new:scribe']) })
    expect(active.filter((t) => t.provisional)).toHaveLength(1)
    expect(active.find((t) => t.provisional)).toMatchObject({ id: NEW_CHAT_TAB_ID, hasDraft: true })
    // Active, born here (adopted id not yet listed): the mark keys on the adopted id.
    const born = agentChatTabs(threads, 'scribe', { activeId: 'fresh', draft: true, draftKeys: new Set(['thread:fresh']) })
    expect(born.find((t) => t.provisional)).toMatchObject({ id: 'fresh', hasDraft: true })
    // Another agent's new-chat draft is not this strip's business.
    expect(agentChatTabs(threads, 'scribe', { activeId: 'main', draftKeys: new Set(['new:atlas']) }).some((t) => t.provisional)).toBe(false)
  })

  it('visibleAgentRows lifts a drafted agent above the collapse the way it lifts an asked one', async () => {
    const { visibleAgentRows, AGENT_COLLAPSE_LIMIT } = await import('../../src/components/portal/portalUtils')
    const roster = Array.from({ length: AGENT_COLLAPSE_LIMIT + 3 }, (_, i) => ({ name: `a${i}` }))
    const last = roster[roster.length - 1].name
    expect(visibleAgentRows(roster).map((a) => a.name)).not.toContain(last)
    expect(visibleAgentRows(roster, { draftAgents: new Set([last]) }).map((a) => a.name)).toEqual([
      ...roster.slice(0, AGENT_COLLAPSE_LIMIT).map((a) => a.name), last,
    ])
    // A drafted agent already in the head is not duplicated.
    expect(visibleAgentRows(roster, { draftAgents: new Set(['a0']) })).toHaveLength(AGENT_COLLAPSE_LIMIT)
  })

  it('agentRowTitle names the draft in words', async () => {
    const { agentRowTitle } = await import('../../src/components/portal/portalUtils')
    expect(agentRowTitle({ label: 'Scribe', name: 'scribe', hasDraft: true })).toBe('Scribe (scribe) — an unsent draft')
    expect(agentRowTitle({ name: 'scribe', unread: 2, hasDraft: true })).toBe('scribe — 2 unread replies, an unsent draft')
    expect(agentRowTitle({ name: 'scribe' })).toBe('Open scribe')
  })
})
