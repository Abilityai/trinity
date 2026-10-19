/**
 * Operator Queue Store (OPS-001)
 *
 * Pinia store for the Operating Room UI.
 * Fetches data from backend API with real-time WebSocket updates.
 */

import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import api from '../api'
import { http } from '../utils/boundedHttp'
import { useAuthStore } from './auth'
import { apiErrorMessage } from '../utils/apiError'
import { decideAutoExpand } from '../utils/loadingState'
import {
  queueResponseBody,
  QUEUE_RESPONSE_NOT_RECORDED,
  QUEUE_RESPONSE_DIVERGED,
  QUEUE_RESPONSE_NOT_ADDRESSEE,
  respondRefusedAsNotPending,
  respondRefusedAsDiverged,
  respondRefusedAsNotAddressee,
  queueEndingSortTime,
  bulkCancelBatches,
} from '../utils/operatorQueue'
import { readQueueSnapshot, pendingCountOf } from '../utils/operatorQueueRead'

// #3302: the list read starts with the same 200-row page as before, and reads
// past it only when the queue needs it (utils/operatorQueueRead.js). Pending is
// read completely; Resolved stays a window of this size, its total stated.
const LIST_PAGE = 200
const RESOLVED_WINDOW = 200

// Agent display helpers
const AGENT_COLORS = [
  'bg-blue-500', 'bg-emerald-500', 'bg-accent-purple-500', 'bg-state-autonomous-500',
  'bg-state-locked-500', 'bg-cyan-500', 'bg-action-primary-500', 'bg-teal-500'
]

function getAgentProfile(name) {
  const initials = name.split('-').map(w => w[0]?.toUpperCase()).join('').slice(0, 2)
  // Deterministic color from name
  let hash = 0
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash)
  const color = AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length]
  return { initials, color, role: 'Agent' }
}

