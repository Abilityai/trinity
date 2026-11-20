/**
 * Flight Deck store (abilityai/trinity-enterprise#1307).
 *
 * Holds only what the Dashboard's own store does not: the host's load and the
 * two execution history strips. Agents, working state and the event list come
 * from `stores/network.js` — the deck reads the same access-scoped, filtered
 * set the Dashboard draws (`visibleAgents`), never a second copy.
 *
 * Each source keeps its last good value when a refresh fails: one broken read
 * must not blank the screen. `lastOkAt` is what the page calls stale from.
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '@/api'

const PRIVACY_KEY = 'trinity-flight-deck-show-text'
const HOST_EVERY_MS = 5000
const HISTORY_EVERY_MS = 300000
const HOST_HISTORY = 90

function readShowText() {
  try {
    return localStorage.getItem(PRIVACY_KEY) === '1'
  } catch {
    return false
  }
}

export const useFlightDeckStore = defineStore('flightDeck', () => {
  const host = ref(null)            // last good GET /api/telemetry/host
  const hostError = ref(false)
  const cpuHistory = ref([])
  const memHistory = ref([])
  const days = ref([])              // 30 daily buckets { bucket, total, failed }
  const hours = ref([])             // 24 hourly buckets
  const historyLoaded = ref(false)
  const historyError = ref(false)
  const lastOkAt = ref(null)
  // Task text is hidden unless the viewer turns it on; the choice stays in
  // this browser, like the theme.
  const showText = ref(readShowText())

  let hostTimer = null
  let historyTimer = null

  async function fetchHost() {
    try {
      const { data } = await api.get('/api/telemetry/host')
      host.value = data
      hostError.value = false
      lastOkAt.value = Date.now()
      const push = (list, v) => {
        const next = [...list.value, v ?? 0]
        list.value = next.slice(-HOST_HISTORY)
      }
      push(cpuHistory, data?.cpu?.percent)
      push(memHistory, data?.memory?.percent)
    } catch {
      hostError.value = true
    }
  }

  async function fetchHistory() {
    try {
      const [d, h] = await Promise.all([
        api.get('/api/executions/timeline', { params: { group_by: 'day', hours: 720 } }),
        api.get('/api/executions/timeline', { params: { group_by: 'hour', hours: 24 } }),
      ])
      days.value = d.data?.buckets || []
      hours.value = h.data?.buckets || []
      historyLoaded.value = true
      historyError.value = false
    } catch {
      historyError.value = true
    }
  }

  function start() {
    stop()
    fetchHost()
    fetchHistory()
    hostTimer = setInterval(fetchHost, HOST_EVERY_MS)
    historyTimer = setInterval(fetchHistory, HISTORY_EVERY_MS)
  }

  function stop() {
    if (hostTimer) clearInterval(hostTimer)
    if (historyTimer) clearInterval(historyTimer)
    hostTimer = historyTimer = null
  }

  function setShowText(on) {
    showText.value = Boolean(on)
    try {
      localStorage.setItem(PRIVACY_KEY, showText.value ? '1' : '0')
    } catch {
      // storage unavailable: the choice holds for this page only
    }
  }

  return {
    host, hostError, cpuHistory, memHistory,
    days, hours, historyLoaded, historyError,
    lastOkAt, showText,
    fetchHost, fetchHistory, start, stop, setShowText,
  }
})
