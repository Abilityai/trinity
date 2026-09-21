<template>
<section data-testid="portal-agent-memory">
  <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">What it remembers about you</h2>
  <div v-if="!memoryLoaded && !memoryError" class="space-y-2" aria-busy="true">
    <div v-for="row in 2" :key="row" class="animate-pulse motion-reduce:animate-none h-10 rounded-xl bg-gray-100 dark:bg-gray-800/60"></div>
    <span class="sr-only">Loading what this agent remembers about you…</span>
  </div>
  <LoadFailed
    v-else-if="memoryError"
    dense
    title="Couldn't load memory"
    :message="memoryError"
    @retry="loadMemory"
  />
  <template v-else>
    <p v-if="!memory.notes" class="text-sm text-gray-400">
      Nothing yet. When it learns something about you — in a chat, or in a scheduled run addressed to you — it shows here.
    </p>
    <div v-else class="rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-2.5">
      <p class="text-sm whitespace-pre-wrap break-words" data-testid="portal-agent-memory-notes">{{ memory.notes }}</p>
      <p v-if="memory.updated_at" class="mt-1 text-[11px] text-gray-400" :title="memory.updated_at">Updated {{ relative(memory.updated_at) }}</p>
    </div>

    <template v-if="memoryWrites.length">
      <h3 class="mt-3 mb-1 text-[11px] font-medium text-gray-400">Changes</h3>
      <InlineError
        v-if="memoryUndoError"
        class="mb-2"
        :message="memoryUndoError"
        @dismiss="store.memoryUndoError = null"
      />
      <ul class="space-y-1.5">
        <li v-for="w in memoryWrites" :key="w.id"
            class="rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]"
            :class="w.undone_at ? 'opacity-60' : ''"
            :data-testid="w.kind === 'scheduled_run' ? 'portal-memory-write-scheduled' : 'portal-memory-write'">
          <div class="flex items-center gap-2">
            <span class="min-w-0 flex-1 truncate text-gray-400">
              <span class="font-medium text-gray-900 dark:text-gray-100">{{ writeLabel(w) }}</span>
              · <span :title="w.written_at">{{ relative(w.written_at) }}</span><template v-if="w.undone_at"> · undone</template>
            </span>
            <BaseButton
              v-if="w.undoable"
              size="sm"
              variant="secondary"
              data-testid="portal-memory-undo"
              :loading="store.memoryUndoing === w.id"
              loading-label="Undoing…"
              @click="undo(w.id)"
            >Undo</BaseButton>
          </div>
          <p v-if="w.kind === 'scheduled_run'" class="mt-1 whitespace-pre-wrap break-words">{{ w.notes || '(cleared)' }}</p>
        </li>
      </ul>
    </template>
  </template>
</section>
</template>

<script setup>
/**
 * ent#637 — what this agent remembers about YOU, and which runs changed it.
 *
 * A scheduled run addressed to you may now write your per-person memory; this
 * is where you see that it did — what it left, when, which run — and undo it.
 * Only the viewer's own memory is ever served here (the route is keyed on the
 * principal), and the server decides what is undoable (latest, not already
 * undone), so this component never guesses which button to show.
 *
 * Split out of PortalAgentDetails so the Undo verb is proven by MOUNTING it
 * (#2918), not by a regex over the details panel.
 */
import { computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import InlineError from '@/components/InlineError.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useClientPortalStore()

const memoryMine = computed(() => store.memoryAgent === props.agentName)
const memory = computed(() => (memoryMine.value ? store.memory : null) || { notes: '', updated_at: null, writes: [] })
const memoryLoaded = computed(() => memoryMine.value && store.memoryLoaded)
const memoryError = computed(() => (memoryMine.value ? store.memoryError : null))
const memoryUndoError = computed(() => (memoryMine.value ? store.memoryUndoError : null))
const memoryWrites = computed(() => memory.value.writes || [])
function loadMemory() { return store.loadAgentMemory(props.agentName) }
function undo(id) { return store.undoMemoryWrite(props.agentName, id) }
/** Who changed it, in the viewer's words: the run's schedule name, or the chat. */
function writeLabel(w) {
  if (w.kind === 'scheduled_run') return w.schedule_name ? `Scheduled run · ${w.schedule_name}` : 'Scheduled run'
  return 'In a conversation'
}

watch(() => props.agentName, loadMemory)
onMounted(loadMemory)

function relative(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString()
}
</script>
