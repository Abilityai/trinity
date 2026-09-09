/**
 * #2620 — the room composer's `v-else` must stay chained to the attachments
 * block.
 *
 * `PortalRoom.vue` renders the attachment chips OR the composer:
 *
 *     <div v-if="attachments.length"> …chips… </div>
 *     <form v-else> …composer… </form>
 *
 * `v-else` binds to the **immediately preceding element**, so anything
 * conditional inserted between them silently steals the chain and the composer
 * then renders on the new condition instead. During this issue a banner was
 * added exactly there, and the effect was that the composer DISAPPEARED at the
 * moment the banner fired — the warning removed the ability to act on it.
 *
 * Nothing else catches this: the SFC compiles (a `v-else` after any `v-if` is
 * valid) and the suite has no mount harness, so nothing renders the template.
 *
 * This walks the parsed template AST rather than matching text. A first
 * attempt did match text — "is the gap after the last `</div>` empty" — and
 * was VACUOUS, because the intruder's own closing tag becomes the last one and
 * the gap reads clean. A structural question needs the structure.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import path from 'path'
import { parse } from '@vue/compiler-sfc'
import { baseParse } from '@vue/compiler-core'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const FILE = path.resolve(HERE, '../../src/components/portal/PortalRoom.vue')
const SRC = readFileSync(FILE, 'utf8')

const ELEMENT = 1

function templateAst() {
  const { descriptor } = parse(SRC, { filename: 'PortalRoom.vue' })
  return baseParse(descriptor.template.content)
}

/** Depth-first: every element node in document order. */
function elements(node, out = []) {
  for (const child of node.children || []) {
    if (child.type === ELEMENT) {
      out.push(child)
      elements(child, out)
    }
  }
  return out
}

function directive(node, name) {
  return (node.props || []).find((p) => p.type === 7 && p.name === name)
}

/** The element children of a node, in order — what `v-else` actually binds across. */
function elementChildren(node) {
  return (node.children || []).filter((c) => c.type === ELEMENT)
}

describe('#2620 room composer v-if/v-else chain', () => {
  const ast = templateAst()
  const all = elements(ast)

  it('the composer form carries v-else', () => {
    const form = all.find((n) => n.tag === 'form' && directive(n, 'else'))
    expect(form, 'no <form v-else> found — did the composer change shape?').toBeTruthy()
  })

  it('its preceding element sibling is the attachments v-if block', () => {
    // THE property. `v-else` binds to the previous element sibling, so this is
    // the one relationship that decides whether the composer renders.
    const form = all.find((n) => n.tag === 'form' && directive(n, 'else'))
    const parent = all.find((n) => elementChildren(n).includes(form))
    expect(parent, 'could not locate the form’s parent').toBeTruthy()

    const siblings = elementChildren(parent)
    const prev = siblings[siblings.indexOf(form) - 1]
    expect(prev, 'the form has no preceding element — its v-else binds to nothing').toBeTruthy()

    const vIf = directive(prev, 'if')
    expect(
      vIf && vIf.exp && vIf.exp.content,
      `the element before the composer is <${prev.tag}> with no v-if — ` +
      'something was inserted between the attachments block and the composer, ' +
      'and it has taken over the v-else',
    ).toContain('attachments.length')
  })

  it('the budget banner is still rendered — moved, not dropped', () => {
    // A static attribute is an ATTRIBUTE node (type 6) with a `value`, not a
    // directive — the first version of this looked for a directive and found
    // nothing, which would have let the banner be deleted silently.
    const banner = all.find((n) =>
      (n.props || []).some(
        (p) => p.type === 6 && p.name === 'data-testid'
          && p.value && p.value.content === 'room-budget-banner',
      ),
    )
    expect(banner, 'the critical-band banner is gone').toBeTruthy()

    const vIf = directive(banner, 'if')
    expect(vIf && vIf.exp.content).toContain("notice.level === 'critical'")
  })
})
