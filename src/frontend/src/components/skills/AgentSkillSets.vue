<template>
  <section data-testid="agent-skill-sets">
    <h3 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">
      Skill sets
      <span class="ml-1 text-[12.5px] font-[400] text-gray-600 dark:text-gray-400 tabular-nums">{{ store.sets.length }}</span>
    </h3>

    <!-- A failed read is named, never rendered as "no sets" (principle 15). -->
    <p v-if="store.setsError" class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-400" data-testid="sets-error">
      Skill sets are unavailable — {{ store.setsError }}
      <button
        type="button"
        class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline"
        @click="store.loadSets()"
      >retry</button>
    </p>

    <p
      v-else-if="store.setsLoaded && !store.sets.length"
      class="mt-2 text-[12.5px] text-gray-600 dark:text-gray-400"
      data-testid="sets-empty"
    >
      No sets assigned. A set assigns a whole family of library skills in one act and keeps it current.
      <template v-if="canManage && store.assignableSets.length"> Pick one below.</template>
    </p>

    <ul v-if="store.sets.length" class="mt-2 space-y-2">
      <li
        v-for="set in store.sets"
        :key="set.name"
        class="rounded-lg border border-gray-200 dark:border-gray-750 px-4 py-3"
        :data-testid="`set-row-${set.name}`"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">{{ set.name }}</span>
              <BaseBadge variant="purple">set</BaseBadge>
              <!-- Honest status (#342): never a silent "on". -->
              <BaseBadge :variant="statusVariant(set.status)" dot :data-testid="`set-status-${set.name}`">
                {{ statusLabel(set.status) }}
              </BaseBadge>
              <BaseBadge v-if="set.drift" variant="warning" title="The source this set was assigned from no longer owns its name">
                source changed
              </BaseBadge>
            </div>
            <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">
              <template v-if="set.status === 'unresolved'">
                {{ unresolvedText(set) }} Its skills are kept until it resolves.
              </template>
              <template v-else>
                <span class="tabular-nums">{{ assignedCount(set) }}</span> of
                <span class="tabular-nums">{{ set.members.length }}</span> members assigned
              </template>
            </p>
            <ul v-if="problemMembers(set).length" class="mt-1 space-y-0.5">
              <li
                v-for="m in problemMembers(set)"
                :key="m.name"
                class="text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
              >{{ m.name }} — {{ memberStateText(m.state) }}</li>
            </ul>
            <!-- Prerequisites are allowed-but-flagged, with the next action. -->
            <p
              v-if="set.prerequisites?.state === 'missing'"
              class="mt-1 text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
              :data-testid="`set-prereq-${set.name}`"
            >
              Missing credentials: <span class="font-mono">{{ set.prerequisites.missing_env.join(', ') }}</span>
              — add them on this agent's Credentials tab.
            </p>
            <p
              v-else-if="set.prerequisites?.state === 'unknown' && set.status !== 'unresolved'"
              class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400"
            >Credentials are checked while the agent runs.</p>
            <div v-if="set.suggested_schedules?.length" class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-400">
              Suggested schedules (not created — add them on the Schedules tab if you want them):
              <ul class="mt-0.5">
                <li v-for="sc in set.suggested_schedules" :key="sc.name">
                  {{ sc.name }} · <span class="font-mono text-[11px]">{{ sc.cron }}</span>
                </li>
              </ul>
            </div>
          </div>
          <BaseButton
            v-if="canManage"
            variant="secondary"
            size="sm"
            class="shrink-0"
            :disabled="store.setBusy !== null"
            :loading="store.setBusy === set.name"
            loading-label="Unassigning…"
            :title="`Removes the members ${set.name} alone brought; skills also assigned on their own or by another set stay`"
            :data-testid="`set-unassign-${set.name}`"
            @click="store.unassignSet(set.name)"
          >Unassign set</BaseButton>
        </div>
      </li>
    </ul>

    <div v-if="canManage && store.setsLoaded && store.assignableSets.length" class="mt-3 flex items-end gap-2">
      <BaseSelect v-model="picked" label="Assign a set" class="min-w-[14rem]" data-testid="set-picker">
        <option value="">Choose a set…</option>
        <option
          v-for="x in store.assignableSets"
          :key="x.name"
          :value="x.name"
          :disabled="x.status !== 'ok'"
        >{{ x.name }} ({{ x.members.length }}){{ x.status !== 'ok' ? ' — incomplete upstream' : '' }}</option>
      </BaseSelect>
      <BaseButton
        size="sm"
        :disabled="!picked || store.setBusy !== null"
        :loading="store.setBusy === picked && !!picked"
        loading-label="Assigning…"
        data-testid="set-assign"
        @click="onAssign"
      >Assign set</BaseButton>
    </div>

    <InlineError
      v-if="store.setWriteError"
      :message="store.setWriteError"
      class="mt-2"
      @dismiss="store.setWriteError = null"
    />
    <p
      v-else-if="store.lastSetResult"
      class="mt-2 text-[12.5px] text-status-success-700 dark:text-status-success-400"
      data-testid="set-assigned-note"
    >
      Assigned {{ store.lastSetResult.set_name }}<template v-if="store.lastSetResult.members_added?.length">
        — added {{ store.lastSetResult.members_added.join(', ') }}</template>.
    </p>
    <p
      v-if="store.removalDeferred"
      class="mt-2 text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
      data-testid="set-removal-deferred"
    >
      Unassigned {{ store.removalDeferred }}, but its skills stay for now: another set on this agent cannot be read,
      so nothing is removed until it can.
    </p>
  </section>
</template>

<script setup>
/**
 * An agent's skill sets (trinity-enterprise#530) — the honest per-set status
 * (#342) and the assign/unassign writes. Both writes go to the set routes,
 * which carry the same ent#596 fence as the skill writes.
 */
import { ref } from 'vue'
import { useSkillsStore } from '../../stores/skills'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseSelect from '../base/BaseSelect.vue'
import InlineError from '../InlineError.vue'

defineProps({
  canManage: { type: Boolean, default: false },
})

const store = useSkillsStore()
const picked = ref('')

function statusVariant(status) {
  return { ok: 'success', partial: 'warning', unresolved: 'danger' }[status] || 'neutral'
}

function statusLabel(status) {
  return { ok: 'complete', partial: 'partial', unresolved: 'unresolved' }[status] || status
}

function unresolvedText(set) {
  return {
    source_changed: 'This set now comes from a different library source than the one it was assigned from — re-assign it to follow the new source.',
    invalid: "This set's definition in its source's catalog.yaml is invalid.",
  }[set.reason] || 'This set cannot be read right now (its source is disabled, removed, or unreadable).'
}

function assignedCount(set) {
  return set.members.filter(m => m.state === 'assigned').length
}

function problemMembers(set) {
  return set.members.filter(m => m.state !== 'assigned')
}

function memberStateText(state) {
  return {
    missing_upstream: 'no longer in the library source',
    not_assigned: 'not assigned yet — sync or restart to reconcile',
    conflict: "name conflict — the agent's own copy runs",
  }[state] || state
}

async function onAssign() {
  if (!picked.value) return
  if (await store.assignSet(picked.value)) picked.value = ''
}
</script>
