<!--
  A project's tasks as a board (trinity-enterprise#957).

  A second VIEW over the same tasks the list shows: one column per step of the
  path, a card per task, and a move is one gesture — drag a card to a column,
  or focus its title, pick a column with Left/Right and press Enter. This
  component draws and listens; it never writes. It is handed `columns` (from
  `projectsUtils.boardColumns`) and says what the person asked for:

    open(id) · move({ id, to }) · set-status({ id, status }) · confirm(id) · new
    busy(bool) — a drag or a keyboard pick is in progress, so the owner holds
                 fresh rows until it ends (they would land under the gesture).

  Rules it keeps on its own:
    - The dragged task is THIS component's state. The drag payload carries no
      id and is never read back: task ids repeat across projects, so a card
      dragged in from another window must not move a task here.
    - A card is a container with sibling controls (title, state menu, Confirm):
      a button inside a button is not valid.
    - Arrow keys only pick. Enter commits; Esc, or leaving the card, cancels.
-->
<template>
  <div ref="rootEl" data-testid="project-board">
    <p class="sr-only" aria-live="polite" data-testid="project-board-armed">{{ armedLine }}</p>
    <div class="grid items-start gap-3" :style="{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }">
      <section
        v-for="(col, ci) in columns"
        :key="col.key"
        :aria-label="`${col.label}, ${col.cards.length}`"
        :data-testid="`board-column-${col.key}`"
        :data-over="overKey === col.key ? 'true' : null"
        class="flex min-w-0 flex-col gap-2 rounded-lg border p-2"
        :class="overKey === col.key
          ? 'border-dashed border-action-primary-500 bg-action-primary-100 dark:border-action-primary-400 dark:bg-action-primary-500/16'
          : 'border-solid border-gray-200 bg-gray-100 dark:border-gray-700 dark:bg-gray-750'"
        @dragenter="onDragEnter($event, col)"
        @dragover="onDragOver"
        @dragleave="onDragLeave(col)"
        @drop="onDrop($event, col)"
      >
        <header class="flex flex-wrap items-center gap-x-2 gap-y-1 px-1 pt-1">
          <BaseBadge size="sm" :variant="col.badge">{{ col.label }}</BaseBadge>
          <span class="text-xs font-semibold tabular-nums text-gray-700 dark:text-gray-200" :data-testid="`board-count-${col.key}`">{{ col.cards.length }}</span>
          <span v-if="col.held.length" class="text-xs tabular-nums text-gray-600 dark:text-gray-300" :data-testid="`board-held-${col.key}`">
            {{ col.held.map((h) => `${h.count} ${h.label}`).join(' · ') }}
          </span>
        </header>
        <p v-if="hints[col.key]" class="px-1 text-xs text-gray-600 dark:text-gray-300" :data-testid="`board-hint-${col.key}`">{{ hints[col.key] }}</p>

        <div
          v-if="!col.cards.length"
          class="flex flex-col items-center gap-2 rounded-lg border border-dashed border-gray-300 px-3 py-4 text-center text-xs text-gray-600 dark:border-gray-600 dark:text-gray-300"
          :data-testid="`board-empty-${col.key}`"
        >
          <span>{{ col.first ? 'Nothing active. Add a task, or ask an agent on the project to break the goal into tasks.' : `Nothing ${col.label.toLowerCase()}.` }}</span>
          <BaseButton v-if="col.first && canCreate" size="sm" variant="ghost" data-testid="board-new-task" @click="$emit('new')">New task</BaseButton>
        </div>

        <BoundedRegion v-else :label="`${col.label} tasks`" :fill="false" class="max-h-[34rem] rounded-md">
          <ul class="flex flex-col gap-2 p-0.5">
            <li
              v-for="c in shown(col)"
              :key="c.id"
              :data-testid="`board-card-${c.id}`"
              :data-saving="c.saving ? 'true' : null"
              :aria-busy="c.saving || undefined"
              :draggable="movable(c) ? 'true' : 'false'"
              class="flex flex-col gap-1.5 rounded-lg border bg-white p-2.5 shadow-sm dark:bg-gray-800"
              :class="[
                dragId === c.id || c.saving ? 'border-dashed border-gray-300 dark:border-gray-600' : 'border-solid border-gray-200 dark:border-gray-700',
                movable(c) ? 'cursor-grab active:cursor-grabbing' : '',
              ]"
              @dragstart="onDragStart($event, c)"
              @dragend="resetDrag"
            >
              <div class="flex min-h-[1.5rem] items-center gap-1.5 text-xs">
                <span class="font-mono tabular-nums text-gray-500 dark:text-gray-400">{{ c.id }}</span>
                <BaseBadge v-if="c.p1" size="sm" variant="urgent">P1</BaseBadge>
                <BaseBadge v-if="c.flag" size="sm" :variant="c.flag.variant" :data-testid="`board-flag-${c.id}`">{{ c.flag.label }}</BaseBadge>
                <BaseBadge v-if="c.unclaimed" size="sm" variant="warning" :data-testid="`board-unclaimed-${c.id}`">Done without a done claim</BaseBadge>
                <span class="flex-1"></span>
                <span v-if="c.saving" class="font-medium text-status-info-700 dark:text-status-info-400" :data-testid="`board-saving-${c.id}`">Saving…</span>
                <span v-else-if="armed && armed.id === c.id" class="font-medium text-action-primary-700 dark:text-action-primary-300">→ {{ labelOf(armed.to) }} · Enter</span>
                <button
                  v-if="movable(c) && actions(c).length"
                  type="button"
                  class="rounded px-1.5 py-0.5 font-semibold text-gray-600 hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-300 dark:hover:bg-gray-750"
                  :aria-label="`Change the state of ${c.id}`"
                  :aria-expanded="menuId === c.id ? 'true' : 'false'"
                  :data-testid="`board-menu-${c.id}`"
                  @click="menuId = menuId === c.id ? '' : c.id"
                ><span aria-hidden="true">⋯</span></button>
              </div>

              <!-- role=button on a div, not a <button>: Firefox does not start a
                   drag from a button inside a draggable element, and the title
                   is where a card gets grabbed. Enter and Space are its own. -->
              <div
                :data-card-title="c.id"
                role="button"
                tabindex="0"
                class="cursor-pointer rounded text-left text-sm font-[550] leading-snug break-words hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
                :class="c.status === 'done' ? 'text-gray-600 line-through dark:text-gray-300' : 'text-gray-900 dark:text-gray-100'"
                :aria-describedby="movable(c) ? 'board-keys' : undefined"
                :data-testid="`board-open-${c.id}`"
                @click="$emit('open', c.id)"
                @keydown="onKey($event, c, ci)"
                @blur="onBlur(c)"
              >{{ c.title }}</div>

              <div v-if="menuId === c.id && movable(c)" class="flex flex-wrap gap-1" :data-testid="`board-actions-${c.id}`">
                <BaseButton
                  v-for="a in actions(c)"
                  :key="a.status"
                  size="sm"
                  variant="ghost"
                  :data-testid="`board-set-${c.id}-${a.status}`"
                  @click="setStatus(c, a.status)"
                >{{ a.label }}</BaseButton>
              </div>

              <p v-if="c.waitingOn || c.waitingFor || c.subtasks" class="break-words text-xs text-gray-600 dark:text-gray-300" :data-testid="`board-shape-${c.id}`">{{ [c.waitingOn && `Waiting on: ${c.waitingOn}`, c.waitingFor, c.subtasks].filter(Boolean).join(' · ') }}</p>

              <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-600 dark:text-gray-300">
                <span v-if="c.owner" class="inline-flex min-w-0 items-center gap-1" :title="`Assigned to ${c.owner}`">
                  <span class="flex h-4 w-4 flex-none items-center justify-center rounded-full bg-action-primary-100 text-[10px] font-semibold text-action-primary-700 dark:bg-action-primary-500/16 dark:text-action-primary-300" aria-hidden="true">{{ initial(c.owner) }}</span>
                  <span class="max-w-[9rem] truncate">{{ c.owner }}</span>
                </span>
                <span v-if="c.agent" class="inline-flex min-w-0 items-center gap-1" :title="`Assigned to the agent ${c.agent}`">
                  <span class="flex h-4 w-4 flex-none items-center justify-center rounded border border-gray-300 bg-gray-100 text-[10px] font-semibold text-gray-600 dark:border-gray-600 dark:bg-gray-750 dark:text-gray-300" aria-hidden="true">{{ initial(c.agent) }}</span>
                  <span class="max-w-[9rem] truncate">{{ c.agent }}</span>
                </span>
                <span v-if="!c.owner && !c.agent" class="text-gray-500 dark:text-gray-400">Unassigned</span>
                <span class="flex-1"></span>
                <span v-if="c.updatedAt" class="tabular-nums text-gray-500 dark:text-gray-400" :title="`Last changed ${formatLocalDateTime(c.updatedAt)}`">{{ age(c.updatedAt) }}</span>
              </div>
              <p v-if="c.creator" class="truncate text-xs text-gray-500 dark:text-gray-400">Created by {{ c.creator }}</p>

              <div v-if="c.status === 'pending-verification'" class="flex items-center gap-2 border-t border-gray-200 pt-1.5 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-300">
                <span class="flex-1 tabular-nums" :title="c.pendingSince ? formatLocalDateTime(c.pendingSince) : ''">{{ c.pendingSince ? `Waiting ${age(c.pendingSince)}` : 'Waiting for a check' }}</span>
                <BaseButton
                  v-if="canConfirm"
                  size="sm"
                  variant="secondary"
                  :loading="c.saving"
                  loading-label="Confirming…"
                  :data-testid="`board-confirm-${c.id}`"
                  @keydown.stop
                  @click="$emit('confirm', c.id)"
                >Confirm</BaseButton>
              </div>
            </li>
          </ul>
          <div v-if="col.cards.length > shown(col).length" class="p-0.5 pt-2">
            <BaseButton size="sm" variant="ghost" :data-testid="`board-show-all-${col.key}`" @click="expanded = { ...expanded, [col.key]: true }">Show all {{ col.cards.length }}</BaseButton>
          </div>
        </BoundedRegion>
      </section>
    </div>
    <p id="board-keys" class="sr-only">Press Left or Right to pick a column, Enter to move the task there, Escape to cancel.</p>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onBeforeUpdate, onMounted, onUpdated, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BoundedRegion from '@/components/BoundedRegion.vue'
