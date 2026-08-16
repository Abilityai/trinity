/**
 * #2198 — the first mount of a KeepAlive'd AgentDetail must not do everything
 * twice, WITHOUT disabling the KeepAlive revisit refresh.
 *
 * Vue fires both `onMounted` and `onActivated` on the first mount of a cached
 * component (`App.vue` includes 'AgentDetail'), so every data call in both hooks
 * ran twice — measured as two `/api/agents/{name}` requests sharing one
 * timestamp.
 *
 * Why this is a SOURCE-STRUCTURE guard, not a behavioural test: there is no
 * component-mount harness in this project (no @vue/test-utils — see
 * `agentDetailDeepLink.spec.js`), and every property below is about the ORDER
 * of statements inside a lifecycle hook.
 *
 * The assertion that earns its keep is the second one. A one-way
 * `initialLoadDone = true` that nothing resets would satisfy every
 * request-count test on a fresh page load, and would ALSO skip the data half on
 * every later activation — so a KeepAlive revisit would never refresh the
 * agent. That is the entire reason `onActivated` exists (#1672) and is a worse
 * bug than the one being fixed. `learnings.md:189` records the same lesson from
 * #1804: the transition that gets missed is the one where the before and after
 * states are equal — here, an activation on an already-loaded page.
 *
 * Four commits exist because behaviour was handled in ONE hook only —
 * `6e50ac36` (#1672), `0fbefdc5` (#2130), `b2e2b02c` (#2153), `3639c30b`
 * (ent#358) — so the last block pins that both hooks still handle everything
 * that is not a duplicate.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const SRC = fileURLToPath(new URL('../../src/views/AgentDetail.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')
const clean = stripComments(source)

/** Body of `hook(async () => { ... })` via brace matching, comments stripped. */
function hookBody(hookName) {
  const start = clean.indexOf(`${hookName}(async () => {`)
  expect(start, `${hookName}(async () => { … }) not found`).toBeGreaterThan(-1)
  const open = clean.indexOf('{', start)
  let depth = 0
  for (let i = open; i < clean.length; i++) {
    if (clean[i] === '{') depth++
    else if (clean[i] === '}') {
      depth--
      if (depth === 0) return clean.slice(open + 1, i)
    }
  }
  throw new Error(`unbalanced braces in ${hookName}`)
}

describe('#2198 first-activation sentinel', () => {
  it('onMounted ARMS the sentinel before its first await', () => {
    const body = hookBody('onMounted')
    const armAt = body.indexOf('skipNextActivation = true')
    expect(armAt, 'onMounted must arm the sentinel').toBeGreaterThan(-1)

    const awaitAt = body.search(/\bawait\b/)
    expect(
      armAt,
      'onActivated runs in the same flush and reads the flag synchronously, so ' +
      'arming it after an await is too late.'
    ).toBeLessThan(awaitAt)
  })

  it('onActivated CONSUMES it — reads AND clears', () => {
    // THE assertion. A one-way flag passes a request-count test and silently
    // disables every KeepAlive revisit refresh.
    const body = hookBody('onActivated')
    expect(body).toMatch(/const\s+isInitialActivation\s*=\s*skipNextActivation/)
    expect(
      body,
      'onActivated must CLEAR the sentinel. A flag that is never reset skips ' +
      'the data half on every later activation, so a revisit never refreshes ' +
      'the agent — the entire reason this hook exists (#1672).'
    ).toMatch(/skipNextActivation\s*=\s*false/)
  })

  it('consumes it BEFORE the retired-session redirect can early-return', () => {
    // Both hooks open with `if (redirectRetiredSessionLink()) return`. Consuming
    // after that guard leaves the flag armed on a still-cached component, and
    // the next genuine revisit silently skips its refresh.
    const body = hookBody('onActivated')
    const consumeAt = body.indexOf('skipNextActivation = false')
    const redirectAt = body.indexOf('redirectRetiredSessionLink()')
    expect(consumeAt).toBeGreaterThan(-1)
    expect(redirectAt).toBeGreaterThan(-1)
    expect(
      consumeAt,
      'the sentinel must be consumed above the redirect guard'
    ).toBeLessThan(redirectAt)
  })

  it('arms it AFTER onMounted\'s own redirect guard', () => {
    // A mount that navigates away loads nothing, so the activation that follows
    // must not be told to skip.
    const body = hookBody('onMounted')
    expect(body.indexOf('skipNextActivation = true'))
      .toBeGreaterThan(body.indexOf('redirectRetiredSessionLink()'))
  })

  it('the skip guards the DATA half only, and sits after the idempotent work', () => {
    const body = hookBody('onActivated')
    const guardAt = body.indexOf('if (isInitialActivation) return')
    expect(guardAt, 'onActivated must short-circuit on the initial activation').toBeGreaterThan(-1)

    // Above the guard: idempotent, data-free, must run on EVERY activation.
    for (const fn of ['redirectRetiredSessionLink()', 'applyDeepLinkRouting()', 'startAllPolling()']) {
      expect(body.indexOf(fn), `${fn} must run on every activation`).toBeLessThan(guardAt)
    }

    // Below the guard: consumes state, or needs loaded agent data.
    // reconcileDeepLinkVisibility reads `deepLinkedTab`, acts, and CLEARS it —
    // running it before the agent loads would judge ?tab=sharing / ?tab=brain
    // invisible against a null agent, fall back to Overview, and clear the flag
    // so onMounted's own later call becomes a no-op. That regresses #2130 and
    // #2153 and no request-count assertion would see it.
    for (const fn of ['loadAgent()', 'loadAvailableEmotions()', 'checkDashboardExists()',
                      'checkBrainOrbCapability()', 'reconcileDeepLinkVisibility()',
                      'startEmotionCycling()']) {
      expect(body.indexOf(fn), `${fn} must sit below the initial-activation guard`)
        .toBeGreaterThan(guardAt)
    }
  })
})

