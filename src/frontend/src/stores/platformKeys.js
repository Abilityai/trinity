import { defineStore } from 'pinia'
import api from '../api'

// ent#582 — the platform keys an admin configures from the first-run flow and
// Settings → Integrations: `GET /api/settings/api-keys` (masked status for
// anthropic / github / resend / gemini) plus each key's test → save → remove.
// One store so every key field shares one status read (Invariants #6/#7).
export const usePlatformKeysStore = defineStore('platformKeys', {
  state: () => ({
    status: {},        // provider -> { configured, masked, source, … }
    hasLoaded: false,  // latches true on the first successful read
    loadError: null,   // this attempt only
  }),

  actions: {
    async fetchStatus() {
      try {
        const { data } = await api.get('/api/settings/api-keys')
        this.status = data || {}
        this.hasLoaded = true
        this.loadError = null
      } catch (e) {
        this.loadError = e
      }
    },

    /** Live check; resolves to `{valid, error?, warning?, …}` — never stores. */
    async test(provider, body) {
      const { data } = await api.post(`/api/settings/api-keys/${provider}/test`, body)
      return data
    },

    async save(provider, body) {
      const { data } = await api.put(`/api/settings/api-keys/${provider}`, body)
      await this.fetchStatus()
      return data
    },

    async remove(provider) {
      const { data } = await api.delete(`/api/settings/api-keys/${provider}`)
      await this.fetchStatus()
      return data
    },
  },
})
