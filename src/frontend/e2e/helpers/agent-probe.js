import { request } from '@playwright/test'
import fs from 'fs'

/**
 * Shared fixture-agent existence probe (#2199).
 *
 * ⚠️ THE PROBE MUST BE AUTHENTICATED. ⚠️
 * `GET /api/agents/{name}` sits behind `AuthorizedAgent`, and Trinity's JWT
 * lives in localStorage — NOT in a cookie. So a bare
 * `request.newContext({ baseURL })` carries no credential, 401s, and makes
 * `test.skip()` fire on EVERY run: the test reads as "skipped" forever and
 * nobody notices it stopped covering anything. That is silent false
 * confidence, and it is strictly worse than a red test.
 *
 * This is not hypothetical — it is the live defect in
 * `circuit-breaker-badge.spec.js` and `honest-failed-states.spec.js`, both of
 * which probe unauthenticated against a default of `trinity-system` (an agent
 * that ALWAYS exists), so their 401 is unconditional.
 *
 * One shared helper exists precisely so that anti-pattern cannot be
 * re-derived independently in each spec. Consume this; do not hand-roll a
 * probe.
 *
 * SCOPE: existence only. It deliberately does NOT check that the agent is
 * running or on a particular runtime — reporting a *present but unsuitable*
 * fixture as "absent" would be a false claim, and a materially different
 * diagnosis. Specs needing a running Claude-runtime agent say so in their own
 * header (see e2e/README.md → Fixture agents).
 *
 * NOTE: this file lives under `e2e/helpers/` and is NOT collected as a test.
 * Playwright's default testMatch only picks up files ending in `.spec` or
 * `.test` (with a js/ts/jsx/tsx extension), and the setup project matches
 * `setup.js`; `agent-probe.js` matches neither.
 */

/**
 * Harvest the JWT the `setup` project persisted into the storageState file.
 *
 * The path stays a plain relative literal: every documented invocation runs
 * with cwd = `src/frontend` (see e2e/README.md), and an `import.meta.url`
 * derivation's survival through Playwright's esbuild transform is unverified.
 *
 * @returns {string|undefined} the token, or undefined when setup has not run
 */
export function tokenFromStorageState() {
  try {
    const state = JSON.parse(fs.readFileSync('e2e/.auth/admin.json', 'utf8'))
    return state.origins
      ?.flatMap((o) => o.localStorage || [])
      .find((i) => i.name === 'token')?.value
  } catch {
    // No storageState yet (setup didn't run). Returning undefined makes the
    // probe 401 and the caller skip — the correct degraded state.
    return undefined
  }
}

/**
 * Does this agent exist on the stack under test?
 *
 * @param {string} name      agent name to probe
 * @param {object} opts
 * @param {string} opts.baseURL  Playwright `baseURL` fixture
 * @param {string} [opts.token]  bearer token; falls back to the storageState
 *                               harvest when omitted (specs that already mint
 *                               their own admin token should pass it in)
 * @returns {Promise<boolean>}
 */
export async function agentExists(name, { baseURL, token } = {}) {
  const bearer = token || tokenFromStorageState()
  const api = await request.newContext({
    baseURL,
    extraHTTPHeaders: bearer ? { Authorization: `Bearer ${bearer}` } : {},
  })
  const ok = await api
    .get(`/api/agents/${name}`)
    .then((r) => r.ok())
    .catch(() => false)
  await api.dispose()
  return ok
}

/**
 * Uniform skip reason that names the env var used to override the fixture, so
 * a reader of the run output knows exactly how to make the test run.
 *
 * @param {string} name    the agent that was not found
 * @param {string} envVar  the override env var for this spec
 * @returns {string}
 */
export function missingAgentReason(name, envVar) {
  return `agent '${name}' not found on this stack — set ${envVar} to an existing agent`
}
