<template>
  <!--
    The one theme picker (trinity-enterprise#625): light / dark / system as a
    radio group, consumed by the NavBar's appearance menu AND the Workspace
    switch so the two cannot drift. A radiogroup, not three buttons: the
    current choice is announced (`aria-checked`) and arrow keys move between
    options, which is what "the current choice is announced" (AC #7) means
    for a screen reader.
  -->
  <div role="radiogroup" :aria-label="ariaLabel" class="flex gap-1" @keydown="onKeydown">
    <button
      v-for="(option, index) in THEME_OPTIONS"
      :key="option.id"
      :ref="(el) => (buttons[index] = el)"
      type="button"
      role="radio"
      :aria-checked="option.id === theme ? 'true' : 'false'"
      :tabindex="option.id === theme ? 0 : -1"
      :data-theme-option="option.id"
      class="flex-1 min-w-0 px-2 py-1.5 text-[12.5px] rounded-md flex items-center justify-center gap-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
      :class="option.id === theme
        ? 'bg-action-primary-100 text-action-primary-700 dark:bg-action-primary-500/16 dark:text-action-primary-300'
        : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-750'"
      @click="$emit('select', option.id)"
    >
      <ThemeIcon :name="option.icon" class="w-3.5 h-3.5 shrink-0" />
      <span>{{ option.label }}</span>
    </button>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { THEME_OPTIONS } from '@/utils/themeSwitch'
import ThemeIcon from './ThemeIcon.vue'

const props = defineProps({
  /** The CHOICE (`useThemeStore().theme`): light | dark | system. */
  theme: { type: String, required: true },
  ariaLabel: { type: String, default: 'Theme' },
})
const emit = defineEmits(['select'])

const buttons = ref([])

// Roving tabindex: Left/Up and Right/Down move AND select, the native radio
// contract, so a keyboard user never lands on an option they cannot pick.
function onKeydown(event) {
  const keys = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }
  const step = keys[event.key]
  if (!step) return
  event.preventDefault()
  const current = Math.max(0, THEME_OPTIONS.findIndex((o) => o.id === props.theme))
  const next = (current + step + THEME_OPTIONS.length) % THEME_OPTIONS.length
  emit('select', THEME_OPTIONS[next].id)
  buttons.value[next]?.focus()
}
</script>
