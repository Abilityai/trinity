<!--
  Show me, on the real page: the one control where a setting lives, lit up with a short
  note (the Workspace tour's light, with one stop). Tabs and panels draw late, so it
  waits a little for the control, marked `data-tour="showme-<key>"`. If it never
  appears, a note says so instead of lighting up nothing. The words are in `showMe.js`.
-->
<template>
  <PortalBuildTour v-if="found" :open="true" :steps="[found]" :min-steps="1" @close="$emit('close')" />
  <Teleport v-else-if="missing" to="body">
    <div
      class="fixed bottom-6 right-6 z-50 w-[340px] max-w-[calc(100vw-32px)] rounded-[10px] border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 p-4 shadow-2xl"
      role="status"
      data-testid="show-me-missing"
    >
      <p class="text-sm text-gray-700 dark:text-gray-200">Couldn't find that on this page. It may be under another tab, or only the agent's owner can see it.</p>
      <BaseButton class="mt-3" size="sm" data-testid="show-me-missing-ok" @click="$emit('close')">OK</BaseButton>
    </div>
  </Teleport>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import PortalBuildTour from '@/components/portal/builder/PortalBuildTour.vue'
import { SHOW_ME_TARGETS } from './showMe'

const props = defineProps({
  showKey: { type: String, required: true },
  pollMs: { type: Number, default: 300 },
  giveUpMs: { type: Number, default: 6000 },
})
const emit = defineEmits(['close'])

const found = ref(null)
const missing = ref(false)
let timer = null

const onScreen = (el) => {
  const r = el?.getBoundingClientRect?.()
  return Boolean(r && r.width > 0 && r.height > 0)
}

function look(startedAt) {
  const target = SHOW_ME_TARGETS[props.showKey]
  const el = document.querySelector(`[data-tour="showme-${props.showKey}"]`)
  if (onScreen(el)) {
    found.value = { target: `showme-${props.showKey}`, title: target.title, body: target.body }
    return
  }
  if (Date.now() - startedAt >= props.giveUpMs) {
    missing.value = true
    return
  }
  timer = setTimeout(() => look(startedAt), props.pollMs)
}

const onKey = (event) => { if (missing.value && event.key === 'Escape') emit('close') }

onMounted(() => {
  if (!Object.hasOwn(SHOW_ME_TARGETS, props.showKey)) { emit('close'); return }
  window.addEventListener('keydown', onKey)
  look(Date.now())
})
onBeforeUnmount(() => {
  clearTimeout(timer)
  window.removeEventListener('keydown', onKey)
})
</script>
