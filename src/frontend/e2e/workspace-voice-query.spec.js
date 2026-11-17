import { test, expect } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * The Workspace `?voice=` one-shot (trinity#2559) seen from the outside.
 *
 * These cases used to live in `agent-detail-talk-door.spec.js`, beside the
 * Agent Detail Talk button that armed the one-shot. trinity-enterprise#950
 * removed that button from the agent header; what stays is the Workspace's own
 * contract — a pasted, bookmarked or mailed `?voice=1` starts nothing (a fresh
 * document is never armed) and the key is stripped from the URL in every
 * branch. The rule itself is unit-tested in `tests/unit/portalVoiceMode.spec.js`.
 */

// The @smoke tier is HERMETIC — a synthetic name that exists only in the route
// stubs below, never a fixture the stack has to provide. That is not a stylistic
// choice, it is the only shape that runs in CI, and the reason is two-sided:
//
//  1. `frontend-e2e.yml` pins the e2e baseline at ZERO user agents on purpose
//     ("Keep the e2e baseline at zero user agents", the ent#124 seeder sentinel),
//     so any real fixture name is absent and a `#2199` existence probe SKIPS.
//  2. Even given an agent, the CI admin is bootstrapped from `ADMIN_PASSWORD`
//     with **no email**, and `client_portal/portal_auth.py:147` 403s a platform
//     principal without one — so `GET /my-agents` cannot answer, the roster is
//     empty, and the Workspace lands on "You don't have access" instead of the call.
//
// Both were observed, not predicted: with a describe-level probe the whole tier
// reported `5 skipped`, and with a real agent the load-bearing case failed on an
// empty roster. `workspace-code-blocks.spec.js` is the house pattern followed
// here (synthetic name + a fully `route.fulfill`ed roster), and
// `workspace-absorbs-session.spec.js:30-32` states the rule this file broke: the
// existence probe belongs INSIDE the @interactive test, never in a
// describe-level `beforeEach`, or it takes the @smoke tier down with it.
const AGENT = 'e2e-voice-query'

// The @interactive tier is the opposite: a REAL agent, on the caller's Workspace
// roster (shared ∪ owned — there is no admin arm), because only a real call can
// carry what those cases assert. Fixture contract #2199: default to a name that
// does NOT exist so a definitive 404 SKIPs and any other probe failure is loud.
const INTERACTIVE_AGENT = process.env.TALK_AGENT || 'testfix'

const API = '**/api/enterprise/client-portal'
const VOICE_START = `${API}/agents/*/voice/start`
const ROSTER = `${API}/my-agents*`
const SESSIONS = `${API}/sessions`

const json = (body) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify(body),
})

/** Count every request to a URL pattern, whoever answers it. */
function countRequests(page, test) {
  const seen = []
  page.on('request', (req) => { if (test(req.url())) seen.push(req.url()) })
  return seen
}

/**
 * Stand up the whole Workspace destination, and swallow the start request.
 *
 * Two traps this closes, both of which produce a test that passes for the wrong
 * reason rather than one that fails honestly:
 *
 *  - `realtime_voice.available` must be true, or `startVoiceCall` bails in
 *    `PortalConversation` BEFORE `voice.startWith` — so "no start POST was made"
 *    would hold for a reason unrelated to the rule under test.
 *  - the roster must actually CONTAIN the agent, or `resolveAgentLanding` puts
 *    the door on the "You don't have access" stage, where nothing can start.
 */
async function stubWorkspaceVoice(page) {
  // Fully synthetic — `route.fetch()` + mutate was the original shape here and it
  // inherits whatever the live roster says, including the CI admin's 403. The
  // payload is `PortalRoster` (client_portal/models.py:189): `agents` is the
  // landing set `resolveAgentLanding` searches, and `realtime_voice` is the
  // ent#534 capability the destination reads before it will start a call.
  await page.route(ROSTER, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      client_email: 'e2e@example.com',
      agents: [{
        name: AGENT,
        display_label: 'Talk Door',
        description: 'e2e fixture',
        availability: 'ready',
        playbooks: [],
      }],
      realtime_voice: { available: true, reason: null },
      multi_agent_chat_available: false,
    }),
  }))
  // The Workspace shell's own reads. Empty is the right answer — it puts the
  // door on the "no thread yet" path, which is the one Talk actually takes for
  // an agent the operator has never chatted with, and therefore the one worth
  // proving. Un-stubbed these hit the live backend, which 403s a platform
  // principal with no email (`portal_auth.py:147`).
  await page.route(`${SESSIONS}*`, (route) => route.fulfill(json({ sessions: [] })))
  await page.route(`${API}/chat-state*`, (route) => route.fulfill(json({ chats: [] })))
  await page.route(`${API}/agents/${AGENT}/history*`, (route) => route.fulfill(json({
    agent_name: AGENT, session_id: null, messages: [],
  })))

  // `startVoiceCall` opens a thread BEFORE the first word is spoken
  // (`PortalConversation.vue:2087-2093`) — "the transcript needs a home". Its
  // failure branch sets "Could not open a chat for the call." and returns
  // WITHOUT ever reaching `voice.startWith`, so leaving this to the live backend
  // is exactly how the start POST goes missing while the page still looks right.
  await page.route(`${API}/agents/${AGENT}/sessions`, (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    return route.fulfill(json({ id: 'e2e-talk-door-session', agent_name: AGENT }))
  })

  await page.route(VOICE_START, (route) => route.fulfill(json({
    // Enough to satisfy the caller; the socket never opens because the URL
    // points nowhere, which is what keeps this case mic-free and flake-free.
    voice_session_id: 'e2e-voice-session',
    websocket_url: '/ws/voice/e2e-voice-session',
    portal_session_id: 'e2e-portal-session',
    // trinity-enterprise#595: a start answers with the socket's single-use ticket.
    websocket_ticket: 'e'.repeat(43),
    websocket_ticket_expires_in: 30,
  })))
}

