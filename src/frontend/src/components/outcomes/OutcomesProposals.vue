<template>
  <!-- Operations › Outcomes proposals list (trinity-enterprise#941). Filter by
       status (server) and owner (client); a person accepts, rejects with a
       reason, or marks an accepted proposal shipped. Nothing here changes an
       agent — an accepted proposal is routed to its owner. -->
  <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-750 rounded-lg" data-testid="outcomes-proposals">
    <div class="flex flex-wrap items-center gap-2 px-4 py-2 border-b border-gray-200 dark:border-gray-750 min-h-[44px]">
      <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100 mr-2">Proposals</h3>
      <BaseSelect variant="ghost" aria-label="Status" :model-value="status" data-testid="outcomes-status" @update:modelValue="$emit('status', $event)">
        <option value="">All statuses</option>
        <option v-for="s in STATUSES" :key="s" :value="s">{{ s }} ({{ counts[s] || 0 }})</option>
      </BaseSelect>
      <BaseSelect variant="ghost" aria-label="Owner" :model-value="owner" data-testid="outcomes-owner" @update:modelValue="$emit('owner', $event)">
        <option value="">All owners</option>
        <option v-for="o in owners" :key="o" :value="o">{{ o }}</option>
      </BaseSelect>
      <span class="ml-auto text-xs text-gray-500 dark:text-gray-400 tabular-nums" data-testid="outcomes-proposals-total">
        {{ items.length }} shown
      </span>
    </div>

    <InlineError
      v-if="actionError"
      class="m-4"
      :message="actionError"
      @dismiss="$emit('clear-error')"
    />

    <p v-if="!items.length" class="px-4 py-6 text-sm text-gray-600 dark:text-gray-300" data-testid="outcomes-proposals-empty">
      No {{ status || '' }} proposals. Proposals are drafted from each weekly record when a signal crosses its threshold.
    </p>
    <ul v-else class="max-h-[32rem] overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750">
      <li v-for="p in items" :key="p.id" class="px-4 py-3" :data-testid="`outcomes-proposal-${p.id}`">
        <div class="flex items-start gap-2">
          <BaseBadge :variant="STATUS_VARIANT[p.status] || 'neutral'" size="sm" :data-testid="`outcomes-status-${p.id}`">{{ p.status }}</BaseBadge>
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ p.title }}</p>
            <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ p.summary }}</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {{ p.target.kind }} “{{ p.target.ref }}” · owner {{ p.owner }} ·
              {{ p.expected_effect.metric }} {{ p.expected_effect.direction }} · {{ effectLine(p) }}
              <template v-if="p.project_name"> · {{ p.project_name }}</template>
            </p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 font-mono break-all">
              {{ evidence(p) }}
            </p>
            <p v-if="p.decided_by" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {{ p.status === 'rejected' ? 'Rejected' : 'Accepted' }} by {{ p.decided_by }}
              <template v-if="p.decision_reason">: “{{ p.decision_reason }}”</template>
              <template v-if="p.routed_kind === 'manual'"> · hand this to {{ p.routed_ref || p.owner }}</template>
              <template v-else-if="p.routed_kind === 'ask'"> · sent to {{ p.owner }}</template>
            </p>

            <div v-if="rejecting === p.id" class="mt-2 space-y-2">
              <BaseTextarea v-model="reason" label="Why reject it?" help="One line, shown in the decision record." :data-testid="`outcomes-reason-${p.id}`" />
              <div class="flex gap-2">
                <BaseButton size="sm" variant="secondary" @click="rejecting = ''">Keep it</BaseButton>
                <BaseButton
                  size="sm"
                  variant="danger"
                  :disabled="!reason.trim()"
                  :loading="acting === p.id"
                  :data-testid="`outcomes-confirm-reject-${p.id}`"
                  @click="confirmReject(p.id)"
                >Reject</BaseButton>
              </div>
            </div>
            <div v-else class="mt-2 flex gap-2">
              <template v-if="p.status === 'proposed'">
                <BaseButton size="sm" variant="primary" :loading="acting === p.id" :data-testid="`outcomes-accept-${p.id}`" @click="$emit('decide', p.id, 'accepted')">Accept</BaseButton>
                <BaseButton size="sm" variant="ghost" :data-testid="`outcomes-reject-${p.id}`" @click="startReject(p.id)">Reject…</BaseButton>
              </template>
              <BaseButton v-else-if="p.status === 'accepted'" size="sm" variant="secondary" :loading="acting === p.id" :data-testid="`outcomes-ship-${p.id}`" @click="$emit('ship', p.id)">Mark shipped</BaseButton>
            </div>
          </div>
        </div>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseSelect from '../base/BaseSelect.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import InlineError from '../InlineError.vue'
import { STATUSES } from '../../stores/outcomes'
import { STATUS_VARIANT, effectLine } from './format'

defineProps({
  items: { type: Array, default: () => [] },
  status: { type: String, default: '' },
  owner: { type: String, default: '' },
  owners: { type: Array, default: () => [] },
  counts: { type: Object, default: () => ({}) },
  acting: { type: String, default: '' },
  actionError: { type: String, default: null },
})
const emit = defineEmits(['status', 'owner', 'decide', 'ship', 'clear-error'])

const rejecting = ref('')
const reason = ref('')

function startReject(id) {
  rejecting.value = id
  reason.value = ''
}

function confirmReject(id) {
  emit('decide', id, 'rejected', reason.value.trim())
  rejecting.value = ''
}

function evidence(p) {
  const refs = (p.evidence || []).map((e) => `${e.kind}:${e.ref}`)
  return refs.length > 6 ? `${refs.slice(0, 6).join(' · ')} · +${refs.length - 6} more` : refs.join(' · ')
}
</script>
