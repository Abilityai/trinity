<!--
  One settings change the guide proposed, as a card above the chat. The words are
  the page's own (`fleetChanges.js`), built from the change it will make. It is the
  design system's alert, with the guide's face, so it reads as the guide asking. Confirm makes
  that one change with the person's session; Not now declines it. A Show me card opens
  the real page in a new tab; a refusal says why. There is never a "confirm all".
-->
<template>
  <!-- The design system's alert recipe, in the family that says where the card stands
       (`TONES`): the action family while it waits for them, the outcome's after. -->
  <div
    class="rounded-lg px-3.5 py-3 ring-1 ring-inset shadow-sm"
    :class="TONES[tone]"
    :data-tone="tone"
    data-testid="build-fleet-card"
  >
    <template v-if="card.kind === 'confirm' || card.kind === 'show-me'">
      <div class="flex items-start gap-3">
        <PortalAvatar class="shrink-0" :name="guide?.name || BUILD_GUIDE_NAME" :avatar-url="guide?.avatar_url || ''" :size="28" />
        <div class="min-w-0 flex-1">
          <p class="text-[12.5px] leading-7" :class="INK[tone]" data-testid="build-fleet-card-lead">
            <span class="font-semibold">{{ guideLabel }}</span> {{ card.kind === 'confirm' ? 'suggests one change' : 'suggests a walk-through' }}
          </p>
          <p class="text-sm font-[550] text-gray-900 dark:text-gray-100" data-testid="build-fleet-card-words">{{ card.words }}</p>

          <template v-if="card.kind === 'confirm'">
            <div v-if="status === 'done'" class="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-status-success-700 dark:text-status-success-300" role="status">
              <span class="font-semibold">Done.</span>
              <a v-if="seeWhere" :href="seeWhere" target="_blank" rel="noopener" class="font-medium underline underline-offset-2 hover:no-underline" data-testid="build-fleet-see-where" @click="$emit('opened')">See where this is set</a>
            </div>
            <p v-else-if="status === 'failed'" class="mt-2 text-[13px] text-status-danger-700 dark:text-status-danger-300" role="status">
              <span class="font-semibold">Nothing was changed.</span> {{ reason }}
            </p>
            <div v-else-if="status === 'unsure'" class="mt-2 flex flex-wrap items-center gap-3 text-[13px] text-status-warning-700 dark:text-status-warning-300" role="status">
              <span>This may or may not have been made. {{ reason }}</span>
              <a v-if="seeWhere" :href="seeWhere" target="_blank" rel="noopener" class="font-medium underline underline-offset-2 hover:no-underline" data-testid="build-fleet-see-where" @click="$emit('opened')">See where this is set</a>
            </div>
            <div v-else class="mt-3 flex items-center gap-2">
              <BaseButton size="sm" :loading="status === 'working'" :disabled="asked" data-testid="build-fleet-confirm" @click="onConfirm">Confirm</BaseButton>
              <BaseButton size="sm" variant="secondary" :disabled="asked" data-testid="build-fleet-decline" @click="onDecline">Not now</BaseButton>
            </div>
          </template>

          <div v-else class="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <!-- A link, not a button: it opens the real page in a new tab, where the walk runs. -->
            <a
              :href="link"
              target="_blank"
              rel="noopener"
              class="inline-flex items-center gap-1.5 rounded-sm text-sm font-semibold text-action-primary-700 dark:text-action-primary-300 underline decoration-2 underline-offset-4 hover:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:focus-visible:ring-action-primary-400/40"
              data-testid="build-fleet-show-me"
              @click="$emit('shown', $event, link)"
            >Show me<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" /></svg></a>
            <span class="text-[12.5px]" :class="INK[tone]">Opens a new tab and lights up each click.</span>
          </div>
        </div>
      </div>
    </template>

    <div v-else class="flex items-start gap-3">
      <svg class="mt-0.5 w-4 h-4 shrink-0 text-status-warning-700 dark:text-status-warning-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9.303 3.376c.866 1.5-.217 3.374-1.948 3.374H4.645c-1.73 0-2.813-1.874-1.948-3.374L10.051 3.378c.866-1.5 3.032-1.5 3.898 0l7.354 12.748zM12 15.75h.007v.008H12v-.008z" /></svg>
      <div class="min-w-0 flex-1">
        <p class="text-[13px] font-semibold text-status-warning-700 dark:text-status-warning-300">{{ card.reason }}</p>
        <BaseButton class="mt-2" size="sm" variant="secondary" data-testid="build-fleet-dismiss" @click="$emit('dismiss')">OK</BaseButton>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import PortalAvatar from '../PortalAvatar.vue'
import { showMeLink, showMeSpot } from '@/components/onboarding/showMe'
import { BUILD_GUIDE_NAME } from './portalBuild'

const props = defineProps({
  card: { type: Object, required: true },
  // The person's guide (its name and face); the card works without it.
  guide: { type: Object, default: null },
  // idle | working | done | failed (refused: nothing changed) | unsure, set by the page as the change runs.
  status: { type: String, default: 'idle' },
  reason: { type: String, default: '' },
})
// `shown`: its Show me link was opened. `opened`: its See where this is set link was.
const emit = defineEmits(['confirm', 'decline', 'shown', 'opened', 'dismiss'])

// Tinted grounds by family (design-system.md, Alerts): light 100 with a 200 edge, dark
// 500 at 16% with a 500/30 edge. Each family is one arm, never two of a property.
const TONES = Object.freeze({
  action: 'bg-action-primary-100 dark:bg-action-primary-500/16 ring-action-primary-200 dark:ring-action-primary-500/30',
  success: 'bg-status-success-100 dark:bg-status-success-500/16 ring-status-success-200 dark:ring-status-success-500/30',
  danger: 'bg-status-danger-100 dark:bg-status-danger-500/16 ring-status-danger-200 dark:ring-status-danger-500/30',
  warning: 'bg-status-warning-100 dark:bg-status-warning-500/16 ring-status-warning-200 dark:ring-status-warning-500/30',
})
// Text on those grounds: 700 light, 300 dark.
const INK = Object.freeze({
  action: 'text-action-primary-700 dark:text-action-primary-300',
  success: 'text-status-success-700 dark:text-status-success-300',
  danger: 'text-status-danger-700 dark:text-status-danger-300',
  warning: 'text-status-warning-700 dark:text-status-warning-300',
})
const OUTCOME_TONES = Object.freeze({ done: 'success', failed: 'danger', unsure: 'warning' })
const tone = computed(() => {
  if (props.card.kind === 'refused') return 'warning'
  return (props.card.kind === 'confirm' && OUTCOME_TONES[props.status]) || 'action'
})
const guideLabel = computed(() => props.guide?.display_label || BUILD_GUIDE_NAME)

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
