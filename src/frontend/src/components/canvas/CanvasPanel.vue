<template>
  <div>
    <!-- Empty state: never a blank panel (AC 6). The next action differs by
         viewer because a client cannot make an agent write a canvas, and
         offering them a tool call would be an instruction they cannot follow. -->
    <div
      v-if="!canvases.length"
      class="rounded-xl border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center"
    >
      <p class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ empty.title }}</p>
      <p class="mx-auto mt-1 max-w-md text-xs text-gray-500 dark:text-gray-400">{{ empty.body }}</p>
      <button
        v-if="empty.action === 'chat'"
        class="mt-3 rounded-lg bg-action-primary-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-action-primary-700"
        @click="$emit('start-chat')"
      >Start a chat</button>
    </div>

    <template v-else>
      <!-- Selector only when there is a choice to make. -->
      <div v-if="canvases.length > 1" class="mb-3 flex flex-wrap gap-1.5">
        <button
          v-for="c in canvases"
          :key="c.canvas_id"
          :class="[
            'rounded-full px-3 py-1 text-xs font-medium border transition-colors',
            c.canvas_id === selectedId
              ? 'bg-action-primary-600 text-white border-action-primary-600'
              : 'border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800',
          ]"
          @click="select(c.canvas_id)"
        >{{ c.title || c.canvas_id }}</button>
      </div>

      <div class="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
        <header class="flex flex-wrap items-center gap-2 border-b border-gray-200 dark:border-gray-800 px-4 py-3">
          <h3 class="min-w-0 flex-1 truncate text-sm font-semibold">
            {{ selected?.title || selectedId }}
          </h3>
          <!-- The timestamp is ALWAYS shown; the staleness mark is an addition
               to it, never a replacement (AC 7). A reader who disagrees with
               our heuristic can still judge for themselves. -->
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ fresh.label }}</span>
          <span
            v-if="fresh.stale"
            :title="fresh.note"
            class="rounded-full bg-status-warning-100 px-2 py-0.5 text-[11px] font-medium text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
          >may be out of date</span>
        </header>

        <p
          v-if="fresh.stale"
          class="border-b border-gray-200 dark:border-gray-800 px-4 py-2 text-xs text-gray-600 dark:text-gray-400"
        >{{ fresh.note }}</p>

        <div class="px-4 py-4">
          <p v-if="detailError" class="text-xs text-status-danger-600 dark:text-status-danger-400">
            {{ detailError }}
          </p>
          <p
            v-else-if="!blocks.length"
            class="text-xs text-gray-500 dark:text-gray-400"
          >This canvas is empty.</p>
          <CanvasBlock v-for="b in blocks" :key="b.key" :block="b" />
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import CanvasBlock from './CanvasBlock.vue'
import { emptyState, freshness, renderableBlocks } from './canvasUtils'

const props = defineProps({
  // Metadata rows (no blocks) — the list read.
  canvases: { type: Array, default: () => [] },
  // (canvasId) => Promise<canvas with blocks>. Injected so this component is
  // shared by the operator tab and the Workspace page, which read different
  // access-scoped endpoints; the panel never knows which.
  fetchDetail: { type: Function, required: true },
  viewer: { type: String, default: 'operator' }, // 'operator' | 'client'
})
defineEmits(['start-chat'])

const selectedId = ref(null)
const detail = ref(null)
const detailError = ref('')

const empty = computed(() => emptyState(props.viewer))
const selected = computed(
  () => detail.value || props.canvases.find((c) => c.canvas_id === selectedId.value) || null,
)
const fresh = computed(() => freshness(selected.value || {}))
const blocks = computed(() => renderableBlocks(detail.value?.blocks))

async function select(id) {
  if (!id) return
  selectedId.value = id
  detail.value = null
  detailError.value = ''
  try {
    detail.value = await props.fetchDetail(id)
  } catch (e) {
    // Keep the header — the metadata row is real and its timestamp is the
    // honest part. Only the blocks are missing, and we say so.
    detailError.value = 'Could not load this canvas.'
  }
}

watch(
  () => props.canvases,
  (rows) => {
    if (!rows?.length) {
      selectedId.value = null
      detail.value = null
      return
    }
    if (!rows.some((c) => c.canvas_id === selectedId.value)) select(rows[0].canvas_id)
  },
  { immediate: true },
)
</script>
