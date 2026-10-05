/**
 * ent#621 — the Workspace key map: the declaration and its pure helpers.
 *
 * This spec imports `portalKeymap.js` FIRST (nothing from `portalUtils` has
 * been evaluated yet), which is half the cycle check the two modules' headers
 * promise; `portalChatTabsAndTitles.spec.js` imports them the other way round.
 *
 * What is worth pinning is what an obvious implementation gets wrong:
 *
 *   * a chord is matched by the printed `key` OR by the physical `code`, and
 *     the two arms disagree about Shift on purpose (Decision 29) — without the
 *     `key` arm, DE's `Shift+7` → `/` is dead; without the `code` arm, `⌥.` on
 *     a US Mac is dead (it types `≥`); with Shift accepted on the `code` arm,
 *     macOS `⌘?` Help search is swallowed;
 *   * the map's own entries must not shadow each other — asserting "no
 *     collisions" is only worth anything if the checker can SEE one, so a
 *     planted clash is asserted too;
 *   * the reserved `⌘K` resolves to NOTHING (the shell must not
 *     `preventDefault` a key it does not handle) and never reaches the list;
 *   * `keymapSuppressed` is a matrix, not a boolean: a modal stops every shell
 *     key including ⌘J, a call stops only the keys that would move you.
 */
import { describe, it, expect } from 'vitest'
import {
  WORKSPACE_KEYMAP, OWNER_SHELL, OWNER_PROTOCOL, SCOPE_MOVING, SCOPE_DOING, SCOPE_PROTOCOL,
  entryChords, findBinding, matchesChord, resolveWorkspaceKey, workspaceChord, keymapCollisions,
  keymapSuppressed, cycleIndex, nextAgent, recordLastOpen, nextRailTab,
  chordLabel, ariaKeyshortcuts, keyListRows,
} from '../../src/components/portal/portalKeymap'
import { isNewChatHotkey, newChatHotkeyLabel } from '../../src/components/portal/portalUtils'

// A keydown as the browser delivers it. Plain object on purpose: the existing
// ⌘J truth table passes one, and `isNewChatHotkey` keeps that signature.
const ev = (o = {}) => ({
  key: '', code: '', metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
  repeat: false, isComposing: false, keyCode: 0, ...o,
})

const chordOf = (action) => {
  const entry = findBinding(action)
  return entryChords(entry)[0]
}

describe('the map as a declaration', () => {
  it('declares one Workspace surface, with an owner and a scope on every entry', () => {
    for (const entry of WORKSPACE_KEYMAP) {
      expect(entry.surface, entry.id).toBe('workspace')
      expect([OWNER_SHELL, OWNER_PROTOCOL], entry.id).toContain(entry.owner)
      expect([SCOPE_MOVING, SCOPE_DOING, SCOPE_PROTOCOL], entry.id).toContain(entry.scope)
      expect(entryChords(entry).length, entry.id).toBeGreaterThan(0)
      expect(typeof entry.label, entry.id).toBe('string')
    }
  })

  it('gives every shell chord a modifier — a bare key belongs to the field', () => {
    for (const entry of WORKSPACE_KEYMAP.filter((e) => e.owner === OWNER_SHELL)) {
      for (const c of entryChords(entry)) {
        expect(c.primary || c.alt, entry.id).toBe(true)
      }
    }
  })

  it('declares the nine chords the issue names, plus the protocol keys', () => {
    const shell = WORKSPACE_KEYMAP.filter((e) => e.owner === OWNER_SHELL)
    expect(shell.map((e) => e.action)).toEqual([
      'find-anything', 'new-chat', 'rail-toggle', 'key-list',
      'agent-next', 'agent-prev', 'chat-next', 'chat-prev', 'rail-tab-next',
    ])
    expect(shell.filter((e) => e.reserved).map((e) => e.action)).toEqual(['find-anything'])
    // The component-owned keys are declared so the collision check sees the
    // whole surface; the handlers stay where the Esc learnings put them.
    expect(WORKSPACE_KEYMAP.filter((e) => e.owner === OWNER_PROTOCOL).map((e) => e.action))
      .toEqual(['close-top', 'call-mute', 'preview-step', 'tab-roving', 'dialog-focus-cycle', 'typeahead-step'])
  })

  it('has no collisions — and the checker can see one when there is', () => {
    expect(keymapCollisions(WORKSPACE_KEYMAP)).toEqual([])
    const planted = [...WORKSPACE_KEYMAP, {
      id: 'spotlight-lookalike', action: 'x', surface: 'workspace', owner: OWNER_SHELL,
      scope: SCOPE_DOING, label: 'x', chord: { key: 'j', code: 'KeyJ', primary: true },
    }]
    expect(keymapCollisions(planted)).toEqual([['new-chat', 'spotlight-lookalike']])
    // A shell key that shadows a protocol key is a collision too.
    const shadow = [...WORKSPACE_KEYMAP, {
      id: 'esc-stealer', action: 'y', surface: 'workspace', owner: OWNER_SHELL,
      scope: SCOPE_DOING, label: 'y', chord: { key: 'Escape' },
    }]
    expect(keymapCollisions(shadow)).toEqual([['close-top', 'esc-stealer']])
    // Two PROTOCOL entries may share one: the preview's arrows and a tab
    // strip's roving are the same keys in different places.
    expect(keymapCollisions(WORKSPACE_KEYMAP.filter((e) => e.owner === OWNER_PROTOCOL))).toEqual([])
  })
})

