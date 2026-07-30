import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

/**
 * System manifest install surface (trinity-enterprise#126).
 *
 * Domain-scoped per Invariant #6 — deliberately NOT bolted onto
 * `systemViews.js`. A "System" here is a set of agents sharing a name prefix,
 * deployed from a manifest; a "System View" is a saved tag filter over agents.
 * Different domains that merely share a word.
 *
 * Goes through the single `api` axios instance (Invariant #7), not raw axios.
 */

// Deploy is fully synchronous server-side AND serial: the agent-create loop and
// the start loop both run one agent at a time, so wall time scales linearly with
// the fleet. `api`'s 30s default would abort a real 3-agent deploy, so this path
// gets its own budget (precedents: stores/agents.js 120s, views/Settings.vue 300s).
const DEPLOY_TIMEOUT_MS = 300000

/**
 * Collapse any axios failure into ONE renderable shape.
 *
 * `deploy_manifest` has six outcomes and the HTTP code alone identifies none of
 * them, so nothing here switches on the status code:
 *
 *   deployed/partial/valid/invalid  200  full report
 *   failed (0 agents created)       500  full report AS THE BODY   <-- trap
 *   parse / validation error        400  {detail: "<string>"}      <-- commonest
 *   request-model violation         422  {detail: [ {msg}, ... ]}  (a LIST)
 *   unexpected, possibly after
 *     agents already exist          500  {detail: "<string>"}
 *   client timeout, server
 *     keeps deploying               ---  no response at all
 *
 * The 500-with-a-report is the important one: a naive `catch` throws away exactly
 * the `failed[]` list AC #3 has to render. It is a RESULT, not an error, so it is
 * returned as `kind: 'result'` and rendered normally.
 */
export function normalizeError (err) {
  const res = err?.response

  if (!res) {
    // Timeout, abort, or network failure. Cancelling the request does NOT cancel
    // the server, which may still be creating agents — see `outcomeUnknown`.
    const timedOut = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
    return {
      kind: 'unknown-outcome',
      message: timedOut
        ? 'The request timed out. Deployment may still be running on the server — '
          + 'refresh before trying again, because re-deploying creates duplicate agents.'
        : (err?.message || 'Network error — the outcome is unknown.')
    }
  }

  // A structured report that happens to arrive with a 500.
  if (res.data && res.data.status === 'failed') {
    return { kind: 'result', data: res.data }
  }

  const detail = res.data?.detail

  // 422 from FastAPI: detail is a list of {loc, msg, type}.
  if (Array.isArray(detail)) {
    const msg = detail.map(e => e?.msg).filter(Boolean).join('; ')
    return { kind: 'invalid', message: msg || `HTTP ${res.status}` }
  }
  // Some handlers raise a dict detail (e.g. {error, ...}).
  if (detail && typeof detail === 'object') {
    return { kind: 'invalid', message: detail.error || JSON.stringify(detail) }
  }
  if (typeof detail === 'string' && detail) {
    return { kind: 'invalid', message: detail }
  }

  // A 500 with no usable body can still have happened AFTER agents were created.
  if (res.status >= 500) {
    return {
      kind: 'unknown-outcome',
      message: `The server returned HTTP ${res.status}. Some agents may already `
        + 'have been created — refresh before trying again.'
    }
  }
  return { kind: 'invalid', message: `HTTP ${res.status}` }
}

