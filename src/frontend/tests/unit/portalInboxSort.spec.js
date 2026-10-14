/**
 * trinity-enterprise#856 Part B — Action's sort: Newest by default, Urgency on a
 * switch (operator ruling 2026-10-09). Pure rules, `portalInbox.js`.
 *
 * What has to hold: the default is newest-first with a total order (a poll can
 * never swap two equal rows); the Urgency order is §3g C1 exactly as it was;
 * the URL wins over the remembered choice, which wins over the default; an
 * unknown value anywhere is ignored, never obeyed; a storage that throws costs
 * the memory and nothing else; and `?from=` + paging do not care which order.
 */
import { describe, it, expect } from 'vitest'
import {
  actionItems, askRecencyCompare, askUrgencyCompare, INBOX_SORTS, INBOX_SORT_DEFAULT,
  normalizeInboxSort, inboxSortFor, inboxSortKey, loadInboxSort, saveInboxSort,
  filterByAgent, pageWindow,
} from '@/components/portal/portalInbox'

const NOW = Date.parse('2026-10-13T12:00:00Z')
const H = 3600 * 1000
const at = (msFromNow) => new Date(NOW + msFromNow).toISOString()
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', title: id, question: 'q?',
  priority: 'medium', created_at: at(-2 * H), expires_at: null, status: 'pending', ...over,
})
// Newest and Urgency disagree on every position here.
const ASKS = [
  ask('old-critical', { priority: 'critical', created_at: at(-9 * H) }),
  ask('mid-medium', { created_at: at(-5 * H) }),
  ask('new-low', { priority: 'low', created_at: at(-1 * H) }),
  ask('done', { status: 'answered', created_at: at(-0.5 * H) }),
]

function memoryStorage(seed = {}) {
  const m = new Map(Object.entries(seed))
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)) },
    removeItem: (k) => { m.delete(k) },
    dump: () => Object.fromEntries(m),
  }
}
const throwing = {
  getItem() { throw new Error('SecurityError') },
  setItem() { throw new Error('QuotaExceededError') },
  removeItem() { throw new Error('SecurityError') },
}

describe('ent#856 B1 — Action opens Newest first', () => {
  it('the default is newest raised first, pending only', () => {
    expect(INBOX_SORT_DEFAULT).toBe('newest')
    expect(actionItems(ASKS, NOW).map((r) => r.id)).toEqual(['new-low', 'mid-medium', 'old-critical'])
    expect(actionItems(ASKS, NOW, 'newest').map((r) => r.id)).toEqual(['new-low', 'mid-medium', 'old-critical'])
  })

  it('is a total order: equal times break on id, so a poll never swaps two rows', () => {
    const a = ask('a'); const b = ask('b')
    expect(askRecencyCompare(a, b)).toBeGreaterThan(0)    // id desc, the rule All uses
    expect(askRecencyCompare(b, a)).toBeLessThan(0)
    expect(actionItems([a, b], NOW).map((r) => r.id)).toEqual(['b', 'a'])
    expect(actionItems([b, a], NOW).map((r) => r.id)).toEqual(['b', 'a'])
  })

  it('an unknown sort is the default, never a third order', () => {
    expect(actionItems(ASKS, NOW, 'oldest').map((r) => r.id)).toEqual(['new-low', 'mid-medium', 'old-critical'])
  })
})

describe('ent#856 B2 — Urgency is §3g C1, unchanged', () => {
  it('urgency sorts exactly by askUrgencyCompare', () => {
    const expected = ASKS.filter((a) => a.status === 'pending').slice()
      .sort((a, b) => askUrgencyCompare(a, b, NOW)).map((a) => a.id)
    expect(actionItems(ASKS, NOW, 'urgency').map((r) => r.id)).toEqual(expected)
    expect(expected).toEqual(['old-critical', 'mid-medium', 'new-low'])
  })
})

describe('ent#856 B4 — ?sort= and the per-viewer memory', () => {
  it('normalizes to the two orders; anything else is null', () => {
    expect(INBOX_SORTS).toEqual(['newest', 'urgency'])
    expect(normalizeInboxSort('newest')).toBe('newest')
    expect(normalizeInboxSort('urgency')).toBe('urgency')
    for (const junk of ['', 'URGENCY', 'oldest', undefined, null, ['urgency'], 1]) {
      expect(normalizeInboxSort(junk)).toBe(null)
    }
  })

  it('precedence: the URL, then the memory, then Newest — on Action only', () => {
    expect(inboxSortFor('action', 'urgency', 'newest')).toBe('urgency')
    expect(inboxSortFor('action', undefined, 'urgency')).toBe('urgency')
    expect(inboxSortFor('action', 'junk', 'urgency')).toBe('urgency')
    expect(inboxSortFor('action', undefined, null)).toBe('newest')
    expect(inboxSortFor('action', undefined, 'junk')).toBe('newest')
    // T1: Unread and All have no sort control and no sort.
    expect(inboxSortFor('unread', 'urgency', 'urgency')).toBe(null)
    expect(inboxSortFor('all', 'urgency', 'urgency')).toBe(null)
  })

  it('the memory is per viewer: one person’s choice is not the next login’s', () => {
    expect(inboxSortKey('Ada@Example.com ')).toBe('trinity-workspace-inbox-sort:ada@example.com')
    expect(inboxSortKey('')).toBe('trinity-workspace-inbox-sort:anon')
    const s = memoryStorage()
    expect(saveInboxSort(s, 'ada@example.com', 'urgency')).toBe(true)
    expect(loadInboxSort(s, 'ada@example.com')).toBe('urgency')
    expect(loadInboxSort(s, 'bob@example.com')).toBe(null)
  })

  it('a stored value outside the two orders is not obeyed, and is not written', () => {
    const s = memoryStorage({ 'trinity-workspace-inbox-sort:ada': 'drop table' })
    expect(loadInboxSort(s, 'ada')).toBe(null)
    expect(saveInboxSort(s, 'ada', 'drop table')).toBe(false)
    expect(s.dump()['trinity-workspace-inbox-sort:ada']).toBe('drop table')
  })

  it('a storage that throws, or none, costs the memory and nothing else', () => {
    expect(loadInboxSort(throwing, 'ada')).toBe(null)
    expect(saveInboxSort(throwing, 'ada', 'urgency')).toBe(false)
    expect(loadInboxSort(null, 'ada')).toBe(null)
    expect(saveInboxSort(null, 'ada', 'urgency')).toBe(false)
  })
})

describe('ent#856 B6 — ?from= and paging work the same in both orders', () => {
  const mixed = [
    ...ASKS,
    ask('bard-new', { agent_name: 'bard', created_at: at(-0.2 * H) }),
    ask('bard-old', { agent_name: 'bard', priority: 'high', created_at: at(-20 * H) }),
  ]
  for (const sort of INBOX_SORTS) {
    it(`${sort}: the narrowed rows are the sorted list filtered, never re-sorted`, () => {
      const all = actionItems(mixed, NOW, sort)
      const scout = filterByAgent(all, 'scout')
      expect(scout.map((r) => r.id)).toEqual(all.filter((r) => r.agent_name === 'scout').map((r) => r.id))
      expect(scout.every((r) => r.agent_name === 'scout')).toBe(true)
      const page = pageWindow(all, 2)
      expect(page.shown.map((r) => r.id)).toEqual(all.slice(0, 2).map((r) => r.id))
      expect(page.total).toBe(5)
      expect(page.hidden).toBe(3)
    })
  }
})
