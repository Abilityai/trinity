import { defineStore } from 'pinia'
import api from '../api'

/**
 * Tier-2 opt-in fleet telemetry sharing (ent#12).
 *
 * The consent + status for the anonymized-aggregate sharing channel. Default-off,
 * reversible. All HTTP goes through the shared api.js client (Invariant #7).
 *
 * The `payload_preview` returned by GET is the EXACT anonymized aggregate that
 * would be sent — surfaced in the Settings panel so the operator inspects what
 * leaves the box before consenting. Coarse counts only; never any PII.
 */
export const useTelemetrySharingStore = defineStore('telemetrySharing', {
  state: () => ({
    loaded: false,
    saving: false,
    status: {
      enabled: false,
      hard_disabled: false,
      consent_at: null,
      backfill_days: 30,
      last_shared_at: null,
      share_url: '',
      interval_hours: 24,
    },
    payloadPreview: null,
    error: '',
  }),

  actions: {
    async load(force = false) {
      if (this.loaded && !force) return
      this.error = ''
      try {
        const r = await api.get('/api/settings/telemetry-sharing')
        const { payload_preview, ...status } = r.data || {}
        this.status = { ...this.status, ...status }
        this.payloadPreview = payload_preview || null
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to load sharing status.'
      } finally {
        this.loaded = true
      }
    },

    async setConsent(enabled, backfillDays = null) {
      this.saving = true
      this.error = ''
      try {
        const body = { enabled }
        if (backfillDays != null) body.backfill_days = backfillDays
        const r = await api.put('/api/settings/telemetry-sharing', body)
        this.status = { ...this.status, ...(r.data || {}) }
        return true
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to update sharing.'
        return false
      } finally {
        this.saving = false
      }
    },
  },
})
