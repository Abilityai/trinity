<template>
  <!--
    One skill on the agent's Skills tab (trinity-enterprise#754). The approved
    design: every slot has a FIXED height, so no content — a long description,
    no hint, a gate or none, a badge row that wraps — changes the card's size
    (the grid never jitters). Long text clips; the full text is on hover or in
    the details dialog. All decisions arrive decided in `card`
    (utils/skillCards.js); this component only renders them and emits verbs.
  -->
  <BaseCard
    flush
    class="flex flex-col px-4 pt-3"
    :class="card.missing ? 'border-dashed' : ''"
    :data-testid="`skill-card-${card.section}-${card.name}`"
  >
    <!-- Name (+ the library version for a shared skill) -->
    <div class="h-5 flex items-center gap-2 min-w-0">
      <span
        class="font-mono text-[14px] font-[550] truncate"
        :class="card.missing ? 'text-gray-500 dark:text-gray-400 line-through' : 'text-action-primary-600 dark:text-action-primary-400'"
        :title="`/${card.name}`"
      >/{{ card.name }}</span>
      <span v-if="card.version" class="flex-none font-mono text-[11px] text-gray-500 dark:text-gray-400">{{ card.version.slice(0, 7) }}</span>
    </div>

    <!-- One badge area, filled from the top-left: the author's mode chip
         first, then the platform's facts. Reserved: one line on an own card,
         two on a shared one (wraps only when the badges don't fit). -->
    <div
      class="mt-1 flex flex-wrap content-start items-center gap-x-[5px] gap-y-1.5 overflow-hidden"
      :class="card.section === 'shared' ? 'h-[46px]' : 'h-5'"
      data-testid="skill-card-badges"
    >
      <BaseBadge
        v-if="card.mode"
        size="sm"
        :variant="card.mode.variant"
        :title="card.mode.title"
        :data-testid="`skill-mode-${card.name}`"
      >
        <svg v-if="card.mode.icon" class="h-2.5 w-2.5 flex-none" viewBox="0 0 16 16" aria-hidden="true">
          <path v-if="card.mode.icon === 'loop'" d="M3 8a5 5 0 0 1 8.6-3.5L13 6M13 2.5V6H9.5M13 8a5 5 0 0 1-8.6 3.5L3 10M3 13.5V10h3.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" />
          <path v-else-if="card.mode.icon === 'pause'" d="M5.5 3.5v9M10.5 3.5v9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
          <g v-else fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
            <circle cx="8" cy="5" r="2.7" />
            <path d="M3 14c.6-2.8 2.6-4.2 5-4.2s4.4 1.4 5 4.2" />
          </g>
        </svg>
        {{ card.mode.label }}
      </BaseBadge>
      <BaseBadge
        v-for="b in card.badges || []"
        :key="b.label"
        :variant="b.variant"
        :dot="!!b.dot"
        :title="b.title"
        :data-testid="b.testid"
      >{{ b.label }}</BaseBadge>
    </div>

    <!-- Description + argument hint — or, while a verb's error stands, the
         error in their place (next to the controls, until dismissed). -->
    <div class="mt-1.5 h-[57px]">
      <div v-if="error" class="h-full overflow-y-auto" :title="error">
        <InlineError :message="error" @dismiss="$emit('dismiss-error')" />
      </div>
      <template v-else>
        <p
          class="h-[38px] text-[12.5px] leading-[19px] line-clamp-2"
          :class="description ? 'text-gray-600 dark:text-gray-300' : 'italic text-gray-500 dark:text-gray-400'"
          :title="description || undefined"
        >{{ description || (card.missing ? 'This gate stays until someone clears it.' : 'No description') }}</p>
        <p
          class="mt-[3px] h-4 font-mono text-[11px] leading-4 truncate text-gray-500 dark:text-gray-400"
          :title="card.argumentHint || undefined"
        >{{ card.argumentHint || '' }}</p>
      </template>
    </div>

    <!-- Note line (shared): one line; a long one opens the details. -->
    <div v-if="card.section === 'shared'" class="mt-1 h-[18px] text-[12.5px] leading-[18px]">
      <button
        v-if="card.note && card.note.detail"
        type="button"
        class="block w-full truncate text-left hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 rounded"
        :class="toneClass(card.note.tone)"
        :title="card.note.text"
        :data-testid="card.note.testid"
        @click="$emit('details')"
      >{{ card.note.text }}</button>
      <p
        v-else-if="card.note"
        class="truncate"
        :class="toneClass(card.note.tone)"
        :title="card.note.text"
        :data-testid="card.note.testid"
      >{{ card.note.text }}</p>
    </div>

    <!-- Gate line: shown to everyone; names a kind, never a person. -->
    <div class="mt-1.5 h-5 flex items-center gap-1.5 text-[12.5px] min-w-0" :data-testid="`skill-gate-${card.name}`">
      <template v-if="card.gateLine">
        <svg v-if="card.gateLine.tone === 'locked'" class="h-3 w-3 flex-none" :class="toneClass('locked')" viewBox="0 0 16 16" aria-hidden="true">
          <rect x="3.5" y="7" width="9" height="7" rx="1.5" fill="currentColor" />
          <path d="M5.5 7V5.2a2.5 2.5 0 0 1 5 0V7" fill="none" stroke="currentColor" stroke-width="1.7" />
        </svg>
        <svg v-else class="h-3 w-3 flex-none" :class="toneClass('warning')" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 2.2 14.5 13.5h-13z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" />
          <path d="M8 6.5v3M8 11.6v.1" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
        </svg>
        <span class="truncate" :class="toneClass(card.gateLine.tone === 'locked' ? 'locked' : 'warning')" :title="card.gateLine.text">{{ card.gateLine.text }}</span>
      </template>
    </div>

    <!-- Verbs -->
    <div class="mt-2 h-11 flex items-center gap-1.5 border-t border-gray-200 dark:border-gray-750">
      <template v-if="!card.missing">
        <BaseButton
          size="sm"
          class="flex-1"
          :disabled="!card.run.enabled || running"
          :loading="running"
          loading-label="Starting…"
          :title="card.run.title"
          :data-testid="`skill-run-${card.name}`"
          @click="$emit('run')"
        >Run</BaseButton>
        <BaseButton
          size="sm"
          variant="secondary"
          :disabled="!card.canEditRun"
          title="Add instructions, then run"
          :data-testid="`skill-edit-run-${card.name}`"
          @click="$emit('edit-run')"
        >Edit &amp; Run</BaseButton>
        <BaseButton
          v-if="card.controls.unassign.show"
          size="sm"
          variant="danger"
          :disabled="card.controls.unassign.disabled || busy"
          :title="card.controls.unassign.title"
          :data-testid="`skill-unassign-${card.name}`"
          @click="$emit('unassign')"
        >Unassign</BaseButton>
      </template>
      <BaseButton
        v-else-if="card.controls.clear"
        size="sm"
        variant="secondary"
        :loading="busy"
        loading-label="Clearing…"
        :data-testid="`skill-clear-gate-${card.name}`"
        @click="$emit('clear-gate')"
      >Clear gate</BaseButton>
    </div>

    <!-- The owner's approval row: on every card or on none, by who is looking. -->
    <div
      v-if="showOwnerRow"
      class="h-10 flex items-center gap-2 border-t border-gray-200 dark:border-gray-750 text-[12.5px] text-gray-600 dark:text-gray-300"
    >
      <template v-if="card.controls.approval">
        <BaseToggle
          :model-value="!!card.gate"
          label="Requires approval"
          :disabled="busy || !!card.controls.toggleDisabled"
          :title="card.controls.toggleDisabled || undefined"
          :data-testid="`skill-approval-${card.name}`"
          @update:model-value="onToggle"
        />
        <BaseSelect
          class="ml-auto w-[150px]"
          :model-value="selectedKind"
          :disabled="busy || !card.gate"
          :aria-label="`Who approves /${card.name}`"
          :data-testid="`skill-approver-${card.name}`"
          @update:model-value="onApprover"
        >
          <option v-for="k in kindOptions" :key="k.kind" :value="k.kind" :disabled="!k.reachable">
            {{ k.label }}{{ k.reachable ? '' : ' (nobody yet)' }}
          </option>
        </BaseSelect>
      </template>
      <span v-else-if="card.missing && card.gate && card.gate.set_at" class="truncate">
        Gate set {{ new Date(card.gate.set_at).toLocaleDateString() }}
      </span>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseCard from '../base/BaseCard.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseToggle from '../base/BaseToggle.vue'
import InlineError from '../InlineError.vue'
import { kindOptionLabel } from '../../utils/skillCards'

const props = defineProps({
  card: { type: Object, required: true },
  // Owner or admin, not ephemeral, not the system agent — the same for every
  // card on the tab, so every card is the same size for one viewer.
  showOwnerRow: { type: Boolean, default: false },
  // [{kind, reachable, viewer_fills}] from the gate map.
  approvers: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  running: { type: Boolean, default: false },
  error: { type: String, default: '' },
})

const emit = defineEmits(['run', 'edit-run', 'set-gate', 'clear-gate', 'unassign', 'details', 'dismiss-error'])

const description = computed(() => (props.card.missing ? '' : props.card.description || ''))

const kindOptions = computed(() => {
  const list = props.approvers.length ? props.approvers : [{ kind: 'primary', reachable: true }]
  const out = list.map((a) => ({ kind: a.kind, reachable: a.reachable !== false, label: kindOptionLabel(a.kind) }))
  // A gate whose kind this install no longer offers still shows what it is.
  const current = props.card.gate?.approver
  if (current && !out.some((o) => o.kind === current)) {
    out.push({ kind: current, reachable: false, label: kindOptionLabel(current) })
  }
  return out
})

// Turning approval on sends the kind shown here: the gate's own, else the
// first kind that reaches someone (never a bodiless reset to `primary`).
const defaultKind = () => kindOptions.value.find((k) => k.reachable)?.kind || 'primary'
const selectedKind = ref(props.card.gate?.approver || defaultKind())
watch(() => props.card.gate?.approver, (kind) => { selectedKind.value = kind || defaultKind() })

function onToggle(on) {
  if (on) emit('set-gate', selectedKind.value)
  else emit('clear-gate')
}

function onApprover(kind) {
  selectedKind.value = kind
  if (props.card.gate && kind !== props.card.gate.approver) emit('set-gate', kind)
}

function toneClass(tone) {
  if (tone === 'locked') return 'text-state-locked-700 dark:text-state-locked-400'
  if (tone === 'danger') return 'text-status-danger-700 dark:text-status-danger-400'
  if (tone === 'muted') return 'text-gray-500 dark:text-gray-400'
  return 'text-status-warning-700 dark:text-status-warning-400'
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
