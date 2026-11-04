<template>
  <!--
    "Assign skills" on the Skills tab's Shared section (trinity-enterprise#754):
    the library picker that used to sit at the bottom of the Skills tab, moved
    into a dialog so the tab is one grid of cards. Behaviour unchanged from
    ent#235 / ent#530 / ent#672 / #2703 / #2914: the draft is the INDIVIDUAL
    list the bulk PUT replaces; a set-only member is ticked and locked; a
    deprecated skill is marked before it is ticked; a draft survives a sync
    that re-reads the same assignment set.
    Select all / Deselect all build the draft in one click, over the rows
    listed here only; Save asks first when it would drop an approval
    requirement. The rules are utils/skillAssignDraft.js.
    trinity-enterprise#660: the filter narrows the rows and both bulk buttons,
    never the draft, so a tick it hides is still counted and saved, and each
    row says what Save would do to it.
  -->
  <BaseModal
    :model-value="modelValue"
    aria-label="Assign shared skills"
    panel-class="relative flex max-h-[calc(100dvh-2rem)] w-full max-w-xl flex-col overflow-y-auto rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    :close-on-backdrop="!dirty"
    @update:model-value="$emit('update:modelValue', $event)"
    @focusin="onFocusIn"
  >
    <h3 class="text-[18px] font-[650] text-gray-900 dark:text-gray-100">Assign shared skills</h3>
    <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">
      Tick to assign, then save. Assigned skills are copied into the agent now if it is running, or when it next starts.
    </p>

    <SkillFilterBar
      v-model:query="query"
      v-model:source="source"
      v-model:category="category"
      :source-options="sourceOptions"
      :category-options="categoryOptions"
      testid-prefix="skill-assign"
      class="mt-3"
    />

    <!-- The list's header row. Both buttons are always drawn, and only their
         disabled state changes, so nothing here moves the list. -->
    <div class="mt-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
      <span class="text-[12.5px] tabular-nums text-gray-600 dark:text-gray-300" data-testid="skill-assign-count">
        {{ countLine }}
      </span>
      <div class="flex items-center gap-1">
        <BaseButton
          ref="selectAllButton"
          variant="ghost"
          size="sm"
          :disabled="bulk.select.disabled"
          :title="bulk.select.title"
          data-testid="skill-assign-select-all"
          @click="selectAll"
        >Select all</BaseButton>
        <!-- The removal verb: never where the dialog opens (data-destructive). -->
        <BaseButton
          ref="deselectAllButton"
          variant="ghost"
          size="sm"
          :disabled="bulk.deselect.disabled"
          :title="bulk.deselect.title"
          data-testid="skill-assign-deselect-all"
          data-destructive
          @click="deselectAll"
        >Deselect all</BaseButton>
      </div>
    </div>

    <!-- One height whatever the filter leaves: the panel is centred, so a list
         that shrank would move the input under the cursor (principle 6). It
         gives way, down to its floor, on a window too short for the panel. -->
    <ul
      ref="listEl"
      class="mt-2 h-[50vh] min-h-40 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750"
      data-testid="skill-assign-list"
    >
      <li
        v-if="!listed.length"
        class="px-4 py-3 text-[12.5px] text-gray-600 dark:text-gray-300"
        data-testid="skill-assign-no-match"
      >{{ store.library.length ? 'No skill matches the filter.' : 'The library has no skills yet.' }}</li>
      <li v-for="s in listed" :key="s.name" class="px-4 py-3">
        <label
          class="flex items-start gap-3"
          :class="isLocked(s.name) ? 'cursor-not-allowed' : 'cursor-pointer'"
          :title="isLocked(s.name) ? `Assigned via ${store.viaSets(s.name).join(', ')} — unassign the set to remove it` : undefined"
        >
          <input
            v-if="isLocked(s.name)"
            type="checkbox"
            checked
            disabled
            :data-testid="`skill-locked-${s.name}`"
            class="mt-1 rounded text-action-primary-600 disabled:opacity-45"
          />
          <input
            v-else
            v-model="draft"
            type="checkbox"
            :value="s.name"
            class="mt-1 rounded text-action-primary-600 focus:ring-action-primary-500"
          />
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">{{ s.name }}</span>
              <BaseBadge
                v-if="s.deprecated"
                variant="warning"
                :title="DEPRECATED_TITLE"
                :data-testid="`skill-deprecated-picker-${s.name}`"
              >deprecated</BaseBadge>
              <SkillContractChips :skill="s" />
            </div>
            <!-- trinity-enterprise#584: the one empty state. -->
            <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300" :class="{ italic: describeSkill(s.description).missing }">{{ describeSkill(s.description).text }}</p>
            <p
              v-if="supersededLine(s)"
              :title="supersededLine(s)"
              :data-testid="`skill-superseded-picker-${s.name}`"
              class="mt-1 text-[12.5px] text-status-warning-700 dark:text-status-warning-400 break-words line-clamp-2"
            >{{ supersededLine(s) }}</p>
            <!-- Declared dependencies, surfaced BEFORE assignment: this is what
                 turns into a missing_binary/missing_env warning later. -->
            <p v-if="deps(s)" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">Requires {{ deps(s) }}</p>
          </div>
          <!-- Reserved, so a tick never reflows the row (principle 6). -->
          <span class="flex w-20 flex-none justify-end pt-0.5">
            <BaseBadge
              v-if="marks.get(s.name)"
              size="sm"
              :variant="marks.get(s.name).variant"
              :title="marks.get(s.name).kind === 'kept' ? keptTitle(store.viaSets(s.name)) : undefined"
              :data-testid="`skill-pending-${s.name}`"
            >{{ marks.get(s.name).label }}</BaseBadge>
          </span>
        </label>
      </li>
    </ul>

    <!-- Below 640 px the verbs take the first row and the count shares the
         second with Close, so neither row comes and goes with `dirty`. -->
    <div class="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2">
      <div class="flex w-full items-center gap-3 sm:w-auto">
        <BaseButton
          :disabled="!dirty"
          :loading="store.saving"
          loading-label="Saving…"
          data-testid="skill-assign-save"
          @click="onSaveClick"
        >
          Save assignments
        </BaseButton>
        <BaseButton
          v-if="dirty"
          variant="ghost"
          data-testid="skill-assign-reset"
          @click="resetDraft"
        >Reset</BaseButton>
      </div>
      <span
        class="min-w-0 flex-1 text-[12.5px] tabular-nums text-gray-600 dark:text-gray-300"
        aria-live="polite"
        data-testid="skill-assign-pending"
      >{{ pendingText }}</span>
      <BaseButton
        ref="closeButton"
        variant="secondary"
        class="ml-auto"
        data-testid="skill-assign-close"
        @click="$emit('update:modelValue', false)"
      >Close</BaseButton>
    </div>
    <p
      v-if="notice && notice.text"
      data-testid="skills-saved-note"
      :data-tone="notice.tone"
      class="mt-3 text-[12.5px]"
      :class="noticeClass(notice.tone)"
    >{{ notice.text }}</p>
    <p
      v-if="notice && notice.text && notice.deprecation"
      data-testid="skills-saved-deprecation"
      class="mt-1 text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
    >{{ notice.deprecation }}</p>
    <p v-if="store.error" class="mt-2 text-[12.5px] text-status-danger-700 dark:text-status-danger-400">{{ store.error }}</p>

    <!-- Principle 19: a save that drops an approval requirement restates it
         first, and opens on Cancel (the card's Unassign does the same). -->
    <ConfirmDialog
      :visible="!!pendingRemoval"
      :title="removalCopy.title"
      :message="removalCopy.message"
      :confirm-text="REMOVAL_CONFIRM_TEXT"
      variant="warning"
      @confirm="confirmRemoval"
      @cancel="pendingRemoval = null"
    />
  </BaseModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { describeSkill } from '../../utils/skillDescription'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseModal from '../base/BaseModal.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import SkillContractChips from './SkillContractChips.vue'
import SkillFilterBar from './SkillFilterBar.vue'
import { deps, supersededLine, DEPRECATED_TITLE } from './contract'
import { useSkillFilter } from '../../composables/useSkillFilter'
import { useSkillsStore } from '../../stores/skills'
import { useSkillGatesStore } from '../../stores/skillGates'
import { deliveryText, deprecationText } from '../../utils/skillDelivery'
import {
  bulkSelection, withSelectAll, withDeselectAll, removedNames, gatedRemovals, removalConfirm, REMOVAL_CONFIRM_TEXT,
  pendingChanges, pendingLine, pendingMarks, keptTitle,
} from '../../utils/skillAssignDraft'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // The tab's shared notice ({text, tone, deprecation}), so a sync on the tab
  // clears the note here too — one note, one owner.
  notice: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const store = useSkillsStore()
const gatesStore = useSkillGatesStore()
const draft = ref([...store.individualNames])
// A save that would take a skill with an approval requirement off the agent
// asks first (a person's unassign drops it, trinity-enterprise#753):
// {names, gatesKnown} while the confirm is open.
const pendingRemoval = ref(null)

// The rows this dialog lists. The list and both bulk buttons read this one
// value, so the filter narrows all three at once (trinity-enterprise#660).
// Nothing that decides a save reads it: those read the whole draft.
const {
  query, source, category, sourceOptions, categoryOptions, filtered: listed, active: filtering, reset: resetFilter,
} = useSkillFilter(() => store.library)
const isLocked = (name) => store.setOnlyNames.has(name)

// ent#530: the draft is the INDIVIDUAL list — what the bulk PUT replaces.
const dirty = computed(() => {
  const a = [...draft.value].sort().join('|')
  const b = [...store.individualNames].sort().join('|')
  return a !== b
})

const countLine = computed(() => {
  const total = store.library.length
  if (filtering.value) return `${total} in the library · ${listed.value.length} shown`
  return `${total} skill${total === 1 ? '' : 's'} in the library`
})

// A new query starts at the first row.
const listEl = ref(null)
watch([query, source, category], () => {
  if (listEl.value) listEl.value.scrollTop = 0
})

function resetDraft() {
  draft.value = [...store.individualNames]
}

// #2914: reset only when the assignment SET changes — not on every refetch of
// the rows, or a Sync (which re-reads them) would wipe unsaved ticks.
watch(() => [...store.individualNames].sort().join('|'), resetDraft)
// trinity-enterprise#754 (PR review): a draft is this opening's. The tab
// outlives an agent switch, and two agents with the same assignments give the
// watcher above nothing to see, so an unsaved tick would be offered, and
// saved, on the next agent. Closing without saving discards it.
watch(() => props.modelValue, (open) => {
  if (!open) return
  resetDraft()
  resetFilter()
  pendingRemoval.value = null
  lastFocus = null
})

// trinity-enterprise#660: what a save would change, over the whole draft, in
// the removal confirm's own terms (`setHeldNames` below).
const pending = computed(() => pendingChanges({
  saved: store.individualNames,
  draft: draft.value,
  setHeld: setHeldNames(),
}))
const pendingText = computed(() => pendingLine(pending.value))
const marks = computed(() => pendingMarks(pending.value))

// trinity-enterprise#539: Select all / Deselect all change the draft only;
// Save still writes, and Reset still restores the saved set.
const bulk = computed(() => bulkSelection({
  rows: listed.value,
  draft: draft.value,
  locked: store.setOnlyNames,
  loaded: store.sharedLoaded,
  emptyReason: store.emptyReason,
  saving: store.saving,
}))

const selectAllButton = ref(null)
const deselectAllButton = ref(null)
const closeButton = ref(null)

function selectAll() {
  draft.value = withSelectAll(draft.value, bulk.value.toAdd)
}

function deselectAll() {
  draft.value = withDeselectAll(draft.value, bulk.value.toRemove)
}

// Focus can sit on a control this dialog then disables (a bulk button with
// nothing left to change, every button while a save runs) or removes (Reset
// once the draft is clean, the confirm's buttons). It then falls to <body>,
// outside the overlay that owns Esc and Tab. After every change that can do
// that, put it back: on the bulk button that undoes the one that lost it,
// else on Close. `lastFocus` is the last control inside the dialog that held
// focus, for when the one that had it is already gone. A row the filter removes
// under focus lands here too (Safari keeps focus on a checkbox across a mouse
// pick in a <select>): `bulk` reads the listed rows, and the source below is a
// fresh array, so any change to them re-runs this.
let lastFocus = null
function onFocusIn(event) {
  lastFocus = event.target
}

watch(
  () => [bulk.value.select.disabled, bulk.value.deselect.disabled, dirty.value, store.saving, pendingRemoval.value],
  () => {
    const close = closeButton.value?.$el
    if (!props.modelValue || pendingRemoval.value || !close) return
    const active = document.activeElement
    let from = null
    if (active && active.disabled && close.closest('[role="dialog"]')?.contains(active)) from = active
    else if ((!active || active === document.body) && lastFocus) from = lastFocus
    if (!from) return
    const select = selectAllButton.value?.$el
    const deselect = deselectAllButton.value?.$el
    const undo = from === select ? deselect : from === deselect ? select : null
    ;(undo && !undo.disabled ? undo : close).focus()
  },
  { flush: 'post' },
)

const removalCopy = computed(() => (pendingRemoval.value
  ? removalConfirm(pendingRemoval.value)
  : { title: '', message: '' }))

// Names an assigned set still holds, so a save only demotes them. Trusted
// only when every held set resolved: with one unresolved, the server keeps
// just the set-only rows (`db.set_agent_skills` fails closed).
function setHeldNames() {
  if (!store.setsLoaded || store.setsError || store.sets.some((s) => s.status === 'unresolved')) return new Set()
  return new Set(store.assigned.filter((s) => s.via_sets?.length).map((s) => s.skill_name))
}

// One decision at a time: a second click while the gate map is re-read is the
// same click.
let deciding = false

async function onSaveClick() {
  if (deciding) return
  const agent = store.agentName
  if (removedNames({ saved: store.individualNames, draft: draft.value, setHeld: setHeldNames() }).length) {
    // Decide on a fresh map. The tab read it when it opened, and a save (the
    // library's default requirements), another session or an MCP call may
    // have changed it since.
    deciding = true
    try {
      await gatesStore.load(agent)
    } finally {
      deciding = false
    }
    if (agent !== store.agentName || !props.modelValue) return
  }
  const removal = gatedRemovals({
    saved: store.individualNames,
    draft: draft.value,
    setHeld: setHeldNames(),
    gated: new Set(gatesStore.gates.map((g) => String(g.skill_name || '').toLowerCase())),
    conflict: store.conflictNames,
    gatesKnown: gatesStore.hasLoaded && !gatesStore.error,
  })
  if (removal) {
    pendingRemoval.value = removal
    return
  }
  onSave()
}

function confirmRemoval() {
  pendingRemoval.value = null
  onSave()
}

async function onSave() {
  if (await store.saveAssignments([...draft.value])) {
    resetDraft()
    // #2703: the PUT delivers; say what happened, in the delivery's own tone.
    const verdict = deliveryText(store.lastDelivery, { saved: true })
    emit('saved', {
      text: verdict.text,
      tone: verdict.tone,
      needsSync: verdict.needsSync,
      deprecation: deprecationText(store.lastDelivery) || '',
    })
  }
}

function noticeClass(tone) {
  if (tone === 'bad') return 'text-status-danger-700 dark:text-status-danger-400'
  if (tone === 'pending') return 'text-status-warning-700 dark:text-status-warning-400'
  return 'text-status-success-700 dark:text-status-success-400'
}
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
