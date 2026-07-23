<template>
  <aside class="hidden lg:flex flex-col h-full w-64 shrink-0 bg-gray-50 dark:bg-gray-950 border-l border-gray-200 dark:border-gray-800">
    <div class="shrink-0 px-4 h-14 flex items-center border-b border-gray-200 dark:border-gray-800">
      <span class="text-sm font-semibold">Participants</span>
      <span class="ml-2 text-xs text-gray-400">{{ participants.length }}</span>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto p-3 space-y-2">
      <div v-for="p in participants" :key="p.kind + ':' + p.identity" class="flex items-center gap-2.5">
        <div class="relative">
          <PortalAvatar :name="p.identity" :avatar-url="p.kind === 'agent' ? (avatarByName[p.identity] || null) : null" :size="30" />
          <span
            v-if="p.kind === 'agent'"
            class="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full ring-2 ring-gray-50 dark:ring-gray-950"
            :class="workingState[p.identity] === 'working' ? 'bg-status-success-500 animate-pulse' : 'bg-gray-300 dark:bg-gray-600'"
            :title="workingState[p.identity] === 'working' ? 'Working…' : 'Idle'"
          ></span>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5">
            <span class="text-sm truncate">{{ p.kind === 'user' ? p.identity : p.identity }}</span>
            <span v-if="p.role !== 'member'" class="text-[10px] text-gray-400">{{ p.role }}</span>
          </div>
          <div class="text-[11px] text-gray-400">{{ p.kind === 'user' ? 'Human' : 'Agent' }}</div>
        </div>
      </div>
    </div>

    <!-- budgets -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 p-3 space-y-3">
      <div>
        <div class="flex items-center justify-between text-[11px] text-gray-500 dark:text-gray-400 mb-1">
          <span>Messages</span>
          <span :class="msgNearLimit ? 'text-status-warning-600 dark:text-status-warning-400 font-medium' : ''">{{ messageCount }} / {{ maxMessages }}</span>
        </div>
        <div class="h-1.5 rounded-full bg-gray-200 dark:bg-gray-800 overflow-hidden">
          <div class="h-full rounded-full transition-all" :class="msgNearLimit ? 'bg-status-warning-500' : 'bg-action-primary-500'" :style="{ width: msgPct + '%' }"></div>
        </div>
      </div>

      <div v-if="maxCost">
        <div class="flex items-center justify-between text-[11px] text-gray-500 dark:text-gray-400 mb-1">
          <span>Cost</span>
          <span :class="costNearLimit ? 'text-status-warning-600 dark:text-status-warning-400 font-medium' : ''">${{ cost.toFixed(3) }} / ${{ maxCost.toFixed(2) }}</span>
        </div>
        <div class="h-1.5 rounded-full bg-gray-200 dark:bg-gray-800 overflow-hidden">
          <div class="h-full rounded-full transition-all" :class="costNearLimit ? 'bg-status-warning-500' : 'bg-action-primary-500'" :style="{ width: costPct + '%' }"></div>
        </div>
      </div>
      <div v-else class="flex items-center justify-between text-[11px] text-gray-400">
        <span>Cost</span><span>${{ cost.toFixed(3) }} · no cap</span>
      </div>

      <div v-if="expiresAt" class="flex items-center justify-between text-[11px] text-gray-400">
        <span>Expires</span><span>{{ expiresLabel }}</span>
      </div>
    </div>
  </aside>
</template>

<script setup>
import { computed } from 'vue'
import PortalAvatar from '../portal/PortalAvatar.vue'

const props = defineProps({
  participants: { type: Array, default: () => [] },
  workingState: { type: Object, default: () => ({}) },
  avatarByName: { type: Object, default: () => ({}) },
  messageCount: { type: Number, default: 0 },
  maxMessages: { type: Number, default: 60 },
  cost: { type: Number, default: 0 },
  maxCost: { type: Number, default: null },
  expiresAt: { type: String, default: null },
})

const msgPct = computed(() => Math.min(100, Math.round((props.messageCount / (props.maxMessages || 1)) * 100)))
const msgNearLimit = computed(() => msgPct.value >= 80)
const costPct = computed(() => (props.maxCost ? Math.min(100, Math.round((props.cost / props.maxCost) * 100)) : 0))
const costNearLimit = computed(() => props.maxCost && costPct.value >= 80)

const expiresLabel = computed(() => {
  if (!props.expiresAt) return ''
  const ms = new Date(props.expiresAt).getTime() - Date.now()
  if (ms <= 0) return 'now'
  const h = Math.floor(ms / 3600000)
  if (h >= 1) return `in ${h}h`
  return `in ${Math.max(1, Math.floor(ms / 60000))}m`
})
</script>
