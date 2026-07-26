import { defineStore } from 'pinia'
import api from '../api'

/**
 * Local product-event capture — activation funnel, Tier-1 (ent#184).
 *
 * Fire-and-forget beacons for the OSS, local-only, zero-egress instrumentation
 * layer. The only caller today is the onboarding wizard, which records its step
 * transitions so the operator can see where their own first-run users drop off.
 *
 * Design:
 *   - NEVER blocks or breaks the UI: `record()` swallows every error. A failed
 *     beacon is invisible by design (mirrors the agent heartbeat philosophy).
 *   - Goes through the shared api.js client (Invariant #7).
 *   - `event_type` must be in the backend allow-list (routers/product_events.py).
 *     Keep this list in lockstep with `ALLOWED_EVENT_TYPES` there.
 *
 * First-value events (first_agent_created, first_chat, ...) are NOT emitted here
 * — they are derived server-side from audit_log/agent_activities by the
 * enterprise funnel view.
 */
export const useProductTelemetryStore = defineStore('productTelemetry', {
  actions: {
    /**
     * Record one local product event. Fire-and-forget — never awaited by the UI.
     * @param {string} eventType allow-listed event type
     * @param {object|null} context optional small metadata blob
     */
    record(eventType, context = null) {
      // Do not await — the wizard must never wait on (or fail from) a beacon.
      api
        .post('/api/product-events', { event_type: eventType, context })
        .catch((e) => {
          // Silent by design; a dropped beacon is not a user-facing failure.
          if (import.meta?.env?.DEV) {
            console.debug('[productTelemetry] beacon dropped:', eventType, e?.message || e)
          }
        })
    },
  },
})
