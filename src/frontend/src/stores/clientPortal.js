/**
 * Client Portal store (enterprise `client_portal`, epic #78 / #79).
 *
 * Domain store for the client-facing portal surface. First slice: the
 * "My Agents" roster (agents shared with the signed-in email) + the operator
 * exposure config. Backed by the gated `/api/enterprise/client-portal/*`
 * endpoints — 404 in OSS/unentitled builds, but the route guard
 * (`requiresEntitlement: 'client_portal'`) keeps this store off those builds.
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

export const useClientPortalStore = defineStore('clientPortal', {
  state: () => ({
    clientEmail: null,
    agents: [],
    loading: false,
    error: null,
  }),

  actions: {
    async fetchRoster() {
      this.loading = true
      this.error = null
      try {
        const authStore = useAuthStore()
        const { data } = await axios.get('/api/enterprise/client-portal/my-agents', {
          headers: authStore.authHeader,
        })
        this.clientEmail = data.client_email || null
        this.agents = data.agents || []
      } catch (err) {
        this.error = err.response?.data?.detail || 'Failed to load your agents.'
        this.agents = []
      } finally {
        this.loading = false
      }
    },
  },
})
