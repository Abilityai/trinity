<template>
  <!--
    One setting (trinity-enterprise#887): what it is and what it does on the
    left, its current value and its verb on the right — the shape every row of
    the mock on trinity#966 shares.

    `#editor` is the inline edit surface. It opens BELOW the row, inside the
    same card, so the control that opened it stays under the pointer and the
    rest of the pane moves down rather than being covered (principle 29).

    At phone width the value and the verb drop under the label instead of
    squeezing it; nothing here scrolls sideways.
  -->
  <div class="px-4 py-3" :data-testid="testid || undefined">
    <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <div class="min-w-0 sm:flex-1">
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span :id="labelId" class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ label }}</span>
          <slot name="badge" />
        </div>
        <p v-if="hint" class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">{{ hint }}</p>
        <slot name="hint" />
      </div>
      <div class="flex flex-wrap items-center gap-2 sm:justify-end sm:max-w-[55%]">
        <slot name="value" />
        <slot name="action" />
      </div>
    </div>
    <div v-if="$slots.editor" class="mt-3">
      <slot name="editor" />
    </div>
  </div>
</template>

<script setup>
import { useId } from 'vue'

defineProps({
  label: { type: String, required: true },
  hint: { type: String, default: '' },
  testid: { type: String, default: '' },
})

const labelId = useId()
defineExpose({ labelId })
</script>
