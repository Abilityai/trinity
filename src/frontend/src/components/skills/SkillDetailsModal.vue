<template>
  <!--
    The whole story of one shared skill (trinity-enterprise#754). A Skills card
    has a fixed footprint, so what does not fit one line — the #2914 name
    conflict and its two ways out, every named delivery warning, the last
    sync's error, the full "Superseded by" text — opens here from the card's
    note line, nothing lost from the old Skills tab.
  -->
  <BaseModal
    :model-value="modelValue && !!card"
    :aria-label="card ? `Details for ${card.name}` : 'Skill details'"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <template v-if="card">
      <h3 class="font-mono text-[18px] font-[650] text-gray-900 dark:text-gray-100 break-all">/{{ card.name }}</h3>
      <p v-if="card.description" class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">{{ card.description }}</p>

      <!-- #2914: which skill, what is running, and the two ways out. The
           library package was NOT written; the agent's own directory is
           intact and is the copy that runs. Sync does not change this. -->
      <div v-if="card.conflict" class="mt-4 text-[12.5px] text-status-warning-700 dark:text-status-warning-400" data-testid="skill-conflict-detail">
        <p>
          This agent already has its own <code class="font-mono">.claude/skills/{{ card.name }}/</code>
          that the platform did not create — usually a skill it authored. It was left intact
          and is what runs; the library version was not installed.
        </p>
        <p class="mt-1">
          Unassign the library skill to keep the agent's, or rename / remove the agent's
          directory and sync again to install the library version.
        </p>
        <BaseButton
          v-if="canManage && !card.setOnly"
          variant="secondary"
          size="sm"
          class="mt-2"
          data-testid="skill-conflict-unassign"
          :loading="busy"
          loading-label="Unassigning…"
          @click="$emit('unassign')"
        >Unassign library skill</BaseButton>
      </div>

      <p v-if="card.deprecated" class="mt-4 text-[12.5px] text-status-warning-700 dark:text-status-warning-400">
        Deprecated in the library{{ card.supersededBy ? `. Superseded by ${card.supersededBy}` : '' }}.
      </p>

      <div v-if="card.warnings && card.warnings.length" class="mt-4">
        <p class="text-[12.5px] font-[550] text-gray-900 dark:text-gray-100">From the last sync</p>
        <ul class="mt-1 space-y-1">
          <li v-for="w in card.warnings" :key="w" class="text-[12.5px] text-status-warning-700 dark:text-status-warning-400">
            {{ warningText(w) }}
          </li>
        </ul>
      </div>
      <p v-if="card.injection && card.injection.error" class="mt-4 text-[12.5px] text-status-danger-700 dark:text-status-danger-400">
        {{ card.injection.error }}
      </p>

      <div class="mt-5 flex justify-end">
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Close</BaseButton>
      </div>
    </template>
  </BaseModal>
</template>

<script setup>
import BaseButton from '../base/BaseButton.vue'
import BaseModal from '../base/BaseModal.vue'
import { warningText } from '../../utils/skillCards'

defineProps({
  modelValue: { type: Boolean, default: false },
  card: { type: Object, default: null },
  canManage: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
})

defineEmits(['update:modelValue', 'unassign'])
</script>
