/**
 * trinity-enterprise#610 PR A2 round 2 — touch and the phone keyboard, where the
 * tile in the thread is now THE place an ask is answered.
 *
 * @source-text-pin (declared): pointer/viewport media queries are CSS; jsdom
 * evaluates neither, so the class that carries each rule is pinned.
 *   - QA mobile F2: the tile's controls were 28–34px on a 768 tablet —
 *     `max-sm:` is a width, not a touch screen. The 44px floor keys on
 *     `(pointer: coarse)` too.
 *   - QA mobile F1: with the keyboard up on a phone (~400px of viewport) the
 *     agent band's ~105px left the thread ~20px and hid the note being typed;
 *     the band steps aside on a short phone viewport.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src')
const read = (rel) => readFileSync(join(ROOT, rel), 'utf8')

describe('touch targets and the keyboard', () => {
  it("the ask card's chips, note field and Send are 44px on any touch screen", () => {
    const asks = read('components/portal/PortalAsks.vue')
    for (const name of ['CHIP_BASE', 'FIELD', 'SEND']) {
      const line = asks.split('\n').find((l) => l.startsWith(`const ${name} =`))
      expect(line, name).toMatch(/\[@media\(pointer:coarse\)\]:min-h-11/)
    }
  })
  it('the agent band steps aside on a short phone viewport (the keyboard is up)', () => {
    const shell = read('views/Portal.vue')
    const at = shell.indexOf('<PortalAgentBand')
    const open = shell.lastIndexOf('<div', at)
    expect(shell.slice(open, at)).toMatch(/\[@media\(max-width:639px\)_and_\(max-height:480px\)\]:hidden/)
  })
})
