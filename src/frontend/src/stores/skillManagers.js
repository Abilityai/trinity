/**
 * Skill managers store (trinity-enterprise#596) — which agents may change
 * agents' skills.
 *
 * A separate domain from `skillSources` (where skills come FROM) and `skills`
 * (which skills an agent HOLDS): this is who may change the second. Every agent
 * absent from `holders` is refused when it tries to change any agent's skills,
 * its own included; people and the system agent never needed a grant, so they
 * never appear here.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

/** The backend's named refusal, verbatim — it says what is wrong and what to do. */
function refusalText (err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  return fallback
}

export const useSkillManagersStore = defineStore('skillManagers', () => {
  const holders = ref([])
  const hasLoaded = ref(false)
  const loadError = ref(null)
  const actionError = ref(null)
  // The agent whose grant is being changed — per-row, so one pending action
  // never disables the whole list.
  const busyAgent = ref(null)

  async function fetch () {
    loadError.value = null
    try {
      const res = await api.get('/api/skills/managers')
      holders.value = res.data.holders || []
      hasLoaded.value = true
    } catch (err) {
      loadError.value = refusalText(err, 'Could not load the skill managers.')
    }
  }

  async function setGranted (agentName, granted) {
    busyAgent.value = agentName
    actionError.value = null
    try {
      await api.put(`/api/agents/${encodeURIComponent(agentName)}/skill-manager`, { granted })
      await fetch()
      return true
    } catch (err) {
      actionError.value = refusalText(
        err, granted ? `Could not grant ${agentName}.` : `Could not revoke ${agentName}.`
      )
      return false
    } finally {
      busyAgent.value = null
    }
  }

  return { holders, hasLoaded, loadError, actionError, busyAgent, fetch, setGranted }
})
