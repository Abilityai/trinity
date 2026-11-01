/**
 * trinity-enterprise#539 — the rules behind Select all / Deselect all in
 * "Assign shared skills" and the confirm before a save drops approval
 * requirements (`utils/skillAssignDraft.js`), executed over every row type and
 * every reason. The wiring — what the buttons do to the real draft, what Save
 * then sends, focus, the confirm — is `skillAssignSelectAll.spec.js` (mounted).
 */
import { describe, it, expect } from 'vitest'
import {
  bulkSelection, withSelectAll, withDeselectAll, removedNames, gatedRemovals, removalConfirm,
  BULK_TITLES, selectAllTitle, deselectAllTitle,
} from '../../src/utils/skillAssignDraft'

// One row of every kind the dialog lists. `locked` is held only through a set;
// `_scratch` is a library directory whose name the PUT refuses (the listing
// does not validate names). The draft also holds `gone`, an assignment whose
// skill left the library, so the dialog has no row for it.
const ROWS = [
  { name: 'plain-on' }, { name: 'plain-off' },
  { name: 'dep-on', deprecated: true }, { name: 'dep-off', deprecated: true },
  { name: 'locked' }, { name: 'both' },
  { name: '_scratch' },
]
const LOCKED = new Set(['locked'])
const SAVED = ['plain-on', 'dep-on', 'both', 'gone']
const sel = (over = {}) => bulkSelection({
  rows: ROWS, draft: SAVED, locked: LOCKED, loaded: true, emptyReason: null, saving: false, ...over,
})

describe('what each button would change', () => {
  it('Select all adds only rows that are unticked, not deprecated, assignable and not a set\'s', () => {
    expect(sel().toAdd).toEqual(['plain-off'])
  })

  it('Deselect all removes every ticked listed row except a set\'s, deprecated ones included', () => {
    expect(sel().toRemove).toEqual(['plain-on', 'dep-on', 'both'])
  })

  it('both keep every name the dialog does not list', () => {
    const s = sel()
    expect(withSelectAll(SAVED, s.toAdd)).toEqual(['plain-on', 'dep-on', 'both', 'gone', 'plain-off'])
    expect(withDeselectAll(SAVED, s.toRemove)).toEqual(['gone'])
  })

  it('Deselect all then Select all does not re-tick a deprecated skill the agent held', () => {
    const cleared = withDeselectAll(SAVED, sel().toRemove)
    const again = withSelectAll(cleared, sel({ draft: cleared }).toAdd)
    expect(again).not.toContain('dep-on')
    expect([...again].sort()).toEqual(['both', 'gone', 'plain-off', 'plain-on'])
  })

  it('a set\'s row never enters the draft, so a library of them has nothing to add', () => {
    const s = bulkSelection({ rows: [{ name: 'locked' }], draft: [], locked: LOCKED, loaded: true })
    expect(s.toAdd).toEqual([])
    expect(s.toRemove).toEqual([])
  })
})

