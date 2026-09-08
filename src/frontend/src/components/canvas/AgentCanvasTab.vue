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
import { onMounted, ref, watch } from 'vue'
import api from '../../api'
import CanvasPanel from './CanvasPanel.vue'

const props = defineProps({ agentName: { type: String, required: true } })

const canvases = ref([])
const error = ref('')
// ent#553 — Agent Detail is the operator surface, so the affordance is shown
// and the SERVER decides: a non-owner's call is refused by the same predicate
// the Workspace uses. Showing it here rather than resolving ownership in the
// client keeps one authority; the failure is a named message, not a dead
// control, because this tab is only reachable by someone with agent access.
const canManage = ref(true)
// Surfaced so the header can warn before the agent meets the refusal. 0 = the
// panel says nothing, which is the honest reading of "not told".
const canvasLimit = ref(0)

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

onMounted(load)
watch(() => props.agentName, load)
</script>
