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

// Teardown is synchronous and serial too, and each member stops + removes a
// container, so it gets the same budget for the same reason. A timeout here is
// worse than on deploy: the server keeps deleting, and the user cannot tell how
// far it got — which is why `teardownOutcomeUnknown` exists and offers no retry.
const TEARDOWN_TIMEOUT_MS = 300000

/**
 * The entitlement id the teardown surface is gated on (ent#454).
 *
 * The verb is an entitlement-gated module: absent (404) on an OSS build,
 * 403 when mounted-but-unlicensed. The panel renders only when this id is in
 * `enterprise_features`, so a user never meets a control whose backend is not
 * there.
 */
export const TEARDOWN_FEATURE_ID = 'system_teardown'

function teardownUrl (name, dryRun) {
  return `/api/enterprise/system-teardown/${encodeURIComponent(name)}`
    + `?dry_run=${dryRun ? 'true' : 'false'}`
}

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
 *
 * `verb` selects the two sentences that are about WHAT was in flight (ent#454).
 * Everything else here is verb-independent, which is why this is one function —
 * but "deployment may still be running, and re-deploying creates duplicates" is
 * actively misleading at the worst moment of a teardown, where the in-flight
 * work is removal and the advice is the opposite.
 */
const UNKNOWN_OUTCOME_COPY = {
  deploy: {
    timeout: 'The request timed out. Deployment may still be running on the server — '
      + 'refresh before trying again, because re-deploying creates duplicate agents.',
    server: status => `The server returned HTTP ${status}. Some agents may already `
      + 'have been created — refresh before trying again.'
  },
  teardown: {
    timeout: 'The request timed out. Removal is serial and may still be running on the '
      + 'server — check your agent list before doing anything else, because the '
      + 'members removed so far are already gone.',
    server: status => `The server returned HTTP ${status}. Some members may already `
      + 'have been removed — check your agent list before trying again.'
  },
  // A dry run writes nothing, so neither sentence may imply work in flight.
  'teardown-preview': {
    timeout: 'The preview timed out. Nothing was removed — try previewing again.',
    server: status => `The server returned HTTP ${status} while previewing. Nothing `
      + 'was removed.'
  }
}

export function normalizeError (err, verb = 'deploy') {
  const res = err?.response
  const copy = UNKNOWN_OUTCOME_COPY[verb] || UNKNOWN_OUTCOME_COPY.deploy

  if (!res) {
    // Timeout, abort, or network failure. Cancelling the request does NOT cancel
    // the server, which may still be working — see `outcomeUnknown`.
    const timedOut = err?.code === 'ECONNABORTED' || /timeout/i.test(err?.message || '')
    return {
      kind: 'unknown-outcome',
      message: timedOut
        ? copy.timeout
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

  // A 500 with no usable body can still have happened AFTER the work began.
  if (res.status >= 500) {
    return { kind: 'unknown-outcome', message: copy.server(res.status) }
  }
  return { kind: 'invalid', message: `HTTP ${res.status}` }
}

/**
 * Positive confirmation that a name really belongs to this system: the server
 * found a deploy tag for it (`tag`), or a tag AND the name prefix (`both`).
 * `prefix` means "matched by name only" — the server cannot tell whether that
 * agent belongs to this system or a sibling one.
 *
 * ONE home for the rule, imported by both the store's default selection and
 * the panel's "matched by name only" badge. They were two copies of the same
 * predicate written in opposite polarities, which is two chances to disagree
 * about a value neither anticipated — and they did disagree: a member with no
 * `evidence` at all was pre-ticked by the store AND left unbadged by the panel,
 * so it was swept into a delete with no signal at all.
 *
 * An allowlist, deliberately. The consumer DELETES, so an unrecognised value
 * must fall to the safe side.
 */
export const CONFIRMED_EVIDENCE = Object.freeze(['tag', 'both'])
export const isConfirmedMember = (m) => CONFIRMED_EVIDENCE.includes(m?.evidence)

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

  // --- teardown (ent#454) ---------------------------------------------------
  // Deliberately a second set of refs rather than a shared `preview`/`result`
  // pair: install and remove are two flows a user can have half-finished at
  // once on the same page, and one shared slot would let a deploy result
  // silently replace a teardown preview the user is still reading.
  const teardownName = ref('')
  // The exact system name `teardownPreview` was produced for. A preview of
  // `acme` says nothing about `acme-2`, and the removal set is the whole point
  // — so the name is bound the same way `previewedText` binds the manifest.
  const teardownPreviewedName = ref(null)
  const teardownPreview = ref(null)
  const teardownResult = ref(null)
  const teardownError = ref(null)
  // Set when the outcome genuinely is not known (timeout / bare 5xx). Agents
  // may already be gone, so this is NOT an invitation to retry.
  const teardownOutcomeUnknown = ref(null)
  const isPreviewingTeardown = ref(false)
  const isTearingDown = ref(false)

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

  // --- teardown computed ----------------------------------------------------
  const teardownPreviewIsCurrent = computed(
    () => teardownPreview.value !== null
      && teardownPreviewedName.value === teardownName.value.trim()
  )

  /**
   * Membership the server could not verify blocks the verb in the UI too.
   *
   * The backend refuses this with a 503 regardless, so this is not the
   * enforcement — it is the UI not offering a button whose only outcome is a
   * refusal (principle 15: never optimistic).
   */
  const teardownMembershipUnverified = computed(
    () => teardownPreview.value !== null
      && teardownPreview.value.membership_verified === false
  )

  /**
   * The members that start TICKED on a fresh preview — everything the server
   * could confirm, and nothing it could not.
   *
   * A member whose `evidence` is `prefix` matched this system's NAME with no
   * deploy tag behind it: the server is saying it cannot tell whether that
   * agent belongs here. Pre-ticking it makes the escape hatch an opt-OUT on a
   * verb that deletes, and #2373's ranking — under-capture is cheaper than
   * over-capture — inverts for exactly that verb, which is why teardown
   * refuses rather than degrades everywhere else.
   *
   * The 503 refusal does NOT already cover this. It fires only when the tag
   * read FAILED (`membership_verified: false`, Remove disabled). A `prefix`
   * member also appears in the healthy state — tags read fine, that one agent
   * simply has no tag row — where membership is verified, Remove is ENABLED,
   * and the unconfirmable agent would have arrived pre-ticked.
   *
   * It lives here rather than in the panel's watcher because the store is the
   * cheaper place to execute it: a node-environment spec drives it directly,
   * with no mount. (`vitest.config.js` pins `environment: 'node'` only as the
   * DEFAULT — a spec opts into jsdom per file and mounts, which 22 specs
   * already do — so the rule is testable either way; it is the store version
   * that is testable without one.)
   *
   * The predicate is an ALLOWLIST of positive confirmation, never `!== 'prefix'`.
   * `evidence` is free-form text off the wire, and the one verb this feeds
   * DELETES: a member arriving with `evidence` absent, null, or set to some
   * future fourth value must fall to the SAFE side — unticked — rather than
   * being pre-ticked by a denylist that has never heard of it. The producer
   * emits exactly `tag` / `both` / `prefix` today (`system_service.py`), and
   * only the first two are a deploy tag confirming the member.
   */
  const teardownDefaultSelection = computed(
    () => (teardownPreview.value?.members || [])
      .filter((m) => isConfirmedMember(m))
      .map((m) => m.name)
  )

  /**
   * The members left unticked by the rule above, so the panel can say why the
   * selection is short rather than letting it read as a miscount. Defined by
   * SUBTRACTION from the rule above, so the two cannot disagree about a value
   * neither was written for — the pair partitions the roster by construction.
   */
  const teardownUnconfirmedMembers = computed(
    () => (teardownPreview.value?.members || [])
      .filter((m) => !isConfirmedMember(m))
      .map((m) => m.name)
  )

  // --- internal ------------------------------------------------------------
  // Clears the preview PAYLOAD only. `previewedText` deliberately survives: it is
  // the "something has been previewed" marker that lets the UI tell "you have not
  // previewed yet" apart from "your preview is stale". Clearing both made the
  // stale-preview hint unreachable dead code, so an edit after a successful
  // preview told the user to "Preview first" — as if they never had.
  //
  // It cannot re-enable Deploy on its own: `previewIsCurrent` requires a non-null
  // `preview` too, and that IS cleared here.
  function invalidatePreview () {
    preview.value = null
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
    // Start-over is the one path that also drops the marker: in the new context
    // nothing has been previewed, so "Preview first" is the honest hint again.
    previewedText.value = null
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
      // Every remaining kind is just a failure here, `unknown-outcome` included:
      // a dry run creates nothing, so there is no in-flight work to warn about
      // and nothing to make a retry unsafe. Only `deploy` needs that distinction.
      error.value = normalized.message
      return null
    } finally {
      isLoading.value = false
    }
  }

  function setTeardownName (name) {
    teardownName.value = name || ''
    // Any change to the target invalidates the preview PAYLOAD; the
    // previewed-name marker survives so the UI can tell "not previewed yet"
    // from "previewed a different system" (the `previewedText` rule).
    teardownPreview.value = null
    teardownError.value = null
    teardownResult.value = null
    teardownOutcomeUnknown.value = null
  }

  function resetTeardown () {
    teardownName.value = ''
    teardownPreview.value = null
    teardownPreviewedName.value = null
    teardownResult.value = null
    teardownError.value = null
    teardownOutcomeUnknown.value = null
  }

  /**
   * Dry run: the complete removal set, zero writes server-side.
   *
   * Sends NO body — `agents` is only meaningful on execute, and a preview that
   * pre-filtered by a confirmed set would be previewing something other than
   * the system.
   */
  async function previewTeardown () {
    const name = teardownName.value.trim()
    if (!name) {
      teardownError.value = 'Enter the name of the system to remove.'
      return null
    }
    isPreviewingTeardown.value = true
    teardownError.value = null
    teardownPreview.value = null
    teardownResult.value = null
    teardownOutcomeUnknown.value = null
    try {
      const response = await api.delete(teardownUrl(name, true))
      teardownPreview.value = response.data
      teardownPreviewedName.value = name
      return response.data
    } catch (err) {
      const normalized = normalizeError(err, 'teardown-preview')
      // A dry run creates nothing, so `unknown-outcome` carries no in-flight
      // work to warn about here — it is just a failure, as on `dryRun()`.
      teardownError.value = normalized.message
      return null
    } finally {
      isPreviewingTeardown.value = false
    }
  }

  /**
   * Execute, against an explicit confirmed list.
   *
   * `agents` is always sent, even when the user checked everything: the server
   * intersects it with freshly re-resolved membership, so sending it is what
   * makes the confirmation mean something — a member that appeared since the
   * preview is reported `not_confirmed` instead of being swept in.
   */
  async function teardown (agents) {
    const name = teardownName.value.trim()
    if (!name) {
      teardownError.value = 'Enter the name of the system to remove.'
      return null
    }
    isTearingDown.value = true
    teardownError.value = null
    teardownResult.value = null
    teardownOutcomeUnknown.value = null
    try {
      const response = await api.delete(teardownUrl(name, false), {
        // Axios carries a DELETE body under `data`.
        data: { agents: agents ?? null, strict: false },
        timeout: TEARDOWN_TIMEOUT_MS
      })
      teardownResult.value = response.data
      return response.data
    } catch (err) {
      const normalized = normalizeError(err, 'teardown')
      if (normalized.kind === 'result') {
        // status === 'failed' at HTTP 500, body IS the report. Discarding it
        // would discard every per-member reason — the only actionable output.
        teardownResult.value = normalized.data
        return normalized.data
      }
      if (normalized.kind === 'unknown-outcome') {
        teardownOutcomeUnknown.value = normalized.message
      } else {
        teardownError.value = normalized.message
      }
      return null
    } finally {
      isTearingDown.value = false
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
    // teardown state (ent#454)
    teardownName,
    teardownPreviewedName,
    teardownPreview,
    teardownResult,
    teardownError,
    teardownOutcomeUnknown,
    isPreviewingTeardown,
    isTearingDown,
    // computed
    previewIsCurrent,
    previewHasBlockers,
    needsAcknowledgement,
    canDeploy,
    teardownPreviewIsCurrent,
    teardownMembershipUnverified,
    teardownDefaultSelection,
    teardownUnconfirmedMembers,
    // actions
    setManifestText,
    reset,
    fetchBundled,
    loadBundled,
    dryRun,
    deploy,
    setTeardownName,
    resetTeardown,
    previewTeardown,
    teardown
  }
})
