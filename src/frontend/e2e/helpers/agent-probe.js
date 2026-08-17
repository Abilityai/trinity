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
 * `circuit-breaker-badge.spec.js` (left in place deliberately — see the Known
 * gap note in e2e/README.md), and was the defect in
 * `honest-failed-states.spec.js` until #2199 fixed it: both probed
 * unauthenticated against a default of `trinity-system` (an agent that ALWAYS
 * exists), so their 401 was unconditional.
 *
 * One shared helper exists precisely so that anti-pattern cannot be
 * re-derived independently in each spec. Consume this; do not hand-roll a
 * probe.
 *
 * ⚠️ A BROKEN PROBE IS LOUD, NEVER A SKIP. ⚠️
 * Only a definitive 404 — the platform's uniform "no such agent" answer
 * (Invariant #8) — reads as "fixture absent". A 401/403 (no/stale token: the
 * setup project didn't run, or the backend restarted mid-run and invalidated
 * the JWT), a 5xx, or a transport error mean the PROBE broke, not that the
 * agent is missing — so `agentExists` THROWS, failing the test with the real
 * diagnosis instead of skipping under a false "agent not found" reason.
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
    // probe go out unauthenticated and 401 — which `agentExists` reports as a
    // LOUD failure, not a skip (the file being absent is an environment fault,
    // not evidence the fixture agent is missing).
    return undefined
  }
}

/**
 * Does this agent exist on the stack under test?
 *
 * `false` means exactly one thing: the stack answered a definitive 404
 * ("no such agent", the uniform Invariant #8 shape). Every other failure —
 * 401/403 (probe unauthenticated: setup didn't run, or the JWT died with a
 * backend restart), 5xx, or a transport error — THROWS, because collapsing it
 * into `false` would make the caller skip under a false "agent not found"
 * reason: the `circuit-breaker-badge` defect wearing a new hat.
 *
 * @param {string} name      agent name to probe
 * @param {object} opts
 * @param {string} opts.baseURL  Playwright `baseURL` fixture
 * @param {string} [opts.token]  bearer token; falls back to the storageState
 *                               harvest when omitted (specs that already mint
 *                               their own admin token should pass it in)
 * @returns {Promise<boolean>} true = exists; false = definitive 404
 * @throws when the probe itself broke — the test must FAIL, not skip
 */
export async function agentExists(name, { baseURL, token } = {}) {
  const bearer = token || tokenFromStorageState()
  const api = await request.newContext({
    baseURL,
    extraHTTPHeaders: bearer ? { Authorization: `Bearer ${bearer}` } : {},
  })
  try {
    let resp
    try {
      resp = await api.get(`/api/agents/${name}`)
    } catch (err) {
      throw new Error(
        `agent probe for '${name}' could not reach the stack at ${baseURL}: ` +
          `${err.message} — a broken probe must fail loudly, never skip (#2199)`
      )
    }
    if (resp.ok()) return true
    if (resp.status() === 404) return false
    throw new Error(
      `agent probe for '${name}' broke: HTTP ${resp.status()} from ` +
        `GET /api/agents/${name}` +
        (bearer
          ? ''
          : ' (no bearer token — did the setup project write e2e/.auth/admin.json?)') +
        ` — auth/stack fault, not a missing fixture; refusing to skip (#2199)`
    )
  } finally {
    await api.dispose()
  }
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
