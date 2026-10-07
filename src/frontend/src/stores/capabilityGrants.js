/**
 * Self-change permissions for ONE agent (trinity-enterprise#756) — the four
 * grants of trinity-enterprise#164, read and set from the agent's Settings.
 *
 * Domain-scoped (Invariant #6): `skillManagers` is the fleet-wide list of who
 * holds `skills.manage`; this is every capability for the agent on screen.
 * Holds the loading flags, so the panel decides nothing about fetch state.
 *
 * The grants read is owner-level (`OwnedAgentByName`): anyone else gets a 404,
 * which is `hidden`, not a failure. The PUT is admin-and-signed-in only; its
 * refusals (calibrating, system, ephemeral) are the backend's own sentences.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'
import { permissionRequests } from '../utils/capabilityGrants'

function refusalText (err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  return fallback
}

export const useCapabilityGrantsStore = defineStore('capabilityGrants', () => {
  const agentName = ref(null)
  const grants = ref([])
  const hasLoaded = ref(false)
  const loadError = ref('')
  const hidden = ref(false)
  const autonomyEnabled = ref(null)
  const requests = ref([])
  const requestsError = ref('')
  // Per-capability, so one pending write never disables the other three.
  const busy = ref(null)
  const actionErrors = ref({})
  let generation = 0

  function reset (name) {
    agentName.value = name
    grants.value = []
    hasLoaded.value = false
    loadError.value = ''
    hidden.value = false
    autonomyEnabled.value = null
    requests.value = []
    requestsError.value = ''
    actionErrors.value = {}
    busy.value = null
  }

  async function load (name) {
    if (name !== agentName.value) reset(name)
    const gen = ++generation
    const enc = encodeURIComponent(name)
    loadError.value = ''
    try {
      const res = await api.get(`/api/agents/${enc}/capability-grants`)
      if (gen !== generation) return
      grants.value = res.data?.grants || []
      hasLoaded.value = true
    } catch (err) {
      if (gen !== generation) return
      const status = err?.response?.status
      if (status === 404 || status === 403) {
        hidden.value = true
        return
      }
      loadError.value = refusalText(err, 'Could not load the permissions.')
      return
    }
    // Both reads are context, not the panel's subject: either failing leaves
    // the toggles usable and says so in place.
    const [autonomy, queue] = await Promise.allSettled([
      api.get(`/api/agents/${enc}/autonomy`),
      api.get('/api/operator-queue', { params: { agent_name: name, status: 'pending', limit: 200 } }),
    ])
    if (gen !== generation) return
    autonomyEnabled.value = autonomy.status === 'fulfilled'
      ? Boolean(autonomy.value.data?.autonomy_enabled) : null
    if (queue.status === 'fulfilled') {
      requests.value = permissionRequests(queue.value.data?.items)
      requestsError.value = ''
    } else {
      requestsError.value = refusalText(queue.reason, 'Could not check for open permission requests.')
    }
  }

  async function setGranted (capability, granted) {
    const name = agentName.value
    busy.value = capability
    actionErrors.value = { ...actionErrors.value, [capability]: '' }
    try {
      await api.put(
        `/api/agents/${encodeURIComponent(name)}/capability-grants/${encodeURIComponent(capability)}`,
        { granted },
      )
      await load(name)
      return true
    } catch (err) {
      actionErrors.value = {
        ...actionErrors.value,
        [capability]: refusalText(err, granted ? 'Could not grant it.' : 'Could not revoke it.'),
      }
      return false
    } finally {
      busy.value = null
    }
  }

  function clearActionError (capability) {
    actionErrors.value = { ...actionErrors.value, [capability]: '' }
  }

  return {
    agentName, grants, hasLoaded, loadError, hidden, autonomyEnabled, requests, requestsError,
    busy, actionErrors, load, setGranted, clearActionError,
  }
})
