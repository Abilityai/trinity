import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

/**
 * Shared sessions / rooms store (ent#170, backend ent#169).
 *
 * A room is a shared persistent RECORD: agents + humans in one transcript,
 * mention-wake turn-taking, hard budgets. The backend WS payloads are thin
 * (ids only, the #918 pattern) — a `room_message` carries `{room_id, seq}`, so
 * the store refetches over the access-controlled REST rather than trusting a
 * broadcast to carry transcript content. Working/idle presence rides
 * `room_participant_state`; a close rides `room_closed`.
 *
 * All HTTP goes through the shared api.js client (Invariant #7). The live
 * updates follow the loops store shape (#1106): react to fleet-wide events for
 * the room on screen, plus a backstop poll while anything is working so a
 * dropped event can't leave the transcript stale.
 */
const POLL_INTERVAL_MS = 12000

export const useRoomsStore = defineStore('rooms', () => {
  // --- state ---
  const rooms = ref([])              // list rail: RoomSummary[]
  const roomsLoading = ref(false)
  const activeRoomId = ref(null)
  const activeRoom = ref(null)       // { …room, participants, messages, message_count, cost }
  const roomLoading = ref(false)
  const posting = ref(false)
  const error = ref(null)
  // identity -> 'working' | 'idle', driven by room_participant_state
  const workingState = ref({})

  let _pollTimer = null
  let _seqSeen = 0                   // highest seq applied to activeRoom
  let _appendInFlight = false        // serialize refetches (chain events arrive back-to-back)
  let _appendAgain = false           // an event landed mid-refetch → run once more

  // --- getters ---
  const anyWorking = computed(() =>
    Object.values(workingState.value).some((s) => s === 'working')
  )
  const activeParticipants = computed(() =>
    (activeRoom.value?.participants || []).filter((p) => !p.left_at)
  )
  const agentParticipants = computed(() =>
    activeParticipants.value.filter((p) => p.kind === 'agent')
  )
  const isClosed = computed(() => activeRoom.value?.status === 'closed')

  // --- list ---
  async function fetchRooms() {
    roomsLoading.value = true
    try {
      const { data } = await api.get('/api/rooms')
      rooms.value = data.rooms || []
    } catch (e) {
      error.value = e?.response?.data?.detail?.message || 'Failed to load rooms'
    } finally {
      roomsLoading.value = false
    }
  }

  // --- one room ---
  async function openRoom(roomId) {
    if (!roomId) return
    activeRoomId.value = roomId
    roomLoading.value = true
    workingState.value = {}
    _seqSeen = 0
    _appendInFlight = false
    _appendAgain = false
    try {
      await _refreshActiveRoom()
    } finally {
      roomLoading.value = false
    }
    _ensurePolling()
  }

  async function _refreshActiveRoom() {
    if (!activeRoomId.value) return
    const { data } = await api.get(`/api/rooms/${encodeURIComponent(activeRoomId.value)}`)
    activeRoom.value = data
    _seqSeen = data.messages?.length ? data.messages[data.messages.length - 1].seq : 0
    // keep the rail row in sync (count / status / cost)
    _syncRailRow(data)
  }

  // Append only the messages after what we've applied, so a WS-triggered refetch
  // doesn't rebuild the whole transcript (and lose scroll).
  //
  // Two guards make this dedup-safe. Room chains fire several `room_message`
  // events back-to-back, so overlapping refetches would otherwise BOTH read the
  // same `_seqSeen`, fetch the same rows, and append them twice (observed: the
  // same message + execution_id rendered twice).
  //  1. In-flight lock — only one refetch runs; an event mid-flight sets a rerun
  //     flag so nothing is missed.
  //  2. Dedup by seq — append only rows whose seq isn't already present, so even
  //     an overlapping `since=` window (or the backstop poll) can't duplicate.
  async function _appendSince() {
    if (!activeRoomId.value || !activeRoom.value) return
    if (_appendInFlight) { _appendAgain = true; return }
    _appendInFlight = true
    try {
      const { data } = await api.get(
        `/api/rooms/${encodeURIComponent(activeRoomId.value)}?since=${_seqSeen}`
      )
      if (!activeRoom.value) return
      const known = new Set(
        activeRoom.value.messages.filter((m) => !m._optimistic).map((m) => m.seq)
      )
      const fresh = (data.messages || []).filter((m) => !known.has(m.seq))
      if (fresh.length) {
        // drop optimistic placeholders now confirmed by real rows
        activeRoom.value.messages = activeRoom.value.messages.filter((m) => !m._optimistic)
        activeRoom.value.messages.push(...fresh)
        _seqSeen = Math.max(_seqSeen, ...fresh.map((m) => m.seq))
      }
      activeRoom.value.status = data.status
      activeRoom.value.stop_reason = data.stop_reason
      activeRoom.value.message_count = data.message_count
      activeRoom.value.cost = data.cost
      activeRoom.value.participants = data.participants
      _syncRailRow(data)
    } finally {
      _appendInFlight = false
      if (_appendAgain) { _appendAgain = false; _appendSince() }
    }
  }

  function _syncRailRow(data) {
    const idx = rooms.value.findIndex((r) => r.id === data.id)
    if (idx !== -1) {
      rooms.value[idx] = {
        ...rooms.value[idx],
        status: data.status,
        stop_reason: data.stop_reason,
        message_count: data.message_count ?? rooms.value[idx].message_count,
        participant_count: (data.participants || []).length || rooms.value[idx].participant_count,
      }
    }
  }

  // --- create / post / close ---
  async function createRoom(payload) {
    const { data } = await api.post('/api/rooms', payload)
    await fetchRooms()
    return data
  }

  async function postMessage(content) {
    if (!activeRoomId.value || !content.trim()) return
    posting.value = true
    // optimistic append (rolled back on failure)
    const optimistic = {
      id: `opt-${Date.now()}`,
      seq: _seqSeen + 0.5,
      sender_kind: 'user',
      sender_identity: 'You',
      kind: 'message',
      mentions: [],
      content,
      created_at: new Date().toISOString(),
      _optimistic: true,
    }
    activeRoom.value?.messages.push(optimistic)
    try {
      // Idempotency-Key so a retried send creates one message (Invariant #18).
      await api.post(
        `/api/rooms/${encodeURIComponent(activeRoomId.value)}/messages`,
        { content },
        { headers: { 'Idempotency-Key': `${activeRoomId.value}:${optimistic.id}` } }
      )
      await _appendSince()
    } catch (e) {
      // rollback the optimistic row and surface the reason
      if (activeRoom.value) {
        activeRoom.value.messages = activeRoom.value.messages.filter((m) => m.id !== optimistic.id)
      }
      const d = e?.response?.data?.detail
      error.value = (d && (d.message || d)) || 'Failed to send'
      throw e
    } finally {
      posting.value = false
      _ensurePolling()
    }
  }

  async function closeRoom(roomId) {
    await api.post(`/api/rooms/${encodeURIComponent(roomId)}/close`)
    if (roomId === activeRoomId.value) await _refreshActiveRoom()
    else await fetchRooms()
  }

  // --- live updates (fleet-wide events; act only on the room on screen) ---
  function handleWebSocketEvent(data) {
    if (!data || data.room_id !== activeRoomId.value) return
    if (data.type === 'room_message') {
      _appendSince().catch(() => {})
    } else if (data.type === 'room_participant_state') {
      workingState.value = { ...workingState.value, [data.identity]: data.state }
    } else if (data.type === 'room_closed') {
      _refreshActiveRoom().catch(() => {})
      workingState.value = {}
    }
    _ensurePolling()
  }

  function _ensurePolling() {
    if (anyWorking.value && activeRoomId.value) {
      if (!_pollTimer) _pollTimer = setInterval(() => _appendSince().catch(() => {}), POLL_INTERVAL_MS)
    } else {
      stopPolling()
    }
  }

  function stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null }
  }

  function clear() {
    stopPolling()
    activeRoomId.value = null
    activeRoom.value = null
    workingState.value = {}
    _seqSeen = 0
    _appendInFlight = false
    _appendAgain = false
  }

  return {
    rooms, roomsLoading, activeRoomId, activeRoom, roomLoading, posting, error,
    workingState, anyWorking, activeParticipants, agentParticipants, isClosed,
    fetchRooms, openRoom, createRoom, postMessage, closeRoom,
    handleWebSocketEvent, stopPolling, clear,
  }
})
