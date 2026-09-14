import { defineStore } from 'pinia'
import api from '../api'

/**
 * Operator intake — Settings surface (ent#463).
 *
 * The durable admin-only opt-in to "occasionally receive important security &
 * product updates". This is identified contact capture (email + optional
 * company/name/role/use_case) — NOT anonymous telemetry (that is ent#12).
 *
 * Three orthogonal state axes drive the panel:
 *   * `hard_disabled` — env kill (OPERATOR_INTAKE_ENABLED / DO_NOT_TRACK); wins
 *     over everything else
 *   * `already_submitted` (+ `submitted_at`) — has the at-most-once fired? A
 *     legacy install can report submitted=true with submitted_at=null; render
 *     "date unknown", never lie.
 *   * `enabled` (+ `consent_at`) — durable consent flag; opt-out is a durable
 *     decline and does NOT roll back a prior submission
 *
 * All HTTP goes through the shared api.js client (Invariant #7).
 */
export const useOperatorIntakeStore = defineStore('operatorIntake', {
  state: () => ({
    loaded: false,
    saving: false,
    status: {
      enabled: false,
      hard_disabled: false,
      already_submitted: false,
      submitted_at: null,
      consent_at: null,
      intake_url: '',
    },
    lastSubmitOutcome: null,
    error: '',
  }),

  actions: {
    async load(force = false) {
      if (this.loaded && !force) return
      this.error = ''
      try {
        const r = await api.get('/api/settings/operator-intake')
        this.status = { ...this.status, ...(r.data || {}) }
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to load intake status.'
      } finally {
        this.loaded = true
      }
    },

    /**
     * Set consent and optionally submit.
     *
     * `payload.enabled` — true to opt in, false to opt out.
     * On a fresh, hard-enabled install with `payload.enabled=true` and an
     * `email`, the backend fires the at-most-once submission as a background
     * task. Repeated submits are a no-op by design.
     */
    async setConsent(payload) {
      this.saving = true
      this.error = ''
      try {
        const r = await api.put('/api/settings/operator-intake', payload)
        const { submit_outcome, ...status } = r.data || {}
        this.status = { ...this.status, ...status }
        this.lastSubmitOutcome = submit_outcome || null
        return true
      } catch (e) {
        this.error = e?.response?.data?.detail || 'Failed to update intake.'
        return false
      } finally {
        this.saving = false
      }
    },
  },
})
