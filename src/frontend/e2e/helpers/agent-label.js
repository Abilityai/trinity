import { request } from '@playwright/test'
import { tokenFromStorageState } from './agent-probe.js'

/**
 * Display-label fixture helpers (#2358).
 *
 * Two specs need the same thing: temporarily give an agent a display label,
 * assert the surface shows BOTH names, then put the agent back exactly as it
 * was. One helper rather than two copies, for the reason `agent-probe.js`
 * states about itself — a fixture pattern re-derived per spec drifts, and the
 * half that drifts here leaves a live agent wearing a test label.
 *
 * ⚠️ LOUD, NEVER SILENT. ⚠️
 * Every call throws with the real HTTP status on any non-2xx. A helper that
 * swallowed a 403 would make the caller assert against an unchanged surface and
 * report a green run for a test that never ran — the failure mode
 * `agent-probe.js` was written to kill.
 *
 * ⚠️ `trinity-system` HAS NO UI UNDO. ⚠️
 * `AgentHeader.vue` hides the label pencil for system agents, so a label
 * stranded on `trinity-system` can only be cleared through the API. Callers
 * must restore in `test.afterEach` (a `finally` inside a timed-out test body is
 * not reliably run) and must read the PRIOR label first, so a dev stack's own
 * label is restored rather than nulled.
 *
 * ⚠️ TWO SPEC FILES BORROW THE SAME AGENT. ⚠️
 * `dashboard-list-view.spec.js` and `dashboard-grid-view.spec.js` both borrow
 * whatever `pickLabelFixture` returns, and `mode: 'serial'` orders tests only
 * WITHIN a file. `playwright.config.js` sets `workers: 1` on CI but leaves it
 * at the default locally — which is exactly where both files are run together
 * (the #2358 verify step names them on one command line). Two workers, one
 * agent: the second one's "prior label" read can land after the first one's
 * borrow, and it would then faithfully restore the FIXTURE label as if it were
 * the operator's own — a permanent strand, on a live agent, from a green run.
 *
 * `FIXTURE_LABEL` is the sentinel that closes that: it is the only label these
 * helpers ever write, and `readLabel` REFUSES to return it. The late worker
 * therefore throws before it writes anything, its `afterEach` has nothing to
 * restore, and the worker that actually holds the borrow still puts the agent
 * back. The same refusal catches a run that was killed mid-borrow: the next
 * run finds the sentinel still on the agent and says so, instead of adopting a
 * test artefact as the value to restore forever. Run the two specs with
 * `--workers=1` when you want them both green in one pass.
 */

/**
 * The one label these helpers ever write.
 *
 * Deliberately self-identifying rather than a plausible operator label: if a
 * run is killed between the write and the restore, whoever finds it on a live
 * agent has to be able to tell at a glance that it is a test artefact and safe
 * to clear — on `trinity-system` they cannot even reach for the pencil. It
 * doubles as the sentinel `readLabel` refuses (see above), so a stranded label
 * is loud on the next run rather than inherited.
 */
export const FIXTURE_LABEL = 'Trinity e2e fixture - safe to clear'

async function apiContext(baseURL) {
  const token = tokenFromStorageState()
  return request.newContext({
    baseURL,
    extraHTTPHeaders: token ? { Authorization: `Bearer ${token}` } : {},
  })
}

/**
 * The agent's PRIOR label — the value a borrow must put back — or null.
 *
 * Throws rather than returning `FIXTURE_LABEL`: seeing the sentinel means a
 * borrow is already in flight in another worker, or a previous run was killed
 * before it restored. Returning it would make this caller restore a test
 * artefact as though it were the operator's own label, which is the one
 * failure here that is permanent.
 *
 * @returns {Promise<string|null>}
 */
export async function readLabel(baseURL, agent) {
  const api = await apiContext(baseURL)
  try {
    const res = await api.get(`/api/agents/${agent}/label`)
    if (!res.ok()) {
      throw new Error(
        `GET /api/agents/${agent}/label → HTTP ${res.status()}; the label fixture ` +
          `cannot run without knowing the prior value (did the setup project write ` +
          `e2e/.auth/admin.json?)`
      )
    }
    const label = (await res.json()).label ?? null
    if (label === FIXTURE_LABEL) {
      throw new Error(
        `${agent} already carries the fixture label "${FIXTURE_LABEL}". Either ` +
          `another worker is borrowing it right now — run the label specs with ` +
          `--workers=1 — or an earlier run was killed before restoring, in which ` +
          `case clear it with: PUT /api/agents/${agent}/label {"label": null}`
      )
    }
    return label
  } finally {
    await api.dispose()
  }
}

/**
 * Set (or, with `null`, clear) the agent's label.
 *
 * `label` is required-but-nullable server-side (#1821): an explicit null clears,
 * and an unrecognised body 422s rather than silently wiping.
 */
export async function writeLabel(baseURL, agent, label) {
  const api = await apiContext(baseURL)
  try {
    const res = await api.put(`/api/agents/${agent}/label`, { data: { label } })
    if (!res.ok()) {
      throw new Error(
        `PUT /api/agents/${agent}/label → HTTP ${res.status()}: ${await res.text()}`
      )
    }
  } finally {
    await api.dispose()
  }
}

/**
 * Which agent to borrow for a label test.
 *
 * Prefers one that ALREADY carries a label: on a real fleet that is an agent
 * whose owner has opted into labels, so the borrow is invisible to them once
 * restored. Falls back to `trinity-system`, the one agent every install has —
 * which is what CI runs against (its fleet is a single agent).
 *
 * @returns {Promise<string>}
 */
export async function pickLabelFixture(baseURL) {
  const api = await apiContext(baseURL)
  try {
    const res = await api.get('/api/agents')
    if (!res.ok()) {
      throw new Error(
        `GET /api/agents → HTTP ${res.status()}; cannot choose a label fixture`
      )
    }
    const agents = await res.json()
    const list = Array.isArray(agents) ? agents : agents.agents || []
    const labelled = list.find((a) => a && typeof a.display_label === 'string' && a.display_label.trim())
    return (labelled && labelled.name) || 'trinity-system'
  } finally {
    await api.dispose()
  }
}
