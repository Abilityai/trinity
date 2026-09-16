<template>
  <!--
    trinity-enterprise#625 — the Workspace's theme switch. Sits at the tail
    of the conversation / room header (the `#header-end` slot), drives the
    same `useThemeStore` as the platform NavBar, and depends on nothing that
    needs a platform session — an external client with no stored preference
    gets it too. Below `sm` the label folds away and the trigger is icon-only.
  -->
  <div ref="root" class="relative shrink-0">
    <button
      ref="trigger"
      type="button"
      data-testid="portal-theme-switch"
      class="h-8 px-2 rounded-md flex items-center gap-1.5 text-[12.5px] text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-750 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
      :aria-label="ariaLabel"
      aria-haspopup="dialog"
      :aria-expanded="open ? 'true' : 'false'"
      :title="ariaLabel"
      @click="open = !open"
    >
      <ThemeIcon :name="icon" class="w-4 h-4 shrink-0" />
      <span class="hidden sm:inline whitespace-nowrap">{{ label }}</span>
    </button>

    <!-- The popover is a sibling of the trigger and anchors to the header's
         right edge, so it never widens the header row (layout stability) and
         never collides with the title band to its left. -->
    <div
      v-if="open"
      role="dialog"
      aria-label="Choose theme"
      data-testid="portal-theme-menu"
      class="absolute right-0 top-full mt-1 z-50 w-56 rounded-lg p-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 shadow-lg"
    >
      <p class="px-1 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">Theme</p>
      <ThemeChoice :theme="themeStore.theme" aria-label="Theme" @select="choose" />
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useThemeStore } from '@/stores/theme'
import { themeSwitchAriaLabel, themeSwitchIcon, themeSwitchLabel } from '@/utils/themeSwitch'
import ThemeChoice from '@/components/base/ThemeChoice.vue'
import ThemeIcon from '@/components/base/ThemeIcon.vue'

const themeStore = useThemeStore()

const open = ref(false)
const root = ref(null)
const trigger = ref(null)

const label = computed(() => themeSwitchLabel(themeStore.theme, themeStore.isDark))
const icon = computed(() => themeSwitchIcon(themeStore.theme, themeStore.isDark))
const ariaLabel = computed(() => themeSwitchAriaLabel(themeStore.theme, themeStore.isDark))

// Choosing writes the ONE store (no portal-local key), closes the menu and
// hands focus back to the trigger, so a keyboard user is where they started.
function choose(value) {
  themeStore.setTheme(value)
  open.value = false
  trigger.value?.focus()
}

function onDocumentClick(event) {
  if (open.value && root.value && !root.value.contains(event.target)) open.value = false
}
function onKeydown(event) {
  if (event.key === 'Escape' && open.value) {
    open.value = false
    trigger.value?.focus()
  }
}
// Armed at mount, above every await (design-system principle 23).
onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
  document.removeEventListener('keydown', onKeydown)
})
</script>
