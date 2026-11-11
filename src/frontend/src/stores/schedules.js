/**
 * Schedule verbs added by #4281 slice F: taking a share holder's schedule over,
 * and the webhook read its review needs.
 *
 * Domain-scoped (Invariant #6) and on the shared API client (Invariant #7).
 * The rest of `SchedulesPanel.vue` still calls axios directly (plan §11.7);
 * this store holds only the new calls, and the in-flight flag lives here, not
 * in the panel.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const schedulePath = (agentName, scheduleId) =>
  `/api/agents/${encodeURIComponent(agentName)}/schedules/${encodeURIComponent(scheduleId)}`

export const useSchedulesStore = defineStore('schedules', () => {
  // The schedule id whose take-over is in flight, or null.
  const takingOver = ref(null)

  /**
   * `POST …/take-over` with `body` (`utils/scheduleRunTier.js::takeOverBody`).
   * Returns the schedule as it now reads; a refusal rethrows the request error
   * so the caller can read its `detail.code`.
   */
  async function takeOver (agentName, scheduleId, body) {
    takingOver.value = scheduleId
    try {
      const { data } = await api.post(`${schedulePath(agentName, scheduleId)}/take-over`, body)
      return data
    } finally {
      takingOver.value = null
    }
  }

  /** The schedule's webhook state (`has_token`, …); rethrows on failure. */
  async function fetchWebhookStatus (agentName, scheduleId) {
    const { data } = await api.get(`${schedulePath(agentName, scheduleId)}/webhook`)
    return data
  }

  return { takingOver, takeOver, fetchWebhookStatus }
})
