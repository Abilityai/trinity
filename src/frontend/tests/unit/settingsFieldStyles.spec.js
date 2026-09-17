/**
 * #2464 — Settings text fields must state the properties Tailwind will not
 * give them.
 *
 * This repo does NOT load `@tailwindcss/forms` (`tailwind.config.js` plugins =
 * typography only), so a text input inherits no form-element base styles.
 * `border-gray-300` on its own therefore sets a COLOUR on a border whose width
 * is 0 under preflight, padding does not exist unless stated, and the light
 * background is a UA default rather than a decision. The class string reads as
 * correct — which is why ent#463's intake form shipped as five borderless,
 * unpadded bars beside the Admin sign-in email field it was meant to match,
 * and why nobody caught it in review.
 *
 * Three things are pinned, in order of durability:
 *
 * 1. **The trap, by discovery** — no input on any Settings surface may name a
 *    border colour without the bare `border` that gives it a width. That is
 *    the rule; the five fields this issue reported are just today's members of
 *    it, and a sixth written the same way next month fails without anyone
 *    remembering this bug.
 * 2. **The shape is shared** — one constant, used by every consumer, so
 *    "these fields look identical" is true by construction rather than by
 *    two people copying the same string correctly.
 * 3. **The premise** — the forms plugin is still absent. If it is ever added,
 *    the constant's reasoning changes and its docstring needs re-reading;
 *    this fails loudly instead of leaving stale prose.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripComments } from './helpers/stripComments'
import {
  SETTINGS_TEXT_INPUT_CLASS,
  SETTINGS_NUMBER_INPUT_CLASS,
} from '../../src/components/settings/fieldStyles'

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(HERE, '../../src')
const SETTINGS_DIR = join(SRC, 'components/settings')

/** Every Settings surface: the panels plus the view that hosts them. */
function settingsFiles() {
  const panels = readdirSync(SETTINGS_DIR)
    .filter((f) => f.endsWith('.vue'))
    .map((f) => join(SETTINGS_DIR, f))
  return [...panels, join(SRC, 'views/Settings.vue')]
}

/**
 * Class strings on TEXT-ENTRY `<input>` / `<textarea>` tags, comments stripped.
 *
 * Checkboxes and radios are excluded, and not as a convenience: without the
 * forms plugin they render as UA-native controls whose `appearance` is never
 * reset, so `border-gray-300` on one is inert rather than broken. Including
 * them would make the rule below fire on four correct controls and force an
 * allowlist — which is how a guard ends up institutionalising the bug it was
 * written to find.
 */
function fieldClassStrings(source) {
  const src = stripComments(source)
  const out = []
  for (const tag of src.matchAll(/<(?:input|textarea)\b[\s\S]*?>/g)) {
    if (/type="(?:checkbox|radio)"/.test(tag[0])) continue
    for (const attr of tag[0].matchAll(/(?::class|class)="([^"]*)"/g)) {
      out.push(attr[1])
    }
  }
  return out
}

describe('the forms-plugin trap', () => {
  it('is still a trap — @tailwindcss/forms is not loaded', () => {
    // The constant's whole rationale is that no base styles exist. If someone
    // adds the plugin, this fails so its docstring gets re-read rather than
    // silently becoming wrong.
    const cfg = readFileSync(resolve(HERE, '../../tailwind.config.js'), 'utf8')
    expect(stripComments(cfg)).not.toMatch(/@tailwindcss\/forms/)
  })

  it('no Settings field names a border colour without a border width', () => {
    const offenders = []
    for (const file of settingsFiles()) {
      for (const cls of fieldClassStrings(readFileSync(file, 'utf8'))) {
        const namesColour = /\bborder-(?:gray|slate|zinc|neutral|stone)-\d{2,3}\b/.test(cls)
        const hasWidth = /(?:^|\s)border(?:-[024680]|-\[[^\]]+\])?(?:\s|$)/.test(cls)
        if (namesColour && !hasWidth) offenders.push(`${file.replace(SRC, 'src')}: "${cls}"`)
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([])
  })

  it('the guard can see a planted offender', () => {
    // Non-vacuity: without this, the rule above passes on a file set that no
    // longer contains any inputs at all.
    const planted = fieldClassStrings(
      '<template><input class="mt-1 block w-full rounded-md border-gray-300 text-sm" /></template>',
    )
    expect(planted).toHaveLength(1)
    expect(/\bborder-gray-\d{2,3}\b/.test(planted[0])).toBe(true)
    expect(/(?:^|\s)border(?:\s|$)/.test(planted[0])).toBe(false)
  })
})

describe('SETTINGS_TEXT_INPUT_CLASS', () => {
  it('states every property the missing plugin would have supplied', () => {
    for (const token of ['border ', 'px-3', 'py-2', 'bg-white', 'rounded-md']) {
      expect(SETTINGS_TEXT_INPUT_CLASS, `missing ${token}`).toContain(token)
    }
  })

  it('carries no width — layout belongs to the call site', () => {
    // The two call sites legitimately disagree (`w-full` in a grid, `flex-1`
    // beside a button); baking either in forces the other to override it.
    expect(SETTINGS_TEXT_INPUT_CLASS).not.toMatch(/(?:^|\s)(?:w-|flex-1)/)
  })

  it('agrees with the number field on the properties they share', () => {
    // Not a copy check — the two constants differ on purpose (width, spinner
    // suppression). These are the ones that must match, or two panels on one
    // tab look like two different products, which is why fieldStyles.js exists.
    for (const token of [
      'px-3', 'py-2', 'text-sm', 'rounded-md',
      'border border-gray-300 dark:border-gray-600',
      'dark:bg-gray-700',
      'disabled:opacity-60',
    ]) {
      expect(SETTINGS_TEXT_INPUT_CLASS, `text field lacks ${token}`).toContain(token)
      expect(SETTINGS_NUMBER_INPUT_CLASS, `number field lacks ${token}`).toContain(token)
    }
  })
})

describe('the shape is shared, not copied', () => {
  const CONSUMERS = [
    'components/settings/OperatorIntakePanel.vue',
    'views/Settings.vue',
  ]

  it.each(CONSUMERS)('%s uses the constant', (rel) => {
    const src = stripComments(readFileSync(join(SRC, rel), 'utf8'))
    expect(src).toMatch(/import \{[^}]*SETTINGS_TEXT_INPUT_CLASS/)
    expect(src).toMatch(/SETTINGS_TEXT_INPUT_CLASS/)
  })

  it('the intake panel keeps no hand-written field class', () => {
    const src = readFileSync(join(SRC, 'components/settings/OperatorIntakePanel.vue'), 'utf8')
    for (const cls of fieldClassStrings(src)) {
      expect(cls, `hand-written field class: "${cls}"`).toContain('SETTINGS_TEXT_INPUT_CLASS')
    }
  })
})
