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
 *   * an unsaved new chat is not a tab (ruling 2026-09-06), so the strip has
 *     no phantom entry and no active id while the first message is unsent;
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

  it('does not invent a tab for an unsaved new chat: the strip is the list, nothing more', () => {
    // The shell passes the same list the sidebar renders; a chat that has not
    // sent its first message has no row there and therefore no tab here.
    expect(agentChatTabs([], 'scribe')).toEqual([])
  })

  it('labels the overflow with a count', () => {
    expect(moreTabsLabel(3)).toBe('3 more')
    expect(moreTabsLabel(1)).toBe('1 more')
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