import { formatCompactAge, formatLocalDateTime, getTimestampMs } from '@/utils/timestamps'
import { boardStateActions } from './projectsUtils'

const props = defineProps({
  // From `projectsUtils.boardColumns`: [{ key, label, badge, first, cards, held }].
  columns: { type: Array, required: true },
  // May this person move cards (a contributor on a live project)?
  canMove: { type: Boolean, default: false },
  canConfirm: { type: Boolean, default: false },
  canCreate: { type: Boolean, default: false },
  // A line under a column's header, by column key (the verification rule).
  hints: { type: Object, default: () => ({}) },
  // How many cards a column shows before "Show all" (Done grows without limit).
  limit: { type: Number, default: 20 },
})
const emit = defineEmits(['open', 'move', 'set-status', 'confirm', 'new', 'busy'])

// A payload type of our own: Firefox starts a drag only when data is set, and
// nothing else on the page (a file, selected text) carries this type.
const DRAG_TYPE = 'application/x-trinity-task'

const dragId = ref('')
const overKey = ref('')
const armed = ref(null)      // { id, to } — a keyboard pick waiting for Enter
const menuId = ref('')
const expanded = ref({})
const rootEl = ref(null)
let depth = {}               // per column: dragenter minus dragleave (children fire both)

const movable = (c) => props.canMove && !c.saving
const actions = (c) => boardStateActions(c.task)
const labelOf = (key) => props.columns.find((c) => c.key === key)?.label || ''
const initial = (name) => String(name || '?').trim().slice(0, 1).toUpperCase()
const age = (ts) => formatCompactAge(Date.now() - getTimestampMs(ts))
const shown = (col) => (expanded.value[col.key] ? col.cards : col.cards.slice(0, props.limit))
const armedLine = computed(() => (armed.value ? `${armed.value.id}: move to ${labelOf(armed.value.to)}. Press Enter to confirm, Escape to cancel.` : ''))

