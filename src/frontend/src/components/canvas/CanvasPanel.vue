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
      <!-- ent#553 — the list has to stay usable at fifty. Search appears only
           once the pile is real, the strip is height-bounded so a long list
           scrolls WITHOUT the rail losing its other tabs (AC 4), and manage
           mode is opt-in so the ordinary read stays a single click. -->
      <div v-if="showSearch || headroom.label || canManage" class="mb-2 flex flex-wrap items-center gap-2">
        <input
          v-if="showSearch"
          v-model="query"
          type="search"
          placeholder="Search canvases…"
          data-testid="canvas-search"
          class="min-w-0 flex-1 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2.5 py-1 text-xs"
        />
        <span v-if="headroom.label" class="text-[11px] text-gray-500 dark:text-gray-400" data-testid="canvas-headroom">
          {{ headroom.label }}
        </span>
        <button
          v-if="canManage"
          class="rounded-lg border border-gray-300 dark:border-gray-700 px-2.5 py-1 text-xs font-medium hover:bg-gray-100 dark:hover:bg-gray-800"
          data-testid="canvas-manage-toggle"
          @click="manage = !manage; selectedIds = []"
        >{{ manage ? 'Done' : 'Manage' }}</button>
      </div>

      <!-- Selector only when there is a choice to make — and ALWAYS when a
           search has a hit (`canvasSelectorVisible`): at exactly one match the
           old `visible.length > 1` gate hid the strip with the previous canvas
           still on screen. -->
      <div
        v-if="selectorVisible"
        class="mb-3 flex max-h-48 flex-wrap gap-1.5 overflow-y-auto"
        data-testid="canvas-select"
      >
        <template v-if="!manage">
          <button
            v-for="c in visible"
            :key="c.canvas_id"
            :data-canvas-id="c.canvas_id"
            :class="[
              'rounded-full px-3 py-1 text-xs font-medium border transition-colors',
              c.canvas_id === selectedId
                ? 'bg-action-primary-600 text-white border-action-primary-600'
                : 'border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800',
            ]"
            @click="select(c.canvas_id)"
          >
            <span v-if="c.pinned" aria-hidden="true">📌 </span>{{ c.title || c.canvas_id }}
          </button>
        </template>

        <!-- Manage mode: one row per canvas, each carrying its own age and
             stale mark so the choice of what to retire is informed. -->
        <ul v-else class="w-full space-y-1" data-testid="canvas-manage-list">
          <li
            v-for="c in visible"
            :key="c.canvas_id"
            class="flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-800 px-2 py-1.5"
            :data-canvas-row="c.canvas_id"
          >
            <input
              type="checkbox"
              :checked="selection.ids.includes(c.canvas_id)"
              :aria-label="`Select ${c.title || c.canvas_id}`"
              @change="toggleSelected(c.canvas_id)"
            />
            <span class="min-w-0 flex-1 truncate text-xs">{{ c.title || c.canvas_id }}</span>
            <span class="shrink-0 text-[11px] text-gray-500 dark:text-gray-400">{{ ageOf(c) }}</span>
            <span
              v-if="c.stale"
              class="shrink-0 rounded-full bg-status-warning-100 px-1.5 text-[10px] text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
            >stale</span>
            <button
              class="shrink-0 rounded px-1 text-xs hover:bg-gray-100 dark:hover:bg-gray-800"
              :disabled="busy"
              :aria-pressed="!!c.pinned"
              :title="c.pinned ? 'Unpin' : 'Pin to the top'"
              :data-canvas-pin="c.canvas_id"
              @click="togglePin(c)"
            >{{ c.pinned ? '📌' : '📍' }}</button>
            <button
              class="shrink-0 rounded px-1 text-xs text-status-danger-600 hover:bg-status-danger-50 dark:hover:bg-status-danger-500/16"
              :disabled="busy"
              title="Delete this canvas"
              :data-canvas-delete="c.canvas_id"
              @click="removeOne(c)"
            >Delete</button>
          </li>
        </ul>
      </div>

      <!-- The bulk bar names the count, and only one confirmation follows. -->
      <div
        v-if="manage && selection.any"
        class="mb-3 flex items-center gap-2 rounded-lg bg-gray-100 dark:bg-gray-800 px-3 py-2"
        data-testid="canvas-bulk-bar"
      >
        <span class="text-xs">{{ selection.count }} selected</span>
        <button class="text-xs underline" @click="toggleAll">
          {{ selection.all ? 'Clear' : 'Select all' }}
        </button>
        <button
          class="ml-auto rounded-lg bg-status-danger-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
          :disabled="busy"
          data-testid="canvas-bulk-delete"
          @click="removeSelected"
        >Delete selected</button>
      </div>

      <p v-if="actionError" class="mb-2 text-xs text-status-danger-600 dark:text-status-danger-400" data-testid="canvas-action-error">
        {{ actionError }}
      </p>
      <p v-else-if="actionNote" class="mb-2 text-xs text-gray-500 dark:text-gray-400" data-testid="canvas-action-note">
        {{ actionNote }}
      </p>

      <p v-if="query && !visible.length" class="mb-3 text-xs text-gray-500 dark:text-gray-400" data-testid="canvas-search-empty">
        No canvas matches “{{ query }}”.
      </p>

      <div
        class="rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900"
        data-testid="canvas-panel"
        :data-canvas-id="selectedId"
      >
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

        <!-- ent#537 — every block renders inside the design kit, so an agent's
             `ck-*` markup looks the same on Agent Detail, the Workspace page
             and the rail. A declared `template` lays slotted blocks into named
             regions; whatever is not slotted follows in the stacked list, so a
             layout never hides a block. -->
        <CanvasKit class="px-4 py-4">
          <p v-if="detailError" class="text-xs text-status-danger-600 dark:text-status-danger-400" data-testid="canvas-detail-error">
            {{ detailError }}
          </p>
          <p
            v-else-if="!blocks.length"
            class="text-xs text-gray-500 dark:text-gray-400"
          >This canvas is empty.</p>
          <template v-else>
            <div
              v-if="placement"
              :class="['ck-layout', `ck-layout-${placement.template}`]"
              :data-canvas-template="placement.template"
            >
              <section
                v-for="region in placement.regions"
                :key="region.slot"
                :class="['ck-slot', `ck-slot-${region.slot}`, region.grid ? 'ck-slot-grid' : null]"
                :data-canvas-slot="region.slot"
              >
                <CanvasBlock
                  v-for="b in region.blocks"
                  :key="b.key"
                  :block="b"
                  :agent-name="detail?.agent_name || null"
                />
              </section>
            </div>
            <CanvasBlock
              v-for="b in (placement ? placement.unslotted : blocks)"
              :key="b.key"
              :block="b"
              :agent-name="detail?.agent_name || null"
            />
          </template>
        </CanvasKit>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import CanvasBlock from './CanvasBlock.vue'