export const useSystemsStore = defineStore('systems', () => {
  // --- state ---------------------------------------------------------------
  const bundled = ref([])
  const bundledLoading = ref(false)

  const manifestText = ref('')
  // The exact text `preview` was produced from. `preview` is only meaningful for
  // this string, so any edit invalidates it — otherwise a user dry-runs A, edits
  // to B, and deploys B while reading A's preview.
  const previewedText = ref(null)
  const preview = ref(null)

  const deployResult = ref(null)
  // Set when the outcome genuinely is not known (timeout / bare 5xx). Distinct
  // from an error: work may be in flight, so a retry is NOT safe.
  const outcomeUnknown = ref(null)

  const isLoading = ref(false)
  const isDeploying = ref(false)
  const error = ref(null)

  // --- computed ------------------------------------------------------------
  const previewIsCurrent = computed(
    () => preview.value !== null && previewedText.value === manifestText.value
  )

  /** A manifest whose preview found blockers must not be deployable. */
  const previewHasBlockers = computed(
    () => (preview.value?.failed?.length || 0) > 0
  )

  /**
   * Deploying replaces the platform-wide trinity_prompt for every agent, and/or
   * starts recurring autonomous executions. Either needs explicit consent, not a
   * banner — so the UI gates Deploy on an acknowledgement when this is true.
   */
  const needsAcknowledgement = computed(() => {
    if (!preview.value) return false
    const enabledSchedules = (preview.value.schedules_preview || [])
      .filter(s => s.enabled).length
    return Boolean(preview.value.prompt_updated) || enabledSchedules > 0
  })

  // NOTE: the `_N`-duplicate warning split lives in ManifestPreview.vue, which
  // needs BOTH halves (duplicates get a confirm-grade panel, everything else a
  // notes list). Keeping a second copy of that heuristic here would be two
  // regexes to drift apart for no gain.

  const canDeploy = computed(
    () => previewIsCurrent.value && !previewHasBlockers.value && !isDeploying.value
  )

  // --- internal ------------------------------------------------------------
  function invalidatePreview () {
    preview.value = null
    previewedText.value = null
  }

  function setManifestText (text) {
    manifestText.value = text || ''
    // Any change to the source invalidates the preview, unconditionally.
    invalidatePreview()
    error.value = null
    deployResult.value = null
    outcomeUnknown.value = null
  }

  function reset () {
    manifestText.value = ''
    invalidatePreview()
    deployResult.value = null
    outcomeUnknown.value = null
    error.value = null
  }

  // --- actions -------------------------------------------------------------
  async function fetchBundled () {
    bundledLoading.value = true
    error.value = null
    try {
      const response = await api.get('/api/systems/manifests')
      bundled.value = response.data || []
    } catch (err) {
      const normalized = normalizeError(err)
      // 403 is expected below `creator` — the panel shows its own empty state
      // rather than an error, so keep the catalog empty and stay quiet.
      if (err?.response?.status !== 403) {
        error.value = normalized.message
      }
      bundled.value = []
    } finally {
      bundledLoading.value = false
    }
  }

  async function loadBundled (manifestId) {
    isLoading.value = true
    error.value = null
    try {
      const response = await api.get(
        `/api/systems/manifests/${encodeURIComponent(manifestId)}`
      )
      // Routed through setManifestText so the preview is invalidated on a source
      // switch exactly as it is on a keystroke.
      setManifestText(response.data?.manifest || '')
      return response.data
    } catch (err) {
      error.value = normalizeError(err).message
      return null
    } finally {
      isLoading.value = false
    }
  }

  async function dryRun () {
    const text = manifestText.value
    if (!text.trim()) {
      error.value = 'Paste, upload, or pick a manifest first.'
      return null
    }
    isLoading.value = true
    error.value = null
    // Never leave a previous manifest's preview on screen beside a new outcome.
    invalidatePreview()
    deployResult.value = null
    outcomeUnknown.value = null
    try {
      const response = await api.post('/api/systems/deploy', {
        manifest: text,
        dry_run: true
        // `strict` is deliberately not sent: the UI wants the best-effort,
        // report-everything behaviour (trinity-enterprise#125).
      })
      preview.value = response.data
      // Bind to the exact text previewed, not to whatever the box holds now.
      previewedText.value = text
      return response.data
    } catch (err) {
      const normalized = normalizeError(err)
      if (normalized.kind === 'result') {
        // A dry run cannot produce `failed` (it creates nothing), but if it ever
        // did, showing the report beats showing a generic error.
        preview.value = normalized.data
        previewedText.value = text
        return normalized.data
      }
      if (normalized.kind === 'unknown-outcome') {
        // A dry run has no side effects, so this is just a failure.
        error.value = normalized.message
      } else {
        error.value = normalized.message
      }
      return null
    } finally {
      isLoading.value = false
    }
  }

  async function deploy () {
    const text = manifestText.value
    if (!text.trim()) {
      error.value = 'Paste, upload, or pick a manifest first.'
      return null
    }
    isDeploying.value = true
    error.value = null
    deployResult.value = null
    outcomeUnknown.value = null
    try {
      const response = await api.post(
        '/api/systems/deploy',
        { manifest: text, dry_run: false },
        { timeout: DEPLOY_TIMEOUT_MS }
      )
      deployResult.value = response.data
      return response.data
    } catch (err) {
      const normalized = normalizeError(err)
      if (normalized.kind === 'result') {
        // status === 'failed' at HTTP 500, body IS the report. This is the whole
        // reason normalizeError exists.
        deployResult.value = normalized.data
        return normalized.data
      }
      if (normalized.kind === 'unknown-outcome') {
        outcomeUnknown.value = normalized.message
      } else {
        error.value = normalized.message
      }
      return null
    } finally {
      isDeploying.value = false
    }
  }

  return {
    // state
    bundled,
    bundledLoading,
    manifestText,
    previewedText,
    preview,
    deployResult,
    outcomeUnknown,
    isLoading,
    isDeploying,
    error,
    // computed
    previewIsCurrent,
    previewHasBlockers,
    needsAcknowledgement,
    canDeploy,
    // actions
    setManifestText,
    reset,
    fetchBundled,
    loadBundled,
    dryRun,
    deploy
  }
})