describe('matchesChord', () => {
  const railToggle = chordOf('rail-toggle')
  const keyList = chordOf('key-list')
  const agentNext = chordOf('agent-next')
  const chatNext = chordOf('chat-next')
  const railTab = chordOf('rail-tab-next')

  it('takes the primary modifier as meta XOR ctrl, on every platform', () => {
    expect(matchesChord(ev({ key: '.', metaKey: true }), railToggle)).toBe(true)
    expect(matchesChord(ev({ key: '.', ctrlKey: true }), railToggle)).toBe(true)
    expect(matchesChord(ev({ key: '.', metaKey: true, ctrlKey: true }), railToggle)).toBe(false)
    expect(matchesChord(ev({ key: '.' }), railToggle)).toBe(false)
  })

  it('matches the physical position when the printed key is something else', () => {
    // ⌥. on a US Mac types `≥`; the Period position is the only arm left.
    expect(matchesChord(ev({ key: '≥', code: 'Period', altKey: true }), railTab)).toBe(true)
    // A Cyrillic layout: `key` is non-Latin, the US position still works.
    expect(matchesChord(ev({ key: 'о', code: 'KeyJ', metaKey: true }), chordOf('new-chat'))).toBe(true)
    // No `code` at all (a hand-built event) simply loses that arm.
    expect(matchesChord({ key: 'j', metaKey: true }, chordOf('new-chat'))).toBe(true)
    expect(matchesChord({ code: 'KeyJ', metaKey: true }, chordOf('new-chat'))).toBe(true)
  })

  it('accepts Shift on the printed key but never on the physical one (Decision 29)', () => {
    // DE: `/` is Shift+7. FR: `.` is a shifted key. Both must work.
    expect(matchesChord(ev({ key: '/', code: 'Digit7', metaKey: true, shiftKey: true }), keyList)).toBe(true)
    expect(matchesChord(ev({ key: '/', code: 'Slash', metaKey: true }), keyList)).toBe(true)
    // macOS ⌘? (Help search) is Shift on the Slash POSITION with `key: '?'`:
    // neither arm may claim it.
    expect(matchesChord(ev({ key: '?', code: 'Slash', metaKey: true, shiftKey: true }), keyList)).toBe(false)
    // A chord without `shiftOnKey` rejects Shift outright.
    expect(matchesChord(ev({ key: '.', metaKey: true, shiftKey: true }), railToggle)).toBe(false)
  })

  it('is exact about Alt and Shift, which is what keeps the four arrows apart', () => {
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true }), agentNext)).toBe(true)
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true, shiftKey: true }), agentNext)).toBe(false)
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true, shiftKey: true }), chatNext)).toBe(true)
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true }), chatNext)).toBe(false)
    expect(matchesChord(ev({ key: 'ArrowDown' }), agentNext)).toBe(false)
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true, metaKey: true }), agentNext)).toBe(false)
  })

  it('answers false rather than throwing for junk', () => {
    expect(matchesChord(null, agentNext)).toBe(false)
    expect(matchesChord(ev({ key: 'ArrowDown', altKey: true }), null)).toBe(false)
    expect(matchesChord({ altKey: true }, agentNext)).toBe(false)
  })
})