test.describe('the Workspace ?voice= one-shot', () => {
  test('@smoke a pasted ?voice=1 link starts NOTHING, and is cleaned up', async ({ page }) => {
    // The armed one-shot, from the outside: this document was never armed, so
    // the conversation opens and no call is requested. `navigator.userActivation`
    // could not make this distinction — it is an audio-playback heuristic, and
    // any click on the page satisfies it.
    const starts = countRequests(page, (u) => /\/client-portal\/agents\/[^/]+\/voice\/start/.test(u))
    await stubWorkspaceVoice(page)

    await page.goto(`/workspace?agent=${AGENT}&voice=1`)
    await page.waitForLoadState('networkidle')

    await expect.poll(() => new URL(page.url()).searchParams.get('voice'), { timeout: 20000 }).toBeNull()
    // Settle well past the nextTick hand-off before claiming nothing happened.
    await page.waitForTimeout(3000)
    expect(starts).toEqual([])
  })

  test('@smoke ?voice=0 and a deep-linked thread both leave a clean URL', async ({ page }) => {
    // The single-strip contract: keyed on the key's PRESENCE, in bootstrap()'s
    // finally, so a rejected value and the `/workspace/c/:sid` early return —
    // which returns before resolveAgentQuery() ever runs — are both covered.
    // Three per-branch strip sites left both of these resident.
    await stubWorkspaceVoice(page)

    // A rejected value is still PRESENT, which is why the strip keys on presence.
    await page.goto(`/workspace?agent=${AGENT}&voice=0`)
    await page.waitForLoadState('networkidle')
    await expect.poll(() => new URL(page.url()).searchParams.get('voice'), { timeout: 20000 }).toBeNull()

    // The `/workspace/c/:sid` branch returns from inside the `try` before
    // resolveAgentQuery() runs, so only the `finally` can clean it. An unknown
    // sid is enough — the strip must happen whatever the thread turns out to be.
    await page.goto('/workspace/c/e2e-unknown-session?voice=1')
    await page.waitForLoadState('networkidle')
    await expect.poll(() => new URL(page.url()).searchParams.get('voice'), { timeout: 20000 }).toBeNull()
  })
})

/**
 * Patch only the capability bit on the LIVE roster, leaving the agent list real.
 *
 * The @interactive tier drives a real call against a real agent, so it cannot use
 * the synthetic roster above — `resolveAgentLanding` has to find the actual
 * fixture. What it does need is `realtime_voice.available`, because a laptop with
 * an empty `GEMINI_API_KEY` reports unavailable and `startVoiceCall` bails at
 * `PortalConversation.vue` BEFORE `voice.startWith`; the assertions would then
 * pass for a reason unrelated to the rule under test.
 *
 * Note this tier does NOT stub `POST …/voice/start` — stubbing it would fulfil
 * the request with a websocket_url pointing nowhere, and "the orb came up" would
 * be true with no call behind it. That is the whole difference between this tier
 * and @smoke, and it is why a real provider key is a precondition here.
 */
async function patchLiveRosterVoice(page) {
  await page.route(ROSTER, async (route) => {
    const response = await route.fetch()
    const body = await response.json().catch(() => null)
    if (!body) return route.fulfill({ response })
    body.realtime_voice = { available: true, reason: null }
    return route.fulfill({ response, json: body })
  })
}

test.describe('@interactive the ?voice= one-shot against a real agent', () => {
  test.use({ permissions: ['microphone'] })

  // #2199: the probe is INSIDE this describe, never at file level, so it
  // cannot sterilise the @smoke tier.
  test.beforeEach(async ({ baseURL }) => {
    test.skip(
      !(await agentExists(INTERACTIVE_AGENT, { baseURL })),
      missingAgentReason(INTERACTIVE_AGENT, 'TALK_AGENT'),
    )
  })

  test('signing in on a pasted ?voice=1 link still starts nothing', async ({ page, context }) => {
    // THE case the retired `navigator.userActivation` rule failed: Portal.vue
    // runs bootstrap() only while signed in, so the link survives unconsumed
    // until exactly the sign-in click — which sets sticky activation on the same
    // document. The armed flag is false on this fresh document regardless.
    const starts = countRequests(page, (u) => /\/client-portal\/agents\/[^/]+\/voice\/start/.test(u))
    await context.clearCookies()
    await page.addInitScript(() => { try { localStorage.clear(); sessionStorage.clear() } catch { /* noop */ } })
    await patchLiveRosterVoice(page)

    await page.goto(`/workspace?agent=${INTERACTIVE_AGENT}&voice=1`)
    await page.waitForLoadState('networkidle')
    // Sign in however this instance asks (code entry / continue as operator);
    // any click here is what would have armed the browser's own heuristic.
    const operator = page.getByRole('button', { name: /continue as operator/i })
    if (await operator.isVisible({ timeout: 5000 }).catch(() => false)) await operator.click()
    await page.waitForTimeout(3000)

    expect(starts).toEqual([])
  })
})
