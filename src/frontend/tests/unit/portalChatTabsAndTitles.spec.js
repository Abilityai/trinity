/**
 * ent#451 (the remaining slice) + ent#473 — the agent's chats as tabs, the
 * New chat hotkey, and renaming a chat or room.
 *
 * Pure functions, tested without mounting (vitest runs `environment: 'node'`
 * with no component-mount harness). The rules worth pinning are the ones an
 * obvious implementation gets wrong:
 *
 *   * a room is not an agent's tab and another agent's thread is not this
 *     agent's — the strip is a SLICE of the sidebar list, not the list;
 *   * an unsaved new chat IS a tab, provisionally (#2579) — a deliberate
 *     reversal of the 2026-09-06 ruling for the STRIP only (the thread is
 *     still created lazily), keyed off the caller's explicit `draft` intent so
 *     an unlisted deep link never wears the "New chat" label;
 *   * the client-side validator mirrors the server's leaf exactly — trims and
 *     collapses, refuses an inner line break rather than joining it, and
 *     leaves a person's punctuation alone;
 *   * a named 400 is rendered VERBATIM, a 404 says the chat is no longer
 *     theirs, and anything else still names the next action;
 *   * ⌘J and Ctrl+J, plain modifier only — Shift/Alt variants and both
 *     modifiers at once are someone else's shortcut.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import {
  CHAT_TITLE_MAX_CHARS, normalizeChatTitle, renameFailureMessage,
  agentChatTabs, moreTabsLabel,
  isNewChatHotkey, newChatHotkeyLabel, isMacLike,
  titleGenerationNotice,
  NEW_CHAT_TAB_ID, NEW_CHAT_TAB_LABEL, MAIN_TAB_LABEL,
  agentHasMain, titleSettling, shouldFetchTitleHealth, TITLE_SETTLE_DELAYS_MS,
} from '../../src/components/portal/portalUtils'

const src = (rel) => readFileSync(resolve(__dirname, '../../src', rel), 'utf8')

describe('normalizeChatTitle (mirror of services/chat_title.py)', () => {
  it('trims and collapses, keeps punctuation, accepts exactly the cap', () => {
    expect(normalizeChatTitle('  Q3   invoices  ')).toEqual({ ok: true, title: 'Q3 invoices' })
    expect(normalizeChatTitle('tab\tseparated')).toEqual({ ok: true, title: 'tab separated' })
    expect(normalizeChatTitle('Q3 invoices?')).toEqual({ ok: true, title: 'Q3 invoices?' })
    expect(normalizeChatTitle('trailing newline\n')).toEqual({ ok: true, title: 'trailing newline' })
    expect(normalizeChatTitle('x'.repeat(CHAT_TITLE_MAX_CHARS)).ok).toBe(true)
  })

  it('refuses with a reason and a sentence carrying the rule and an example', () => {
    for (const raw of ['', '   ', null, undefined, 42]) {
      const r = normalizeChatTitle(raw)
      expect(r.ok).toBe(false)
      expect(r.reason).toBe('empty')
      expect(r.message).toMatch(/Example:/)
    }
    expect(normalizeChatTitle('two\nlines')).toMatchObject({ ok: false, reason: 'multiline' })
    expect(normalizeChatTitle('two\r\nlines')).toMatchObject({ ok: false, reason: 'multiline' })
    const long = normalizeChatTitle('y'.repeat(130))
    expect(long).toMatchObject({ ok: false, reason: 'too_long' })
    expect(long.message).toContain('130')
    expect(long.message).toContain(String(CHAT_TITLE_MAX_CHARS))
  })
})

describe('renameFailureMessage', () => {
  it('renders a named 400 verbatim', () => {
    const err = { response: { status: 400, data: { detail: { code: 'invalid_title', reason: 'too_long', message: 'Keep it short. Example: X' } } } }
    expect(renameFailureMessage(err)).toBe('Keep it short. Example: X')
  })
  it('says a 404 means the chat is no longer theirs, and names the next action otherwise', () => {
    expect(renameFailureMessage({ response: { status: 404, data: { detail: 'Conversation not found' } } })).toMatch(/reload/i)
    expect(renameFailureMessage(new Error('Network Error'))).toMatch(/try again/i)
    expect(renameFailureMessage(undefined)).toMatch(/try again/i)
  })
})

describe('agentChatTabs', () => {
  const threads = [
    { id: 'old', agent_name: 'scribe', title: 'Older', last_message_at: '2026-09-01T10:00:00Z' },
    { id: 'room-1', is_room: true, agent_names: ['scribe', 'atlas'], title: 'A room', last_message_at: '2026-09-06T10:00:00Z' },
    { id: 'new', agent_name: 'scribe', title: '', last_message_at: '2026-09-06T09:00:00Z' },
    { id: 'theirs', agent_name: 'atlas', title: 'Not this agent', last_message_at: '2026-09-06T11:00:00Z' },
    { id: 'unsent', agent_name: 'scribe', title: null, created_at: '2026-09-06T12:00:00Z' },
  ]

  it('is this agent\'s threads only, most recent first, with the fallback label', () => {
    const tabs = agentChatTabs(threads, 'scribe')
    expect(tabs.map((t) => t.id)).toEqual(['unsent', 'new', 'old'])
    expect(tabs.map((t) => t.label)).toEqual(['New chat', 'New chat', 'Older'])
    expect(tabs.every((t) => t.thread && !t.thread.is_room)).toBe(true)
  })

  it('orders by last message, falling back to created_at, and is empty with no agent', () => {
    expect(agentChatTabs(threads, '')).toEqual([])
    expect(agentChatTabs(undefined, 'scribe')).toEqual([])
    expect(agentChatTabs(threads, 'atlas').map((t) => t.id)).toEqual(['theirs'])
  })

  it('labels the overflow with a count', () => {
    expect(moreTabsLabel(3)).toBe('3 more')
    expect(moreTabsLabel(1)).toBe('1 more')
  })
})

describe('a draft is a tab (#2579)', () => {
  // The reversal, and the exact shape of it. Pressing New chat and seeing
  // nothing change was the reported defect; the fix is a tab with no thread
  // behind it, inserted where the real row will land.
  const main = { id: 'main', agent_name: 'scribe', is_main: true, title: 'ignored', created_at: '2026-09-01T00:00:00Z' }
  const other = { id: 'o1', agent_name: 'scribe', title: 'Q3 invoices', last_message_at: '2026-09-06T10:00:00Z' }

  it('inserts one provisional tab directly after Main — the slot the real row takes', () => {
    const tabs = agentChatTabs([main, other], 'scribe', { draft: true })
    expect(tabs.map((t) => t.id)).toEqual(['main', NEW_CHAT_TAB_ID, 'o1'])
    expect(tabs[0].label).toBe(MAIN_TAB_LABEL)
    expect(tabs[1]).toMatchObject({ label: NEW_CHAT_TAB_LABEL, provisional: true, pinned: false, thread: null })
  })

  it('is the whole strip when the agent has no chats at all', () => {
    const tabs = agentChatTabs([], 'scribe', { draft: true })
    expect(tabs.map((t) => [t.id, t.label])).toEqual([[NEW_CHAT_TAB_ID, NEW_CHAT_TAB_LABEL]])
    expect(tabs[0].thread).toBeNull()
  })

  it('takes the adopted id across the gap before the list catches up', () => {
    // The adoption seam: the thread exists, the batch has not listed it yet.
    // Keying the tab to the real id is what stops it jumping on arrival.
    const tabs = agentChatTabs([main], 'scribe', { draft: true, activeId: 'ps_new' })
    expect(tabs.map((t) => t.id)).toEqual(['main', 'ps_new'])
    expect(tabs[1]).toMatchObject({ label: NEW_CHAT_TAB_LABEL, provisional: true, thread: null })
  })

  it('inserts nothing once a real row carries the active id', () => {
    const tabs = agentChatTabs([main, other], 'scribe', { draft: true, activeId: 'o1' })
    expect(tabs.map((t) => t.id)).toEqual(['main', 'o1'])
    expect(tabs.every((t) => !t.provisional)).toBe(true)
  })

  it('invents NOTHING for an unknown active id without a draft — the named regression', () => {
    // A cold deep link to a thread the cross-agent batch has not listed yet.
    // Keying off "the active id is not in the list" instead of the explicit
    // intent would label a real conversation "New chat".
    const tabs = agentChatTabs([main, other], 'scribe', { activeId: 'ps_deep_link' })
    expect(tabs.map((t) => t.id)).toEqual(['main', 'o1'])
    expect(agentChatTabs([], 'scribe', { activeId: 'ps_deep_link' })).toEqual([])
  })

  it('leaves the two-argument callers exactly as they were', () => {
    expect(agentChatTabs([], 'scribe')).toEqual([])
    expect(agentChatTabs([main, other], 'scribe').map((t) => t.id)).toEqual(['main', 'o1'])
  })
})

describe('the shell rules behind the strip (#2579)', () => {
  it('agentHasMain is scoped to non-room threads of THIS agent', () => {
    const rows = [
      { id: 'm', agent_name: 'scribe', is_main: true },
      { id: 'r', is_room: true, is_main: true },
    ]
    expect(agentHasMain(rows, 'scribe')).toBe(true)
    expect(agentHasMain(rows, 'atlas')).toBe(false)
    expect(agentHasMain([{ id: 'x', agent_name: 'scribe' }], 'scribe')).toBe(false)
    expect(agentHasMain(rows, '')).toBe(false)
    expect(agentHasMain(undefined, 'scribe')).toBe(false)
  })

  it('titleSettling is the two-attempt window, floor included, Main included', () => {
    // 0 is the voice path's `createSession` firing `sessions-changed` on a
    // zero-message thread — nothing is generating, so nothing may be awaited.
    expect(titleSettling({ message_count: 0 })).toBe(false)
    expect(titleSettling({ message_count: 1 })).toBe(false)
    expect(titleSettling({ message_count: 2 })).toBe(true)
    expect(titleSettling({ message_count: 4 })).toBe(true)
    expect(titleSettling({ message_count: 5 })).toBe(false)
    expect(titleSettling({ message_count: 6 })).toBe(false)
    // Main's TAB is labelled by role, but its sidebar row renders threadTitle
    // and post-ent#523 it is the default landing thread — excluding it left the
    // commonest conversation showing its first message as its name.
    expect(titleSettling({ message_count: 2, is_main: true })).toBe(true)
    expect(titleSettling(undefined)).toBe(false)
  })

  it('the settle schedule is bounded and best-effort, not a mirror of the server budget', () => {
    expect(TITLE_SETTLE_DELAYS_MS.length).toBeGreaterThanOrEqual(2)
    expect(TITLE_SETTLE_DELAYS_MS.every((ms) => ms > 0 && ms < 20000)).toBe(true)
    const sorted = [...TITLE_SETTLE_DELAYS_MS].sort((a, b) => a - b)
    expect(TITLE_SETTLE_DELAYS_MS).toEqual(sorted)
  })

  it('only a platform admin asks for title health', () => {
    expect(shouldFetchTitleHealth(true, 'admin')).toBe(true)
    expect(shouldFetchTitleHealth(true, 'user')).toBe(false)
    expect(shouldFetchTitleHealth(true, 'creator')).toBe(false)
    // A portal client. #2128: the endpoint is dead for this audience, so the
    // notice must be too — and it must not even be requested.
    expect(shouldFetchTitleHealth(false, 'admin')).toBe(false)
    expect(shouldFetchTitleHealth(undefined, undefined)).toBe(false)
  })
})

describe('the New chat hotkey', () => {
  const ev = (o) => ({ key: 'j', metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...o })
  it('is ⌘J or Ctrl+J with a plain modifier', () => {
    expect(isNewChatHotkey(ev({ metaKey: true }))).toBe(true)
    expect(isNewChatHotkey(ev({ ctrlKey: true }))).toBe(true)
    expect(isNewChatHotkey(ev({ ctrlKey: true, key: 'J' }))).toBe(true)
  })
  it('leaves every other chord alone', () => {
    expect(isNewChatHotkey(ev({}))).toBe(false)
    expect(isNewChatHotkey(ev({ metaKey: true, shiftKey: true }))).toBe(false)
    expect(isNewChatHotkey(ev({ ctrlKey: true, altKey: true }))).toBe(false)
    expect(isNewChatHotkey(ev({ metaKey: true, ctrlKey: true }))).toBe(false)
    expect(isNewChatHotkey(ev({ metaKey: true, key: 'k' }))).toBe(false)
    expect(isNewChatHotkey(null)).toBe(false)
    expect(isNewChatHotkey({ metaKey: true })).toBe(false)
  })
  it('labels itself for the platform', () => {
    expect(isMacLike('MacIntel')).toBe(true)
    expect(isMacLike('iPhone')).toBe(true)
    expect(isMacLike('Win32')).toBe(false)
    expect(newChatHotkeyLabel('MacIntel')).toBe('⌘J')
    expect(newChatHotkeyLabel('Linux x86_64')).toBe('Ctrl+J')
    expect(newChatHotkeyLabel(undefined)).toBe('Ctrl+J')
  })
})

describe('titleGenerationNotice', () => {
  it('is silent while the generator works or has not run', () => {
    expect(titleGenerationNotice({ state: 'ok' })).toBeNull()
    expect(titleGenerationNotice({ state: 'unknown' })).toBeNull()
    expect(titleGenerationNotice(null)).toBeNull()
    expect(titleGenerationNotice(undefined)).toBeNull()
  })
  it('names the missing credential and the next action', () => {
    const n = titleGenerationNotice({ state: 'no_credential', last_failure_at: '2026-09-06T10:00:00Z' })
    expect(n.level).toBe('warning')
    expect(n.title).toMatch(/aren't being generated/)
    expect(n.body).toMatch(/API key/)
    expect(n.body).toContain('2026-09-06T10:00:00Z')
  })
  it('counts the failing episode and quotes the bounded reason', () => {
    const n = titleGenerationNotice({ state: 'failing', consecutive_failures: 3, last_failure: 'HTTP 529' })
    expect(n.title).toMatch(/failing/)
    expect(n.body).toContain('3 attempts')
    expect(n.body).toContain('HTTP 529')
    expect(titleGenerationNotice({ state: 'failing', consecutive_failures: 1 }).body).toContain('1 attempt in a row')
  })
})

describe('the strip is the primitive, and the editor has one home', () => {
  it('PortalChatTabs renders OverflowTabs (ruling: never a hand-rolled strip) with the counted label', () => {
    const s = src('components/portal/PortalChatTabs.vue')
    expect(s).toMatch(/import OverflowTabs from '@\/components\/OverflowTabs\.vue'/)
    expect(s).toMatch(/:more-label="moreTabsLabel"/)
    expect(s).toMatch(/\bdense\b/)
  })
  it('OverflowTabs keeps "More" for every existing strip', () => {
    const s = src('components/OverflowTabs.vue')
    expect(s).toMatch(/moreLabel: \{ type: Function, default: \(\) => 'More' \}/)
  })

  // #2579 — fixed width. These are SOURCE pins, not behaviour: vitest runs
  // `environment: 'node'` with no mount harness, so nothing here can prove a
  // tab is 160px. That is `e2e/workspace-chat-tabs.spec.js`'s job.
  //
  // The mirror-slice idiom (precedent: portalRail.spec.js) rather than a count:
  // "the class appears twice" is satisfied by putting it on the MIRROR's More
  // button instead of its tab, which measures the wrong thing and is exactly
  // the parity bug this pin exists to prevent.
  describe('fixed-width tabs (#2579)', () => {
    const s = () => src('components/OverflowTabs.vue')
    const halves = () => {
      const code = s()
      const cut = code.indexOf('ref="measureNav"')
      expect(cut).toBeGreaterThan(-1)
      return { visible: code.slice(0, cut), mirror: code.slice(cut) }
    }

    it('declares the prop, default off, with one named width constant', () => {
      expect(s()).toMatch(/fixedWidth: \{ type: Boolean, default: false \}/)
      expect(s()).toMatch(/export const FIXED_TAB_WIDTH = 'w-40'/)
    })

    it('puts the width on the tab button in BOTH rows — a mirror that measures narrower overflows too late', () => {
      const { visible, mirror } = halves()
      expect(visible).toContain('FIXED_TAB_WIDTH')
      expect(mirror).toContain('FIXED_TAB_WIDTH')
    })

    it('the clamp machinery is on the VISIBLE row only', () => {
      // `shrink-0` + the nav's `overflow-hidden` fix the first-paint squeeze
      // (inlineCount starts at +Infinity, so every tab is inline before the
      // first measure). The mirror is `width: max-content` and never shrinks,
      // and getBoundingClientRect returns the border box, so it needs neither
      // — nor the label span.
      const { visible, mirror } = halves()
      expect(visible).toMatch(/\$\{FIXED_TAB_WIDTH\} shrink-0/)
      expect(visible).toMatch(/fixedWidth \? 'overflow-hidden' : ''/)
      expect(visible).toMatch(/<span class="min-w-0 truncate">\{\{ tab\.label \}\}<\/span>/)
      // The mirror takes the width and nothing else. (`shrink-0` on its own
      // appears there on the pinned glyph, which both rows have always drawn —
      // what must NOT appear is the width/shrink pairing on the tab button.)
      expect(mirror).toMatch(/fixedWidth \? FIXED_TAB_WIDTH : ''/)
      expect(mirror).not.toMatch(/\$\{FIXED_TAB_WIDTH\} shrink-0/)
      expect(mirror).not.toContain('min-w-0 truncate')
    })

    it('the native tooltip and the menu clamp are GATED, so the other strips grow neither', () => {
      // Unconditional, AgentDetail / Library / PortalRail sprout tooltips on
      // "Overview" / "Tasks" / "Files" for nothing.
      const titles = s().match(/:title="fixedWidth \? tab\.label : undefined"/g) || []
      expect(titles).toHaveLength(2)   // the visible tab button and the menu row
      expect(s()).toMatch(/:class="fixedWidth \? 'max-w-\[20rem\] truncate' : ''"/)
    })

    it('only the chat strip opts in — the other three consumers are untouched by construction', () => {
      expect(src('components/portal/PortalChatTabs.vue')).toMatch(/\bfixed-width\b/)
      for (const rel of ['views/AgentDetail.vue', 'views/Library.vue', 'components/portal/PortalRail.vue']) {
        expect(src(rel), rel).not.toMatch(/\bfixed-width\b/)
      }
    })

    it('the strip refuses to emit a tab with no thread behind it', () => {
      // The provisional tab's "select" would hand the shell a null, and
      // openThread reads `is_room` off it.
      const s2 = src('components/portal/PortalChatTabs.vue')
      expect(s2).toMatch(/if \(!tab\?\.thread\) return/)
      // The draft intent is a prop, threaded into the pure rule — never
      // inferred from "the active id is not in the list".
      expect(s2).toMatch(/draft: \{ type: Boolean, default: false \}/)
      expect(s2).toMatch(/activeId: props\.activeId, draft: props\.draft/)
      expect(src('components/portal/PortalConversation.vue')).toMatch(/:draft="newChat \|\| bornHere"/)
    })
  })
  it('the three rename homes all mount PortalEditableTitle', () => {
    for (const rel of ['components/portal/PortalChatRow.vue', 'components/portal/PortalConversation.vue', 'components/portal/PortalRoom.vue']) {
      expect(src(rel)).toMatch(/<PortalEditableTitle/)
    }
  })
  it('the conversation header carries New chat with its hotkey, and the shell arms the hotkey at mount', () => {
    expect(src('components/portal/PortalConversation.vue')).toMatch(/data-testid="new-chat-header"/)
    const shell = src('views/Portal.vue')
    expect(shell).toMatch(/window\.addEventListener\('keydown', onGlobalKeydown\)\n  if \(store\.isClientSignedIn\) await bootstrap\(\)/)
    expect(shell).toMatch(/window\.removeEventListener\('keydown', onGlobalKeydown\)/)
  })
})
