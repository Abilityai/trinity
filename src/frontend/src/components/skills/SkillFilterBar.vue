<template>
  <!--
    "Filter skills" with its source and category filters
    (trinity-enterprise#660): the Assign skills dialog and the Library. The
    state is composables/useSkillFilter.js; the rule is utils/skillFilter.js.
    The source filter hides with one source, and the category filter when no
    skill has one. On a phone the input takes the row and the selects share the
    next one. `total` is the stated total under the controls (principle 28),
    its line reserved so a filter never moves what is below it.
  -->
  <div class="flex flex-wrap gap-2">
    <BaseInput
      :model-value="query"
      class="w-full sm:w-auto sm:min-w-0 sm:max-w-md sm:flex-1"
      placeholder="Filter skills"
      aria-label="Filter skills"
      :data-testid="`${testidPrefix}-filter`"
      @update:model-value="$emit('update:query', $event)"
      @keydown.esc="onEscape"
    />
    <BaseSelect
      v-if="sourceOptions.length > 1"
      :model-value="source"
      class="min-w-0 flex-1 sm:w-40 sm:flex-none"
      aria-label="Filter by source"
      :data-testid="`${testidPrefix}-source`"
      @update:model-value="$emit('update:source', $event)"
    >
      <option value="">All sources</option>
      <option v-for="o in sourceOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
    </BaseSelect>
    <BaseSelect
      v-if="categoryOptions.length"
      :model-value="category"
      class="min-w-0 flex-1 sm:w-40 sm:flex-none"
      aria-label="Filter by category"
      :data-testid="`${testidPrefix}-category`"
      @update:model-value="$emit('update:category', $event)"
    >
      <option value="">All categories</option>
      <option v-for="o in categoryOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
    </BaseSelect>
    <p
      v-if="total"
      class="h-5 w-full text-[12.5px] leading-5 tabular-nums text-gray-600 dark:text-gray-400"
      aria-live="polite"
      :data-testid="`${testidPrefix}-total`"
    >{{ total }}</p>
  </div>
</template>

<script setup>
import BaseInput from '../base/BaseInput.vue'
import BaseSelect from '../base/BaseSelect.vue'

const props = defineProps({
  query: { type: String, default: '' },
  source: { type: String, default: '' },
  category: { type: String, default: '' },
  sourceOptions: { type: Array, default: () => [] },
  categoryOptions: { type: Array, default: () => [] },
  total: { type: String, default: '' },
  testidPrefix: { type: String, required: true },
})

const emit = defineEmits(['update:query', 'update:source', 'update:category'])

// Esc clears text before it means anything else. In a dialog it would
// otherwise close it, and closing drops an unsaved draft.
function onEscape(event) {
  if (!props.query) return
  event.stopPropagation()
  emit('update:query', '')
}
</script>
