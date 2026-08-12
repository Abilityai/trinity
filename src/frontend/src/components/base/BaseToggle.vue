<template>
  <!--
    The toggle primitive (docs/memory/design-system.md §5, #2122).
    36×20 pill track, 16px knob travelling 2→18; border-strong off,
    action-primary on. Toggles are for INSTANT-APPLY binary settings —
    anything behind a Save button is a checkbox in a form, not a toggle.
    Keyboard: focusable, Space/Enter toggles, visible focus ring.
  -->
  <button
    type="button"
    role="switch"
    :aria-checked="String(!!modelValue)"
    :aria-label="label ? undefined : ariaLabel"
    :disabled="disabled"
    class="group inline-flex items-center gap-2.5 focus-visible:outline-none disabled:opacity-45 disabled:cursor-not-allowed"
    @click="$emit('update:modelValue', !modelValue)"
  >
    <span
      :class="[
        'relative inline-block h-5 w-9 flex-none rounded-full transition-colors duration-150',
        'group-focus-visible:ring-2 group-focus-visible:ring-offset-2 ring-offset-white dark:ring-offset-gray-800',
        'group-focus-visible:ring-action-primary-500/40 dark:group-focus-visible:ring-action-primary-400/40',
        modelValue
          ? 'bg-action-primary-600 dark:bg-action-primary-500'
          : 'bg-gray-300 dark:bg-gray-700',
      ]"
      aria-hidden="true"
    >
      <span
        :class="[
          'absolute top-0.5 h-4 w-4 rounded-full bg-white shadow-sm transition-[left] duration-150',
          modelValue ? 'left-[18px]' : 'left-0.5',
        ]"
      ></span>
    </span>
    <span v-if="label" class="text-[13.5px] text-gray-900 dark:text-gray-100">{{ label }}</span>
  </button>
</template>

<script setup>
defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
  label: {
    type: String,
    default: '',
  },
  // Required for screen readers when no visible label is rendered.
  ariaLabel: {
    type: String,
    default: '',
  },
  disabled: {
    type: Boolean,
    default: false,
  },
})

defineEmits(['update:modelValue'])
</script>
