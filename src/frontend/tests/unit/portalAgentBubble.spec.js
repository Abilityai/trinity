/**
 * The Workspace agent bubble and the markdown body inside it (#2515).
 *
 * vitest runs `environment: 'node'` with no mount harness, so the wiring is
 * pinned from source — the repo's established pattern. What matters here is
 * that the three things which used to be scattered (the v-html, the stylesheet,
 * the copy handler) are now in ONE place, and that the security-relevant
 * details of the handler have not drifted.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { DATA_COPY_CODE, DATA_CODE_BLOCK } from '../../src/utils/codeBlocks.js'
import { COPY_CODE_LABEL, COPY_CODE_ARIA } from '../../src/utils/clipboard.js'

const here = dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(resolve(here, '../../src', rel), 'utf8')

/**
 * Source with its COMMENTS removed — the `src()` / `code()` split
 * `portalComposerWiring.spec.js` already draws. These files explain themselves
 * at length, and the prose legitimately names `v-html`, `overflow-x` and issue
 * numbers like #2515; asserting their ABSENCE has to look at the code.
 */
const code = (text) => text
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const MARKDOWN = read('components/portal/PortalMarkdown.vue')
const BUBBLE = read('components/portal/PortalAgentBubble.vue')
const CONVERSATION = read('components/portal/PortalConversation.vue')
const ROOM = read('components/portal/PortalRoom.vue')

