// @vitest-environment jsdom
/**
 * ent#621 — `hasModalOpen`, against a real DOM.
 *
 * The probe is the whole modal-suppression mechanism, so it is tested on
 * nodes rather than on a mocked `querySelector`: a selector that is subtly
 * wrong (`[aria-modal]` instead of `[aria-modal="true"]`, or a `:not()` on the
 * wrong element) passes a mock and fails in a browser.
 *
 * Two properties matter and pull in opposite directions. It must see every
 * overlay that says it is modal — including ones that do not exist yet, which
 * is why this is a probe and not a registry — and it must NOT see the one the
 * pressed key belongs to: the rail's own sheet for the rail keys, the key list
 * itself for ⌥/. That exemption is per action, which is what keeps `⌥↓` from
 * switching agent behind an open sheet.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { hasModalOpen } from '../../src/components/portal/portalKeymap'

const add = (html) => {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

afterEach(() => { document.body.innerHTML = '' })

describe('hasModalOpen', () => {
  it('is false on a page with nothing over it', () => {
    add('<div role="dialog">a non-modal popover (the theme switch)</div>')
    expect(hasModalOpen(document)).toBe(false)
  })

  it('sees any overlay that declares itself modal', () => {
    add('<div role="dialog" aria-modal="true" aria-label="Confirm">…</div>')
    expect(hasModalOpen(document)).toBe(true)
  })

  it('reads the attribute\'s value, not just its presence', () => {
    // A dialog that is mounted-but-closed writes `aria-modal="false"`.
    add('<div role="dialog" aria-modal="false">closed</div>')
    expect(hasModalOpen(document)).toBe(false)
  })

  it('ignores the overlay the pressed key belongs to — per action', () => {
    add('<aside aria-modal="true" data-ws-rail-sheet aria-label="Rail">…</aside>')
    // The rail keys: the sheet is not "something over the rail".
    expect(hasModalOpen(document, { ignore: '[data-ws-rail-sheet]' })).toBe(false)
    // Every other key: it is.
    expect(hasModalOpen(document)).toBe(true)
  })

  it('still sees a second overlay above the exempted one', () => {
    add('<aside aria-modal="true" data-ws-rail-sheet></aside>'
      + '<div aria-modal="true" data-ws-key-list></div>')
    expect(hasModalOpen(document, { ignore: '[data-ws-rail-sheet]' })).toBe(true)
    expect(hasModalOpen(document, { ignore: ['[data-ws-rail-sheet]', '[data-ws-key-list]'] })).toBe(false)
  })

  it('lets ⌥/ close its own list while every other key stays suppressed', () => {
    add('<div role="dialog" aria-modal="true" data-ws-key-list></div>')
    expect(hasModalOpen(document, { ignore: '[data-ws-key-list]' })).toBe(false)
    expect(hasModalOpen(document)).toBe(true)
  })

  it('answers false for a root it cannot query', () => {
    expect(hasModalOpen(null)).toBe(false)
    expect(hasModalOpen(undefined)).toBe(false)
    expect(hasModalOpen({})).toBe(false)
  })

  it('scopes to the root it is given', () => {
    const host = add('<div aria-modal="true"></div>')
    expect(hasModalOpen(host)).toBe(true)
    expect(hasModalOpen(document.createElement('section'))).toBe(false)
  })
})