import CanvasKit from './CanvasKit.vue'
import { placeBlocks } from './canvasLayouts'
import {
  bulkDeleteOutcome,
  bulkDeletePrompt,
  canvasAutoSelect,
  canvasHeadroom,
  canvasSelectorVisible,
  emptyState,
  filterCanvases,
  freshness,
  relativeTime,
  renderableBlocks,
  selectionState,
  sortCanvases,
} from './canvasUtils'

const props = defineProps({
  // Metadata rows (no blocks) — the list read.
  canvases: { type: Array, default: () => [] },
  // (canvasId) => Promise<canvas with blocks>. Injected so this component is
  // shared by the operator tab and the Workspace page, which read different
  // access-scoped endpoints; the panel never knows which.
  fetchDetail: { type: Function, required: true },
  viewer: { type: String, default: 'operator' }, // 'operator' | 'client'
  // ent#553 — the lifecycle actions, injected for the same reason
  // `fetchDetail` is: the operator tab and the Workspace call different
  // access-scoped endpoints and this component never learns which.
  //
  // `canManage` gates the AFFORDANCE, not the permission — the server
  // re-checks with its own predicate. It is false by default so a surface that
  // has not opted in shows a read-only panel rather than controls that 403
  // (AC #2: a user who may not delete does not see a control that fails).
  canManage: { type: Boolean, default: false },
  deleteCanvas: { type: Function, default: null },      // (id) => Promise
  bulkDeleteCanvases: { type: Function, default: null }, // (ids) => Promise<{deleted}>
  pinCanvas: { type: Function, default: null },          // (id, pinned) => Promise
  // The per-agent cap, so the header can warn BEFORE the agent hits the
  // refusal — the person who can act on it is not the one who receives it.
  canvasLimit: { type: Number, default: 0 },
})
const emit = defineEmits(['start-chat', 'changed'])

// ent#553 — a searchable, bounded list once the pile is real.
const SEARCH_THRESHOLD = 6
const query = ref('')
const manage = ref(false)
const selectedIds = ref([])
const busy = ref(false)
const actionError = ref('')
const actionNote = ref('')