describe('resolveWorkspaceKey', () => {
  it('names the action for each of the eight live chords', () => {
    expect(resolveWorkspaceKey(ev({ key: 'j', metaKey: true }))).toBe('new-chat')
    expect(resolveWorkspaceKey(ev({ key: 'j', ctrlKey: true }))).toBe('new-chat')
    expect(resolveWorkspaceKey(ev({ key: '.', metaKey: true }))).toBe('rail-toggle')
    expect(resolveWorkspaceKey(ev({ key: '/', metaKey: true }))).toBe('key-list')
    expect(resolveWorkspaceKey(ev({ key: 'ArrowDown', altKey: true }))).toBe('agent-next')
    expect(resolveWorkspaceKey(ev({ key: 'ArrowUp', altKey: true }))).toBe('agent-prev')
    expect(resolveWorkspaceKey(ev({ key: 'ArrowDown', altKey: true, shiftKey: true }))).toBe('chat-next')
    expect(resolveWorkspaceKey(ev({ key: 'ArrowUp', altKey: true, shiftKey: true }))).toBe('chat-prev')
    expect(resolveWorkspaceKey(ev({ key: '.', altKey: true }))).toBe('rail-tab-next')
  })

  it('resolves the reserved and the protocol keys to nothing', () => {
    // ⌘K is ent#577's. Resolving it would make the dispatcher `preventDefault`
    // a key it cannot answer, taking the browser's own Ctrl+K with it.
    expect(resolveWorkspaceKey(ev({ key: 'k', code: 'KeyK', metaKey: true }))).toBeNull()
    // Esc is a capture-phase protocol owned by whoever is innermost.
    expect(resolveWorkspaceKey(ev({ key: 'Escape' }))).toBeNull()
    expect(resolveWorkspaceKey(ev({ key: 'm' }))).toBeNull()
    expect(resolveWorkspaceKey(ev({ key: 'Tab' }))).toBeNull()
  })

  it('ignores a held key and an IME composition', () => {
    expect(resolveWorkspaceKey(ev({ key: 'ArrowDown', altKey: true, repeat: true }))).toBeNull()
    expect(resolveWorkspaceKey(ev({ key: 'ArrowDown', altKey: true, isComposing: true }))).toBeNull()
    expect(resolveWorkspaceKey(ev({ key: 'ArrowDown', altKey: true, keyCode: 229 }))).toBeNull()
    expect(resolveWorkspaceKey(null)).toBeNull()
  })

  it('still knows whose chord a held key is, so its repeat can be kept from the browser', () => {
    // One press is one action, so `resolveWorkspaceKey` answers null for a
    // repeat — but the repeat of a chord the shell claimed is not the
    // browser's either: a held ⌥. types `≥` into the message field on a Mac,
    // and a held Ctrl+J opens the browser's Downloads.
    expect(workspaceChord(ev({ key: '≥', code: 'Period', altKey: true, repeat: true }))).toBe('rail-tab-next')
    expect(workspaceChord(ev({ key: 'j', code: 'KeyJ', ctrlKey: true, repeat: true }))).toBe('new-chat')
    expect(workspaceChord(ev({ key: 'ArrowDown', altKey: true }))).toBe('agent-next')
    // Never for a chord the shell does not dispatch, and never mid-composition.
    expect(workspaceChord(ev({ key: 'k', code: 'KeyK', metaKey: true, repeat: true }))).toBeNull()
    expect(workspaceChord(ev({ key: 'Escape', repeat: true }))).toBeNull()
    expect(workspaceChord(ev({ key: 'ArrowDown', altKey: true, repeat: true, isComposing: true }))).toBeNull()
    expect(workspaceChord(null)).toBeNull()
  })

  it('says nothing about plain typing', () => {
    for (const key of ['a', 'Enter', '.', '/', 'j', 'ArrowDown', 'ArrowUp']) {
      expect(resolveWorkspaceKey(ev({ key })), key).toBeNull()
    }
  })

  it('still answers for ⌘J exactly as isNewChatHotkey does', () => {
    const cases = [
      ev({ key: 'j', metaKey: true }), ev({ key: 'J', ctrlKey: true }),
      ev({ key: 'j' }), ev({ key: 'j', metaKey: true, shiftKey: true }),
      ev({ key: 'j', ctrlKey: true, altKey: true }), ev({ key: 'k', metaKey: true }),
    ]
    for (const e of cases) {
      expect(isNewChatHotkey(e), e.key).toBe(resolveWorkspaceKey(e) === 'new-chat')
    }
  })
})

