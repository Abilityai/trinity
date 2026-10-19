// @vitest-environment jsdom
/**
 * ent#621 [C] — the `⌥/` key list, mounted.
 *
 * Two things can only be proven by mounting it. First, that every row comes
 * from the MAP: a hand-typed list passes any test written against the same
 * hand-typed strings, and the defect it hides is a help dialog that promises a
 * key the dispatcher no longer answers. Second, the keyboard contract — Esc
 * closes and focus goes back to where it came from — which is `BaseModal`'s
 * (#1923) and is only actually wired if this component is built ON it.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalKeyList from '@/components/portal/PortalKeyList.vue'
import { keyListRows, WORKSPACE_KEYMAP, chordLabel, findBinding, entryChords } from '@/components/portal/portalKeymap'

const MAC = 'MacIntel'
const WIN = 'Win32'

function open(props = {}) {
  return mount(PortalKeyList, {
    attachTo: document.body,
    props: {
      modelValue: true,
      platform: MAC,
      rows: keyListRows(WORKSPACE_KEYMAP, MAC),
      ...props,
    },
  })
}

const rowFor = (action) => document.querySelector(`[data-ws-key-row="${action}"]`)
const capOf = (action) => rowFor(action).querySelector('kbd').textContent.trim()

beforeEach(() => { document.body.innerHTML = '' })

describe('ent#621 — the key list is the map, rendered', () => {
  it('shows a row per non-reserved action, with the map\'s own label', async () => {
    const w = open()
    const actions = Array.from(document.querySelectorAll('[data-ws-key-row]'))
      .map((el) => el.getAttribute('data-ws-key-row'))
    expect([...actions].sort()).toEqual(keyListRows(WORKSPACE_KEYMAP, MAC).map((r) => r.action).sort())
    // ⌘K is declared and RESERVED: nothing binds it until ent#577, so a row
    // here would promise a key that does nothing.
    expect(rowFor('find-anything')).toBe(null)
    w.unmount()
  })

  it('prints the chord the matcher actually answers, per platform', async () => {
    const w = open()
    expect(capOf('agent-next')).toBe(chordLabel(entryChords(findBinding('agent-next'))[0], MAC))
    expect(capOf('agent-next')).toBe('⌥↓')
    expect(capOf('chat-prev')).toBe('⌥⇧↑')
    expect(capOf('rail-toggle')).toBe('⌘.')
    w.unmount()

    const win = open({ platform: WIN, rows: keyListRows(WORKSPACE_KEYMAP, WIN) })
    expect(capOf('agent-next')).toBe('Alt+↓')
    expect(capOf('rail-toggle')).toBe('Ctrl+.')
    win.unmount()
  })

  it('says a key is answered by one press, and where a rail is missing', async () => {
    const w = open()
    expect(rowFor('agent-next').textContent).toContain('One agent per press')
    expect(rowFor('rail-tab-next').textContent).not.toContain('no rail')
    w.unmount()

    // On a page with no rail (Projects, the Inbox with nothing selected) the
    // row STAYS and says so: a key
    // that vanishes from the list and does nothing on the page is the one a
    // person retries forever.
    const bare = open({ rows: keyListRows(WORKSPACE_KEYMAP, MAC, { railAvailable: false }) })
    expect(rowFor('rail-toggle').textContent).toContain('This page has no rail.')
    bare.unmount()
  })

  it('names the Windows input-language clash only on Windows', async () => {
    // Read the DOCUMENT, not the wrapper: `BaseModal` teleports its overlay to
    // `<body>`, so `wrapper.text()` is empty and `not.toContain` would pass on
    // a dialog that never rendered at all.
    const w = open()
    expect(document.body.textContent).toContain('non-US layouts')
    expect(document.body.textContent).not.toContain('language switch')
    w.unmount()
    const win = open({ platform: WIN, rows: keyListRows(WORKSPACE_KEYMAP, WIN) })
    expect(document.body.textContent).toContain('language switch')
    win.unmount()
  })
})

describe('ent#621 — the key list keeps BaseModal\'s keyboard contract', () => {
  it('is a real modal dialog, marked so ⌥/ can close its own list', async () => {
    const w = open()
    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(dialog.getAttribute('aria-label')).toBe('Keyboard shortcuts')
    // The marker must be on the SAME element as `aria-modal` — that is the
    // element the dispatcher's `:not([data-ws-key-list])` exclusion matches.
    expect(dialog.hasAttribute('data-ws-key-list')).toBe(true)
    w.unmount()
  })

  it('Esc closes it through the shared contract, not a bespoke handler', async () => {
    const w = open()
    const dialog = document.querySelector('[role="dialog"]')
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await w.vm.$nextTick()
    expect(w.emitted('update:modelValue').at(-1)).toEqual([false])
    w.unmount()
  })

  it('gives focus back to where it came from — the message field', async () => {
    // Focus is placed by hand FIRST: a test that asserts focus-return without
    // ever moving focus away passes on a component that never returns it
    // (learnings 2026-10-01).
    const field = document.createElement('textarea')
    document.body.appendChild(field)
    field.focus()
    expect(document.activeElement).toBe(field)

    const w = mount(PortalKeyList, {
      attachTo: document.body,
      props: { modelValue: false, platform: MAC, rows: keyListRows(WORKSPACE_KEYMAP, MAC) },
    })
    await w.setProps({ modelValue: true })
    await new Promise((r) => setTimeout(r, 0))
    expect(document.activeElement).not.toBe(field)

    await w.setProps({ modelValue: false })
    await new Promise((r) => setTimeout(r, 0))
    expect(document.activeElement).toBe(field)
    w.unmount()
  })

  it('the close button asks the shell to close, so ⌥/ and the X agree', async () => {
    const w = open()
    document.querySelector('[data-testid="ws-key-list-close"]').click()
    await w.vm.$nextTick()
    expect(w.emitted('update:modelValue').at(-1)).toEqual([false])
    w.unmount()
  })
})