// Sorted here as well as server-side: an optimistic pin or delete mutates the
// list in place, and re-sorting only on refetch would leave a just-pinned
// canvas sitting where it was.
const ordered = computed(() => sortCanvases(props.canvases))
const visible = computed(() => filterCanvases(ordered.value, query.value))
const showSearch = computed(() => ordered.value.length > SEARCH_THRESHOLD)
const headroom = computed(() => canvasHeadroom(props.canvases.length, props.canvasLimit))
const selection = computed(() => selectionState(selectedIds.value, visible.value))
const selectorVisible = computed(() => canvasSelectorVisible({
  visible: visible.value.length, manage: manage.value, query: query.value,
}))
const canManage = computed(() => props.canManage && !!props.deleteCanvas)

function ageOf(c) { return c?.updated_at ? relativeTime(c.updated_at) : 'never updated' }

function toggleSelected(id) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = [...next]
}

function toggleAll() {
  selectedIds.value = selection.value.all ? [] : visible.value.map((c) => c.canvas_id)
}

async function run(fn) {
  busy.value = true
  actionError.value = ''
  actionNote.value = ''
  try {
    return await fn()
  } catch (e) {
    // Named, never a generic failure: the person just tried to destroy or
    // reorder something and needs to know whether it happened.
    actionError.value = e?.response?.data?.detail || e?.message || 'That did not work.'
    return null
  } finally {
    busy.value = false
  }
}

async function togglePin(c) {
  if (!canManage.value || !props.pinCanvas) return
  const next = !c.pinned
  const ok = await run(() => props.pinCanvas(c.canvas_id, next))
  if (ok !== null) {
    c.pinned = next          // optimistic; `ordered` re-sorts immediately
    emitChanged()
  }
}

async function removeOne(c) {
  if (!canManage.value) return
  if (!window.confirm(bulkDeletePrompt(1))) return
  const ok = await run(() => props.deleteCanvas(c.canvas_id))
  if (ok !== null) {
    actionNote.value = bulkDeleteOutcome(1, [c.canvas_id])
    emitChanged()
  }
}

async function removeSelected() {
  if (!canManage.value || !selection.value.any) return
  const ids = selection.value.ids
  if (!window.confirm(bulkDeletePrompt(ids.length))) return
  const result = await run(() =>
    props.bulkDeleteCanvases ? props.bulkDeleteCanvases(ids) : null,
  )
  if (result !== null) {
    // Reports what the SERVER removed, not what was asked — "3 of 5" is the
    // honest line when someone else deleted one first.
    actionNote.value = bulkDeleteOutcome(ids.length, result?.deleted ?? ids)
    selectedIds.value = []
    emitChanged()
  }
}

function emitChanged() { emit('changed') }

const selectedId = ref(null)
const detail = ref(null)
const detailError = ref('')

const empty = computed(() => emptyState(props.viewer))
const selected = computed(
  () => detail.value || props.canvases.find((c) => c.canvas_id === selectedId.value) || null,
)
const fresh = computed(() => freshness(selected.value || {}))
const blocks = computed(() => renderableBlocks(detail.value?.blocks))
// null → stacked (no template, an unknown one, or nothing slotted).
const placement = computed(() => placeBlocks(detail.value?.template, blocks.value))

// Guards the fetch against a later selection: the list's own first pick and
// a reader's click a moment later are two fetches in flight, and without this
// the SLOWER one used to win — the header named one canvas and the blocks
// belonged to another (#2583, caught by the gallery on every switch).
let selectSeq = 0

async function select(id) {
  if (!id) return
  const seq = ++selectSeq
  selectedId.value = id
  detail.value = null
  detailError.value = ''
  try {
    const next = await props.fetchDetail(id)
    if (seq !== selectSeq) return // superseded
    detail.value = next
  } catch (e) {
    if (seq !== selectSeq) return
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

// ent#553 review — while a search is active the selection follows the MATCHES:
// the list-watcher above keys off the unfiltered `props.canvases`, so a
// narrowing to one hit never selected it. `canvasAutoSelect` is a no-op with
// no query and when the selection is already a match.
watch(
  () => [visible.value, query.value],
  () => {
    const next = canvasAutoSelect(visible.value, selectedId.value, query.value)
    if (next) select(next)
  },
)

// ent#475 — the selected canvas was REWRITTEN (same id, newer `updated_at`) by
// a refresh of the metadata list: re-read its blocks. Without this the rail's
// "updated" dot could light, the tab open, and the blocks on screen be the
// ones fetched before the agent's last write — the header would say "updated
// just now" over content that was not.
watch(
  () => props.canvases.find((c) => c.canvas_id === selectedId.value)?.updated_at,
  (updatedAt) => {
    if (!updatedAt || !detail.value) return
    if (detail.value.updated_at !== updatedAt) select(selectedId.value)
  },
)
</script>
