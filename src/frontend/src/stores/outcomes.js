/**
 * Outcomes — the Operations › Outcomes tab's domain store (trinity-enterprise#941;
 * Invariant #6/#7: store = domain, single API client).
 *
 * Reads and writes ONLY the eval_outcomes routes. The guard is the API (admin +
 * person + entitlement, `test_941_eval_outcomes_wiring.py`); Operations lists
 * the tab only for an admin on an entitled install, which mirrors it.
 */
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'
import { apiErrorMessage } from '../utils/apiError'

export const BASE = '/api/enterprise/eval-outcomes'
export const PROJECTS_URL = `${BASE}/projects`
export const PROPOSALS_URL = `${BASE}/proposals`
export const ANALYSES_URL = `${BASE}/analyses`
export const REGISTRIES_URL = `${BASE}/registries`
// The server's cap and shape (registry.py MAX_REPOS / _REPO_RE), checked here
// first so a typo is named before the round trip.
export const MAX_TRACKERS = 20
export const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

/** A registries read as the card renders it: a malformed body reads as "none". */
export function normalizeTrackers(d) {
  const list = (v) => (Array.isArray(v) ? v.filter((r) => typeof r === 'string') : [])
  return { repos: list(d?.repos), source: ['setting', 'env'].includes(d?.source) ? d.source : 'none',
    env_default: list(d?.env_default) }
}

/** Lines of a tracker draft → `{ repos, error }`; blank lines are ignored. */
export function parseTrackers(text) {
  const repos = []
  for (const line of String(text || '').split(/[\n,]/)) {
    const r = line.trim()
    if (!r) continue
    if (!REPO_RE.test(r)) return { repos, error: `"${r.slice(0, 100)}" is not owner/repo — for example acme/roadmap.` }
    if (!repos.includes(r)) repos.push(r)
  }
  if (repos.length > MAX_TRACKERS) return { repos, error: `At most ${MAX_TRACKERS} trackers.` }
  return { repos, error: null }
}
export const STATUSES = ['proposed', 'accepted', 'rejected', 'shipped']
// Weeks of history read for the trend: the current record and the ones before it.
export const HISTORY_WEEKS = 8

/** The record one week before `latest` in `records` (same scope), or null. */
export function priorOf(latest, records) {
  if (!latest) return null
  const before = (records || [])
    .filter((r) => r.scope_kind === latest.scope_kind && r.scope_ref === latest.scope_ref
      && r.window_start < latest.window_start)
    .sort((a, b) => (a.window_start < b.window_start ? 1 : -1))
  return before[0] || null
}

export const useOutcomesStore = defineStore('outcomes', () => {
  const overview = ref(null)
  const history = ref([])          // the last HISTORY_WEEKS records of every scope, newest first
  const proposals = ref([])
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const statusFilter = ref('proposed')
  const ownerFilter = ref('')
  const acting = ref('')           // proposal id with a verb in flight
  const actionError = ref(null)
  // The GitHub trackers the fleet read uses (`{repos, source, env_default}`).
  const trackers = ref(null)
  const trackersError = ref(null)
  const trackersSaving = ref(false)
  const trackersSaveError = ref(null)

  const projects = computed(() => overview.value?.projects || [])
  const fleet = computed(() => overview.value?.fleet || null)
  // Last week's records beside this week's, so the tiles and the project rows
  // can say which way each number moved.
  // The overview's rows are analysis records, so they carry their scope; the
  // defaults make the match hold for a row that does not.
  const priorFleet = computed(() => fleet.value
    && priorOf({ scope_kind: 'fleet', scope_ref: 'fleet', ...fleet.value }, history.value))
  const priorByProject = computed(() => Object.fromEntries(
    projects.value.map((p) => [p.scope_ref, priorOf({ scope_kind: 'project', ...p }, history.value)])))
  // The fleet's checks-passed-style series for the trend line: oldest first.
  const fleetHistory = computed(() => history.value
    .filter((r) => r.scope_kind === 'fleet')
    .sort((a, b) => (a.window_start < b.window_start ? -1 : 1)))
  const counts = computed(() => overview.value?.proposal_counts || {})
  const owners = computed(() => [...new Set(proposals.value.map((p) => p.owner))].sort())
  const visibleProposals = computed(() =>
    ownerFilter.value ? proposals.value.filter((p) => p.owner === ownerFilter.value) : proposals.value)

  async function loadProposals() {
    const params = statusFilter.value ? { status: statusFilter.value } : {}
    const r = await api.get(PROPOSALS_URL, { params })
    proposals.value = r.data?.proposals || []
  }

  // History is context, not the reading: a failed read leaves the deltas at
  // "—" and the current numbers on screen, with no banner.
  async function loadHistory(ov) {
    const n = 1 + (ov?.projects?.length || 0)
    try {
      const r = await api.get(ANALYSES_URL, { params: { limit: Math.min(200, HISTORY_WEEKS * n) } })
      history.value = r.data?.analyses || []
    } catch {
      history.value = []
    }
  }

  async function load() {
    loading.value = true
    try {
      const [ov] = await Promise.all([api.get(PROJECTS_URL), loadProposals()])
      overview.value = ov.data
      hasLoaded.value = true
      error.value = null
      await loadHistory(ov.data)
    } catch (e) {
      error.value = apiErrorMessage(e, "Couldn't load outcomes")
    } finally {
      loading.value = false
    }
  }

  async function setStatus(status) {
    statusFilter.value = status
    ownerFilter.value = ''
    try {
      await loadProposals()
      error.value = null
    } catch (e) {
      error.value = apiErrorMessage(e, "Couldn't load proposals")
    }
  }

  function setOwner(owner) {
    ownerFilter.value = owner
  }

  async function act(id, path, body) {
    acting.value = id
    actionError.value = null
    try {
      await api.post(`${PROPOSALS_URL}/${id}/${path}`, body)
      await load()
      return true
    } catch (e) {
      actionError.value = apiErrorMessage(e, "That didn't go through")
      return false
    } finally {
      acting.value = ''
    }
  }

  async function loadTrackers() {
    try {
      const r = await api.get(REGISTRIES_URL)
      trackers.value = normalizeTrackers(r.data)
      trackersError.value = null
    } catch (e) {
      trackersError.value = apiErrorMessage(e, "Couldn't load the tracker list")
    }
  }

  async function writeTrackers(call) {
    trackersSaving.value = true
    trackersSaveError.value = null
    try {
      const r = await call()
      trackers.value = normalizeTrackers(r.data)
      return true
    } catch (e) {
      trackersSaveError.value = apiErrorMessage(e, "The tracker list wasn't saved")
      return false
    } finally {
      trackersSaving.value = false
    }
  }

  const saveTrackers = (repos) => writeTrackers(() => api.put(REGISTRIES_URL, { repos }))
  const resetTrackers = () => writeTrackers(() => api.delete(REGISTRIES_URL))

  const decide = (id, decision, reason = '') =>
    act(id, 'decision', { decision, ...(reason ? { reason } : {}) })
  const markShipped = (id) => act(id, 'shipped')

  return {
    overview, history, proposals, loading, hasLoaded, error, statusFilter, ownerFilter, acting, actionError,
    projects, fleet, priorFleet, priorByProject, fleetHistory, counts, owners, visibleProposals,
    trackers, trackersError, trackersSaving, trackersSaveError,
    load, setStatus, setOwner, decide, markShipped, loadTrackers, saveTrackers, resetTrackers,
    clearActionError: () => { actionError.value = null },
    clearTrackersSaveError: () => { trackersSaveError.value = null },
  }
})
