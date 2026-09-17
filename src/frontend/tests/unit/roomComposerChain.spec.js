/**
 * The room composer renders on a condition it STATES (#2620, rewritten #2794).
 *
 * #2620 shipped this file to stop a conditional being inserted between the
 * attachment chips and `<form v-else>`, because `v-else` binds to the
 * immediately preceding element and the composer would then render on the
 * wrong condition. The mechanism it describes is real and the hazard is real.
 *
 * What it got wrong is WHICH relationship was correct. The composer was
 * written as `v-else` to the "this conversation has ended" line (ent#358) —
 * render the composer unless the room is closed. By the time #2620 looked, the
 * batch notice, the attachment chips (ent#524) and its own budget banner had
 * each been inserted in between, so the chain already ended on
 * `attachments.length`. #2620 then pinned that as the contract, and with it two
 * live defects: attaching a file to a room REPLACED the composer (and the room
 * cleared no chips, so it never came back), and a closed room rendered a live
 * composer directly under the line saying it had ended.
 *
 * So the composer now carries `v-if="!isClosed"`. A `v-else` is a promise about
 * whatever element happens to sit above it, and this neighbourhood has broken
 * that promise three times in three changes.
 *
 * This file therefore pins the OUTCOME rather than the chain: the composer's
 * condition is its own and names the closed state; the chips render beside the
 * composer rather than instead of it; and the banner is still there. It walks
 * the parsed template AST rather than matching text — a structural question
 * needs the structure (#2620's own first attempt matched text and was vacuous).
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

describe('#2620/#2794 the room composer renders on a stated condition', () => {
  const ast = templateAst()
  const all = elements(ast)

  const composer = () => all.find((n) => n.tag === 'form' && directive(n, 'if'))

  it('the composer names its own condition, and it is the closed state', () => {
    // THE property. Anything inserted above a `v-else` silently repoints it;
    // a stated condition cannot be stolen by a neighbour.
    const form = composer()
    expect(form, 'no <form v-if> found — did the composer go back to v-else?').toBeTruthy()
    expect(directive(form, 'if').exp.content).toContain('isClosed')
  })

  it('no composer form carries v-else or v-else-if', () => {
    const chained = all.find((n) => n.tag === 'form' && (directive(n, 'else') || directive(n, 'else-if')))
    expect(
      chained,
      'the composer is chained to whatever element precedes it again — that is ' +
      'how it came to render on `attachments.length` (#2794)',
    ).toBeFalsy()
  })

  it('the attachment chips render BESIDE the composer, not instead of it', () => {
    // The defect in one assertion: with the chips and the composer on mutually
    // exclusive conditions, attaching a file removes the box you type in.
    const chips = all.find((n) => {
      const vIf = directive(n, 'if')
      return vIf && vIf.exp.content.includes('attachments.length')
    })
    expect(chips, 'the attachment chip block is gone').toBeTruthy()

    const form = composer()
    const parent = all.find((n) => elementChildren(n).includes(form))
    const siblings = elementChildren(parent)
    // Both are children of the composer region, and both can be true at once.
    expect(siblings).toContain(chips)
    expect(directive(chips, 'else')).toBeFalsy()
    expect(directive(chips, 'else-if')).toBeFalsy()
  })

  it('a closed room still says so', () => {
    const line = all.find((n) => {
      const vIf = directive(n, 'if')
      return n.tag === 'p' && vIf && vIf.exp.content === 'isClosed'
    })
    expect(line, 'the "this conversation has ended" line is gone').toBeTruthy()
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
