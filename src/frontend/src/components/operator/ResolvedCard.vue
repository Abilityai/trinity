<template>
  <div class="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-4 opacity-80">
    <div class="flex items-start gap-3">
      <!-- Agent Avatar -->
      <AgentAvatar :name="item.agent_name" :avatar-url="agentAvatarUrl" size="md" />

      <!-- Content -->
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span
            class="text-sm font-medium text-gray-700 dark:text-gray-300"
            :title="agentNameTooltip(agentsStore.agentRefForSlug(item.agent_name))"
          >{{ item.agent_name }}</span>
          <span class="text-xs text-gray-500 dark:text-gray-400">&middot;</span>
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ timeAgo(item.created_at) }}</span>
          <!-- #2915: an answer that never reached the agent says so here. -->
          <BaseBadge
            v-if="syncBadge"
            :variant="syncBadge.variant"
            dot
            :title="syncBadge.title"
            data-testid="queue-sync-badge"
          >{{ syncBadge.label }}</BaseBadge>
          <!-- trinity-enterprise#611 (#627 AC6): a re-ask and the expired ask it
               re-raises name each other — one rule
               (utils/operatorQueue.js::queueReaskBadges), one fact per badge. #3247:
               a replaced ask and its successor name each other the same way. -->
          <BaseBadge
            v-for="b in reaskBadges"
            :key="b.key"
            :title="b.title"
            :data-testid="`queue-${b.key}`"
          >{{ b.prefix }} <span v-if="b.id" class="min-w-0 max-w-[12rem] truncate font-mono" :title="b.id" data-testid="queue-reask-id">{{ b.id }}</span></BaseBadge>
        </div>

        <p class="text-sm text-gray-600 dark:text-gray-400"><AskMarkdown :text="item.title" inline data-testid="resolved-title" /></p>

        <!-- trinity-enterprise#611: the action this ask proposed, so the record
             says what was approved, denied or let expire (the Workspace keeps it
             on ended asks too). -->
        <QueueProposal v-if="item.proposal" :proposal="item.proposal" class="mt-2" />

        <!-- Response (responded/acknowledged) or terminal status (cancelled/expired, #1017).
             trinity-enterprise#611: who ended it and when, from the endings
             ledger — one rule (utils/operatorQueue.js::queueEnding). -->
        <div class="mt-2 flex items-center gap-2">
          <span
            v-if="isTerminalWithoutResponse"
            class="inline-flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400"
            data-testid="queue-ending"
          >
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
            {{ endingText }}
          </span>
          <span v-else class="inline-flex items-center gap-1 text-xs text-status-success-700 dark:text-status-success-400">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
            </svg>
            <AskMarkdown :text="decisionLabel(item.response)" inline data-testid="resolved-response" />
          </span>
          <!-- The answer's note, or the operator's reason for cancelling. -->
          <span v-if="note" class="text-xs text-gray-500 dark:text-gray-400">
            &mdash; {{ note }}
          </span>
          <!-- When it ENDED — relative, absolute on hover. A row that ended
               before the ledger has no ending time, and shows none rather than
               its filing time. -->
          <span
            class="text-xs text-gray-500 dark:text-gray-400 ml-auto"
            :title="endedAtAbsolute || undefined"
            data-testid="queue-ending-when"
          >
            {{ endingMeta }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useOperatorQueueStore } from '../../stores/operatorQueue'
import { useAgentsStore } from '../../stores/agents'
import { agentNameTooltip } from '../../utils/agentName'
import { queueSyncBadge, queueEnding, queueEndingText, queueReaskBadges, decisionLabel } from '../../utils/operatorQueue'
import { formatLocalDateTime } from '../../utils/timestamps'
import AgentAvatar from '../AgentAvatar.vue'
import BaseBadge from '../base/BaseBadge.vue'
import QueueProposal from './QueueProposal.vue'
import AskMarkdown from './AskMarkdown.vue'

const props = defineProps({
  item: { type: Object, required: true }
})

const store = useOperatorQueueStore()
const agentsStore = useAgentsStore()

const syncBadge = computed(() => queueSyncBadge(props.item))   // #2915
const reaskBadges = computed(() => queueReaskBadges(props.item, store.items))   // trinity-enterprise#611
const isTerminalWithoutResponse = computed(() =>
  props.item.status === 'cancelled' || props.item.status === 'expired'
)
const ending = computed(() => queueEnding(props.item))   // trinity-enterprise#611
const endingText = computed(() => queueEndingText(ending.value))
// #3246: a platform ending's reason is a token the ending text already words
// (`queueEndingText`), never printed raw as though an operator wrote it.
// #3247: likewise an ending the agent authored carries a closed token
// (`replaced`), not a person's words.
// trinity-enterprise#844: so is the clock's (`outcome_unknown`) — a timeout
// carries no person's words.
const note = computed(() => props.item.response_text
  || ((ending.value && (ending.value.who === 'the platform' || ending.value.kind === 'expired'))
    || props.item.disposed_by === 'agent'
    ? '' : props.item.disposition_reason) || '')
const endedAtAbsolute = computed(() => (ending.value?.when ? formatLocalDateTime(ending.value.when) : ''))
// An answer's "who" rides beside its time; a cancel / expiry names it in its label.
const endingMeta = computed(() => {
  const parts = []
  if (ending.value?.kind === 'answered' && ending.value.who) parts.push(`by ${ending.value.who}`)
  if (ending.value?.when) parts.push(timeAgo(ending.value.when))
  return parts.join(' · ')
})
const agentAvatarUrl = computed(() => {
  const agent = agentsStore.agents.find(a => a.name === props.item.agent_name)
  return agent?.avatar_url || null
})

function timeAgo(isoString) {
  if (!isoString) return ''
  const now = new Date()
  const then = new Date(isoString)
  const diffMs = now - then
  const diffMin = Math.floor(diffMs / 60000)
  const diffHr = Math.floor(diffMs / 3600000)
  const diffDay = Math.floor(diffMs / 86400000)

  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  if (diffHr < 24) return `${diffHr}h ago`
  return `${diffDay}d ago`
}
</script>
