<template>
  <!--
    The segmented control (trinity-enterprise#957): pick ONE of a few short,
    fixed options that change how the same content is shown (List | Board).
    A radiogroup with a roving tabindex — the current choice is announced, and
    Left/Right move AND select, the native radio contract. For a lightweight
    preference beside content; a form field stays a BaseSelect.
  -->
  <div
    role="radiogroup"
    :aria-label="ariaLabel"
    class="inline-flex gap-0.5 rounded-md border border-gray-300 p-0.5 dark:border-gray-700"
    @keydown="onKeydown"
  >
    <button
      v-for="(option, index) in options"
      :key="option.value"
      :ref="(el) => (buttons[index] = el)"
      type="button"
      role="radio"
      :aria-checked="option.value === modelValue ? 'true' : 'false'"
      :tabindex="option.value === modelValue ? 0 : -1"
      :data-segment="option.value"
      class="rounded px-2.5 py-1 text-[12.5px] font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
      :class="option.value === modelValue
        ? 'bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-500/16 dark:text-action-primary-300'
        : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-750'"
      @click="$emit('update:modelValue', option.value)"
    >{{ option.label }}</button>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
  modelValue: { type: String, required: true },
  // [{ value, label }] — two to four short labels.
  options: { type: Array, required: true },
  ariaLabel: { type: String, required: true },
})
const emit = defineEmits(['update:modelValue'])

const buttons = ref([])

function onKeydown(event) {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key]
  if (!step) return
  event.preventDefault()
  const n = props.options.length
  const current = Math.max(0, props.options.findIndex((o) => o.value === props.modelValue))
  const next = (current + step + n) % n
  emit('update:modelValue', props.options[next].value)
  buttons.value[next]?.focus()
}
</script>
