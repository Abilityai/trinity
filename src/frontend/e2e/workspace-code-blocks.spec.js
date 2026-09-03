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
    // no unit test in this repo can check.
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