describe('keymapSuppressed', () => {
  const sup = (o) => keymapSuppressed(o)

  it('lets a shell key through when nothing is in the way', () => {
    for (const action of ['new-chat', 'rail-toggle', 'key-list', 'agent-next', 'chat-prev', 'rail-tab-next']) {
      expect(sup({ action }), action).toBe(false)
    }
  })

  it('yields to a nearer owner that already claimed the event', () => {
    expect(sup({ action: 'agent-next', defaultPrevented: true })).toBe(true)
    expect(sup({ action: 'new-chat', defaultPrevented: true })).toBe(true)
  })

  it('stops every shell key under something modal — ⌘J included', () => {
    for (const action of ['new-chat', 'rail-toggle', 'key-list', 'agent-next', 'chat-next', 'rail-tab-next']) {
      expect(sup({ action, modalOpen: true }), action).toBe(true)
      expect(sup({ action, drawerOpen: true }), action).toBe(true)
    }
  })

  it('stops only the moving keys and the rail during a call', () => {
    for (const action of ['agent-next', 'agent-prev', 'chat-next', 'chat-prev', 'rail-tab-next', 'rail-toggle']) {
      expect(sup({ action, callActive: true }), action).toBe(true)
    }
    // ⌘J keeps its own "leave the call?" ask; ⌘/ opens a dialog, which leaves
    // nothing.
    expect(sup({ action: 'new-chat', callActive: true })).toBe(false)
    expect(sup({ action: 'key-list', callActive: true })).toBe(false)
  })

  it('fails closed for anything it does not dispatch', () => {
    expect(sup({ action: 'find-anything' })).toBe(true)
    expect(sup({ action: 'close-top' })).toBe(true)
    expect(sup({ action: 'a-future-action' })).toBe(true)
    expect(sup({})).toBe(true)
  })
})

