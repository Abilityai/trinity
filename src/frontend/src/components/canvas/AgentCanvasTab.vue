<template>
  <div class="p-6">
    <p class="mb-4 text-xs text-gray-500 dark:text-gray-400">
      A canvas is a surface this agent keeps <em>current</em> — it writes and rewrites it with
      <code class="rounded bg-gray-100 px-1 dark:bg-gray-800">set_canvas</code>.
      Reports are the other half: published once, and they accumulate.
      A canvas marked <span class="font-medium">shared</span> also appears on this agent's
      Workspace page for the people it works with.
    </p>

    <p v-if="error" class="mb-3 text-xs text-status-error-600 dark:text-status-error-400">
      {{ error }}
    </p>

    <CanvasPanel
      :canvases="canvases"
      :fetch-detail="fetchDetail"
      viewer="operator"
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

onMounted(load)
watch(() => props.agentName, load)
</script>
