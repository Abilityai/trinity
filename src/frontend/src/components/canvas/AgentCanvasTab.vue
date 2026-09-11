<template>
  <div class="p-6">
    <p class="mb-4 text-xs text-gray-500 dark:text-gray-400">
      A canvas is a surface this agent keeps <em>current</em> — it writes and rewrites it with
      <code class="rounded bg-gray-100 px-1 dark:bg-gray-800">set_canvas</code>.
      Reports are the other half: published once, and they accumulate.
      A canvas marked <span class="font-medium">shared</span> also appears on this agent's
      Workspace page for the people it works with.
    </p>

    <p v-if="error" class="mb-3 text-xs text-status-danger-600 dark:text-status-danger-400">
      {{ error }}
    </p>

    <CanvasPanel
      :canvases="canvases"
      :fetch-detail="fetchDetail"
      :can-manage="canManage"
      :delete-canvas="removeCanvas"
      :bulk-delete-canvases="removeCanvases"
      :pin-canvas="pinCanvas"
      :canvas-limit="canvasLimit"
      :agent-name="agentName"
      :share-canvas="shareCanvas"
      :list-shares="listShares"
      :revoke-canvas-share="revokeShare"
      viewer="operator"
      @changed="load"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import api from '../../api'
import { useSessionsStore } from '../../stores/sessions'
import CanvasPanel from './CanvasPanel.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  // ent#553 (review) — the agent's own `can_share`, which is `db.
  // can_user_share_agent`: exactly the predicate `_gate_human_removal` enforces
  // server-side, and the same one `ReportsPanel` five lines up in `AgentDetail`
  // already reads for its delete control.
  //
  // It was hardcoded `true`, on the argument that "the server decides". The
  // server does decide — but a merely-SHARED user was then shown Manage →
  // Delete / Pin and got a 403 on click, which is the failing-control problem
  // `can_manage_canvases` exists to prevent on the Workspace. One model for
  // both surfaces: the control renders where the call would succeed.
  //
  // Defaults FALSE, deliberately: an ancestor that forgets the prop hides an
  // affordance rather than offering one that 403s.
  canManage: { type: Boolean, default: false },
})

const canvases = ref([])
const error = ref('')
const sessions = useSessionsStore()
// Surfaced so the header can warn BEFORE the agent meets the refusal. It is a
// platform constant, not per-agent state, and the client already holds the
// count — so it rides `GET /api/settings/feature-flags`, the established home
// for a value the browser needs to render a surface, rather than a new route
// (Invariant #13) or an envelope around the canvas list (which the MCP tool and
// the Workspace both read as a bare array). 0 = "not told", and
// `canvasHeadroom(n, 0)` renders nothing — the honest reading, and what an
// older backend gets.
const canvasLimit = computed(() => sessions.canvasMaxPerAgent)

async function load() {
  error.value = ''
  try {
    const { data } = await api.get(`/api/agents/${encodeURIComponent(props.agentName)}/canvas`)
    canvases.value = Array.isArray(data) ? data : []
  } catch (e) {
    // Keep whatever was already rendered — a failed refresh must not blank a
    // surface that was working (the ent#253 treatment).
    error.value = 'Could not load canvases.'
  }
}

async function fetchDetail(canvasId) {
  const { data } = await api.get(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/${encodeURIComponent(canvasId)}`,
  )
  return data
}

async function removeCanvas(canvasId) {
  await api.delete(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/${encodeURIComponent(canvasId)}`,
  )
  return true
}

async function removeCanvases(canvasIds) {
  const { data } = await api.post(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/bulk-delete`,
    { canvas_ids: canvasIds },
  )
  return data
}

async function shareCanvas(canvasId, scope) {
  const { data } = await api.post(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/${encodeURIComponent(canvasId)}/share`,
    { scope },
  )
  return data
}

async function listShares(canvasId) {
  const { data } = await api.get(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/shares`,
    { params: { canvas_id: canvasId } },
  )
  return data
}

async function revokeShare(shareId) {
  await api.delete(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/shares/${encodeURIComponent(shareId)}`,
  )
  return true
}

async function pinCanvas(canvasId, pinned) {
  await api.put(
    `/api/agents/${encodeURIComponent(props.agentName)}/canvas/${encodeURIComponent(canvasId)}/pin`,
    { pinned },
  )
  return true
}

onMounted(() => {
  load()
  // Cached for the page load and shared with every other flag consumer, so this
  // is a no-op whenever anything else already asked.
  sessions.loadFeatureFlags?.()
})
watch(() => props.agentName, load)
</script>