describe('the walk helpers', () => {
  it('wraps an index at both ends and survives an empty list', () => {
    expect(cycleIndex(0, 1, 3)).toBe(1)
    expect(cycleIndex(2, 1, 3)).toBe(0)
    expect(cycleIndex(0, -1, 3)).toBe(2)
    expect(cycleIndex(-1, 1, 3)).toBe(0)
    expect(cycleIndex(-1, -1, 3)).toBe(2)
    expect(cycleIndex(0, 1, 1)).toBe(0)
    expect(cycleIndex(0, 1, 0)).toBe(-1)
  })

  it('steps through the roster the sidebar shows, and wraps', () => {
    const order = [{ name: 'scout' }, { name: 'sage' }, { name: 'rabbit' }]
    expect(nextAgent(order, 'scout', 1)).toBe('sage')
    expect(nextAgent(order, 'rabbit', 1)).toBe('scout')
    expect(nextAgent(order, 'scout', -1)).toBe('rabbit')
    expect(nextAgent(['scout', 'sage'], 'sage', 1)).toBe('scout')
    // An agent that is not in the list (a room, the Inbox, a stale name) starts
    // the walk at an end rather than doing nothing.
    expect(nextAgent(order, null, 1)).toBe('scout')
    expect(nextAgent(order, 'gone', -1)).toBe('rabbit')
  })

  it('does nothing when there is nowhere to go', () => {
    expect(nextAgent([{ name: 'solo' }], 'solo', 1)).toBeNull()
    expect(nextAgent([], 'solo', 1)).toBeNull()
    expect(nextAgent(null, 'solo', 1)).toBeNull()
    expect(nextAgent([{ name: '' }, { id: 1 }], null, 1)).toBeNull()
  })

  it('cycles only the rail tabs this session may see', () => {
    const tabs = [{ id: 'work' }, { id: 'files' }, { id: 'info' }]
    expect(nextRailTab(tabs, 'work')).toBe('files')
    expect(nextRailTab(tabs, 'info')).toBe('work')
    expect(nextRailTab(tabs, 'info', -1)).toBe('files')
    // A remembered tab this session cannot see starts at the first visible one.
    expect(nextRailTab(tabs, 'canvas')).toBe('work')
    expect(nextRailTab([{ id: 'work' }], 'work')).toBe('work')
    expect(nextRailTab([], 'work')).toBeNull()
    expect(nextRailTab(null, 'work')).toBeNull()
  })
})

describe('recordLastOpen', () => {
  it('records the chat on stage for the agent on stage', () => {
    const m = new Map()
    expect(recordLastOpen(m, { agentName: 'scout', sessionId: 't1', keyChanged: true })).toBe(true)
    expect(m.get('scout')).toBe('t1')
    expect(recordLastOpen(m, { agentName: 'scout', sessionId: 't2' })).toBe(true)
    expect(m.get('scout')).toBe('t2')
  })

  it('ignores a cleared load instruction under the agent already on stage', () => {
    // Clicking an agent row nulls `pendingSession` BEFORE the route changes:
    // recording that would forget the chat the person was just reading.
    const m = new Map([['scout', 't2']])
    expect(recordLastOpen(m, { agentName: 'scout', sessionId: null })).toBe(false)
    expect(m.get('scout')).toBe('t2')
  })

  it('records a null for a NEW agent — an unsent new chat is a landing', () => {
    // `agentLanding`'s arms 3–5 then return to the draft, the agent's existing
    // empty chat, or a fresh one. No sentinel, no second rule.
    const m = new Map([['scout', 't2']])
    expect(recordLastOpen(m, { agentName: 'sage', sessionId: null, keyChanged: true })).toBe(true)
    expect(m.get('sage')).toBeNull()
    expect(m.get('scout')).toBe('t2')
  })

  it('writes nothing without a map or an agent', () => {
    expect(recordLastOpen(null, { agentName: 'scout', sessionId: 't1' })).toBe(false)
    expect(recordLastOpen(new Map(), { sessionId: 't1', keyChanged: true })).toBe(false)
    expect(recordLastOpen(new Map(), {})).toBe(false)
  })
})

