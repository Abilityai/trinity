/**
 * #2169 — the portal avatar's edge.
 *
 * A bare `rounded-full` span has no boundary, so an image avatar whose own edges
 * are light bleeds into the sidebar and chat surfaces it sits on. `PortalAvatar`
 * is the one shared component behind fourteen call sites, so the edge is one
 * line there — and until this file, `PortalAvatar` had zero test references of
 * any kind.
 *
 * What a source guard can and cannot do here is worth stating, because the
 * distinction is why the browser pass is blocking rather than optional. Node has
 * no layout engine and this project has no component-mount harness, so nothing
 * below observes geometry: not the outer footprint, not the 1px inset on the
 * <img>, not whether the edge is actually visible against a given ground. That
 * is the ent#245 class recorded in learnings — a wrapper that interposes a box
 * squashes percentage-sized children, silently, green in every unit test.
 *
 * What a source guard IS good for is the cross-component interaction, which is
 * the last assertion here: `PortalChatRow` passes its own `ring-2` separator
 * into this component, and "tidying up" that ring after the border landed would
 * silently collapse the stacked-avatar gap with nothing failing.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'

const AVATAR = fileURLToPath(new URL('../../src/components/portal/PortalAvatar.vue', import.meta.url))
const CHAT_ROW = fileURLToPath(new URL('../../src/components/portal/PortalChatRow.vue', import.meta.url))

const avatarSource = () => stripComments(readFileSync(AVATAR, 'utf8'))
const chatRowSource = () => stripComments(readFileSync(CHAT_ROW, 'utf8'))

// The root <span> carries the size style, so it is the element the edge belongs
// on — an edge on the <img> would be absent for an initials avatar.
const rootSpanClasses = (src) => {
  const at = src.indexOf('<span')
  expect(at).toBeGreaterThan(-1)
  const attrs = src.slice(at, src.indexOf('>', at))
  const m = attrs.match(/\sclass="([^"]*)"/)
  expect(m).not.toBeNull()
  return m[1]
}

describe('PortalAvatar edge', () => {
  it('draws a border on the root span, in both themes', () => {
    // A half-themed edge is the failure mode no other check sees: it looks
    // right in whichever theme the author had open and vanishes in the other.
    const classes = rootSpanClasses(avatarSource())
    expect(classes).toMatch(/\bborder\b/)
    expect(classes).toMatch(/\bborder-gray-\d+\b/)
    expect(classes).toMatch(/\bdark:border-gray-\d+\b/)
  })

  it('uses gray, the contract shade for a neutral edge', () => {
    // There is no semantic token for decoration that separates; `status-*` /
    // `action-*` would claim this reports a result or affords a click. Gray is
    // the sanctioned answer, and a raw non-gray palette class is not.
    const classes = rootSpanClasses(avatarSource())
    expect(classes).not.toMatch(/\b(dark:)?border-(red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d+/)
    expect(classes).not.toMatch(/border-\[#/)
  })

  it('is still a circle that clips its image', () => {
    // A border on a square is a box, and dropping `overflow-hidden` lets a
    // non-square image escape the radius the edge is drawn on.
    const classes = rootSpanClasses(avatarSource())
    expect(classes).toMatch(/\brounded-full\b/)
    expect(classes).toMatch(/\boverflow-hidden\b/)
  })

  it('is not a ring, which would not survive the composition', () => {
    // An inset ring paints below child content, and the <img> is exactly the
    // padding box — so it is invisible on precisely the image avatars this
    // fixes. An outer ring shares `--tw-ring-*` and one box-shadow with the
    // separator ring PortalChatRow passes in, so the two collide.
    expect(avatarSource()).not.toMatch(/\bring-(inset|\d)/)
  })

  it('leaves the stacked-row separator ring intact', () => {
    // Two components, one element: the consumer paints a ring in the sidebar
    // ground colour to punch a gap between overlapping avatars. Border and ring
    // are independent properties and compose; removing the ring as "redundant"
    // after adding the border would merge the stack back into a blob.
    //
    // Scoped to the <PortalAvatar> tag itself. A file-wide match for the ring
    // string passes with the ring deleted from the avatar, because the +N chip
    // beside it carries the identical classes — verified by planting exactly
    // that violation and watching this guard stay green (learnings L2).
    const src = chatRowSource()
    const at = src.indexOf('<PortalAvatar')
    expect(at).toBeGreaterThan(-1)
    const tag = src.slice(at, src.indexOf('/>', at))
    expect(tag).toMatch(/ring-2 ring-gray-50 dark:ring-gray-950/)
  })

  it('gives the +N overflow chip the same edge', () => {
    // AC #6 says every instance, and the chip is hand-rolled rather than a
    // PortalAvatar, so it inherits nothing. Without this a four-agent row draws
    // three hairlined circles and one bare blob.
    const src = chatRowSource()
    const chip = src.slice(src.indexOf('avatars.overflow'))
    expect(chip).toMatch(/\bborder border-gray-\d+ dark:border-gray-\d+\b/)
  })
})
