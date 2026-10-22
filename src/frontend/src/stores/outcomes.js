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
export const STATUSES = ['proposed', 'accepted', 'rejected', 'shipped']

export const useOutcomesStore = defineStore('outcomes', () => {
  const overview = ref(null)
  const proposals = ref([])
  const loading = ref(false)
  const hasLoaded = ref(false)
  const error = ref(null)
  const statusFilter = ref('proposed')
  const ownerFilter = ref('')
  const acting = ref('')           // proposal id with a verb in flight
  const actionError = ref(null)

  const projects = computed(() => overview.value?.projects || [])
  const fleet = computed(() => overview.value?.fleet || null)
  const counts = computed(() => overview.value?.proposal_counts || {})
  const owners = computed(() => [...new Set(proposals.value.map((p) => p.owner))].sort())
  const visibleProposals = computed(() =>
    ownerFilter.value ? proposals.value.filter((p) => p.owner === ownerFilter.value) : proposals.value)

  async function loadProposals() {
    const params = statusFilter.value ? { status: statusFilter.value } : {}
    const r = await api.get(PROPOSALS_URL, { params })
    proposals.value = r.data?.proposals || []
  }

  async function load() {
    loading.value = true
    try {
      const [ov] = await Promise.all([api.get(PROJECTS_URL), loadProposals()])
      overview.value = ov.data
      hasLoaded.value = true
      error.value = null
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

  const decide = (id, decision, reason = '') =>
    act(id, 'decision', { decision, ...(reason ? { reason } : {}) })
  const markShipped = (id) => act(id, 'shipped')

  return {
    overview, proposals, loading, hasLoaded, error, statusFilter, ownerFilter, acting, actionError,
    projects, fleet, counts, owners, visibleProposals,
    load, setStatus, setOwner, decide, markShipped,
    clearActionError: () => { actionError.value = null },
  }
})
