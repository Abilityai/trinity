<!--
  One settings change the guide proposed, as a card above the composer. The words are
  the page's own (`fleetChanges.js`), built from the change it will make. Confirm makes
  that one change with the person's session; Not now declines it. A Show me card opens
  the real page in a new tab; a refusal says why. There is never a "confirm all".
-->
<template>
  <BaseCard class="mb-2" data-testid="build-fleet-card">
    <template v-if="card.kind === 'confirm'">
      <p class="text-[13px] text-gray-500 dark:text-gray-400">{{ BUILD_GUIDE_NAME }} suggests one change:</p>
      <p class="mt-1 text-sm font-[550] text-gray-900 dark:text-gray-100" data-testid="build-fleet-card-words">{{ card.words }}</p>
      <div v-if="status === 'done'" class="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-status-success-700 dark:text-status-success-400" role="status">
        <span>Done.</span>
        <a v-if="seeWhere" :href="seeWhere" target="_blank" rel="noopener" class="text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline" data-testid="build-fleet-see-where" @click="$emit('opened')">See where this is set</a>
      </div>
      <p v-else-if="status === 'failed'" class="mt-2 text-[13px] text-status-danger-700 dark:text-status-danger-400" role="status">
        Nothing was changed. {{ reason }}
      </p>
      <div v-else-if="status === 'unsure'" class="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-status-warning-700 dark:text-status-warning-300" role="status">
        <span>This may or may not have been made. {{ reason }}</span>
        <a v-if="seeWhere" :href="seeWhere" target="_blank" rel="noopener" class="text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline" data-testid="build-fleet-see-where" @click="$emit('opened')">See where this is set</a>
      </div>
      <div v-else class="mt-3 flex items-center gap-2">
        <BaseButton size="sm" :loading="status === 'working'" :disabled="asked" data-testid="build-fleet-confirm" @click="onConfirm">Confirm</BaseButton>
        <BaseButton size="sm" variant="secondary" :disabled="asked" data-testid="build-fleet-decline" @click="onDecline">Not now</BaseButton>
      </div>
    </template>

    <template v-else-if="card.kind === 'show-me'">
      <p class="text-sm text-gray-900 dark:text-gray-100">{{ card.words }}</p>
      <a
        :href="link"
        target="_blank"
        rel="noopener"
        class="mt-2 inline-block text-sm font-medium text-action-primary-600 dark:text-action-primary-500 underline-offset-2 hover:underline"
        data-testid="build-fleet-show-me"
        @click="$emit('shown')"
      >Show me</a>
    </template>

    <template v-else>
      <p class="text-sm text-gray-700 dark:text-gray-200">{{ card.reason }}</p>
      <BaseButton class="mt-2" size="sm" variant="secondary" data-testid="build-fleet-dismiss" @click="$emit('dismiss')">OK</BaseButton>
    </template>
  </BaseCard>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseCard from '@/components/base/BaseCard.vue'
import { showMeLink, showMeSpot } from '@/components/onboarding/showMe'
import { BUILD_GUIDE_NAME } from './portalBuild'

const props = defineProps({
  card: { type: Object, required: true },
  // idle | working | done | failed (refused: nothing changed) | unsure, set by the page as the change runs.
  status: { type: String, default: 'idle' },
  reason: { type: String, default: '' },
})
// `shown`: its Show me link was opened. `opened`: its See where this is set link was.
const emit = defineEmits(['confirm', 'decline', 'shown', 'opened', 'dismiss'])

// One answer per card, however often it is clicked.
const asked = ref(false)
function onConfirm() {
  if (asked.value) return
  asked.value = true
  emit('confirm')
}
function onDecline() {
  if (asked.value) return
  asked.value = true
  emit('decline')
}

const link = computed(() => showMeLink(props.card.key, props.card.agent, { keys: props.card.keys, to: props.card.to, schedule: props.card.schedule }))
// A change that turned something on walks there (`card.walk`); one that turned it off shows the spot.
const seeWhere = computed(() => {
  const agent = props.card.from || props.card.agent
  return props.card.walk ? showMeLink(props.card.showMe, agent, props.card.walk) : showMeSpot(props.card.showMe, agent)
})
</script>
