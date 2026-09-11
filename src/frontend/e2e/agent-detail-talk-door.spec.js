import { test, expect } from '@playwright/test'
import { agentExists, missingAgentReason } from './helpers/agent-probe.js'

/**
 * trinity#2559 — Talk on Agent Detail is a door into the Workspace voice call.
 *
 * The `@smoke` cases prove the whole hand-off WITHOUT a microphone, a provider
 * key, or a real call: the roster is stubbed to report voice available and the
 * Workspace start POST is fulfilled at the network boundary, so what is checked
 * is the wiring — the click navigates, the intent survives the navigation, the
 * start request is ISSUED, the URL is cleaned, and nothing asks the retired
 * `/voice/status`. That tier matters because `.github/workflows/frontend-e2e.yml`
 * runs `npm run test:e2e:smoke` (= `--grep @smoke`), so `@interactive` runs on a
 * laptop and never again; a source-grep spec cannot catch a sibling PR breaking
 * `defineExpose` on rebase, and this can.
 *
 * `route.fulfill()` with a hand-built body is the house pattern (see
 * `agent-not-found.spec.js`). Patching `**\/api\/settings\/feature-flags` and the
 * portal roster has no precedent in this suite — the technique is new here, not
 * borrowed.
 *
 * The `@interactive` cases need fake media devices and a live provider; they
 * carry the rules that only a real call can show.
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
//     empty, and the door lands on "You don't have access" instead of the call.
//
// Both were observed, not predicted: with a describe-level probe the whole tier
// reported `5 skipped`, and with a real agent the load-bearing case failed on an
// empty roster. `workspace-code-blocks.spec.js` is the house pattern followed
// here (synthetic name + a fully `route.fulfill`ed roster), and
// `workspace-absorbs-session.spec.js:30-32` states the rule this file broke: the
// existence probe belongs INSIDE the @interactive test, never in a
// describe-level `beforeEach`, or it takes the @smoke tier down with it.
const AGENT = 'e2e-talk-door'

// The @interactive tier is the opposite: a REAL agent, on the caller's Workspace
// roster (shared ∪ owned — there is no admin arm), because only a real call can
// carry what those cases assert. Fixture contract #2199: default to a name that
// does NOT exist so a definitive 404 SKIPs and any other probe failure is loud.
const INTERACTIVE_AGENT = process.env.TALK_AGENT || 'testfix'

const API = '**/api/enterprise/client-portal'
const VOICE_START = `${API}/agents/*/voice/start`
const ROSTER = `${API}/my-agents*`
const SESSIONS = `${API}/sessions`
const FLAGS = '**/api/settings/feature-flags'
const VOICE_STATUS = '**/api/agents/*/voice/status'

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
  })))
}

/**
 * Make Agent Detail render a header for the synthetic agent.
 *
 * Talk lives in `AgentHeader`, which only renders once `agent` resolves — so
 * without this the page is "Agent not found" and every `getByTestId('agent-talk')`
 * times out. `GET /api/agents/{name}` is the single call `agents.js::fetchAgent`
 * makes; the shape is a real `trinity-system` payload trimmed to the fields the
 * header reads. Everything else Agent Detail fetches (info, token stats,
 * dashboard) tolerates a 404, which is what the un-stubbed backend returns.
 */
async function stubAgentDetail(page) {
  await page.route(`**/api/agents/${AGENT}`, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      name: AGENT,
      status: 'running',
      port: 2299,
      created: new Date().toISOString(),
      resources: { cpu: '1', memory: '1g' },
      container_id: 'e2e-talk-door-container',
      template: 'local:test-echo',
      runtime: 'claude-code',
      ephemeral: false,
      display_label: null,
      owner: 'admin',
      is_owner: true,
      is_shared: false,
      is_system: false,
      can_share: true,
      can_delete: true,
      autonomy_enabled: false,
      read_only_enabled: false,
      avatar_url: null,
      shares: [],
      circuit_breaker: null,
    }),
  }))
}