describe('what people and screen readers read', () => {
  it('labels a chord for the platform', () => {
    expect(chordLabel(chordOf('new-chat'), 'MacIntel')).toBe('⌘J')
    expect(chordLabel(chordOf('new-chat'), 'Win32')).toBe('Ctrl+J')
    expect(chordLabel(chordOf('rail-toggle'), 'MacIntel')).toBe('⌘.')
    expect(chordLabel(chordOf('key-list'), 'Linux x86_64')).toBe('Ctrl+/')
    expect(chordLabel(chordOf('agent-next'), 'MacIntel')).toBe('⌥↓')
    expect(chordLabel(chordOf('agent-prev'), 'Win32')).toBe('Alt+↑')
    expect(chordLabel(chordOf('chat-next'), 'MacIntel')).toBe('⌥⇧↓')
    expect(chordLabel(chordOf('chat-prev'), 'Win32')).toBe('Alt+Shift+↑')
    expect(chordLabel(chordOf('rail-tab-next'), 'MacIntel')).toBe('⌥.')
    expect(chordLabel(chordOf('close-top'), 'MacIntel')).toBe('Esc')
    expect(chordLabel(null, 'MacIntel')).toBe('')
  })

  it('keeps the shipped ⌘J label, which now comes from the map', () => {
    expect(newChatHotkeyLabel('MacIntel')).toBe('⌘J')
    expect(newChatHotkeyLabel('Linux x86_64')).toBe('Ctrl+J')
    expect(newChatHotkeyLabel(undefined)).toBe('Ctrl+J')
  })

  it('spells aria-keyshortcuts ARIA\'s way: the character for printables', () => {
    expect(ariaKeyshortcuts(chordOf('agent-next'), 'MacIntel')).toBe('Alt+ArrowDown')
    expect(ariaKeyshortcuts(chordOf('chat-prev'), 'Win32')).toBe('Alt+Shift+ArrowUp')
    expect(ariaKeyshortcuts(chordOf('rail-tab-next'), 'Win32')).toBe('Alt+.')
    expect(ariaKeyshortcuts(chordOf('rail-toggle'), 'MacIntel')).toBe('Meta+.')
    expect(ariaKeyshortcuts(chordOf('rail-toggle'), 'Win32')).toBe('Control+.')
    expect(ariaKeyshortcuts(chordOf('key-list'), 'MacIntel')).toBe('Meta+/')
    expect(ariaKeyshortcuts(chordOf('new-chat'), 'MacIntel')).toBe('Meta+J')
    // Alternatives are space-separated, which is the attribute's own grammar.
    expect(ariaKeyshortcuts(entryChords(findBinding('tab-roving')), 'Win32'))
      .toBe('ArrowLeft ArrowRight Home End')
  })
})

describe('keyListRows', () => {
  const rows = keyListRows(WORKSPACE_KEYMAP, 'MacIntel')

  it('renders every live binding from the map, and the reserved one from none', () => {
    expect(rows.map((r) => r.action)).not.toContain('find-anything')
    expect(rows).toHaveLength(WORKSPACE_KEYMAP.length - 1)
    expect(rows.find((r) => r.action === 'agent-next')).toMatchObject({
      label: 'Next agent', keys: '⌥↓', ariaKeyshortcuts: 'Alt+ArrowDown', scope: SCOPE_MOVING,
    })
    expect(rows.find((r) => r.action === 'tab-roving').keys).toBe('← / → / Home / End')
  })

  it('says a key is one-per-press rather than letting someone hold it', () => {
    expect(rows.find((r) => r.action === 'agent-next').note).toMatch(/one agent per press/i)
  })

  it('tells the truth on a page with no rail instead of hiding the rows', () => {
    const noRail = keyListRows(WORKSPACE_KEYMAP, 'Win32', { railAvailable: false })
    for (const action of ['rail-toggle', 'rail-tab-next']) {
      expect(noRail.find((r) => r.action === action).note, action).toMatch(/no rail/i)
    }
    expect(noRail.find((r) => r.action === 'agent-next').note || '').not.toMatch(/no rail/i)
    expect(rows.find((r) => r.action === 'rail-toggle').note || '').not.toMatch(/no rail/i)
  })

  it('names where a protocol key applies, so Esc does not read as global', () => {
    expect(rows.find((r) => r.action === 'close-top').note).toMatch(/innermost/i)
    expect(rows.find((r) => r.action === 'call-mute').note).toMatch(/voice call/i)
  })
})