watch(() => Boolean(dragId.value || armed.value), (busy) => emit('busy', busy))

// --- drag -------------------------------------------------------------------------

function resetDrag() {
  dragId.value = ''
  overKey.value = ''
  depth = {}
}

function onDragStart(event, card) {
  if (!movable(card)) { event.preventDefault(); return }
  dragId.value = card.id
  armed.value = null
  menuId.value = ''
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    try { event.dataTransfer.setData(DRAG_TYPE, '1') } catch { /* a browser that refuses the type still drags */ }
  }
}

// Every handler below does nothing without a drag that started HERE: a card
// from another window, a file or selected text passes through untouched.
function onDragEnter(event, col) {
  if (!dragId.value) return
  event.preventDefault()
  depth[col.key] = (depth[col.key] || 0) + 1
  overKey.value = col.key
}

function onDragOver(event) {
  if (!dragId.value) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
}

function onDragLeave(col) {
  if (!dragId.value) return
  depth[col.key] = Math.max(0, (depth[col.key] || 0) - 1)
  if (!depth[col.key] && overKey.value === col.key) overKey.value = ''
}

function onDrop(event, col) {
  if (!dragId.value) return
  event.preventDefault()
  const id = dragId.value
  resetDrag()
  emit('move', { id, to: col.key })
}

