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
      'inline-flex items-center justify-center gap-[7px] rounded-md border border-transparent font-medium leading-[1.35] transition-colors duration-[120ms]',
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
    'bg-action-primary-600 hover:bg-action-primary-700 text-white ' +
    'dark:bg-action-primary-500 dark:hover:bg-action-primary-400',
  secondary:
    'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 ' +
    'border-gray-300 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-750',
  danger:
    'bg-status-danger-600 hover:bg-status-danger-700 text-white ' +
    'dark:bg-status-danger-500 dark:hover:bg-status-danger-400',
  ghost:
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
