/**
 * Enterprise feature-flags store (#847 Phase 0).
 *
 * Caches the list of entitled enterprise features fetched from
 * `GET /api/settings/feature-flags` (`enterprise_features` field).
 * Consumed by:
 *   - NavBar.vue + route guards — hide enterprise tabs when not entitled
 *   - Enterprise views — show "feature not licensed" hint
 *
 * The fetch is gated by `featureFlagsLoaded` so the network call fires
 * once per page load. Force-refresh available via `force = true`.
 *
 * Why a separate store (not extending `sessions.js` or `auth.js`):
 *   - Enterprise flags are cross-cutting — they affect navigation,
 *     not chat sessions or auth specifically.
 *   - Allows the enterprise submodule to bind to this store cleanly
 *     when it loads (`useEnterpriseStore()` from inside enterprise/...).
 */
import { defineStore } from 'pinia'
import axios from 'axios'
import { useAuthStore } from './auth'

export const useEnterpriseStore = defineStore('enterprise', {
  state: () => ({
    featureFlagsLoaded: false,
    enterpriseFeatures: [],   // e.g. ['sso', 'scim', 'siem']
  }),

  getters: {
    isEntitled: (state) => (featureId) => state.enterpriseFeatures.includes(featureId),
    hasAnyEnterprise: (state) => state.enterpriseFeatures.length > 0,
  },

  actions: {
    async loadFeatureFlags(force = false) {
      if (this.featureFlagsLoaded && !force) return
      const authStore = useAuthStore()
      // Only fetch if authenticated — endpoint requires a JWT.
      if (!authStore.isAuthenticated) {
        this.enterpriseFeatures = []
        this.featureFlagsLoaded = true
        return
      }
      try {
        const r = await axios.get('/api/settings/feature-flags', {
          headers: authStore.authHeader,
        })
        this.enterpriseFeatures = Array.isArray(r.data?.enterprise_features)
          ? r.data.enterprise_features
          : []
      } catch (e) {
        console.warn('[enterprise] failed to load feature-flags:', e?.message || e)
        this.enterpriseFeatures = []
      } finally {
        this.featureFlagsLoaded = true
      }
    },

    // Test-only seam.
    _setFeaturesForTest(features) {
      this.enterpriseFeatures = features
      this.featureFlagsLoaded = true
    },
  },
})
