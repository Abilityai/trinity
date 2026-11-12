/**
 * The board's decidable rules (trinity-enterprise#957) — pure functions, no mount.
 */
import { describe, it, expect } from 'vitest'
import {
  BOARD_COLUMNS, boardColumns, boardMinWidth, boardMove, boardMoveLine, boardStateActions, columnOf,
  readTasksView, tasksViewKey, writeTasksView,
} from '@/components/portal/projects/projectsUtils'

const t = (id, status, over = {}) => ({ id, title: `Task ${id}`, status, priority: 'p2', owner: null, agent: null, ...over })

describe('columnOf', () => {
  it('puts the held statuses in the Active column', () => {
    for (const st of ['active', 'blocked', 'needs-decision', 'paused']) expect(columnOf(t('T-1', st))).toBe('active')
    expect(columnOf(t('T-1', 'pending-verification'))).toBe('pending-verification')
    expect(columnOf(t('T-1', 'done'))).toBe('done')
  })

  it('a status no column names lands in the first column, never nowhere', () => {
    expect(columnOf(t('T-1', 'triage'))).toBe(BOARD_COLUMNS[0].key)
    expect(columnOf(t('T-1', undefined))).toBe(BOARD_COLUMNS[0].key)
  })

  it('ent#1232: a todo task is named by the Active column, not dropped into it', () => {
    expect(BOARD_COLUMNS[0].statuses).toContain('todo')
    expect(columnOf(t('T-1', 'todo'))).toBe('active')
  })
})

describe('boardColumns', () => {
  const rows = [
    t('T-10', 'active'), t('T-2', 'blocked', { waiting_on: 'legal' }), t('T-3', 'paused'),
    t('T-4', 'pending-verification', { pending_since: '2026-11-10T10:00:00Z' }),
    t('T-5', 'done', { updated_at: '2026-11-01T00:00:00Z' }), t('T-6', 'done', { updated_at: '2026-11-09T00:00:00Z' }),
    t('T-7', 'triage'), t('T-8', 'todo'),
  ]

  it('every task lands in exactly one column, and empty columns stay', () => {
    const cols = boardColumns(rows)
    expect(cols.map((c) => c.key)).toEqual(['active', 'pending-verification', 'done'])
    const ids = cols.flatMap((c) => c.cards.map((k) => k.id))
    expect(ids.slice().sort()).toEqual(rows.map((r) => r.id).sort())
    expect(boardColumns([]).map((c) => c.cards.length)).toEqual([0, 0, 0])
  })

  it('orders by id, Done newest first, and counts what is held', () => {
    const [active, , done] = boardColumns(rows)
    expect(active.cards.map((k) => k.id)).toEqual(['T-2', 'T-3', 'T-7', 'T-8', 'T-10'])
    expect(done.cards.map((k) => k.id)).toEqual(['T-6', 'T-5'])
    expect(active.held).toEqual([
      { status: 'blocked', label: 'blocked', count: 1 }, { status: 'paused', label: 'paused', count: 1 },
    ])
  })

  it('a held or unknown status is a badge; a path status is not', () => {
    const cards = Object.fromEntries(boardColumns(rows).flatMap((c) => c.cards).map((k) => [k.id, k]))
    expect(cards['T-2'].flag).toEqual({ label: 'Blocked', variant: 'danger' })
    expect(cards['T-7'].flag).toEqual({ label: 'triage', variant: 'neutral' })
    expect(cards['T-8'].flag).toEqual({ label: 'To do', variant: 'neutral' })
    expect(cards['T-10'].flag).toBeNull()
    expect(cards['T-4'].flag).toBeNull()
    expect(cards['T-2'].waitingOn).toBe('legal')
  })

  it('waiting_on shows only while the task is blocked', () => {
    const [col] = boardColumns([t('T-1', 'active', { waiting_on: 'legal' })])
    expect(col.cards[0].waitingOn).toBe('')
  })

  it('ent#1232: a card carries derived waiting, subtasks and a done without a claim', () => {
    const [active, , done] = boardColumns([
      t('T-1', 'todo', { waiting_for: ['T-3'], subtasks: { ids: ['T-2'], total: 1, done: 0 } }),
      t('T-3', 'done', { closed_without_claim: true, waiting_for: [] }),
      t('T-4', 'done', { closed_without_claim: false }),
    ])
    const [one] = active.cards
    expect(one.waitingFor).toBe('Waiting on T-3')
    expect(one.subtasks).toBe('0/1 subtasks')
    expect(one.unclaimed).toBe(false)
    expect(one.status).toBe('todo')
    const byId = Object.fromEntries(done.cards.map((k) => [k.id, k]))
    expect(byId['T-3'].unclaimed).toBe(true)
    expect(byId['T-4'].unclaimed).toBe(false)
    expect(byId['T-3'].waitingFor).toBe('')
  })

  it('a move in flight draws the card where it is going, marked saving', () => {
    const cols = boardColumns(rows, { inflight: { 'T-10': 'done' } })
    expect(cols[0].cards.map((k) => k.id)).not.toContain('T-10')
    const card = cols[2].cards.find((k) => k.id === 'T-10')
    expect(card.saving).toBe(true)
    expect(card.status).toBe('done')
  })

  it('names the owner from the members, the creator in words', () => {
    const [col] = boardColumns(
      [t('T-1', 'active', { owner: 'eva@example.com', agent: 'scout', created_by: 'agent:corbin' })],
      { members: [{ email: 'eva@example.com', label: 'Eva' }], myEmail: 'eva@example.com' },
    )
    expect(col.cards[0]).toMatchObject({ owner: 'Eva', agent: 'scout', creator: 'corbin (agent)' })
  })
})

