<template>
  <!--
    The button primitive (docs/memory/design-system.md §5, #2122).
    Four variants × two sizes; disabled opacity .45; in-flight = the one
    sanctioned spinner (16px, inside a pressed control) + progressive label.
    Focus ring on ALL variants — never `outline: none` alone.
  -->
  <button
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading || undefined"
    :class="[
      // `border` with NO colour keyword, exactly like FIELD_CLASS: the 1px is
      // reserved so the focus ring costs no layout shift, but WHICH colour it
      // takes is each variant's business. Carrying `border-transparent` here
      // made `secondary` borderless in LIGHT mode — `.border-transparent` is
      // emitted after `.border-gray-300` and the two are equal specificity, so
      // the resting keyword silently beat the variant's colour. Dark mode
      // survived only by accident: `dark:border-gray-700` compiles to
      // `.dark .border-gray-700`, one class heavier, so it won on specificity —
      // which is why this read as a light-only defect. Same cascade trap as
      // FIELD_GHOST_CLASS's, one primitive over. Any variant added here owes
      // its own border colour in the same breath.
      'inline-flex items-center justify-center gap-[7px] rounded-md border font-medium leading-[1.35] transition-colors duration-[120ms]',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 ring-offset-white dark:ring-offset-gray-800',
      'focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40',
      'disabled:opacity-45 disabled:cursor-not-allowed',
      SIZE_CLASSES[size],
      VARIANT_CLASSES[variant],
    ]"
  >
    <span
      v-if="loading"
      class="h-4 w-4 flex-none rounded-full border-2 border-current border-t-transparent animate-[spin_0.7s_linear_infinite]"
      aria-hidden="true"
    ></span>
    <template v-if="loading && loadingLabel">{{ loadingLabel }}</template>
    <slot v-else />
  </button>
</template>

<script setup>
const SIZE_CLASSES = {
  md: 'text-[13.5px] px-3.5 py-[7px]',
  sm: 'text-[12.5px] px-2.5 py-1',
}

const VARIANT_CLASSES = {
  primary:
    'border-transparent ' +
    'bg-action-primary-600 hover:bg-action-primary-700 text-white ' +
    'dark:bg-action-primary-500 dark:hover:bg-action-primary-400',
  // The only variant whose border is part of the design: a white button on a
  // white card is its own outline (design-system-reference.html, .btn-secondary
  // → border-color: var(--border-strong)).
  secondary:
    'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 ' +
    'border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-750',
  danger:
    'border-transparent ' +
    'bg-status-danger-600 hover:bg-status-danger-700 text-white ' +
    'dark:bg-status-danger-500 dark:hover:bg-status-danger-400',
  ghost:
    'border-transparent ' +
    'bg-transparent text-action-primary-600 dark:text-action-primary-500 ' +
    'hover:bg-action-primary-100 dark:hover:bg-action-primary-500/16',
}

defineProps({
  variant: {
    type: String,
    default: 'primary',
    validator: (v) => ['primary', 'secondary', 'danger', 'ghost'].includes(v),
  },
  size: {
    type: String,
    default: 'md',
    validator: (v) => ['md', 'sm'].includes(v),
  },
  // Native type defaults to "submit" inside forms — always be explicit.
  type: {
    type: String,
    default: 'button',
  },
  disabled: {
    type: Boolean,
    default: false,
  },
  // In-flight: acknowledges the press (principle 18). Pair with a
  // progressive label ("Deploying…") via loadingLabel.
  loading: {
    type: Boolean,
    default: false,
  },
  loadingLabel: {
    type: String,
    default: '',
  },
})
</script>
