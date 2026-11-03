/**
 * Terms acceptance (trinity-enterprise#1054): the text a Workspace-only person
 * accepts once per version before any Workspace route answers them.
 *
 * The server is the boundary — every route a gated person calls answers 403
 * `terms_required` until this store's `accept` succeeds. The two routes here
 * are exempt from that refusal, so reading and accepting can never be refused
 * by the gate they end.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const BASE = '/api/enterprise/user-management/terms'

export const useTermsStore = defineStore('terms', () => {
  /** `{ required, version, text, accepted }` from `GET /terms/current`. */
  const current = ref(null)
  const hasLoaded = ref(false)
  const loading = ref(false)
  const error = ref(null)

  async function load() {
    loading.value = true
    try {
      const { data } = await api.get(`${BASE}/current`)
      current.value = data
      hasLoaded.value = true
      error.value = null
    } catch (e) {
      error.value = e
    } finally {
      loading.value = false
    }
  }

  /** Accept `version` — the one the person read. Rejects with the server's error. */
  async function accept(version) {
    const { data } = await api.post(`${BASE}/accept`, { version })
    if (current.value) current.value = { ...current.value, version: data.version, accepted: true }
    return data
  }

  return { current, hasLoaded, loading, error, load, accept }
})
