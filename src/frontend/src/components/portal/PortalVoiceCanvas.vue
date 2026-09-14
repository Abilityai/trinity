<!--
  The right column of a Workspace voice call (trinity-enterprise#534): the
  agent's canvas, live, while the orb has the conversation column. Orb left /
  canvas right — the retired `/agents/:name/workspace` page's split.

  ONE rendering layer: this is `CanvasPanel`, the same component the rail's
  Canvas tab and Agent Detail render — nothing here draws a block. It reads
  the voice panel route (`GET /api/agents/{name}/voice/{sid}/panel`, platform
  JWT), which returns the agent's default canvas whatever its audience — the
  person in the call sees what the call draws.

  Refresh rule (`portalVoiceMode.js`): refetch when the bridge reports a panel
  verb finished (`panelVersion` bumps on each), plus a slow safety poll for
  boards rewritten some other way. A fetch that fails keeps the last board on
  screen; the column never blanks mid-call.
-->
<template>
  <aside
    class="flex flex-col min-h-0 h-full border-l border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950"
    data-testid="portal-voice-canvas"
    aria-label="Canvas"
  >
    <header class="shrink-0 flex items-center gap-2 px-4 h-14 border-b border-gray-200 dark:border-gray-800">
      <span class="text-sm font-semibold">Canvas</span>
      <span class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ agentName }}</span>
      <span
        v-if="fetchError"
        class="ml-auto text-xs text-status-warning-700 dark:text-status-warning-300 truncate"
        role="status"
      >{{ fetchError }}</span>
    </header>
    <div class="flex-1 min-h-0 overflow-y-auto p-4">
      <!-- Mid-call empty state: the next action is to keep talking, so the
           column says what the agent CAN put here rather than borrowing the
           Canvas tab's "ask it in the chat" (the chat is inert right now). -->
      <div
        v-if="!canvases.length"
        class="rounded-xl border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center"
        data-testid="portal-voice-canvas-empty"
      >
        <p class="text-sm font-medium text-gray-700 dark:text-gray-200">Nothing drawn yet</p>
        <p class="mx-auto mt-1 max-w-md text-xs text-gray-500 dark:text-gray-400">
          While you talk, the agent can show notes, diagrams, images and tables here. Ask it to.
        </p>
      </div>
      <CanvasPanel
        v-else
        :canvases="canvases"
        :fetch-detail="fetchDetail"
        viewer="client"
      />
    </div>
  </aside>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import axios from 'axios'
import { useAuthStore } from '@/stores/auth'
import CanvasPanel from '@/components/canvas/CanvasPanel.vue'
import { CANVAS_SAFETY_POLL_MS, canvasChanged } from './portalVoiceMode'

const props = defineProps({
  agentName: { type: String, required: true },
  voiceSessionId: { type: String, required: true },
  // Bumped by the conversation whenever the bridge reports a panel verb done.
  panelVersion: { type: Number, default: 0 },
})

const authStore = useAuthStore()
const canvas = ref(null)
const fetchError = ref('')
let timer = null
let inFlight = false

// `CanvasPanel` takes a metadata list and a detail fetcher; the live board is
// both, so the list is the one canvas and the fetcher answers from it.
const canvases = computed(() => (canvas.value?.blocks?.length ? [canvas.value] : []))
async function fetchDetail() { return canvas.value }

async function refresh() {
  if (inFlight || !props.voiceSessionId) return
  inFlight = true
  try {
    const { data } = await axios.get(
      `/api/agents/${encodeURIComponent(props.agentName)}/voice/${encodeURIComponent(props.voiceSessionId)}/panel`,
      { headers: authStore.authHeader },
    )
    if (canvasChanged(canvas.value, data)) canvas.value = data
    fetchError.value = ''
  } catch (e) {
    // Keep the last board; say the refresh missed. The call goes on.
    fetchError.value = 'Canvas may be out of date'
  } finally {
    inFlight = false
  }
}

watch(() => props.panelVersion, () => { void refresh() })

onMounted(() => {
  void refresh()
  timer = setInterval(() => { void refresh() }, CANVAS_SAFETY_POLL_MS)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer); timer = null })
</script>