describe('Select all — enabled, or the reason it is not', () => {
  it.each([
    ['the library has not loaded', { loaded: false }, BULK_TITLES.loading],
    ['no library is configured', { rows: [], emptyReason: 'library_unconfigured' }, BULK_TITLES.unconfigured],
    ['the library is empty', { rows: [], emptyReason: 'library_empty' }, BULK_TITLES.empty],
    ['a save is in flight', { saving: true }, BULK_TITLES.saving],
    ['a save is in flight and nothing is left', { saving: true, draft: ROWS.map((r) => r.name) }, BULK_TITLES.saving],
    ['every row is ticked', { draft: ROWS.map((r) => r.name) }, BULK_TITLES.allTicked],
    ['only a set\'s rows are listed', { rows: [{ name: 'locked' }], draft: [] }, BULK_TITLES.allTicked],
    ['only a deprecated row is left', { draft: [...SAVED, 'plain-off', '_scratch'] }, BULK_TITLES.deprecatedLeft],
    ['only an unassignable row is left', { draft: [...SAVED, 'plain-off', 'dep-off'] }, BULK_TITLES.unassignableLeft],
    ['deprecated and unassignable rows are left', { draft: [...SAVED, 'plain-off'] }, BULK_TITLES.deprecatedLeft],
  ])('disabled when %s', (_, over, title) => {
    expect(sel(over).select).toEqual({ disabled: true, title })
  })

  it('an agent with nothing assigned yet can still Select all', () => {
    expect(sel({ draft: [], emptyReason: 'none_assigned' }).select.disabled).toBe(false)
  })

  it('enabled, its title counts what it will tick, and names what it skips', () => {
    // `_scratch` is unticked and refused by the PUT, so the title says it is skipped.
    expect(sel().select).toEqual({ disabled: false, title: selectAllTitle(1, { skipsUnassignable: true }) })
    expect(sel({ draft: [] }).select.title).toBe(selectAllTitle(3, { skipsUnassignable: true }))
    // With no such row left unticked, only the deprecated exception is named.
    expect(sel({ draft: [...SAVED, '_scratch'] }).select.title).toBe(selectAllTitle(1))
    expect(selectAllTitle(1)).not.toMatch(/can't assign/)
    expect(selectAllTitle(1, { skipsUnassignable: true })).toMatch(/can't assign/)
  })
})

describe('Deselect all — enabled, or the reason it is not', () => {
  it.each([
    ['the library has not loaded', { loaded: false }, BULK_TITLES.loading],
    ['a save is in flight', { saving: true }, BULK_TITLES.saving],
    ['nothing is ticked', { rows: [{ name: 'plain-off' }], draft: [] }, BULK_TITLES.noneTicked],
    ['the draft holds only a name the dialog does not list', { rows: [{ name: 'plain-off' }], draft: ['gone'] }, BULK_TITLES.noneTicked],
    ['only a set\'s row is ticked', { rows: [{ name: 'locked' }, { name: 'plain-off' }], draft: [] }, BULK_TITLES.onlySetTicked],
    ['the library is empty', { rows: [], draft: [], emptyReason: 'library_empty' }, BULK_TITLES.noneTicked],
  ])('disabled when %s', (_, over, title) => {
    expect(sel(over).deselect).toEqual({ disabled: true, title })
  })

  it('enabled, its title counts what it will untick', () => {
    expect(sel().deselect).toEqual({ disabled: false, title: deselectAllTitle(3) })
  })
})

describe('the titles say what the rule is', () => {
  it('Select all names the listed rows and the deprecated exception', () => {
    // "listed here": a filter, if one arrives, narrows what both buttons act on.
    expect(selectAllTitle(2)).toMatch(/listed here/)
    expect(selectAllTitle(2)).toMatch(/deprecated/)
  })

  it('Deselect all names the rows it leaves and what a save then does', () => {
    expect(deselectAllTitle(2)).toMatch(/listed here/)
    // A row both assigned on its own and through a set IS unticked; only a
    // set-only row is left, so the title must not promise more.
    expect(deselectAllTitle(2)).toMatch(/only a set assigns/)
    expect(deselectAllTitle(2)).toMatch(/asks first if that would drop an approval requirement/)
  })

  it('every reason reads differently', () => {
    const all = Object.values(BULK_TITLES)
    expect(new Set(all).size).toBe(all.length)
  })
})

describe('what a save takes off the agent', () => {
  it('saved names left out of the draft, except ones a set still holds', () => {
    expect(removedNames({ saved: ['a', 'b', 'c'], draft: ['a'], setHeld: new Set(['c']) })).toEqual(['b'])
  })

  it('with the sets untrusted (an empty setHeld), a set-held name is removed too', () => {
    expect(removedNames({ saved: ['a', 'c'], draft: ['a'], setHeld: new Set() })).toEqual(['c'])
  })

  it('nothing when the draft keeps every saved name', () => {
    expect(removedNames({ saved: new Set(['a', 'b']), draft: ['b', 'a', 'x'] })).toEqual([])
  })
})

describe('which saves ask first', () => {
  const base = {
    saved: ['plain-on', 'dep-on', 'both', 'gone'],
    draft: ['gone'],
    setHeld: new Set(['both']),
    gated: new Set(),
    conflict: new Set(),
    gatesKnown: true,
  }

  it('none when nothing is removed', () => {
    expect(gatedRemovals({ ...base, draft: base.saved })).toBe(null)
  })

  it('none when nothing removed has an approval requirement', () => {
    expect(gatedRemovals(base)).toBe(null)
  })

  it('a removed skill with one, matched on the lower-cased gate key', () => {
    expect(gatedRemovals({ ...base, saved: ['Plain-On', 'gone'], gated: new Set(['plain-on']) }))
      .toEqual({ names: ['Plain-On'], gatesKnown: true })
  })

  it('not a skill a set still holds: the save only demotes it, and its requirement stays', () => {
    expect(gatedRemovals({ ...base, gated: new Set(['both']) })).toBe(null)
  })

  it('not a skill in conflict: the server keeps that requirement', () => {
    expect(gatedRemovals({ ...base, gated: new Set(['dep-on']), conflict: new Set(['dep-on']) })).toBe(null)
  })

  it('every removal when the approval map could not be read', () => {
    expect(gatedRemovals({ ...base, gatesKnown: false }))
      .toEqual({ names: ['plain-on', 'dep-on'], gatesKnown: false })
  })

  it('none when the map could not be read but nothing is removed', () => {
    expect(gatedRemovals({ ...base, draft: base.saved, gatesKnown: false })).toBe(null)
  })

  it('never a conflict name, even with the map unread: the agent\'s own skill and its requirement stay', () => {
    expect(gatedRemovals({ ...base, conflict: new Set(['dep-on']), gatesKnown: false }))
      .toEqual({ names: ['plain-on'], gatesKnown: false })
    expect(gatedRemovals({ ...base, saved: ['dep-on', 'gone'], conflict: new Set(['dep-on']), gatesKnown: false }))
      .toBe(null)
  })
})

describe('what the confirm says', () => {
  it('one skill', () => {
    expect(removalConfirm({ names: ['a'], gatesKnown: true })).toEqual({
      title: 'Unassign /a?',
      message: '/a needs approval. Saving will remove it from this agent, along with its approval requirement.',
    })
  })

  it('several, in a bounded list', () => {
    expect(removalConfirm({ names: ['a', 'b'], gatesKnown: true })).toEqual({
      title: 'Unassign 2 skills that need approval?',
      message: '/a and /b need approval. Saving will remove them from this agent, along with their approval requirements.',
    })
    const seven = removalConfirm({ names: ['a', 'b', 'c', 'd', 'e', 'f', 'g'], gatesKnown: true })
    expect(seven.title).toBe('Unassign 7 skills that need approval?')
    expect(seven.message.startsWith('/a, /b, /c, /d, /e and 2 more need approval.')).toBe(true)
  })

  it('an unread map says why it asks', () => {
    expect(removalConfirm({ names: ['a'], gatesKnown: false })).toEqual({
      title: 'Unassign /a?',
      message: "Saving will remove /a from this agent. Which skills need approval couldn't be read, so any approval requirement on it will go too.",
    })
    expect(removalConfirm({ names: ['a', 'b', 'c'], gatesKnown: false })).toEqual({
      title: 'Unassign 3 skills?',
      message: "Saving will remove /a, /b and /c from this agent. Which skills need approval couldn't be read, so any approval requirement on them will go too.",
    })
  })
})
