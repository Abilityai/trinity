import { defineStore } from 'pinia'
import api from '../api'

/**
 * Calling agents on other Trinity instances (abilityai/trinity-enterprise#862):
 * this instance's AAuth switch + issuer, and the platform-wide outbound A2A
 * endpoint registry `call_a2a_agent` resolves against (#736).
 *
 * Admin-only surfaces; callers gate on admin before `load*()`. Each half keeps
 * its own `*Loaded` latch and error, so a failed fetch renders LoadFailed and a
 * failed refresh keeps the data on screen (loading means "no data yet").
 */
function detailOf(e, fallback) {
  const d = e?.response?.data?.detail
  if (typeof d === 'string') return d
  if (d?.message) return d.message
  if (d?.error) return d.error
  return fallback
}

export const useA2aFederationStore = defineStore('a2aFederation', {
  state: () => ({
    aauth: null,
    aauthLoaded: false,
    aauthLoading: false,
    aauthError: '',
    endpoints: [],
    outboundEnabled: false,
    endpointsLoaded: false,
    endpointsLoading: false,
    endpointsError: '',
  }),

  actions: {
    async loadAAuth() {
      this.aauthLoading = true
      try {
        const { data } = await api.get('/api/settings/aauth')
        this.aauth = data
        this.aauthError = ''
        this.aauthLoaded = true
      } catch (e) {
        this.aauthError = detailOf(e, "Couldn't load the AAuth settings.")
      } finally {
        this.aauthLoading = false
      }
    },

    /** The AAuth checklist reads the endpoint registry; refresh it in place. */
    async refreshReadiness() {
      if (this.aauthLoaded) await this.loadAAuth()
    },

    /** PUT only the changed fields; throws with a readable message on refusal. */
    async saveAAuth(body) {
      try {
        const { data } = await api.put('/api/settings/aauth', body)
        this.aauth = data
        return data
      } catch (e) {
        throw new Error(detailOf(e, "Couldn't save the AAuth settings."))
      }
    },

    async loadEndpoints() {
      this.endpointsLoading = true
      try {
        const { data } = await api.get('/api/settings/a2a-endpoints')
        this.endpoints = Array.isArray(data?.endpoints) ? data.endpoints : []
        this.outboundEnabled = Boolean(data?.enabled)
        this.endpointsError = ''
        this.endpointsLoaded = true
      } catch (e) {
        this.endpointsError = detailOf(e, "Couldn't load the outbound endpoints.")
      } finally {
        this.endpointsLoading = false
      }
    },

    async upsertEndpoint(body) {
      try {
        const { data } = await api.put('/api/settings/a2a-endpoints', body)
        await this.loadEndpoints()
        await this.refreshReadiness()
        return data
      } catch (e) {
        throw new Error(detailOf(e, "Couldn't save the endpoint."))
      }
    },

    async removeEndpoint(ref) {
      try {
        await api.delete(`/api/settings/a2a-endpoints/${encodeURIComponent(ref)}`)
        await this.loadEndpoints()
        await this.refreshReadiness()
      } catch (e) {
        throw new Error(detailOf(e, "Couldn't remove the endpoint."))
      }
    },

    /** The outbound kill switch (`a2a_outbound_enabled`, §32.5 FR-11). */
    async setOutboundEnabled(enabled) {
      try {
        await api.put('/api/settings/a2a_outbound_enabled', { value: enabled ? 'true' : 'false' })
        this.outboundEnabled = Boolean(enabled)
        await this.refreshReadiness()
      } catch (e) {
        throw new Error(detailOf(e, "Couldn't change outbound calling."))
      }
    },
  },
})
