import { defineStore } from 'pinia'
import api from '../api'

/**
 * Tier-2 opt-in fleet telemetry sharing (ent#12, extended by ent#437).
 *
 * The consent + status for the anonymized-aggregate sharing channel. Default-off,
 * reversible. All HTTP goes through the shared api.js client (Invariant #7).
 *
 * The `payload_preview` returned by GET is the EXACT anonymized aggregate that
 * would be sent — surfaced in the Settings panel and behind the consent card's
 * "See what would be sent" so the operator inspects what leaves the box before
 * consenting. Coarse counts only; never any PII.
 *
 * ent#437: TWO latches. `loaded` covers the status (share id, dismissal, the
 * last five sends); `previewLoaded` covers the aggregate itself, which the card
 * fetches lazily with `?preview=0` first. One latch would let the card's
 * preview-less load make the Settings panel show "(load to preview)" forever.
 * Every caller gates on admin BEFORE calling `load()`: the GET is admin + human
 * only and a non-admin call is a 403 the store would faithfully surface.
 */
export const useTelemetrySharingStore = defineStore('telemetrySharing', {
  state: () => ({
    loaded: false,
    previewLoaded: false,
    loading: false,
    saving: false,
    status: {
      enabled: false,
      hard_disabled: false,
      consent_at: null,
      backfill_days: 30,
      last_shared_at: null,
      share_url: '',
      interval_hours: 24,
      schema_version: 2,
      sharing_id: null,
      dismissed_at: null,
      first_value_at: null,
      backfill_delivered_at: null,
      recent_sends: [],
      receiver_hint: null,
    },
    payloadPreview: null,
    error: '',
  }),

  actions: {
    /**
     * Load the status, and the preview when asked. `preview: false` is the
     * cheap read (no aggregate build); a later `preview: true` refetches even
     * when the status is already loaded, because the preview is what's missing.
     */
    async load({ preview = true, force = false } = {}) {
      const needStatus = force || !this.loaded
      const needPreview = preview && (force || !this.previewLoaded)
      if (!needStatus && !needPreview) return
      this.loading = true
      this.error = ''
      try {
        const r = await api.get('/api/settings/telemetry-sharing', {
          params: { preview: preview ? 1 : 0 },
        })
        const { payload_preview, ...status } = r.data || {}
        this.status = { ...this.status, ...status }
        if (preview) {
          this.payloadPreview = payload_preview || null
          this.previewLoaded = true
        }
        this.loaded = true
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to load sharing status.'
        // A failed status read still counts as a completed attempt: the
        // consumers render LoadFailed rather than a loading state forever.
        this.loaded = true
      } finally {
        this.loading = false
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
        // The preview is keyed on the share id, which consent (re)mints.
        this.previewLoaded = false
        return true
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to update sharing.'
        return false
      } finally {
        this.saving = false
      }
    },

    /** "Don't ask again" — the server-side, once-per-install marker (ent#437). */
    async dismissAsk() {
      this.saving = true
      this.error = ''
      try {
        const r = await api.post('/api/settings/telemetry-sharing/ask/dismiss')
        this.status = { ...this.status, ...(r.data || {}) }
        return true
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to save your choice.'
        return false
      } finally {
        this.saving = false
      }
    },
  },
})
