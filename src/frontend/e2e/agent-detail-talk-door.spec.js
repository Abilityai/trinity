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

// The fixture contract (#2199): default to an agent that does NOT exist, probe
// with the shared authenticated helper, and let a definitive 404 SKIP while any
// other probe failure fails loudly. Defaulting to `trinity-system` — an agent
// that always exists — is the anti-pattern the helper's own docstring names.
//
// Beyond existence (all this probe promises), these cases need the agent to be
// on the signed-in operator's **Workspace roster**: `resolveAgentLanding` has to
// land on it, or the door reaches the "you don't have access" stage instead. The
// roster is shared ∪ owned, with no admin arm — so an admin's own agent, or one
// shared with them, is the right fixture.
const AGENT = process.env.TALK_AGENT || 'testfix'

const VOICE_START = '**/api/enterprise/client-portal/agents/*/voice/start'
const ROSTER = '**/api/enterprise/client-portal/my-agents'
const FLAGS = '**/api/settings/feature-flags'
const VOICE_STATUS = '**/api/agents/*/voice/status'

/** Count every request to a URL pattern, whoever answers it. */
function countRequests(page, test) {
  const seen = []
  page.on('request', (req) => { if (test(req.url())) seen.push(req.url()) })
  return seen
}

/**
 * Make the destination claim it can run a call, and swallow the start request.
 *
 * Without the roster patch the local stack reports `realtime_voice.available:
 * false` (the dev `GEMINI_API_KEY` is empty), and `startVoiceCall` bails in
 * `PortalConversation` BEFORE it ever reaches `voice.startWith` — so a
 * "no start POST was made" assertion would pass for a reason that has nothing to
 * do with the rule under test. That false green is the trap this helper exists
 * to close.
 */
async function stubWorkspaceVoice(page) {
  await page.route(ROSTER, async (route) => {
    const response = await route.fetch()
    const body = await response.json().catch(() => null)
    if (!body) return route.fulfill({ response })
    body.realtime_voice = { available: true, reason: null }
    return route.fulfill({ response, json: body })
  })
  await page.route(VOICE_START, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    // Enough to satisfy the caller; the socket never opens because the URL
    // points nowhere, which is what keeps this case mic-free and flake-free.
    body: JSON.stringify({
      voice_session_id: 'e2e-voice-session',
      websocket_url: '/ws/voice/e2e-voice-session',
      portal_session_id: 'e2e-portal-session',
    }),
  }))
}

test.beforeEach(async ({ baseURL }) => {
  test.skip(!(await agentExists(AGENT, { baseURL })), missingAgentReason(AGENT, 'TALK_AGENT'))
})

test.describe('the Talk door', () => {
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
  test('@smoke the chat composer has no microphone', async ({ page }) => {
    await page.goto(`/agents/${AGENT}?tab=chat`)
    await expect(page.getByTestId('agent-talk')).toBeVisible({ timeout: 20000 })
    // The mic carried this exact title; it moved to the header as Talk.
    await expect(page.locator('button[title="Start voice conversation"]')).toHaveCount(0)
    await expect(page.locator('button[title="Voice session active"]')).toHaveCount(0)
  })
})

test.describe('@interactive the call itself', () => {
  test.use({ permissions: ['microphone'] })

  test('Talk lands on the agent and the orb comes up', async ({ page }) => {
    // Needs a real provider key. Patching the roster is still required — with an
    // empty GEMINI_API_KEY the instance reports unavailable and the call bails
    // before `voice.startWith`, which would make this pass for the wrong reason.
    await stubWorkspaceVoice(page)

    await page.goto(`/agents/${AGENT}`)
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
    await stubWorkspaceVoice(page)

    await page.goto(`/workspace?agent=${AGENT}&voice=1`)
    await page.waitForLoadState('networkidle')
    // Sign in however this instance asks (code entry / continue as operator);
    // any click here is what would have armed the browser's own heuristic.
    const operator = page.getByRole('button', { name: /continue as operator/i })
    if (await operator.isVisible({ timeout: 5000 }).catch(() => false)) await operator.click()
    await page.waitForTimeout(3000)

    expect(starts).toEqual([])
  })
})