describe('boardMove', () => {
  it('its own column is nothing — a blocked card dropped on Active stays blocked', () => {
    expect(boardMove(t('T-1', 'blocked'), 'active')).toEqual({ kind: 'noop' })
    expect(boardMove(t('T-1', 'active'), 'nowhere')).toEqual({ kind: 'noop' })
  })

  it('a done task only reopens', () => {
    expect(boardMove(t('T-1', 'done'), 'active')).toEqual({ kind: 'move', status: 'active', confirm: false })
    const refused = boardMove(t('T-1', 'done'), 'pending-verification')
    expect(refused.kind).toBe('refused')
    expect(refused.reason).toContain('T-1 is done')
  })

  it('Done from Awaiting verification is the confirmation; from anywhere else it asks first', () => {
    expect(boardMove(t('T-1', 'pending-verification'), 'done')).toEqual({ kind: 'move', status: 'done', confirm: false })
    expect(boardMove(t('T-1', 'active'), 'done')).toEqual({ kind: 'move', status: 'done', confirm: true })
    expect(boardMove(t('T-1', 'blocked'), 'done')).toEqual({ kind: 'move', status: 'done', confirm: true })
  })

  it('a held task moves along the path like any other', () => {
    expect(boardMove(t('T-1', 'blocked'), 'pending-verification')).toEqual({ kind: 'move', status: 'pending-verification', confirm: false })
  })
})

describe('boardStateActions', () => {
  it('offers the holds, and Resume once held', () => {
    expect(boardStateActions(t('T-1', 'active')).map((a) => a.status)).toEqual(['blocked', 'needs-decision', 'paused'])
    expect(boardStateActions(t('T-1', 'blocked')).map((a) => a.status)).toEqual(['active', 'needs-decision', 'paused'])
  })

  it('offers nothing past the working step', () => {
    expect(boardStateActions(t('T-1', 'pending-verification'))).toEqual([])
    expect(boardStateActions(t('T-1', 'done'))).toEqual([])
  })
})

describe('the rest', () => {
  it('the width the board needs grows with its columns', () => {
    expect(boardMinWidth([...BOARD_COLUMNS, BOARD_COLUMNS[0]])).toBeGreaterThan(boardMinWidth())
  })

  it('says a move in words', () => {
    expect(boardMoveLine('T-104', 'blocked', 'pending-verification')).toBe('T-104 · Blocked → Awaiting verification')
  })

  it('remembers the view per person, and reads junk as List', () => {
    const mem = {}
    const storage = { getItem: (k) => mem[k] ?? null, setItem: (k, v) => { mem[k] = v } }
    expect(readTasksView('Eva@Example.com', storage)).toBe('list')
    writeTasksView('Eva@Example.com', 'board', storage)
    expect(readTasksView('eva@example.com', storage)).toBe('board')
    expect(readTasksView('bob@example.com', storage)).toBe('list')
    mem[tasksViewKey('bob@example.com')] = 'kanban'
    expect(readTasksView('bob@example.com', storage)).toBe('list')
    writeTasksView('bob@example.com', 'kanban', storage)
    expect(mem[tasksViewKey('bob@example.com')]).toBe('kanban')
    const broken = { getItem: () => { throw new Error('denied') }, setItem: () => { throw new Error('denied') } }
    expect(readTasksView('eva@example.com', broken)).toBe('list')
    expect(() => writeTasksView('eva@example.com', 'board', broken)).not.toThrow()
  })
})
