/**
 * ent#392 — Workspace composer typeahead (`/` playbooks, `@` agents).
 *
 * Both invocation syntaxes already worked; neither was discoverable. The risk in
 * adding the affordance is not that it fails loudly — it is that it succeeds
 * quietly at the wrong thing:
 *
 *   1. **A selected mention must always escalate.** Agent slugs may contain `.`
 *      and have no length cap; the mention grammar allows neither. So offering
 *      `data.scout` would MANUFACTURE the silent degrade-to-plain-text that AC#4
 *      forbids. The round-trip below is asserted over the SPLICED VALUE, not the
 *      bare token — asserting the token alone is near-tautological and passes
 *      while a double-space splice ships.
 *
 *   2. **Enter must never be swallowed.** A popup that merely happens to be open
 *      (a paste, or prose like "check /status of the deploy") would otherwise
 *      splice up to 500 characters over the message the user meant to send.
 *
 * There is no component-mount harness in this project (no `@vue/test-utils`), so
 * every decision worth pinning lives in a pure export and is tested here
 * directly; the parts no unit test can reach (which handler the textarea binds,
 * where the popup is anchored) are covered by source-structure guards at the
 * bottom, comments stripped first so prose about a rule is not scanned as code.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

import {
  MAX_TYPEAHEAD_QUERY,
  TYPEAHEAD_LIMIT,
  detectTypeaheadTrigger,
  applyTypeaheadInsert,
  buildMentionToken,
  isMentionable,
  starterFor,
  filterPlaybookCandidates,
  filterAgentCandidates,
  typeaheadEmptyMessage,
  boundCandidates,
  roomMentionSource,
  resolveComposerKey,
  nextDismissState,
  isSuppressed,
  dismissAfterInsert,
  nextActiveIndex,
  clampActiveIndex,
  mentionedAgents,
  EMPTY_REASON_NO_PLAYBOOKS,
  EMPTY_REASON_NO_PEERS,
  EMPTY_REASON_NO_MENTIONABLE_PEERS,
  EMPTY_REASON_NO_ROOM_PEERS,
} from '@/components/portal/portalUtils'

const CONV = fileURLToPath(new URL('../../src/components/portal/PortalConversation.vue', import.meta.url))
const ROOM = fileURLToPath(new URL('../../src/components/portal/PortalRoom.vue', import.meta.url))
const BRIEF = fileURLToPath(new URL('../../src/components/portal/PortalBriefing.vue', import.meta.url))
const POPUP = fileURLToPath(new URL('../../src/components/portal/PortalTypeahead.vue', import.meta.url))

const convSource = () => stripComments(readFileSync(CONV, 'utf8'))
const roomSource = () => stripComments(readFileSync(ROOM, 'utf8'))
const briefSource = () => stripComments(readFileSync(BRIEF, 'utf8'))
const popupSource = () => stripComments(readFileSync(POPUP, 'utf8'))

// ---------------------------------------------------------------------------
// 1. detectTypeaheadTrigger — the scan
// ---------------------------------------------------------------------------
describe('detectTypeaheadTrigger', () => {
  const at = (text, caret) => detectTypeaheadTrigger(text, caret === undefined ? text.length : caret)

  it('fires on a bare trigger at index 0', () => {
    expect(at('/')).toEqual({ kind: '/', start: 0, end: 1, query: '' })
    expect(at('@')).toEqual({ kind: '@', start: 0, end: 1, query: '' })
  })

  it('fires mid-text after whitespace and after a newline', () => {
    expect(at('Hi /wee')).toEqual({ kind: '/', start: 3, end: 7, query: 'wee' })
    expect(at('Ask @sc')).toEqual({ kind: '@', start: 4, end: 7, query: 'sc' })
    expect(at('line1\n@bo')).toEqual({ kind: '@', start: 6, end: 9, query: 'bo' })
  })

  it('scans FORWARD to the token end, so a caret parked mid-token still spans it', () => {
    // Without the forward scan the splice ends at the caret and leaves a tail.
    expect(at('@bob', 3)).toEqual({ kind: '@', start: 0, end: 4, query: 'bo' })
    expect(at('Hello @bob there', 10)).toEqual({ kind: '@', start: 6, end: 10, query: 'bob' })
  })

  it('boundary rule is non-word, not whitespace — CJK, emoji and punctuation all trigger', () => {
    for (const text of ['你好@rec', '🎉@rec', '(@bob', '"@bob', '-@bob']) {
      expect(at(text)?.kind, text).toBe('@')
    }
  })

  it('finds the INNERMOST trigger', () => {
    expect(at('@@bo')).toEqual({ kind: '@', start: 1, end: 4, query: 'bo' })
    expect(at('/@rec')).toEqual({ kind: '@', start: 1, end: 5, query: 'rec' })
  })

  it('rejects a query carrying a second trigger char', () => {
    expect(at('see /etc/hosts')).toBeNull()
  })

  it('stops at a token boundary behind the caret', () => {
    expect(at('@a b')).toBeNull()
  })

  it('floors the backward scan at MAX_TYPEAHEAD_QUERY (O(1) per keystroke)', () => {
    expect(at('/' + 'a'.repeat(MAX_TYPEAHEAD_QUERY))?.query).toHaveLength(MAX_TYPEAHEAD_QUERY)
    expect(at('/' + 'a'.repeat(MAX_TYPEAHEAD_QUERY + 1))).toBeNull()
    // A 100 KB paste is not a 100 KB walk.
    expect(at('x'.repeat(100000))).toBeNull()
  })

  it('survives absent, out-of-range and non-numeric input', () => {
    expect(detectTypeaheadTrigger(null, 0)).toBeNull()
    expect(detectTypeaheadTrigger(undefined, 3)).toBeNull()
    expect(detectTypeaheadTrigger('', 0)).toBeNull()
    expect(detectTypeaheadTrigger('@bo', 999)).toEqual({ kind: '@', start: 0, end: 3, query: 'bo' })
    expect(detectTypeaheadTrigger('@bo', -5)).toBeNull()
    expect(detectTypeaheadTrigger('@bo', NaN)).toEqual({ kind: '@', start: 0, end: 3, query: 'bo' })
  })

  it('yields nothing while a selection is non-collapsed', () => {
    expect(detectTypeaheadTrigger('@bo', 3, 3)).not.toBeNull()
    expect(detectTypeaheadTrigger('@bo', 1, 3)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 2. AC#6 exhaustively — the popup never fights ordinary prose
// ---------------------------------------------------------------------------
describe('AC#6: literal / and @ stay literal', () => {
  it.each(['50/50', 'and/or', 'user@example.com', 'a@b.c'])(
    '%s yields no trigger at ANY caret position', (text) => {
      for (let caret = 0; caret <= text.length; caret++) {
        expect(detectTypeaheadTrigger(text, caret), `${text} @${caret}`).toBeNull()
      }
    })
})

// ---------------------------------------------------------------------------
// 3. isMentionable — the P-3 matrix
// ---------------------------------------------------------------------------
describe('isMentionable', () => {
  it.each([
    ['data.scout', false],      // sanitize_agent_name keeps '.', MENTION_RE does not
    ['v2.1-bot', false],
    ['a'.repeat(101), false],   // grammar caps at 100
    ['_lead', false],           // first char must be alphanumeric
    ['-lead', false],
    ['', false],
    ['a@b', false],             // a name carrying the trigger char itself
    ['a b', false],
    ['ok-agent', true],
    ['under_score', true],
    ['a', true],
    ['a'.repeat(100), true],
  ])('%s → %s', (name, expected) => {
    expect(isMentionable(name)).toBe(expected)
  })

  it('is derived from the parser, so it cannot drift from it', () => {
    // Every name it accepts round-trips; every name it rejects does not. The
    // second half is the one that matters: it is what keeps an un-mentionable
    // slug out of the list instead of manufacturing a silent failure.
    for (const n of ['ok-agent', 'a', 'under_score', 'data.scout', '_lead', 'a'.repeat(101)]) {
      const resolves = mentionedAgents(buildMentionToken(n), [{ name: n }]).length === 1
      expect(isMentionable(n)).toBe(resolves)
    }
  })

  it('rejects nullish input rather than throwing', () => {
    expect(isMentionable(null)).toBe(false)
    expect(isMentionable(undefined)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 4. AC#4 round-trip — asserted over the SPLICED VALUE
// ---------------------------------------------------------------------------
describe('AC#4: a selected mention always escalates', () => {
  const roster = [{ name: 'alice' }, { name: 'ok-agent' }, { name: 'a' }, { name: 'under_score' }]

  // content before / after / both / neither, and a next char that is a space, a
  // letter, a newline and end-of-string.
  const surroundings = [
    ['', ''], ['Hello ', ''], ['', ' there'], ['Hello ', ' there'],
    ['', 'x'], ['', '\nnext line'], ['multi\nline ', ''],
  ]

  it.each(roster.map((a) => a.name))('a spliced @%s resolves through mentionedAgents', (name) => {
    for (const [before, after] of surroundings) {
      const text = `${before}@bo${after}`
      const caret = before.length + 3
      const trigger = detectTypeaheadTrigger(text, caret)
      expect(trigger, `no trigger for ${JSON.stringify(text)}`).not.toBeNull()
      const { value } = applyTypeaheadInsert(text, trigger, buildMentionToken(name))
      expect(mentionedAgents(value, roster), value).toContain(name)
    }
  })

  it('the NEGATIVE direction: every name the list excludes is one the parser cannot resolve', () => {
    const unusable = ['data.scout', 'v2.1-bot', '_lead', 'a'.repeat(101)]
    const wide = unusable.map((name) => ({ name }))
    for (const name of unusable) {
      expect(isMentionable(name)).toBe(false)
      const { value } = applyTypeaheadInsert('@bo', detectTypeaheadTrigger('@bo', 3), buildMentionToken(name))
      expect(mentionedAgents(value, wide)).not.toContain(name)
    }
    // …and none of them is ever offered.
    expect(filterAgentCandidates(wide, '').items).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 5. applyTypeaheadInsert — the splice and its separator
// ---------------------------------------------------------------------------
describe('applyTypeaheadInsert', () => {
  const splice = (text, caret, insert) =>
    applyTypeaheadInsert(text, detectTypeaheadTrigger(text, caret), insert)

  it('replaces the WHOLE token, leaving no tail', () => {
    expect(splice('@bob', 3, buildMentionToken('alice')).value).toBe('@alice ')
  })

  // The insert goes through buildMentionToken, NOT a literal string: the
  // separator must be decided by the splice, and baking a trailing space into
  // the token is the regression this table exists to catch (it produced
  // "Hello @alice  there" and "@recon  x").
  it('carries no separator of its own', () => {
    expect(buildMentionToken('alice')).toBe('@alice')
    expect(buildMentionToken('alice')).not.toMatch(/\s/)
  })

  it.each([
    ['Hello @bob there', 10, 'alice', 'Hello @alice there'],  // next char is a space → no second one
    ['Hello @bob there', 9, 'alice', 'Hello @alice there'],   // caret mid-token, same result
    ['Ask @al', 7, 'alice', 'Ask @alice '],                   // end of string → trailing space
    ['@ x', 1, 'recon', '@recon x'],                          // never two spaces
    ['@bo\nnext', 3, 'alice', '@alice\nnext'],                // a newline is whitespace too
  ])('%s (caret %i) → %s', (text, caret, name, expected) => {
    expect(splice(text, caret, buildMentionToken(name)).value).toBe(expected)
  })

  it('returns a caret positioned after the insert', () => {
    const r = splice('Ask @al', 7, buildMentionToken('alice'))
    expect(r.caret).toBe(r.value.length)
    const mid = splice('Hello @bob there', 10, buildMentionToken('alice'))
    expect(mid.value.slice(0, mid.caret)).toBe('Hello @alice')
  })

  it('splices a multi-word starter prompt at index 0', () => {
    const r = splice('/we', 3, 'Give me the weekly summary')
    expect(r.value).toBe('Give me the weekly summary ')
    expect(r.caret).toBe(r.value.length)
  })

  it('is a no-op without a trigger, and never throws on junk', () => {
    expect(applyTypeaheadInsert('hi', null, 'x')).toEqual({ value: 'hi', caret: 2 })
    expect(applyTypeaheadInsert(null, null, null)).toEqual({ value: '', caret: 0 })
    expect(applyTypeaheadInsert('hi', { start: 99, end: 99 }, '!').value).toBe('hi! ')
  })
})

// ---------------------------------------------------------------------------
// 6. filterAgentCandidates — AC#3 + the #2128 capability gate
// ---------------------------------------------------------------------------
describe('filterAgentCandidates', () => {
  const roster = [
    { name: 'acme-scout', display_label: 'Data Scout' },
    { name: 'acme-scribe', display_label: 'Scribe' },
    { name: 'cornelius' },
    { name: 'data.scout' },              // structurally un-mentionable
    { name: 'self-agent' },
  ]
  const opts = { exclude: ['self-agent'] }

  it('matches on the SLUG', () => {
    expect(filterAgentCandidates(roster, 'scribe', opts).items.map((a) => a.name)).toEqual(['acme-scribe'])
  })

  it('matches on the DISPLAY LABEL, which is what the roster shows', () => {
    expect(filterAgentCandidates(roster, 'Data', opts).items.map((a) => a.name)).toEqual(['acme-scout'])
  })

  it('matches a shared deployment prefix by substring, not prefix only', () => {
    expect(filterAgentCandidates(roster, 'scout', opts).items.map((a) => a.name)).toEqual(['acme-scout'])
  })

  it('is case-insensitive', () => {
    expect(filterAgentCandidates(roster, 'CORNEL', opts).items.map((a) => a.name)).toEqual(['cornelius'])
  })

  it('excludes self', () => {
    expect(filterAgentCandidates(roster, '', opts).items.map((a) => a.name)).not.toContain('self-agent')
  })

  it('excludes un-mentionable slugs — the whole point of AC#4', () => {
    expect(filterAgentCandidates(roster, '', opts).items.map((a) => a.name)).not.toContain('data.scout')
    expect(filterAgentCandidates(roster, 'data', opts).items.map((a) => a.name)).toEqual(['acme-scout'])
  })

  it('reports peers and mentionable peers separately, so the empty state can be honest', () => {
    const r = filterAgentCandidates(roster, '', opts)
    expect(r.peerCount).toBe(4)
    expect(r.mentionableCount).toBe(3)
  })

  it('is INERT without the rooms capability (#2128) — tested, not grepped', () => {
    const r = filterAgentCandidates(roster, '', { ...opts, enabled: false })
    expect(r.items).toEqual([])
    expect(r.enabled).toBe(false)
  })

  it('survives a missing or malformed roster', () => {
    expect(filterAgentCandidates(undefined, 'x').items).toEqual([])
    expect(filterAgentCandidates([null, {}, { name: '' }], '').items).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 7. filterPlaybookCandidates — prose titles, not slugs
// ---------------------------------------------------------------------------
describe('filterPlaybookCandidates', () => {
  const playbooks = [
    { title: 'Weekly research digest', description: 'Roll up the week', starter_prompt: 'Give me the digest' },
    { title: 'Research a competitor' },
    { title: 'Draft a status update', description: null },
    { title: '   ' },                    // no usable title
  ]

  it('ranks a whole-title prefix above a word-start prefix, beating source order', () => {
    expect(filterPlaybookCandidates(playbooks, 'res').items.map((p) => p.title))
      .toEqual(['Research a competitor', 'Weekly research digest'])
  })

  it('matches a word-start prefix inside a prose title', () => {
    expect(filterPlaybookCandidates(playbooks, 'dig').items.map((p) => p.title)).toEqual(['Weekly research digest'])
    expect(filterPlaybookCandidates(playbooks, 'stat').items.map((p) => p.title)).toEqual(['Draft a status update'])
  })

  it('does NOT match an arbitrary substring — a one-char query must not match everything', () => {
    expect(filterPlaybookCandidates(playbooks, 'e').items).toEqual([])
  })

  it('drops entries with no usable title', () => {
    const r = filterPlaybookCandidates(playbooks, '')
    expect(r.sourceCount).toBe(3)
    expect(r.items.map((p) => p.title)).not.toContain('   ')
  })

  it('distinguishes source-empty from filter-empty', () => {
    expect(filterPlaybookCandidates([], '')).toEqual({ items: [], sourceCount: 0 })
    const filtered = filterPlaybookCandidates(playbooks, 'zzzz')
    expect(filtered.items).toEqual([])
    expect(filtered.sourceCount).toBe(3)
  })

  it('survives a null/non-array source (a stopped or slow agent yields no playbooks)', () => {
    expect(filterPlaybookCandidates(null, '').items).toEqual([])
    expect(filterPlaybookCandidates(undefined, 'x').items).toEqual([])
    expect(filterPlaybookCandidates('nope', '').items).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 8. typeaheadEmptyMessage — three different statements, never substituted
// ---------------------------------------------------------------------------
describe('typeaheadEmptyMessage', () => {
  it('says nothing about operator configuration for the `/` case', () => {
    expect(typeaheadEmptyMessage('/', { sourceCount: 0 })).toBe(EMPTY_REASON_NO_PLAYBOOKS)
    // The client cannot tell "none exposed" from "the agent was stopped when the
    // roster was built", so the copy claims neither.
    expect(EMPTY_REASON_NO_PLAYBOOKS).not.toMatch(/exposed|configured|none/i)
  })

  it('distinguishes "no peers" from "peers exist but none is mentionable"', () => {
    expect(typeaheadEmptyMessage('@', { enabled: true, peerCount: 0, mentionableCount: 0 }))
      .toBe(EMPTY_REASON_NO_PEERS)
    expect(typeaheadEmptyMessage('@', { enabled: true, peerCount: 3, mentionableCount: 0 }))
      .toBe(EMPTY_REASON_NO_MENTIONABLE_PEERS)
    expect(EMPTY_REASON_NO_PEERS).not.toBe(EMPTY_REASON_NO_MENTIONABLE_PEERS)
  })

  it('uses room wording in a room, where "shared with you" is the wrong statement', () => {
    expect(typeaheadEmptyMessage('@', { enabled: true, peerCount: 0, mentionableCount: 0 }, { scope: 'room' }))
      .toBe(EMPTY_REASON_NO_ROOM_PEERS)
  })

  it('says nothing at all when the capability is absent — no dead-end affordance', () => {
    expect(typeaheadEmptyMessage('@', { enabled: false, peerCount: 5, mentionableCount: 5 })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// 9. resolveComposerKey — the truth table
// ---------------------------------------------------------------------------
describe('resolveComposerKey', () => {
  const enter = (over = {}) => resolveComposerKey({ key: 'Enter', ...over })

  it('reproduces Vue `.exact` across all 16 modifier combinations', () => {
    for (const shiftKey of [false, true]) {
      for (const ctrlKey of [false, true]) {
        for (const metaKey of [false, true]) {
          for (const altKey of [false, true]) {
            const plain = !shiftKey && !ctrlKey && !metaKey && !altKey
            const mods = { shiftKey, ctrlKey, metaKey, altKey }
            expect(enter({ ...mods }), JSON.stringify(mods)).toBe(plain ? 'send' : 'pass')
            // Open with a selection: only the plain one accepts; the rest still
            // fall through unprevented and insert a newline, exactly as today.
            expect(enter({ ...mods, open: true, hasCandidates: true, hasActive: true }))
              .toBe(plain ? 'accept' : 'pass')
          }
        }
      }
    }
  })

  it('NEVER swallows Enter when nothing is explicitly selected (A-9)', () => {
    expect(enter({ open: true, hasCandidates: true, hasActive: false })).toBe('send')
    expect(enter({ open: true, hasCandidates: false, hasActive: false })).toBe('send')  // empty-state panel
    expect(enter({ open: false })).toBe('send')
  })

  it('is byte-identical to today when the popup is closed', () => {
    for (const key of ['Enter', 'a', 'Escape', 'Tab', 'ArrowDown', 'ArrowUp', 'Home']) {
      expect(resolveComposerKey({ key, open: false })).toBe(key === 'Enter' ? 'send' : 'pass')
    }
  })

  it('passes an IME composition through — the current binding sends mid-word', () => {
    expect(enter({ isComposing: true })).toBe('pass')
    expect(enter({ keyCode: 229 })).toBe('pass')
    expect(enter({ isComposing: true, open: true, hasCandidates: true, hasActive: true })).toBe('pass')
  })

  it('moves with the arrows only when there is something to move over', () => {
    const open = { open: true, hasCandidates: true }
    expect(resolveComposerKey({ key: 'ArrowDown', ...open })).toBe('move-down')
    expect(resolveComposerKey({ key: 'ArrowUp', ...open })).toBe('move-up')
    expect(resolveComposerKey({ key: 'ArrowDown', open: true, hasCandidates: false })).toBe('close')
  })

  it('accepts the top row on Tab, and lets Shift+Tab leave the composer', () => {
    expect(resolveComposerKey({ key: 'Tab', open: true, hasCandidates: true })).toBe('accept')
    expect(resolveComposerKey({ key: 'Tab', open: true, hasCandidates: true, shiftKey: true })).toBe('pass')
    expect(resolveComposerKey({ key: 'Tab', open: true, hasCandidates: false })).toBe('pass')
  })

  it('dismisses on Escape only while open (principle 23)', () => {
    expect(resolveComposerKey({ key: 'Escape', open: true })).toBe('dismiss')
    expect(resolveComposerKey({ key: 'Escape', open: false })).toBe('pass')
  })

  it('closes on a caret-moving key rather than accepting against stale bounds', () => {
    for (const key of ['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown']) {
      expect(resolveComposerKey({ key, open: true, hasCandidates: true })).toBe('close')
    }
  })

  it('survives being called with nothing', () => {
    expect(resolveComposerKey()).toBe('pass')
  })
})

// ---------------------------------------------------------------------------
// 10. Dismissal state
// ---------------------------------------------------------------------------
describe('Esc dismissal', () => {
  const trig = (text, caret) => detectTypeaheadTrigger(text, caret ?? text.length)

  it('stays closed while the same token keeps being typed', () => {
    const d = nextDismissState(trig('@b'))
    expect(isSuppressed(d, trig('@b'))).toBe(true)
    expect(isSuppressed(d, trig('@bo'))).toBe(true)
    expect(isSuppressed(d, trig('@bob'))).toBe(true)
  })

  it('re-arms when the token is retyped from scratch, or a different kind appears', () => {
    const d = nextDismissState(trig('@bo'))
    expect(isSuppressed(d, trig('@b'))).toBe(false)      // deleted back past the dismissal
    expect(isSuppressed(d, trig('@'))).toBe(false)
    expect(isSuppressed(d, trig('/bo'))).toBe(false)     // different kind at the same offset
    expect(isSuppressed(d, trig('Hi @bo'))).toBe(false)  // different start
  })

  it('a cleared sentinel re-opens everything — the session-long-dead-feature guard', () => {
    // send() clears input.value programmatically, so no input event fires and
    // nothing recomputes. Without resetTypeahead() a sentinel armed in message 1
    // kills the popup for every later message starting with the same token.
    expect(isSuppressed(null, trig('@bo'))).toBe(false)
    expect(isSuppressed(nextDismissState(null), trig('@bo'))).toBe(false)
  })

  it('never suppresses without a trigger to compare against', () => {
    expect(isSuppressed(nextDismissState(trig('@bo')), null)).toBe(false)
  })
})

describe('a pick settles the token it inserted', () => {
  // The splice only appends its separator when the next character is not
  // already whitespace, so a mid-sentence pick leaves the caret INSIDE the
  // freshly inserted token. That is not hypothetical: the accept moves the
  // caret with setSelectionRange(), which fires a `select` event, which is
  // bound to the same recompute as a click — so without a sentinel the popup
  // reopens on top of its own successful choice, listing what was just picked.
  const pick = (text, caret, name) => {
    const trigger = detectTypeaheadTrigger(text, caret)
    return applyTypeaheadInsert(text, trigger, buildMentionToken(name))
  }

  it.each([
    ['Hello @bo there', 9],
    ['@ x', 1],
  ])('mid-sentence (%s) leaves the caret inside the inserted token', (text, caret) => {
    const { value, caret: c } = pick(text, caret, 'alice')
    // The bug this guards: a recompute here finds a trigger again.
    const reopened = detectTypeaheadTrigger(value, c)
    expect(reopened, value).not.toBeNull()
    expect(reopened.query).toBe('alice')
    // …which the accept suppresses, so the popup stays shut.
    expect(isSuppressed(dismissAfterInsert(value, c), reopened)).toBe(true)
  })

  it('needs no sentinel when the splice appended a separator', () => {
    const { value, caret } = pick('Ask @al', 7, 'alice')
    expect(value).toBe('Ask @alice ')
    expect(detectTypeaheadTrigger(value, caret)).toBeNull()
    // Nothing to suppress — and returning null leaves any earlier sentinel alone.
    expect(dismissAfterInsert(value, caret)).toBeNull()
  })

  it('re-arms as soon as the settled token is edited back', () => {
    const { value, caret } = pick('Hello @bo there', 9, 'alice')
    const settled = dismissAfterInsert(value, caret)
    // Backspacing into the name is the user asking for the list again.
    expect(isSuppressed(settled, detectTypeaheadTrigger('Hello @alic there', 11))).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 11. Roving selection
// ---------------------------------------------------------------------------
describe('active index', () => {
  it('starts at nothing and wraps at both ends', () => {
    expect(nextActiveIndex(-1, +1, 3)).toBe(0)
    expect(nextActiveIndex(-1, -1, 3)).toBe(2)
    expect(nextActiveIndex(2, +1, 3)).toBe(0)
    expect(nextActiveIndex(0, -1, 3)).toBe(2)
  })

  it('is a no-op on an empty list', () => {
    expect(nextActiveIndex(-1, +1, 0)).toBe(-1)
    expect(nextActiveIndex(1, -1, 0)).toBe(-1)
  })

  it('DROPS a stale index rather than clamping it to a neighbour', () => {
    // After a roster refresh, "whatever is now at index 5" is not what the user
    // chose. -1 then makes Enter send instead of inserting the wrong agent.
    expect(clampActiveIndex(5, 2)).toBe(-1)
    expect(clampActiveIndex(1, 2)).toBe(1)
    expect(clampActiveIndex(-1, 2)).toBe(-1)
    expect(clampActiveIndex(0, 0)).toBe(-1)
  })
})

// ---------------------------------------------------------------------------
// 12. Bounds + shared helpers
// ---------------------------------------------------------------------------
describe('boundCandidates', () => {
  const list = (n) => Array.from({ length: n }, (_, i) => i)

  it.each([
    [TYPEAHEAD_LIMIT - 1, TYPEAHEAD_LIMIT - 1, 0],
    [TYPEAHEAD_LIMIT, TYPEAHEAD_LIMIT, 0],
    [TYPEAHEAD_LIMIT + 1, TYPEAHEAD_LIMIT, 1],
    [24, TYPEAHEAD_LIMIT, 24 - TYPEAHEAD_LIMIT],
  ])('%i items → %i visible, %i overflow', (n, visible, overflow) => {
    const r = boundCandidates(list(n))
    expect(r.visible).toHaveLength(visible)
    expect(r.overflow).toBe(overflow)
  })

  it('survives junk', () => {
    expect(boundCandidates(null)).toEqual({ visible: [], overflow: 0 })
    expect(boundCandidates(list(3), 0).visible).toHaveLength(3)
  })
})

describe('starterFor', () => {
  it('prefers the starter prompt, falls back to the title', () => {
    expect(starterFor({ starter_prompt: 'Run it', title: 'T' })).toBe('Run it')
    expect(starterFor({ starter_prompt: '   ', title: 'T' })).toBe('T')
    expect(starterFor({ title: 'T' })).toBe('T')
  })

  it('does not throw on a nullish argument', () => {
    expect(starterFor(null)).toBe('')
    expect(starterFor(undefined)).toBe('')
    expect(starterFor({})).toBe('')
  })
})

// ---------------------------------------------------------------------------
// 13. The room wake-set — established by observing the running server
// ---------------------------------------------------------------------------
describe('roomMentionSource', () => {
  // The rooms engine is a private submodule that is not checked out here, so the
  // evidence is what the server answered, not what its source says. Observed on
  // a live instance (room_b30d6afc16d94d07, participants acme-scout +
  // acme-scribe):
  //   POST @acme-scout   → {"mentions":["acme-scout"],"woke":["acme-scout"]}
  //   POST @cornelius    → {"mentions":[],"woke":[]}
  // A participant wakes; a non-participant wakes nobody on that turn. That is
  // what makes the participants the right candidate set — offering the roster
  // would list names with no evidence that picking one does anything.
  const roster = [
    { name: 'acme-scout', display_label: 'Data Scout' },
    { name: 'cornelius', display_label: 'Cornelius' },
  ]

  it('is the participant list, never the roster', () => {
    const src = roomMentionSource(['acme-scout'], roster)
    expect(src.map((a) => a.name)).toEqual(['acme-scout'])
    expect(src.map((a) => a.name)).not.toContain('cornelius')
  })

  it('joins the roster only to recover display labels', () => {
    expect(roomMentionSource(['acme-scout'], roster)[0].display_label).toBe('Data Scout')
    // A participant the caller cannot see in their roster is still in the room,
    // so it is still wakeable.
    expect(roomMentionSource(['ghost'], roster)).toEqual([{ name: 'ghost' }])
  })

  it('feeds the same tested filter, so the room reuses the 1:1 logic verbatim', () => {
    const src = roomMentionSource(['acme-scout', 'data.scout'], roster)
    expect(filterAgentCandidates(src, 'scout').items.map((a) => a.name)).toEqual(['acme-scout'])
  })

  it('survives junk', () => {
    expect(roomMentionSource(null, null)).toEqual([])
    expect(roomMentionSource([null, ''], [])).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// 14. Source-structure guards — the half no unit test can reach
// ---------------------------------------------------------------------------
describe('PortalConversation wiring', () => {
  it('no longer binds the raw .exact handler, and delegates to the TESTED keymap', () => {
    const src = convSource()
    expect(src).not.toContain('@keydown.enter.exact.prevent')
    expect(src).toContain('resolveComposerKey')
  })

  it('still reaches autoGrow from the input path', () => {
    // Forgetting this silently kills textarea growth for everyone, and nothing
    // else in this suite would catch it.
    expect(convSource()).toMatch(/@input="onComposerInput"/)
    expect(convSource()).toMatch(/function onComposerInput[\s\S]{0,200}autoGrow\(\)/)
  })

  it('clears the dismissal sentinel on every PROGRAMMATIC write to input.value', () => {
    // send(), the prefill watcher and both dictation handlers all write the
    // model directly, which fires no input event.
    const calls = convSource().replace('function resetTypeahead()', '')
    expect((calls.match(/resetTypeahead\(\)/g) || []).length).toBeGreaterThanOrEqual(4)
  })

  it('reads the event target, not the v-model ref, when detecting a trigger', () => {
    // Reading the ref makes correctness depend on Vue's internal listener
    // ordering — true today, an implementation detail.
    expect(convSource()).toMatch(/detectTypeaheadTrigger\(\s*el\.value/)
  })

  it('settles the token a pick inserted, so the popup cannot reopen over its own choice', () => {
    expect(convSource()).toMatch(/dismissAfterInsert\(value, caret\)[\s\S]{0,80}dismissed\.value/)
  })

  it('anchors the popup without collapsing the flex field', () => {
    const src = convSource()
    expect(src).toContain('relative flex-1 min-w-0')
    // Matched on the class LIST, not on `class="w-full` — #2259 prepends `block`
    // to the same attribute, and an anchored match would read that as the field
    // having lost its width.
    expect(src).toMatch(/<textarea[\s\S]{0,400}class="[^"]*\bw-full\b/)
  })

  it('advertises both triggers in the placeholder, and @ only with the capability', () => {
    const src = convSource()
    expect(src).toMatch(/for playbooks/)
    expect(src).toMatch(/multiAgentChatAvailable[\s\S]{0,120}@ to add an agent/)
  })
})

describe('PortalRoom wiring', () => {
  it('scopes its @ list to the room participants, not the roster', () => {
    const src = roomSource()
    expect(src).toContain('roomMentionSource(agentParticipants.value')
    expect(src).not.toMatch(/filterAgentCandidates\(\s*props\.roster/)
  })

  it('uses the same tested keymap and popup as the 1:1 composer', () => {
    const src = roomSource()
    expect(src).not.toContain('@keydown.enter.exact.prevent')
    expect(src).toContain('resolveComposerKey')
    expect(src).toContain('PortalTypeahead')
  })

  it('settles the token a pick inserted, exactly as the 1:1 composer does', () => {
    expect(roomSource()).toMatch(/dismissAfterInsert\(value, caret\)[\s\S]{0,80}dismissed\.value/)
  })

  it('does NOT offer a / typeahead — a room has no active-agent subject', () => {
    expect(roomSource()).not.toContain('filterPlaybookCandidates')
  })
})

describe('PortalBriefing de-duplication', () => {
  it('imports starterFor rather than defining a second copy', () => {
    const src = briefSource()
    expect(src).toMatch(/import \{[^}]*starterFor[^}]*\} from '\.\/portalUtils'/)
    expect(src).not.toMatch(/function starterFor/)
  })
})

describe('PortalTypeahead markup', () => {
  // Anchored on the stable role="listbox" element, never on a comment marker —
  // stripComments deletes those by design.
  const listblock = () => {
    const src = popupSource()
    const i = src.indexOf('role="listbox"')
    expect(i).toBeGreaterThan(-1)
    return src
  }

  it('opens upward, scrolls internally and is height-bounded (AC#8, principle 28)', () => {
    const src = listblock()
    expect(src).toContain('bottom-full')
    expect(src).toContain('overflow-y-auto')
    expect(src).toMatch(/max-h-\[min\(/)
  })

  it('accepts on mousedown, so the textarea never loses its caret to a blur', () => {
    expect(listblock()).toContain('mousedown')
    expect(listblock()).toContain('.prevent')
  })

  it('states the overflow rather than silently truncating', () => {
    expect(listblock()).toMatch(/more — keep typing/)
  })

  it('renders agent- and playbook-authored text through interpolation, never v-html', () => {
    expect(popupSource()).not.toContain('v-html')
  })

  it('carries listbox/option semantics but claims no combobox on the textarea', () => {
    expect(popupSource()).toContain('role="option"')
    expect(popupSource()).toContain('aria-selected')
    expect(popupSource()).toContain('aria-live="polite"')
    // aria-expanded/aria-activedescendant belong to role="combobox", which is
    // itself out of spec on a multiline control. Shipping a claim screen readers
    // ignore is worse than shipping less.
    expect(convSource()).not.toContain('aria-expanded')
    expect(convSource()).not.toContain('aria-activedescendant')
  })

  it('keeps the conversation file free of v-html sites', () => {
    // Was `toBe(1)`: the assistant body legitimately used one, through
    // renderMarkdown()/DOMPurify. #2515 moved that body into PortalMarkdown.vue,
    // so this file should now have NONE — and the mirror pin (exactly one
    // v-html there, fed by the markdown util) lives in portalAgentBubble.spec.js.
    expect((convSource().match(/v-html/g) || []).length).toBe(0)
  })
})