export const useOperatorQueueStore = defineStore('operatorQueue', () => {
  const authStore = useAuthStore()

  // State
  const items = ref([])
  const expandedItemId = ref(null)
  // #1927: the landing rule's arming bit. True until a human toggles any card
  // (they have taken control of expansion — principle 5), re-armed whenever the
  // open set drains to zero so the next 0→N arrival lands expanded again.
  const autoExpandArmed = ref(true)
  const loading = ref(false)
  const error = ref(null)
  // #1926: "the list is empty" is only true once a fetch has SUCCEEDED and
  // returned zero. Before the first success, `items.length === 0` means
  // "loading" or "failed" — consumers must gate their empty state on this.
  const hasLoaded = ref(false)
  const activeTab = ref('open') // 'open' or 'resolved'
  // #2915: the visible escalation — answers that never reached the agent and
  // items the agent closed on its side — from the list response, never
  // recomputed client-side. `divergedItemId` marks the card whose response was
  // refused with 409 item_diverged; its next send carries the acknowledgement.
  const undeliveredCount = ref(0)
  const closedByFilerCount = ref(0)
  const divergedItemId = ref(null)
  // trinity-enterprise#751: the card whose answer was refused because it was not
  // addressed to this person. Its own ref, not `error`: `fetchItems` clears
  // `error` on every poll, and a failed verb must persist beside its control (p18).
  const notAddresseeItemId = ref(null)
  // #3302: every header's counts (NavBar badge, subtitle, tab badge). Written
  // by the list read from the complete pending set, by the count read off the
  // Operations page, and by an answer — never derived from a window.
  const pendingTotal = ref(0)
  const pendingCountExact = ref(true)
  const criticalTotal = ref(0)
  // false: the pending walk could not finish, so the list is its first part.
  const pendingComplete = ref(true)
  // Ended asks in all, beside the Resolved window; null = could not be read.
  const resolvedTotal = ref(null)
  // True while a view renders the list (Operations polls). Only then is a
  // trigger worth a list read; elsewhere the store only feeds the NavBar badge.
  const listLive = ref(false)
  // Bumped by every write the server acknowledged (an answer, a bulk cancel, a
  // clear): a read that left before it carries the old state and is dropped.
  let writeGen = 0
  let _pollTimer = null

  // Getters
  const openItems = computed(() => {
    const result = items.value.filter(i => i.status === 'pending')
    const priorityOrder = { critical: 0, high: 1, medium: 2, low: 3 }
    result.sort((a, b) => {
      const pa = priorityOrder[a.priority] ?? 4
      const pb = priorityOrder[b.priority] ?? 4
      if (pa !== pb) return pa - pb
      return new Date(a.created_at) - new Date(b.created_at)
    })
    return result
  })

  // Resolved feed includes cancelled/expired (#1017) — before that, items
  // cancelled from Needs Response vanished from the UI entirely.
  const RESOLVED_STATUSES = ['responded', 'acknowledged', 'cancelled', 'expired']

  // trinity-enterprise#611 (#627 AC6): ordered by when each item ENDED — a
  // cancellation made this morning used to sort by the day it was filed.
  const resolvedItems = computed(() => {
    return items.value
      .filter(i => RESOLVED_STATUSES.includes(i.status))
      .sort((a, b) => new Date(queueEndingSortTime(b)) - new Date(queueEndingSortTime(a)))
  })

  const pendingCount = computed(() => pendingTotal.value)

  const criticalCount = computed(() => criticalTotal.value)

  const openItemsByAgent = computed(() => {
    const groups = {}
    for (const item of openItems.value) {
      if (!groups[item.agent_name]) {
        groups[item.agent_name] = []
      }
      groups[item.agent_name].push(item)
    }
    return groups
  })

  function getProfile(agentName) {
    return getAgentProfile(agentName)
  }

  // API Actions
  // #3302: bounded (30 s). The global axios carries no timeout, and with the
  // reads one-at-a-time a request that never settles would hold every later
  // read — poll, WS refresh, Retry, the refetch after an answer — forever.
  function queueGet(params) {
    return http.get('/api/operator-queue', { params, headers: authStore.authHeader })
  }

  // #3302: one read at a time, plus ONE more for every trigger that lands while
  // it runs. A read can take several requests now, and a sync trigger can fire
  // every 5 s on every open tab; a sequence guard would still SEND them all.
  function coalesced(run) {
    let running = null
    let again = null
    function start() {
      if (running) {
        if (!again) {
          const next = () => { again = null; return start() }
          again = running.then(next, next)
        }
        return again
      }
      running = run().finally(() => { running = null })
      return running
    }
    return start
  }

  // An acknowledged write that lands while a read is out makes that read stale;
  // it is read again (bounded) so whoever awaits this read sees the state after
  // the write — a refetch after a 409 must show what the agent changed.
  const STALE_READ_RETRIES = 2

  async function loadList() {
    loading.value = true
    error.value = null
    try {
      let gen
      let snap
      let critical
      for (let attempt = 0; ; attempt += 1) {
        gen = writeGen
        snap = await readQueueSnapshot(queueGet, { pageLimit: LIST_PAGE, endedLimit: RESOLVED_WINDOW })
        // A walk that could not finish ranks an alert raised during it last, so a
        // partial read takes the critical count from the route, not the rows.
        critical = snap.pendingComplete ? null
          : pendingCountOf((await queueGet({ status: 'pending', priority: 'critical', limit: 1 })).data)
        if (gen === writeGen || attempt >= STALE_READ_RETRIES) break
      }
      if (gen !== writeGen) return // still overtaken: the optimistic state is newer than any read
      items.value = [...snap.pending, ...snap.ended]
      pendingComplete.value = snap.pendingComplete
      pendingCountExact.value = snap.pendingTotal != null
      pendingTotal.value = snap.pendingTotal ?? snap.pending.length
      criticalTotal.value = critical ?? snap.pending.filter(i => i.priority === 'critical').length
      resolvedTotal.value = snap.endedTotal
      undeliveredCount.value = snap.flags.undelivered
      closedByFilerCount.value = snap.flags.closedByFiler
      hasLoaded.value = true
    } catch (err) {
      error.value = apiErrorMessage(err, 'Request failed')
      // Don't clear items on error — keep stale data visible
    } finally {
      loading.value = false
    }
  }

  // #3302: the counts alone, for a page that shows no list (the NavBar badge):
  // two one-row reads whose `total` is the count — not a window of rows.
  async function loadCounts() {
    const gen = writeGen
    try {
      const [all, critical] = await Promise.all([
        queueGet({ status: 'pending', limit: 1 }),
        queueGet({ status: 'pending', priority: 'critical', limit: 1 }),
      ])
      if (gen !== writeGen) return
      const pending = pendingCountOf(all.data)
      const criticalPending = pendingCountOf(critical.data)
      // An unverified total keeps the number already on screen.
      if (pending != null) {
        pendingTotal.value = pending
        pendingCountExact.value = true
      }
      if (criticalPending != null) criticalTotal.value = criticalPending
    } catch {
      // The badge keeps its last value; the list surfaces report their own failures.
    }
  }

  const fetchItems = coalesced(loadList)
  const fetchCounts = coalesced(loadCounts)

  function refresh() {
    return listLive.value ? fetchItems() : fetchCounts()
  }

  async function respondToItem(id, response, responseText = '', { acknowledgeDivergence = false } = {}) {
    const item = items.value.find(i => i.id === id)
    if (!item) return false
    // A decision is required — never let an empty/undefined one be stringified
    // into the body (the callers guard this today; this is the belt).
    if (response == null || String(response).trim() === '') return false

    // #2370: ONE builder for every producer of this body — the decision rides
    // `response`, the note rides `response_text` (trimmed; empty → null).
    const body = queueResponseBody(response, responseText)
    // #2915: a second send after the divergence notice answers anyway.
    if (acknowledgeDivergence || divergedItemId.value === id) body.acknowledge_divergence = true
    try {
      await axios.post(
        `/api/operator-queue/${id}/respond`,
        body,
        { headers: authStore.authHeader }
      )
      if (divergedItemId.value === id) divergedItemId.value = null
      if (notAddresseeItemId.value === id) notAddresseeItemId.value = null
      // #3302: recorded. A read already out still carries this ask as pending,
      // and the counts are no longer derived from the rows, so both move here.
      writeGen += 1
      if (item.status === 'pending') {
        pendingTotal.value = Math.max(0, pendingTotal.value - 1)
        if (item.priority === 'critical') criticalTotal.value = Math.max(0, criticalTotal.value - 1)
      }

      // Optimistic update — mirror the body that was sent
      item.status = 'responded'
      item.response = body.response
      item.response_text = body.response_text
      item.responded_by_email = authStore.userEmail || authStore.user?.username
      item.responded_at = new Date().toISOString()
      expandedItemId.value = null

      // Auto-advance: expand next open item
      const nextOpen = openItems.value.find(i => i.id !== id && i.status === 'pending')
      if (nextOpen) {
        expandedItemId.value = nextOpen.id
      }
      // #3242: true = recorded, so a form clears only on success (a 409
      // divergence must not wipe a typed instruction).
      return true
    } catch (err) {
      if (respondRefusedAsNotAddressee(err)) {
        notAddresseeItemId.value = id
        return false
      }
      if (respondRefusedAsDiverged(err)) {
        // #2915: the agent rewrote or closed this item after the card was read.
        // Refetch so the badge shows the divergence, keep the card open, and let
        // the next send carry the acknowledgement. Verb outcome, so it lives on
        // the card (InlineError), not in the fetch-error banner.
        await fetchItems()
        divergedItemId.value = id
        expandedItemId.value = id
        return false
      }
      if (respondRefusedAsNotPending(err)) {
        // Item left 'pending' under us (409 — e.g. another operator cleared
        // the queue, #1017), was already terminal (400) or the row is gone
        // (404) — the response was NOT recorded. Copy shared with `/m` (#2370)
        // and attribution-free: the status may be responded, cancelled or
        // expired.
        //
        // The refetch is AWAITED and the copy assigned after it: `fetchItems`
        // sets `error.value = null` in its own synchronous prologue, so
        // assigning first and calling it un-awaited destroyed the notice
        // before anything rendered. That cost nothing while 409 was the only
        // refused status (#2377 owns that half), but this branch now also
        // takes 400 and 404 — which used to fall through to the `else` and
        // leave a message — so ordering it the other way would have widened a
        // silent failure to two statuses that previously reported. The
        // refetch's own error, if it failed, is deliberately overwritten: the
        // operator needs to know their answer was not recorded more than they
        // need to know the list is stale.
        await fetchItems()
        error.value = QUEUE_RESPONSE_NOT_RECORDED
      } else {
        error.value = apiErrorMessage(err, 'Request failed')
      }
      return false
    }
  }

  // Bulk actions (#1017)
  async function bulkCancel(ids) {
    if (!ids || ids.length === 0) return { cancelled: 0, skipped: 0 }
    error.value = null
    // #3302: the list is complete now, so it can hold more ids than the route
    // takes. Batches keep each agent's asks together (bulkCancelBatches), so
    // the confirm's "one turn per agent" holds across them.
    const agentOf = new Map(items.value.map(i => [i.id, i.agent_name]))
    const batches = bulkCancelBatches(ids.map(id => ({ id, agent_name: agentOf.get(id) })))
    const result = { cancelled: 0, skipped: 0, batch_ids: [] }
    let landed = 0
    try {
      for (const batch of batches) {
        const response = await api.post('/api/operator-queue/bulk-cancel', { ids: batch })
        landed += 1
        result.cancelled += Number(response.data?.cancelled) || 0
        result.skipped += Number(response.data?.skipped) || 0
        if (response.data?.batch_id) result.batch_ids.push(response.data.batch_id)
      }
    } catch (err) {
      if (landed > 0) {
        // Some asks did end: show that before saying the rest failed (the
        // refetch clears `error` in its prologue, so it runs first).
        writeGen += 1
        await fetchItems()
      }
      error.value = apiErrorMessage(err, 'Request failed')
      throw err
    }
    writeGen += 1
    await fetchItems()
    return result
  }

  async function clearResolved(agentName = null) {
    error.value = null
    try {
      const response = await api.post('/api/operator-queue/clear-resolved', {
        agent_name: agentName,
      })
      writeGen += 1
      await fetchItems()
      return response.data
    } catch (err) {
      error.value = apiErrorMessage(err, 'Request failed')
      throw err
    }
  }

  async function acknowledgeItem(id) {
    await respondToItem(id, 'acknowledged', '')
  }

  function toggleExpand(id) {
    expandedItemId.value = expandedItemId.value === id ? null : id
    // A human chose — no poll, WS delta or remount may override it (#1927).
    autoExpandArmed.value = false
  }

  // #1927: auto-expand the first open item ONCE per armed episode. Lives beside
  // the expansion state it governs (design-system p21), not in the view — the
  // store is a singleton fed by WS events and earlier visits, so a view-local
  // "once per mount" either misses a warm store or re-expands over a remembered
  // collapse. `decideAutoExpand` checks MEMBERSHIP of expandedItemId in the open
  // set, so an id that was answered while the operator was away never blocks
  // the rule forever. `respondToItem`'s auto-advance is a separate, unchanged rule.
  function maybeAutoExpand() {
    const id = decideAutoExpand({
      armed: autoExpandArmed.value,
      openIds: openItems.value.map(i => i.id),
      expandedId: expandedItemId.value,
    })
    if (id == null) return false
    expandedItemId.value = id
    autoExpandArmed.value = false
    return true
  }

  // Re-arm when the queue drains, whatever drained it (poll, respond, WS, clear).
  watch(() => openItems.value.length, (len) => {
    if (len === 0) autoExpandArmed.value = true
  })

  // WebSocket event handler. #3302: every refetch here is `refresh()` — the
  // list where a view shows it, the two counts everywhere else.
  function handleWebSocketEvent(data) {
    if (data.type === 'operator_queue_new') {
      // New item from agent — refetch to get full data
      refresh()
    } else if (data.type === 'operator_queue_responded' || data.type === 'operator_queue_cancelled') {
      // trinity-enterprise#611: an ask ended (answered / cancelled). The trigger
      // is thin — identifiers only (#918) — so refetch the access-controlled
      // list rather than patching the row from a payload.
      refresh()
    } else if (data.type === 'operator_queue_acknowledged') {
      // Agent acknowledged — update locally
      const item = items.value.find(i => i.id === data.data?.id)
      if (item) {
        item.status = 'acknowledged'
      }
    } else if (data.type === 'operator_queue_cleared') {
      // Bulk clear by an operator (#1017) — refetch authoritative state
      refresh()
    } else if (data.type === 'operator_queue_sync') {
      // #2915: the poller changed some row's sync/delivery state — one thin
      // trigger per cycle; refetch the access-controlled list (#918)
      refresh()
    }
  }

  // Polling (fallback for when WebSocket misses events). The poller is the
  // view that renders the list, so it also marks the list live (#3302).
  function startPolling(interval = 15000) {
    stopPolling()
    listLive.value = true
    fetchItems() // Initial fetch
    _pollTimer = setInterval(fetchItems, interval)
  }

  function stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer)
      _pollTimer = null
    }
    listLive.value = false
  }

  return {
    items,
    expandedItemId,
    autoExpandArmed,
    loading,
    error,
    hasLoaded,
    activeTab,
    undeliveredCount,
    closedByFilerCount,
    divergedItemId,
    QUEUE_RESPONSE_DIVERGED,
    notAddresseeItemId,
    QUEUE_RESPONSE_NOT_ADDRESSEE,
    openItems,
    resolvedItems,
    pendingCount,
    pendingCountExact,
    pendingComplete,
    criticalCount,
    resolvedTotal,
    openItemsByAgent,
    getProfile,
    fetchItems,
    fetchCounts,
    refresh,
    listLive,
    toggleExpand,
    maybeAutoExpand,
    respondToItem,
    acknowledgeItem,
    bulkCancel,
    clearResolved,
    handleWebSocketEvent,
    startPolling,
    stopPolling,
  }
})
