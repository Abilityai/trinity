<template>
  <!--
    "Assign skills" on the Skills tab's Shared section (trinity-enterprise#754):
    the library picker that used to sit at the bottom of the Skills tab, moved
    into a dialog so the tab is one grid of cards. Behaviour unchanged from
    ent#235 / ent#530 / ent#672 / #2703 / #2914: the draft is the INDIVIDUAL
    list the bulk PUT replaces; a set-only member is ticked and locked; a
    deprecated skill is marked before it is ticked; a draft survives a sync
    that re-reads the same assignment set.
  -->
  <BaseModal
    :model-value="modelValue"
    aria-label="Assign shared skills"
    panel-class="relative w-full max-w-xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <h3 class="text-[18px] font-[650] text-gray-900 dark:text-gray-100">Assign shared skills</h3>
    <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">
      {{ store.library.length }} skill{{ store.library.length === 1 ? '' : 's' }} in the library.
      Tick to assign, then save. Assigned skills are copied into the agent now if it is running, or when it next starts.
    </p>

    <ul
      class="mt-3 max-h-[50vh] overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750"
      data-testid="skill-assign-list"
    >
      <li v-for="s in store.library" :key="s.name" class="px-4 py-3">
        <label
          class="flex items-start gap-3"
          :class="store.setOnlyNames.has(s.name) ? 'cursor-not-allowed' : 'cursor-pointer'"
          :title="store.setOnlyNames.has(s.name) ? `Assigned via ${store.viaSets(s.name).join(', ')} — unassign the set to remove it` : undefined"
        >
          <input
            v-if="store.setOnlyNames.has(s.name)"
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
            <p v-if="s.description" class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">{{ s.description }}</p>
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
        </label>
      </li>
    </ul>

    <div class="mt-4 flex items-center gap-3">
      <BaseButton :disabled="!dirty" :loading="store.saving" loading-label="Saving…" @click="onSave">
        Save assignments
      </BaseButton>
      <BaseButton v-if="dirty" variant="ghost" @click="resetDraft">Reset</BaseButton>
      <BaseButton variant="secondary" class="ml-auto" @click="$emit('update:modelValue', false)">Close</BaseButton>
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
  </BaseModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseModal from '../base/BaseModal.vue'
import SkillContractChips from './SkillContractChips.vue'
import { deps, supersededLine, DEPRECATED_TITLE } from './contract'
import { useSkillsStore } from '../../stores/skills'
import { deliveryText, deprecationText } from '../../utils/skillDelivery'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // The tab's shared notice ({text, tone, deprecation}), so a sync on the tab
  // clears the note here too — one note, one owner.
  notice: { type: Object, default: null },
})

const emit = defineEmits(['update:modelValue', 'saved'])

const store = useSkillsStore()
const draft = ref([...store.individualNames])

// ent#530: the draft is the INDIVIDUAL list — what the bulk PUT replaces.
const dirty = computed(() => {
  const a = [...draft.value].sort().join('|')
  const b = [...store.individualNames].sort().join('|')
  return a !== b
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
watch(() => props.modelValue, (open) => { if (open) resetDraft() })

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