test.describe('the Talk door', () => {
  test.beforeEach(async ({ page }) => { await stubAgentDetail(page) })

  test('@smoke clicking Talk lands in the Workspace and STARTS the call', async ({ page }) => {
    const starts = countRequests(page, (u) => /\/client-portal\/agents\/[^/]+\/voice\/start/.test(u))
    const statusProbes = countRequests(page, (u) => /\/api\/agents\/[^/]+\/voice\/status/.test(u))
    await stubWorkspaceVoice(page)

    await page.goto(`/agents/${AGENT}`)
    const talk = page.getByTestId('agent-talk')
    await expect(talk).toBeVisible({ timeout: 20000 })
    await talk.click()

    await page.waitForURL(/\/workspace/, { timeout: 20000 })

    // The call was actually requested. Asserting only that nothing threw would
    // pass with a null ref: the hand-off is `conversationRef.value
    // ?.startVoiceCall?.()`, which silently consumes the intent when the ref is
    // not there.
    await expect.poll(() => starts.length, { timeout: 20000 }).toBeGreaterThan(0)

    // The agent came along, and the one-shot key did not linger.
    const url = new URL(page.url())
    expect(url.searchParams.get('voice')).toBeNull()
    expect(page.url()).toMatch(new RegExp(`agent=${AGENT}|/workspace/c/`))

    // The retired per-agent probe is gone for good.
    expect(statusProbes).toEqual([])
  })

  test('@smoke Talk is present even when the platform voice flag is off', async ({ page }) => {
    // #2559 G-1: the door is UNGATED. The destination owns availability and
    // says why it cannot start; a gate here would only add a cold-load pop-in
    // and a sticky-false hide on a failed flags fetch.
    await page.route(FLAGS, async (route) => {
      const response = await route.fetch()
      const body = await response.json().catch(() => null)
      if (!body) return route.fulfill({ response })
      body.voice_available = false
      return route.fulfill({ response, json: body })
    })

    await page.goto(`/agents/${AGENT}`)
    await expect(page.getByTestId('agent-talk')).toBeVisible({ timeout: 20000 })
  })

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

test.describe('nothing on Agent Detail offers voice any more', () => {
  test.beforeEach(async ({ page }) => { await stubAgentDetail(page) })

  test('@smoke the chat composer has no microphone', async ({ page }) => {
    await page.goto(`/agents/${AGENT}?tab=chat`)
    await expect(page.getByTestId('agent-talk')).toBeVisible({ timeout: 20000 })
    // The mic carried this exact title; it moved to the header as Talk.
    await expect(page.locator('button[title="Start voice conversation"]')).toHaveCount(0)
    await expect(page.locator('button[title="Voice session active"]')).toHaveCount(0)
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

test.describe('@interactive the call itself', () => {
  test.use({ permissions: ['microphone'] })

  // #2199, and `workspace-absorbs-session.spec.js:30-32`: the probe is INSIDE
  // this describe, never at file level, so it cannot sterilise the @smoke tier.
  test.beforeEach(async ({ baseURL }) => {
    test.skip(
      !(await agentExists(INTERACTIVE_AGENT, { baseURL })),
      missingAgentReason(INTERACTIVE_AGENT, 'TALK_AGENT'),
    )
  })

  test('Talk lands on the agent and the orb comes up', async ({ page }) => {
    await patchLiveRosterVoice(page)

    await page.goto(`/agents/${INTERACTIVE_AGENT}`)
    await page.getByTestId('agent-talk').click()
    await page.waitForURL(/\/workspace/, { timeout: 20000 })

    const orb = page.getByTestId('portal-voice-call')
    const line = page.getByTestId('portal-voice-line')
    await expect(orb.or(line).first()).toBeVisible({ timeout: 30000 })

    // Whatever it says, it must not be the two reasons that mean the roster
    // patch failed rather than the call.
    const text = await line.textContent().catch(() => '')
    expect(text || '').not.toContain('Voice is turned off on this instance.')
    expect(text || '').not.toContain('No voice provider key is configured on this instance.')
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
