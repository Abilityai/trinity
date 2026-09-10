/**
 * Typography of an agent reply (#2616).
 *
 * The Workspace transcript was the one markdown surface in the app with no
 * typographic treatment: `.prose-portal` styled paragraphs, unordered lists,
 * links and code, and Tailwind's preflight reset everything else — so a GFM
 * table arrived with no rules and no padding, headings at body weight, and an
 * ordered list with its numbers gone.
 *
 * Two halves, because the defect had two:
 *   1. what the parser emits (the structure the CSS keys on), exercised through
 *      the app's CONFIGURED marked, as `markdownCodeBlocks.spec.js` does;
 *   2. that `PortalMarkdown.vue`'s stylesheet actually covers that element set —
 *      vitest runs `environment: 'node'` with no mount harness, so the sheet is
 *      pinned from source, the repo's established pattern.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { marked } from '../../src/utils/markedConfig.js'

const here = dirname(fileURLToPath(import.meta.url))
const MARKDOWN = readFileSync(
  resolve(here, '../../src/components/portal/PortalMarkdown.vue'), 'utf8',
)
/** Source with comments removed: the prose legitimately names the very
 *  selectors and values these assertions are about. */
const code = (text) => text
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const CSS = code(MARKDOWN)
/** Does the sheet have a rule whose selector list mentions this element? */
const styles = (selector) => CSS.includes(`.prose-portal :deep(${selector})`)

const TABLE_MD = '| Agent | Runs |\n| --- | ---: |\n| cornelius | 1284 |\n'

describe('what marked emits — the structure the treatment keys on', () => {
  it('parses a GFM table into thead/tbody with per-cell alignment', () => {
    const html = marked(TABLE_MD)
    expect(html).toContain('<thead>')
    expect(html).toContain('<th>Agent</th>')
    // The alignment survives as the `align` attribute (DOMPurify admits it),
    // which is what the right-aligned/tabular-figures rule selects on. A marked
    // bump that switched to an inline `style` would strand that rule, and
    // `FORBID_TAGS: ['style']` is not the same thing as dropping the attribute.
    expect(html).toContain('<th align="right">Runs</th>')
    expect(html).toContain('<td align="right">1284</td>')
  })

  it('parses an ordered list, a nested one, and a heading run', () => {
    const html = marked('1. one\n2. two\n   1. nested\n')
    expect(html).toContain('<ol>')
    expect((html.match(/<ol>/g) || []).length).toBe(2)
    expect((html.match(/<li>/g) || []).length).toBe(3)

    const heads = marked('# one\n\n## two\n\n### three\n')
    for (const level of [1, 2, 3]) expect(heads).toContain(`<h${level}>`)
  })

  it('parses a blockquote and a horizontal rule', () => {
    expect(marked('> quoted\n')).toContain('<blockquote>')
    expect(marked('---\n')).toContain('<hr>')
  })
})

describe('.prose-portal covers the whole element set an agent can write', () => {
  it('styles tables, their header, their cells and their rows', () => {
    for (const el of ['table', 'thead th', 'th', 'td', 'tbody tr:last-child td']) {
      expect(styles(el), el).toBe(true)
    }
  })

  it('gives the table its own scroll viewport so the bubble never widens', () => {
    // The pair CanvasKit proved in the 24rem rail (#2583): the table element is
    // the viewport, and cells wrap by word so the auto layout cannot squeeze a
    // narrow table to one character per line.
    expect(CSS).toMatch(/\.prose-portal :deep\(table\)[^}]*display: block/)
    expect(CSS).toMatch(/\.prose-portal :deep\(table\)[^}]*overflow-x: auto/)
    expect(CSS).toMatch(/:deep\(td\)[\s\S]{0,200}?overflow-wrap: normal/)
  })

  it('right-aligns a numeric column with figures that line up', () => {
    expect(CSS).toContain('td[align="right"]')
    expect(CSS).toMatch(/\.prose-portal :deep\(table\)[^}]*font-variant-numeric: tabular-nums/)
  })

  it('styles every heading level, ordered lists and list items', () => {
    for (const el of ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ol', 'ul', 'li']) {
      expect(styles(el), el).toBe(true)
    }
  })

  it('keeps the heading ladder bounded — no page-title size in a chat bubble', () => {
    // 18px is the design system's SECTION size; 24px is the page title, which a
    // reply is not. A regression here is a `# Heading` that shouts.
    expect(CSS).toMatch(/:deep\(h1\)[\s\S]{0,120}?text-\[18px\]/)
    expect(CSS).not.toMatch(/:deep\(h[1-6]\)[\s\S]{0,120}?text-(2xl|3xl|\[2[0-9]px\])/)
  })

  it('styles blockquotes and horizontal rules', () => {
    expect(styles('blockquote')).toBe(true)
    expect(styles('hr')).toBe(true)
  })
})

describe('what the pass must not have disturbed', () => {
  it('leaves the code-block treatment intact (#2515)', () => {
    for (const el of ['.code-block', '.code-block-bar', '.code-block-copy', 'pre', 'pre code']) {
      expect(styles(el), el).toBe(true)
    }
    // The `pre` still wraps at the edge rather than scrolling, and it is the
    // ONLY place `anywhere` may appear: inside a table it is the bug.
    expect((CSS.match(/overflow-wrap: anywhere/g) || []).length).toBe(1)
    expect(CSS).toMatch(/:deep\(pre\)[\s\S]{0,220}?overflow-wrap: anywhere/)
  })

  it('admits no new tags, attributes or classes from the agent', () => {
    // The treatment is CSS on platform-owned selectors. If this pass had needed
    // the sanitiser loosened, it would show up as a policy import here.
    expect(CSS).not.toMatch(/ADD_TAGS|ADD_ATTR|FORBID_TAGS|DOMPurify/)
  })

  it('is still the ONE stylesheet — no second copy on a sibling surface', () => {
    for (const sibling of ['PortalConversation.vue', 'PortalRoom.vue', 'PortalAgentBubble.vue']) {
      const src = readFileSync(resolve(here, '../../src/components/portal', sibling), 'utf8')
      expect(code(src).includes('.prose-portal :deep('), sibling).toBe(false)
    }
  })
})
