/**
 * ent#554 — printing a canvas must print the CANVAS, not the whole app.
 *
 * The first version of this feature called `window.print()` with a print
 * stylesheet that only STYLED the document and never hid anything else, so the
 * browser printed the entire page — nav bar, tabs, the on-screen panel and the
 * print copy — which fails the acceptance criterion ("one clean column") and is
 * what an operator hit on the first try.
 *
 * Nothing automated can see a print preview, so these pin the three structural
 * facts the isolation depends on:
 *
 *   1. a rule that hides every body child except the print root;
 *   2. the print root actually BEING a body child (teleported) — the rule is
 *      inert otherwise;
 *   3. the document mounting before `print()` is called, or the sheet is empty.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import path from 'path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.resolve(HERE, '../../src', rel), 'utf8')

const DOC = read('components/canvas/CanvasDocument.vue')
const PANEL = read('components/canvas/CanvasPanel.vue')
const SHARED = read('views/SharedCanvas.vue')

describe('ent#554 print isolation', () => {
  it('a print rule hides every body child except the print root', () => {
    // The single rule that turns "print the page" into "print the canvas".
    expect(DOC).toMatch(/@media print[\s\S]*body\s*>\s*\*:not\(\.canvas-print-root\)[\s\S]*display:\s*none/)
  })

  it('the print root is hidden on screen', () => {
    expect(DOC).toMatch(/\.canvas-print-root\s*\{[^}]*display:\s*none/)
  })

  for (const [name, src] of [['CanvasPanel', PANEL], ['SharedCanvas', SHARED]]) {
    it(`${name} teleports its print copy to <body>`, () => {
      // A print root nested inside the app is not a body child, so the hiding
      // rule above would hide its ancestor and print nothing at all.
      expect(src).toMatch(/<Teleport to="body">/)
      const teleport = src.slice(src.indexOf('<Teleport to="body">'))
      expect(teleport).toMatch(/class="canvas-print-root"/)
    })

    it(`${name} mounts the document before calling print()`, () => {
      // `v-if="printing"` + `await nextTick()` — without the flush, print()
      // races the render and the sheet comes out blank.
      expect(src).toMatch(/v-if="printing"/)
      const fn = src.slice(src.indexOf('function downloadPdf'))
      expect(fn).toMatch(/printing\.value = true[\s\S]*await nextTick\(\)[\s\S]*window\.print\(\)/)
      expect(fn).toMatch(/finally[\s\S]*printing\.value = false/)
    })
  }

  it('the on-screen chrome is still marked print:hidden', () => {
    // Belt: even with isolation, the controls must never read as document.
    expect(PANEL).toMatch(/data-testid="canvas-pdf"/)
    expect(PANEL).toMatch(/print:hidden/)
  })
})
