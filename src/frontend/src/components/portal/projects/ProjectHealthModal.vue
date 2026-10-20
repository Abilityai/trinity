<!--
  Update a project's health (trinity-enterprise#661 v3.3). The steward or the
  creator says how it is going, in one of three words and a line. Each update
  is also added to the project log, so the history is there to read.
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-health-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <form class="space-y-4" data-testid="project-health-form" @submit.prevent="submit">
      <h2 id="project-health-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">How is it going?</h2>
      <fieldset>
        <legend class="sr-only">Health</legend>
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <label
            v-for="s in HEALTH_STATES"
            :key="s"
            class="flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm"
            :class="state === s
              ? 'border-action-primary-500 bg-action-primary-50 dark:bg-action-primary-900/30 text-gray-900 dark:text-gray-100'
              : 'border-gray-200 dark:border-gray-750 text-gray-700 dark:text-gray-300'"
          >
            <input v-model="state" type="radio" name="project-health" :value="s" class="sr-only" :data-testid="`project-health-${s}`">
            <BaseBadge :variant="HEALTH_BADGE[s]" dot>{{ healthLabel(s) }}</BaseBadge>
          </label>
        </div>
      </fieldset>
      <BaseInput
        id="project-health-note"
        v-model="note"
        label="Why (optional)"
        placeholder="e.g. Vendor is two weeks late."
        :error="note.length > NOTE_MAX ? `Keep it under ${NOTE_MAX} characters.` : ''"
        help="One line. It shows on the project and goes into its log."
      />
      <InlineError v-if="submitError" :message="submitError" @dismiss="submitError = ''" />
      <div class="flex justify-end gap-2">
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Cancel</BaseButton>
        <BaseButton type="submit" :loading="saving" loading-label="Saving…" :disabled="note.length > NOTE_MAX" data-testid="project-health-save">Save</BaseButton>
      </div>
    </form>
  </BaseModal>
</template>

<script setup>
import { ref, watch } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { HEALTH_STATES, HEALTH_BADGE, healthLabel, projectErrorMessage } from './projectsUtils'

const NOTE_MAX = 280

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  projectId: { type: String, required: true },
  current: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue'])

const store = useProjectsStore()
const state = ref('on-track')
const note = ref('')
const saving = ref(false)
const submitError = ref('')

// Opens on the current read (principle 3), with a fresh line to write.
watch(() => props.modelValue, (open) => {
  if (!open) return
  state.value = props.current?.state || 'on-track'
  note.value = ''
  submitError.value = ''
}, { immediate: true })

async function submit() {
  saving.value = true
  submitError.value = ''
  try {
    await store.setHealth(props.projectId, state.value, note.value.trim())
    emit('update:modelValue', false)
  } catch (err) {
    submitError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}
</script>