describe('#2198 both hooks still handle everything (the #1672 contract)', () => {
  for (const fn of ['redirectRetiredSessionLink', 'applyDeepLinkRouting',
                    'reconcileDeepLinkVisibility', 'startAllPolling']) {
    it(`${fn} is still called in BOTH hooks`, () => {
      // onMounted alone silently no-ops on the cached-revisit path, which
      // learnings.md:118-120 records as "the common path" — it passes every
      // fresh-reload manual test and fails in real use.
      for (const hook of ['onMounted', 'onActivated']) {
        expect(hookBody(hook), `${hook} lost its ${fn}() call`).toContain(`${fn}(`)
      }
    })
  }
})

describe('#2198 the loading skeleton gate is identity-aware', () => {
  it('compares the loaded agent against the ROUTED name, not mere presence', () => {
    // design-system-contract:41-43 — a background refresh of the same entity is
    // invisible, a switch to a different entity animates.
    //
    // `!agent.value` alone would be a regression, not a fix: the
    // `route.params.name` watcher resets hasDashboard/agentTags/authStatus/
    // tokenStats but deliberately never clears `agent.value`, so an A -> B
    // switch would render agent A's data while B loads.
    const fn = clean.slice(clean.indexOf('async function loadAgent()'))
    const body = fn.slice(0, fn.indexOf('\n}'))

    expect(body).toMatch(/loading\.value\s*=\s*true/)
    expect(
      body,
      'the loading gate must compare agent identity against the routed name'
    ).toMatch(/agent\.value\.name\s*!==\s*route\.params\.name/)
  })
})

describe('#2198 the dashboard probe is shared, and keyed on running-ness', () => {
  it('checkDashboardExists returns an in-flight probe instead of starting a second', () => {
    // A store-level promise-join cannot collapse this: the four triggers fire
    // hundreds of ms apart (measured +0/+326/+396ms) and a join only merges
    // CONCURRENT calls. Each staggered ladder then issued its own 3 requests.
    const fn = clean.slice(clean.indexOf('function checkDashboardExists()'))
    const body = fn.slice(0, fn.indexOf('\n}\n'))
    expect(body).toMatch(/if\s*\(dashboardProbe\s*&&\s*dashboardProbeKey\s*===\s*key\)\s*return\s+dashboardProbe/)
  })

  it('the probe key carries running-ness, not just the agent name', () => {
    // The ladder early-returns on `status !== 'running'`, so a probe started
    // while the agent was booting settles without ever asking. If the "just
    // became running" watcher joined it, a slow-starting agent would lose its
    // Dashboard tab forever — its status will not change again.
    expect(clean).toMatch(/const key = `\$\{agent\.value\.name\}:\$\{agent\.value\.status === 'running'\}`/)
  })

  it('the route watcher drops the probe so agent B never inherits A\'s answer', () => {
    const start = clean.indexOf("watch(() => route.params.name")
    const body = clean.slice(start, clean.indexOf('\n})', start))
    expect(body).toContain('resetDashboardProbe()')
  })

  it('the retry ladder stops on a settled answer', () => {
    // `settled` means the agent ran its handler and answered, so "no dashboard"
    // is final. Without it the page spent 3 requests over 9 SECONDS on every
    // load of an agent that will never have a dashboard.
    expect(clean).toMatch(/if\s*\(response\?\.settled === true\)\s*break/)
  })
})
