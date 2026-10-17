<!--
  "Needs you" (trinity-enterprise#661 v3.2, #3574): my own open asks on this
  project as Inbox list rows, each answered in place behind Answer with the
  Inbox's component (`ProjectNeedsYou`); everyone else's appear only as a
  count. One of the Overview's cards; the page places it (#3694) in the side
  stack, so an opened ask grows only its own column.
-->
<template>
  <BaseCard data-testid="project-needs-you">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Needs you</h2>
    <ProjectNeedsYou :ask-ids="myAskIds" :unavailable="asks.unavailable === true" @open-thread="(t) => $emit('open-thread', t)" />
    <p v-if="asks.others_open" class="mt-3 border-t border-gray-100 dark:border-gray-750 pt-3 text-xs text-gray-500 dark:text-gray-400" data-testid="project-asks-others">
      <span class="tabular-nums">{{ asks.others_open }}</span> {{ asks.others_open === 1 ? 'ask is' : 'asks are' }} waiting on other people.
    </p>
  </BaseCard>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import BaseCard from '@/components/base/BaseCard.vue'
import ProjectNeedsYou from './ProjectNeedsYou.vue'
import { useClientPortalStore } from '@/stores/clientPortal'

const props = defineProps({
  project: { type: Object, required: true },
})
defineEmits(['open-thread'])

const portal = useClientPortalStore()
const asks = computed(() => props.project.asks || { mine: [], others_open: 0 })
const myAskIds = computed(() => asks.value.mine || [])

// "Needs you" renders from the Workspace's shared asks list; load it once if
// nothing has yet (the sidebar badge usually has).
onMounted(() => {
  if (!portal.asksLoaded && typeof portal.fetchAsks === 'function') void portal.fetchAsks()
})
</script>
