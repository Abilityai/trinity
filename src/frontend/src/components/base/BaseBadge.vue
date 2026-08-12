<template>
  <!--
    The badge primitive (docs/memory/design-system.md §5, #2122).
    The variant IS the token family name — light token-100/token-700,
    dark token-500/16 / token-300. A badge answers ONE question — status,
    mode, or identity — never two at once. Two facts = two badges.
  -->
  <span
    :class="[
      'inline-flex items-center gap-1.5 rounded-full px-[9px] py-[2.5px] text-[11.5px] font-[550] leading-[1.4] tracking-[.01em] whitespace-nowrap',
      VARIANT_CLASSES[variant],
    ]"
  >
    <span v-if="dot" class="h-1.5 w-1.5 flex-none rounded-full bg-current" aria-hidden="true"></span>
    <slot />
  </span>
</template>

<script setup>
// Full literal class strings so Tailwind's content scan sees every one.
const VARIANT_CLASSES = {
  success: 'bg-status-success-100 text-status-success-700 dark:bg-status-success-500/16 dark:text-status-success-300',
  warning: 'bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300',
  danger: 'bg-status-danger-100 text-status-danger-700 dark:bg-status-danger-500/16 dark:text-status-danger-300',
  info: 'bg-status-info-100 text-status-info-700 dark:bg-status-info-500/16 dark:text-status-info-300',
  urgent: 'bg-status-urgent-100 text-status-urgent-700 dark:bg-status-urgent-500/16 dark:text-status-urgent-300',
  autonomous: 'bg-state-autonomous-100 text-state-autonomous-700 dark:bg-state-autonomous-500/16 dark:text-state-autonomous-300',
  locked: 'bg-state-locked-100 text-state-locked-700 dark:bg-state-locked-500/16 dark:text-state-locked-300',
  claude: 'bg-brand-claude-100 text-brand-claude-700 dark:bg-brand-claude-500/16 dark:text-brand-claude-300',
  gemini: 'bg-brand-gemini-100 text-brand-gemini-700 dark:bg-brand-gemini-500/16 dark:text-brand-gemini-300',
  purple: 'bg-accent-purple-100 text-accent-purple-700 dark:bg-accent-purple-500/16 dark:text-accent-purple-300',
  neutral: 'bg-gray-100 text-gray-600 dark:bg-gray-750 dark:text-gray-400',
}

defineProps({
  variant: {
    type: String,
    default: 'neutral',
    // Keep in sync with VARIANT_CLASSES (defineProps validators are hoisted
    // and cannot reference it).
    validator: (v) =>
      ['success', 'warning', 'danger', 'info', 'urgent', 'autonomous', 'locked', 'claude', 'gemini', 'purple', 'neutral'].includes(v),
  },
  // Optional 6px status dot in currentColor.
  dot: {
    type: Boolean,
    default: false,
  },
})
</script>