// `dragend` is delivered to the dragged element; if that element was re-drawn
// mid-drag it may never arrive, and the board would stay "busy" for good. The
// document hears the end of every drag, and a pointer moving with no button
// held means no drag is in progress.
const onDocEnd = () => { if (dragId.value) setTimeout(resetDrag, 0) }
const onPointerMove = (event) => { if (dragId.value && event.buttons === 0) resetDrag() }

onMounted(() => {
  document.addEventListener('dragend', onDocEnd, true)
  document.addEventListener('drop', onDocEnd, true)
  document.addEventListener('pointermove', onPointerMove, true)
})
onBeforeUnmount(() => {
  document.removeEventListener('dragend', onDocEnd, true)
  document.removeEventListener('drop', onDocEnd, true)
  document.removeEventListener('pointermove', onPointerMove, true)
  if (dragId.value || armed.value) emit('busy', false)
})

// --- keyboard ---------------------------------------------------------------------

function onKey(event, card, columnIndex) {
  const mine = armed.value && armed.value.id === card.id ? armed.value : null
  // With nothing picked, Enter and Space open the task — for readers too.
  if (!mine && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault()
    emit('open', card.id)
    return
  }
  if (!movable(card)) return
  const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key]
  if (step) {
    event.preventDefault()
    const from = mine ? props.columns.findIndex((c) => c.key === mine.to) : columnIndex
    const next = Math.min(props.columns.length - 1, Math.max(0, from + step))
    armed.value = next === columnIndex ? null : { id: card.id, to: props.columns[next].key }
    return
  }
  if (!mine) return
  if (event.key === 'Enter') {
    // This Enter moves the task; it does not also open it.
    event.preventDefault()
    armed.value = null
    emit('move', { id: card.id, to: mine.to })
  } else if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    armed.value = null
  }
}

function onBlur(card) {
  if (armed.value && armed.value.id === card.id) armed.value = null
}

function setStatus(card, status) {
  menuId.value = ''
  emit('set-status', { id: card.id, status })
}

// --- focus ------------------------------------------------------------------------
// A card that changes column is a different element. If its title held focus
// before the re-draw, the same task's title holds it after (principle 5).
// (Found by the task id on the title, not by a held element: the old card is
// removed AFTER the new one is drawn, so a per-id element map ends up empty.)
let focusedId = ''
onBeforeUpdate(() => {
  // Only ever SET here: a re-draw can run this hook twice before `updated`,
  // and the second time the old card is already gone and nothing has focus.
  const el = document.activeElement
  if (el && rootEl.value?.contains(el) && el.dataset?.cardTitle) focusedId = el.dataset.cardTitle
})
onUpdated(() => {
  const id = focusedId
  focusedId = ''
  if (!id || !rootEl.value) return
  const el = [...rootEl.value.querySelectorAll('[data-card-title]')].find((n) => n.dataset.cardTitle === id)
  if (el && el !== document.activeElement) el.focus({ preventScroll: true })
})
</script>