describe('PortalMarkdown.vue — the one body', () => {
  it('has exactly one v-html, fed by the code-block renderer', () => {
    expect((code(MARKDOWN).match(/v-html/g) || []).length).toBe(1)
    expect(MARKDOWN).toContain('renderMarkdownWithCodeBlocks')
    // Never the plain renderer here: that one is the twelve-consumer export and
    // produces no copy controls.
    expect(code(MARKDOWN)).not.toMatch(/\brenderMarkdown\(/)
  })

  it('selects on the exported marker constants rather than retyping them', () => {
    // A duplicated literal drifts from the emitter; deriving it means renaming
    // the attribute cannot silently unbind the handler.
    expect(MARKDOWN).toMatch(/DATA_COPY_CODE/)
    expect(MARKDOWN).toMatch(/DATA_CODE_BLOCK/)
    expect(MARKDOWN).toMatch(/from '@\/utils\/codeBlocks'/)
    expect(DATA_COPY_CODE).toBe('data-copy-code')
    expect(DATA_CODE_BLOCK).toBe('data-code-block')
  })

  it('copies the wrapper’s OWN pre, never a descendant and never innerHTML', () => {
    // `:scope >` is what stops a nested element inside a raw block from being
    // the thing that gets copied.
    expect(MARKDOWN).toContain("':scope > pre'")
    expect(MARKDOWN).toMatch(/pre\.textContent/)
    expect(code(MARKDOWN)).not.toMatch(/innerHTML/)
  })

  it('null-guards both lookups, so a stray click is a no-op', () => {
    const fn = MARKDOWN.slice(MARKDOWN.indexOf('function onBodyClick'))
    const body = fn.slice(0, fn.indexOf('\n}'))
    expect(body).toMatch(/if \(!btn\) return/)
    expect(body).toMatch(/if \(!pre\) return/)
  })

  it('restores the CONSTANTS after the window, and re-arms cleanly', () => {
    // Restoring a value captured before the click would let two clicks inside
    // the window leave the button permanently reading "Copied".
    expect(MARKDOWN).toMatch(/COPY_CODE_LABEL/)
    expect(MARKDOWN).toMatch(/COPY_CODE_ARIA/)
    expect(MARKDOWN).toMatch(/clearTimeout\(resetTimers\.get\(btn\)\)/)
    expect(COPY_CODE_LABEL).toBe('Copy')
    expect(COPY_CODE_ARIA).toBe('Copy code')
  })

  it('awaits the clipboard write before anything else in the click task', () => {
    const fn = MARKDOWN.slice(MARKDOWN.indexOf('async function copyBlock'))
    const body = fn.slice(0, fn.indexOf('\n}'))
    const firstAwait = body.indexOf('await ')
    expect(firstAwait).toBeGreaterThan(-1)
    expect(body.slice(firstAwait, firstAwait + 20)).toContain('copyText')
  })

  it('announces the outcome as well as showing it', () => {
    expect(MARKDOWN).toContain('aria-live="polite"')
    expect(MARKDOWN).toContain('sr-only')
  })

  it('never logs — the payload may be a credential', () => {
    expect(code(MARKDOWN)).not.toMatch(/console\.(error|warn|log)/)
  })
})

describe('PortalMarkdown.vue — the visual contract', () => {
  const style = MARKDOWN.slice(MARKDOWN.indexOf('<style scoped>'))
  const styleCode = code(style)

  it('wraps at the edge instead of scrolling sideways', () => {
    expect(style).toMatch(/white-space: pre-wrap/)
    expect(style).toMatch(/overflow-wrap: anywhere/)
    expect(styleCode).not.toMatch(/overflow-x/)
  })

  it('keeps the #2211 paragraph rhythm the transcripts used to carry', () => {
    expect(style).toMatch(/\.prose-portal :deep\(p\) \{ margin: 0\.5rem 0; \}/)
    expect(style).toMatch(/\.prose-portal :deep\(p:first-child\)/)
    expect(style).toMatch(/\.prose-portal :deep\(p:last-child\)/)
  })

  it('renders a block at the AC floor size in the mono stack', () => {
    expect(style).toMatch(/:deep\(pre\)[\s\S]{0,220}text-xs/)
    expect(style).toMatch(/:deep\(pre\)[\s\S]{0,220}font-mono/)
    // The old 0.8em made inline code smaller than the meta ink around it.
    expect(styleCode).not.toMatch(/0\.8em/)
  })

  it('uses the contract’s dark border shade, not the input/button one', () => {
    expect(style).toMatch(/dark:border-gray-750/)
  })

  it('keeps the bar out of a manual selection', () => {
    expect(style).toMatch(/\.code-block-bar[\s\S]{0,240}select-none/)
  })

  it('does not let a message that IS a block double the bubble padding', () => {
    expect(style).toMatch(/\.code-block:first-child/)
    expect(style).toMatch(/\.code-block:last-child/)
  })

  it('uses tokens only — no hex, no raw non-gray palette class', () => {
    expect(styleCode).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(styleCode).not.toMatch(/\b(?:bg|text|border|ring)-(?:red|green|blue|yellow|indigo|purple|orange|amber|rose|emerald|teal|cyan|sky|violet|fuchsia|pink|lime)-\d{2,3}\b/)
  })
})

describe('PortalAgentBubble.vue — the chat chrome, and nothing more', () => {
  it('renders no markdown of its own', () => {
    expect(code(BUBBLE)).not.toMatch(/v-html/)
    expect(code(BUBBLE)).not.toMatch(/\.prose-portal :deep\(/)
    expect(BUBBLE).toContain('PortalMarkdown')
  })

  it('offers a message-level copy with an accessible name', () => {
    expect(BUBBLE).toMatch(/COPY_MESSAGE_ARIA/)
    expect(BUBBLE).toMatch(/:aria-label=/)
    expect(BUBBLE).toMatch(/<svg/)
    expect(BUBBLE).toMatch(/<button/)
  })

  it('says WHY a copy failed — an icon alone cannot', () => {
    expect(BUBBLE).toMatch(/feedback\.label/)
    expect(BUBBLE).toMatch(/text-status-danger-600 dark:text-status-danger-400/)
    expect(code(BUBBLE)).not.toMatch(/console\.(error|warn|log)/)
  })

  it('copies the RAW markdown, not the rendered text', () => {
    expect(BUBBLE).toMatch(/copyText\(props\.content/)
  })

  it('leaves a slot for the rating, in the same action row', () => {
    expect(BUBBLE).toMatch(/<slot\s*\/>/)
  })
})

describe('both transcripts delegate to it', () => {
  for (const [name, src] of [['PortalConversation', CONVERSATION], ['PortalRoom', ROOM]]) {
    it(`${name} renders the shared bubble and defines no body of its own`, () => {
      expect(src).toContain('PortalAgentBubble')
      expect(code(src)).not.toMatch(/v-html/)
      expect(code(src)).not.toMatch(/\.prose-portal :deep\(/)
      // The old local helper is gone with the markup it fed.
      expect(code(src)).not.toMatch(/const render = /)
    })
  }

  it('the rating stays a child of the bubble, on a persisted message only', () => {
    expect(code(CONVERSATION)).toMatch(/<PortalAgentBubble[\s\S]{0,200}<PortalRating/)
    expect(CONVERSATION).toMatch(/<PortalRating[\s\S]{0,200}v-if="m\.id"/)
  })

  it('neither new component introduces a bare loading gate', () => {
    for (const [name, src] of [['PortalMarkdown', MARKDOWN], ['PortalAgentBubble', BUBBLE]]) {
      expect(code(src), name).not.toMatch(/v-if="loading[A-Za-z]*"/)
    }
  })
})
